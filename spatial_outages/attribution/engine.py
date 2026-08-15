import json
from collections import defaultdict
from datetime import timedelta

from pyproj import Geod
from shapely import to_wkt
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.strtree import STRtree

from .domain.cell_state import CellState
from .domain.decision import Decision
from .domain.event import AttributionEvent
from .domain.outage import Device, Outage
from .domain.rule_context import RuleContext
from .domain.thresholds import Thresholds
from .rules import RuleEngine


GEOD = Geod(ellps="WGS84")
ENGINE_VERSION = "triage-v1"


def number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def outage_polygon(outage: Outage) -> tuple[Polygon, str, str] | None:
    points = {(longitude, latitude) for latitude, longitude in outage.locations.values()}
    # ponytail: three unique points is the v1 floor; add coverage/max-gap rules when product locks them.
    if len(points) < 3:
        return None
    polygon = MultiPoint(points).convex_hull
    if polygon.geom_type != "Polygon" or polygon.is_empty:
        return None
    area_m2, _ = GEOD.geometry_area_perimeter(polygon)
    return polygon, to_wkt(polygon, rounding_precision=6), number(abs(area_m2) / 1_000_000)


class AttributionEngine:
    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.rules = RuleEngine()

    def classify(
        self, fleet: dict[str, Device], outages: list[Outage]
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        fleet_by_csp: dict[str, set[str]] = defaultdict(set)
        fleet_by_cell: dict[tuple[str, str], set[str]] = defaultdict(set)
        h3s_by_csp: dict[str, set[str]] = defaultdict(set)
        csps_by_h3: dict[str, set[str]] = defaultdict(set)
        located_devices: list[str] = []
        located_points: list[Point] = []
        for device_id, device in fleet.items():
            if not device.is_active or not device.h3_id:
                continue
            fleet_by_csp[device.csp_id].add(device_id)
            key = device.csp_id, device.h3_id
            fleet_by_cell[key].add(device_id)
            h3s_by_csp[device.csp_id].add(device.h3_id)
            csps_by_h3[device.h3_id].add(device.csp_id)
            if device.latitude is not None and device.longitude is not None:
                located_devices.append(device_id)
                located_points.append(Point(device.longitude, device.latitude))
        point_index = STRtree(located_points)

        state_rows, event_rows, evidence_rows, attribution_rows = [], [], [], []
        for event in self._group_events(outages):
            affected: dict[tuple[str, str], set[str]] = defaultdict(set)
            affected_by_csp: dict[str, set[str]] = defaultdict(set)
            for outage in event.outages:
                affected_by_csp[outage.csp_id].update(outage.members)
                for device, h3_id in outage.h3_ids.items():
                    affected[(outage.csp_id, h3_id)].add(device)

            affected_csps = {outage.csp_id for outage in event.outages}
            affected_h3s = {h3_id for _, h3_id in affected}
            relevant = set(affected)
            for csp in affected_csps:
                relevant.update((csp, h3_id) for h3_id in h3s_by_csp[csp])
            for h3_id in affected_h3s:
                relevant.update((csp, h3_id) for csp in csps_by_h3[h3_id])

            states: dict[tuple[str, str], CellState] = {}
            down_h3_by_csp: dict[str, set[str]] = defaultdict(set)
            down_csp_by_h3: dict[str, set[str]] = defaultdict(set)
            for csp, h3_id in sorted(relevant):
                eligible_devices = fleet_by_cell[(csp, h3_id)] | affected[(csp, h3_id)]
                eligible, failed = len(eligible_devices), len(affected[(csp, h3_id)])
                share = failed / eligible if eligible else 0
                state = "DOWN" if share >= self.thresholds.min_affected_share else "UP"
                cell = states[(csp, h3_id)] = CellState(csp, h3_id, failed, eligible, share, state)
                if state == "DOWN":
                    down_h3_by_csp[csp].add(h3_id)
                    down_csp_by_h3[h3_id].add(csp)
                state_rows.append(
                    {
                        "attribution_event_id": event.event_id,
                        "csp_id": csp,
                        "h3_id": h3_id,
                        "affected_devices": failed,
                        "eligible_devices": eligible,
                        "affected_share": number(share),
                        "csp_h3_state": cell.state,
                    }
                )

            event_rows.append(
                {
                    "attribution_event_id": event.event_id,
                    "event_start_ist": event.start.isoformat(sep=" "),
                    "event_end_ist": event.end.isoformat(sep=" "),
                    "outage_count": len(event.outages),
                    "affected_csp_count": len(affected_csps),
                    "affected_h3_count": len(affected_h3s),
                }
            )

            for outage in event.outages:
                polygon = outage_polygon(outage)
                geometry_quality = "OK" if polygon else "FAILED"
                eligible_devices_csp = fleet_by_csp[outage.csp_id]
                down_devices_csp = affected_by_csp[outage.csp_id] & eligible_devices_csp
                csp_down_share = (
                    len(down_devices_csp) / len(eligible_devices_csp)
                    if eligible_devices_csp else 0
                )
                active_csps_in_polygon: set[str] = set()
                down_csps_in_polygon: set[str] = set()
                if polygon:
                    inside = {
                        located_devices[int(index)]
                        for index in point_index.query(polygon[0], predicate="covers")
                    }
                    active_csps_in_polygon = {fleet[device].csp_id for device in inside}
                    down_csps_in_polygon = {
                        csp for csp, devices in affected_by_csp.items() if devices & inside
                    }
                member_h3s = set(outage.h3_ids.values())
                target_h3 = ""
                target = CellState(outage.csp_id, "", 0, 0, 0, "UP")
                affected_h3_count = len(down_h3_by_csp[outage.csp_id])
                compared_csp_count = 0

                other_active_csps = active_csps_in_polygon - {outage.csp_id}
                other_down_csps = down_csps_in_polygon - {outage.csp_id}
                if not polygon:
                    decision = Decision("UNKNOWN", "R0_GEOMETRY_QUALITY_FAILURE", "LOW", "LOCAL")
                elif csp_down_share >= self.thresholds.almost_all_h3_share:
                    confidence = (
                        "HIGH"
                        if csp_down_share > self.thresholds.almost_all_h3_share
                        else "MEDIUM"
                    )
                    decision = Decision("CSP_SIDE", "R0_CSP_DOWN_SHARE", confidence, "REGIONAL")
                elif other_active_csps and not other_down_csps:
                    decision = Decision("CSP_SIDE", "R0_POLYGON_PEER_UP", "MEDIUM", "LOCAL")
                elif not member_h3s:
                    decision = Decision("UNKNOWN", "R0_NO_LOCATED_H3", "LOW", "LOCAL")
                else:
                    csp = outage.csp_id
                    down_h3s = down_h3_by_csp[csp]
                    target_h3 = max(
                        sorted(down_h3s or member_h3s),
                        key=lambda h3_id: (
                            len(down_csp_by_h3[h3_id]),
                            states[(csp, h3_id)].affected_share,
                        ),
                    )
                    target = states[(csp, target_h3)]
                    shared_csps = down_csp_by_h3[target_h3]
                    neighbors = csps_by_h3[target_h3] - {csp}
                    eligible_h3_count = len(h3s_by_csp[csp])
                    compared_csp_count = len(csps_by_h3[target_h3])
                    context = RuleContext(
                        down_h3s=frozenset(down_h3s),
                        down_csps_here=frozenset(shared_csps),
                        eligible_h3_count=eligible_h3_count,
                        affected_h3_share=(
                            affected_h3_count / eligible_h3_count if eligible_h3_count else 0
                        ),
                        neighboring_csps=frozenset(neighbors),
                        neighbor_up=any(states[(other, target_h3)].state == "UP" for other in neighbors),
                        cross_h3_comparison=all(len(h3s_by_csp[other]) >= 2 for other in shared_csps),
                        shared_down_elsewhere=bool(shared_csps)
                        and all(down_h3_by_csp[other] - {target_h3} for other in shared_csps),
                        cause_confidence=(
                            "HIGH"
                            if all(
                                states[(csp, h3_id)].affected_share
                                > self.thresholds.min_affected_share
                                for h3_id in down_h3s
                            )
                            else "MEDIUM"
                        ),
                        thresholds=self.thresholds,
                    )
                    decision = self.rules.evaluate(context)

                eligible_h3_count = len(h3s_by_csp[outage.csp_id])
                evidence = json.dumps(
                    [
                        {"signal": "rule_matched", "value": decision.rule},
                        {"signal": "csp_down_share", "value": number(csp_down_share)},
                        {"signal": "spatial_saturation", "value": number(target.affected_share)},
                        {"signal": "geometry_quality", "value": geometry_quality},
                        {"signal": "healthy_device_count", "value": "UNAVAILABLE"},
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                )
                evidence_rows.append(
                    {
                        "outage_id": outage.outage_id,
                        "attribution_event_id": event.event_id,
                        "csp_id": outage.csp_id,
                        "h3_id": target_h3,
                        "outage_trigger_time": outage.trigger_time.isoformat(sep=" "),
                        "down_devices_csp": len(down_devices_csp),
                        "eligible_devices_csp": len(eligible_devices_csp),
                        "csp_down_share": number(csp_down_share),
                        "affected_devices": target.affected_devices,
                        "eligible_devices": target.eligible_devices,
                        "affected_share": number(target.affected_share),
                        "affected_h3_count": affected_h3_count,
                        "eligible_h3_count": eligible_h3_count,
                        "compared_csp_count": compared_csp_count,
                        "rule_matched": decision.rule,
                        "root_cause": decision.root_cause,
                        "spatial_extent": decision.spatial_extent,
                        "confidence": decision.confidence,
                        "geometry_quality": geometry_quality,
                    }
                )
                attribution_rows.append(
                    {
                        "outage_id": outage.outage_id,
                        "geometry": polygon[1] if polygon else "",
                        "geometry_version": 1 if polygon else "",
                        "polygon_area_km2": polygon[2] if polygon else "",
                        "unhealthy_device_count": len(outage.members),
                        "located_unhealthy_device_count": len(outage.locations),
                        "healthy_device_count": "",
                        "root_cause": decision.root_cause,
                        "sub_cause": "",
                        "spatial_extent": decision.spatial_extent,
                        "event_pattern": "UNKNOWN",
                        "confidence": decision.confidence,
                        "cause_distribution": "",
                        "evidence": evidence,
                        "expected_restoration_class": "",
                        "is_final": "",
                        "engine_version": ENGINE_VERSION,
                    }
                )
        return state_rows, event_rows, evidence_rows, attribution_rows

    def _group_events(self, outages: list[Outage]) -> list[AttributionEvent]:
        groups: list[list[Outage]] = []
        window = timedelta(minutes=self.thresholds.overlap_window_minutes)
        for outage in outages:
            if not groups or outage.trigger_time - groups[-1][0].trigger_time > window:
                groups.append([outage])
            else:
                groups[-1].append(outage)
        return [
            AttributionEvent(f"AE{index:06d}", tuple(group))
            for index, group in enumerate(groups, 1)
        ]
