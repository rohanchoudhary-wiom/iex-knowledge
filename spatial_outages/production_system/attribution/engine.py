import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from .address import extract_address_entities_many
from .models import Device, Inventory
from .spatial import (
    cluster_stability,
    comparison_polygon,
    directional_profile,
    inside_polygon,
    radius_core,
    radius_profile,
    strongest_window,
    variable_density_clusters,
)
from .status import StatusReader, device_state, timestamp


class AttributionEngine:
    def __init__(
        self,
        inventory: Inventory,
        status_reader: StatusReader,
        min_provider_devices: int = 5,
        address_reader: Callable[[list[str | None]], list[dict[str, list[dict]]]] = extract_address_entities_many,
    ) -> None:
        self.inventory, self.status_reader = inventory, status_reader
        self.min_provider_devices = min_provider_devices
        self.address_reader = address_reader

    def evaluate(
        self,
        outage_id: object,
        member_ids: list[str],
        ongoing_time: int | float,
        evaluated_at: datetime | None = None,
    ) -> tuple[dict, dict]:
        evaluated_at = (evaluated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        resolved = [self.inventory.devices.get(device_id) for device_id in member_ids]
        missing = [device_id for device_id, device in zip(member_ids, resolved) if device is None]
        members = [device for device in resolved if device is not None]
        known_member_ids = [device.device_id for device in members]
        cache: dict[str, tuple[str, str | None]] = {}

        def states(device_ids: list[str]) -> dict[str, tuple[str, str | None]]:
            missing_ids = [device_id for device_id in device_ids if device_id not in cache]
            if missing_ids:
                raw = self.status_reader(missing_ids)
                cache.update({device_id: device_state(raw.get(device_id), evaluated_at) for device_id in missing_ids})
            return {device_id: cache[device_id] for device_id in device_ids}

        if not members:
            states(member_ids)
            devices = self._map_devices(sorted(cache), member_ids, cache, set())
            return self._result(
                outage_id, "UNKNOWN", 0.0, "CUSTOMER_V2_MISSING", evaluated_at, ongoing_time, [], devices, missing
            )

        target_csps = {device.csp_id for device in members}
        target_csp = next(iter(target_csps)) if len(target_csps) == 1 else None
        csp_signal = None
        if target_csp:
            csp_ids = self.inventory.by_csp[target_csp]
            csp_states = states(csp_ids)
            csp_down = sum(state == "DOWN" for state, _ in csp_states.values())
            csp_down_share = csp_down / len(csp_ids)
            gate_threshold = .7
            score = self._csp_signal_confidence(csp_down_share)
            csp_signal = {
                "csp_id": target_csp,
                "down": csp_down,
                "eligible": len(csp_ids),
                "down_share": round(csp_down_share, 4),
                "gate_threshold": gate_threshold,
                "policy_score": score,
            }
        csp_match = bool(csp_signal and csp_signal["down_share"] >= csp_signal["gate_threshold"])

        member_states = states(member_ids)
        located_down = [
            device for device in members
            if member_states[device.device_id][0] == "DOWN" and device.latitude is not None
        ]
        failure_times = {
            device.device_id: self._failure_time(device.device_id, cache)
            for device in located_down
        }
        located_down = [device for device in located_down if failure_times[device.device_id] is not None]
        refined: list[tuple[list[Device], bool]] = []
        for component in variable_density_clusters(located_down):
            refined.extend(self._refine(component))

        groups, review_ids, polygon_ids = [], [], set()
        for index, (component, reclustered) in enumerate(refined, 1):
            group, comparison_ids = self._group(
                outage_id, index, component, set(known_member_ids), target_csp, cache, states,
                failure_times, reclustered
            )
            groups.append(group)
            polygon_ids.update(comparison_ids)
            if not group["supported"] and group["attribution"] != "FIBRE_CUT":
                review_ids.extend(group["member_ids"])

        supported = [group for group in groups if group["supported"]]
        attribution_groups = [
            group for group in groups
            if group["supported"] or group["attribution"] == "FIBRE_CUT"
        ]
        causes = {group["attribution"] for group in attribution_groups}
        affected_device_ids: list[str] = []
        if csp_match:
            attribution, confidence, rule = "ISP_OLT_CSP_SIDE", csp_signal["policy_score"], "CSP_DOWN_SHARE"
            affected_device_ids = known_member_ids
        elif len(causes) == 1 and "UNKNOWN" not in causes:
            attribution = next(iter(causes))
            confidence = min(group["confidence"] for group in attribution_groups)
            rule = next(group["decision_rule"] for group in attribution_groups)
            affected_device_ids = sorted({
                device_id for group in attribution_groups for device_id in group["affected_device_ids"]
            })
        else:
            attribution, confidence = "UNKNOWN", 0.0
            if not located_down:
                rule = "NO_CURRENT_DOWN_MEMBERS"
            elif not supported:
                rule = "SUB_OUTAGE_REVIEW_ONLY"
            elif len(causes) > 1:
                rule = "LOCAL_GROUPS_DISAGREE"
            else:
                rule = next((group["decision_rule"] for group in supported), "LOCAL_PATTERN_AMBIGUOUS")

        ping_devices = self._map_devices(sorted(cache), member_ids, cache, polygon_ids)
        devices = self._map_devices(sorted(set(member_ids) | polygon_ids), member_ids, cache, polygon_ids)
        local_evidence = next((group["local_csp_evidence"] for group in supported if group["local_csp_evidence"]), None)
        spatial_evidence = "SUPPORTED" if supported else "REVIEW"
        return self._result(
            outage_id, attribution, confidence, rule, evaluated_at, ongoing_time, groups, devices,
            sorted(set(([] if csp_match else review_ids) + missing)), parent_evidence=[csp_signal] if csp_signal else None,
            polygon_evidence=local_evidence, csp_signal=csp_signal,
            spatial_evidence=spatial_evidence, recovered_member_ids=self._recovered(known_member_ids, cache),
            affected_device_ids=affected_device_ids, ping_devices=ping_devices,
        )

    def _refine(self, component: list[Device]) -> list[tuple[list[Device], bool]]:
        if len(component) < 10:
            return [(component, False)]
        _, radii = radius_profile(component)
        if radii["r90"] - radii["r80"] <= 500:
            return [(component, False)]
        split = variable_density_clusters(component, reach_scale=.7, max_reach=500)
        return [(group, True) for group in split] if len(split) > 1 else [(component, True)]

    def _group(
        self,
        outage_id: object,
        index: int,
        component: list[Device],
        outage_member_ids: set[str],
        target_csp: str | None,
        cache: dict[str, tuple[str, str | None]],
        states,
        failure_times: dict[str, datetime],
        reclustered: bool,
    ) -> tuple[dict, list[str]]:
        center, radii = radius_profile(component)
        core, tails = radius_core(component, center)
        reasons = []
        if len(component) < 10:
            reasons.append("MIN_DOWN_MEMBERS")
        if radii["r90"] > 1_000:
            reasons.append("R90_OVER_1KM")
        if radii["r90"] - radii["r80"] > 500:
            reasons.append("R80_R90_JUMP")
        supported = not reasons
        fibre_evaluable = supported or reasons == ["MIN_DOWN_MEMBERS"]
        times = [failure_times[device.device_id] for device in component]
        strongest_count = strongest_window(times)
        strongest_share = round(strongest_count / len(times), 4)
        boundary = comparison_polygon(core, center, radii["r90"])
        comparison = [device for device in self.inventory.devices.values() if inside_polygon(device, boundary)]
        comparison_ids = [device.device_id for device in comparison]
        comparison_states = states(comparison_ids)
        providers, local_evidence, address_evidence = [], None, None
        cause, confidence, decision_rule, affected_device_ids = "UNKNOWN", 0.0, "SPATIAL_REVIEW", []

        for csp_id in sorted({device.csp_id for device in comparison}):
            ids = [device.device_id for device in comparison if device.csp_id == csp_id]
            down = sum(comparison_states[device_id][0] == "DOWN" for device_id in ids)
            unknown = sum(comparison_states[device_id][0] == "UNKNOWN" for device_id in ids)
            up = sum(comparison_states[device_id][0] == "UP" for device_id in ids)
            providers.append({
                "csp_id": csp_id,
                "down": down,
                "up": up,
                "unknown": unknown,
                "eligible": len(ids),
                "down_share": round(down / len(ids), 4),
                "up_share": round(up / len(ids), 4),
                "qualified": len(ids) >= self.min_provider_devices,
                "eligibility_reason": "ELIGIBLE" if len(ids) >= self.min_provider_devices else "BELOW_MINIMUM",
            })
        if supported:
            cause, confidence, decision_rule = self._local_decision(target_csp, providers)
            local_evidence = self._local_csp_evidence(target_csp, providers, boundary, radii, tails, decision_rule)
        if fibre_evaluable and cause == "UNKNOWN":
            address_evidence = self._fibre_evidence(
                component, comparison, cache, outage_member_ids
            )
            if address_evidence["matched"]:
                cause, confidence = "FIBRE_CUT", address_evidence["confidence"]
                decision_rule = (
                    "RULE_4A_HOUSE_GALI"
                    if address_evidence["path"] == "RULE_4A"
                    else "RULE_4B_LOCALITY_CONTROLS"
                    if address_evidence["path"] == "RULE_4B"
                    else "RULE_4C_DIRECTIONAL_CLUSTER"
                )
                affected_device_ids = address_evidence["affected_device_ids"]
            else:
                decision_rule = "RULE_5_NO_HOUSE_GALI"
        elif supported:
            affected_device_ids = sorted(device.device_id for device in component)

        return {
            "sub_outage_id": f"{outage_id}:{index}",
            "member_ids": [device.device_id for device in component],
            "boundary_member_ids": [device.device_id for device in core],
            "tail_device_ids": [device.device_id for device in tails],
            "boundary": boundary,
            "center": [round(center[0], 6), round(center[1], 6)],
            "radii_m": radii,
            "supported": supported,
            "fibre_evaluable": fibre_evaluable,
            "evidence_grade": "SUPPORTED" if supported else "REVIEW",
            "review_reasons": reasons,
            "reclustered": reclustered,
            "stability": cluster_stability(component),
            "timing": {
                "source": "LAST_PING_PROXY",
                "window_start": min(times).isoformat(),
                "window_end": max(times).isoformat(),
                "failure_span_minutes": round((max(times) - min(times)).total_seconds() / 60, 1),
                "strongest_10m_count": strongest_count,
                "strongest_10m_share": strongest_share,
            },
            "attribution": cause,
            "confidence": confidence,
            "decision_rule": decision_rule,
            "cause_likelihood": "LIKELY" if confidence >= .7 else "POSSIBLE" if confidence else "UNKNOWN",
            "confirmation_status": "MISSING",
            "providers": providers,
            "local_csp_evidence": local_evidence,
            "address_evidence": address_evidence,
            "affected_device_ids": affected_device_ids,
        }, comparison_ids

    @staticmethod
    def _local_csp_evidence(
        target_csp: str | None,
        providers: list[dict],
        boundary: list[tuple[float, float]],
        radii: dict[str, float],
        tails: list[Device],
        decision_rule: str,
    ) -> dict | None:
        if not target_csp:
            return None
        target = next((provider for provider in providers if provider["csp_id"] == target_csp), None)
        if not target:
            return None
        peers = [
            provider for provider in providers
            if provider["csp_id"] != target_csp and provider["qualified"]
        ]
        peer_devices = sum(provider["eligible"] for provider in peers)
        peer_up = sum(provider["up"] for provider in peers)
        peer_down = sum(provider["down"] for provider in peers)
        peer_up_share = peer_up / peer_devices if peer_devices else 0
        return {
            "matched": decision_rule in {"RULE_2A_LOCAL_CSP_PEERS", "RULE_2B_LOCAL_CSP_MONOPOLY"},
            "decision_rule": decision_rule,
            "polygon_devices": sum(provider["eligible"] for provider in providers),
            "target_csp": target_csp,
            "target_devices": target["eligible"],
            "target_down": target["down"],
            "target_down_share": target["down_share"],
            "qualified_peer_count": len(peers),
            "peer_devices": peer_devices,
            "peer_down": peer_down,
            "peer_down_share": round(peer_down / peer_devices, 4) if peer_devices else 0,
            "peer_up": peer_up,
            "peer_up_share": round(peer_up_share, 4),
            "all_qualified_peers_down_at_most_20": bool(peers) and all(
                provider["down_share"] <= .2 for provider in peers
            ),
            "boundary": boundary,
            "member_radii_m": radii,
            "tail_device_ids": [device.device_id for device in tails],
        }

    @staticmethod
    def _local_decision(target_csp: str | None, providers: list[dict]) -> tuple[str, float, str]:
        qualified = [provider for provider in providers if provider["qualified"]]
        target = next((provider for provider in qualified if provider["csp_id"] == target_csp), None)
        peers = [provider for provider in qualified if provider["csp_id"] != target_csp]
        if target and target["down_share"] >= .8 and peers and all(provider["down_share"] <= .2 for provider in peers):
            return "CSP_SPECIFIC_LOCAL", .8, "RULE_2A_LOCAL_CSP_PEERS"
        if target and not peers and target["down_share"] >= .9:
            return "CSP_SPECIFIC_LOCAL", .6, "RULE_2B_LOCAL_CSP_MONOPOLY"
        if len(qualified) >= 2 and all(provider["down_share"] >= .7 for provider in qualified):
            return "PREMISE_POWER", round(min(.9, min(provider["down_share"] for provider in qualified)), 2), "RULE_3A_POWER_MULTI_CSP"
        if target and not peers and target["down_share"] >= .7:
            return "PREMISE_POWER", .6, "RULE_3B_POWER_MONOPOLY"
        return "UNKNOWN", 0.0, "RULE_4_FIBRE_CHECK"

    def _fibre_evidence(
        self,
        outage_down: list[Device],
        comparison: list[Device],
        states: dict[str, tuple[str, str | None]],
        outage_member_ids: set[str],
    ) -> dict:
        population = {device.device_id: device for device in [*comparison, *outage_down]}
        candidate_down = [
            device for device in population.values() if states[device.device_id][0] == "DOWN"
        ]
        candidate_down_ids = {device.device_id for device in candidate_down}
        mixed = [
            device for device in population.values()
            if states[device.device_id][0] in {"UP", "DOWN"}
        ]
        healthy_ids = sorted(
            device.device_id for device in mixed if states[device.device_id][0] == "UP"
        )
        addressed_down = [device for device in candidate_down if device.address]
        entity_rows = self.address_reader([device.address for device in addressed_down])
        entities = dict(zip((device.device_id for device in addressed_down), entity_rows))
        best = None
        clusters = variable_density_clusters(mixed)
        for cluster in clusters:
            down = [device for device in cluster if device.device_id in candidate_down_ids]
            healthy = [device for device in cluster if states[device.device_id][0] == "UP"]
            cluster_addressed = [device for device in down if device.address]
            if len(cluster_addressed) < 2 or not healthy:
                continue
            match = self._address_group(
                cluster_addressed, [entities[device.device_id] for device in cluster_addressed]
            )
            if match and (best is None or (len(match["affected_device_ids"]), match["confidence"]) > (len(best["affected_device_ids"]), best["confidence"])):
                best = {**match, "healthy_device_ids": sorted(device.device_id for device in healthy)}
        locality = self._locality_group(
            addressed_down, [entities[device.device_id] for device in addressed_down]
        )
        metric_ids = set(
            (best or locality or {"affected_device_ids": candidate_down_ids})["affected_device_ids"]
        )
        metric_down = [device for device in candidate_down if device.device_id in metric_ids]
        _, metric_radii = radius_profile(metric_down)
        metric_times = [timestamp(states[device.device_id][1]) + timedelta(minutes=5) for device in metric_down]
        strongest_10m_count = strongest_window(metric_times)
        strongest_10m_share = round(strongest_10m_count / len(metric_times), 4)
        controls = [
            device for device in population.values()
            if device.device_id not in metric_ids and states[device.device_id][0] in {"UP", "DOWN"}
        ]
        control_up_count = sum(states[device.device_id][0] == "UP" for device in controls)
        control_up_share = round(control_up_count / len(controls), 4) if controls else 0.0
        fallback = bool(
            not best
            and len(metric_down) >= 3
            and metric_radii["r90"] <= 500
            and locality
            and len(controls) >= 5
            and control_up_share >= .7
        )
        directional_rows = []
        for component in variable_density_clusters(candidate_down, reach_scale=.5, max_reach=250):
            profile = directional_profile(component)
            component_ids = {device.device_id for device in component}
            component_controls = [
                device for device in population.values()
                if device.device_id not in component_ids
                and states[device.device_id][0] in {"UP", "DOWN"}
            ]
            component_up = [
                device for device in component_controls if states[device.device_id][0] == "UP"
            ]
            component_up_share = round(len(component_up) / len(component_controls), 4) if component_controls else 0.0
            component_times = [
                timestamp(states[device.device_id][1]) + timedelta(minutes=5) for device in component
            ]
            directional_rows.append({
                **profile,
                "affected_device_ids": sorted(component_ids),
                "healthy_device_ids": sorted(device.device_id for device in component_up),
                "known_control_count": len(component_controls),
                "control_up_count": len(component_up),
                "control_up_share": component_up_share,
                "strongest_10m_share": round(strongest_window(component_times) / len(component_times), 4),
                "matched": (
                    len(component) >= 5
                    and 50 <= profile["length_m"] <= 500
                    and profile["directionality_ratio"] >= 3
                    and profile["perpendicular_p90_m"] <= 50
                    and len(component_controls) >= 5
                    and component_up_share >= .7
                ),
            })
        directional_evidence = max(
            directional_rows,
            key=lambda row: (
                row["matched"], len(row["affected_device_ids"]), row["directionality_ratio"],
                -row["perpendicular_p90_m"],
            ),
            default=None,
        )
        directional = bool(not best and not fallback and directional_evidence and directional_evidence["matched"])
        affected_device_ids = (
            best["affected_device_ids"] if best else
            locality["affected_device_ids"] if fallback else
            directional_evidence["affected_device_ids"] if directional else []
        )
        return {
            "matched": best is not None or fallback or directional,
            "path": "RULE_4A" if best else "RULE_4B" if fallback else "RULE_4C" if directional else "NONE",
            "mixed_device_ids": sorted(device.device_id for device in mixed),
            "mixed_cluster_count": len(clusters),
            "scope": best["scope"] if best else "LOCALITY" if fallback else "DIRECTIONAL" if directional else "UNKNOWN",
            "affected_device_ids": affected_device_ids,
            "affected_member_ids": sorted(set(affected_device_ids) & outage_member_ids),
            "affected_comparison_ids": sorted(set(affected_device_ids) - outage_member_ids),
            "candidate_down_ids": sorted(candidate_down_ids),
            "candidate_up_ids": healthy_ids,
            "healthy_device_ids": best["healthy_device_ids"] if best else directional_evidence["healthy_device_ids"] if directional else healthy_ids,
            "confidence": best["confidence"] if best else .6 if fallback or directional else 0.0,
            "r90_m": metric_radii["r90"],
            "strongest_10m_share": strongest_10m_share,
            "shared_locality": locality["matched_components"][0] if locality else None,
            "shared_locality_count": len(locality["affected_device_ids"]) if locality else 0,
            "known_control_count": len(controls),
            "control_up_count": control_up_count,
            "control_up_share": control_up_share,
            "directional_component_count": len(directional_rows),
            "directional_device_ids": directional_evidence["affected_device_ids"] if directional_evidence else [],
            "principal_length_m": directional_evidence["length_m"] if directional_evidence else 0.0,
            "perpendicular_p90_m": directional_evidence["perpendicular_p90_m"] if directional_evidence else 0.0,
            "directionality_ratio": directional_evidence["directionality_ratio"] if directional_evidence else 0.0,
            "directional_known_control_count": directional_evidence["known_control_count"] if directional_evidence else 0,
            "directional_control_up_count": directional_evidence["control_up_count"] if directional_evidence else 0,
            "directional_control_up_share": directional_evidence["control_up_share"] if directional_evidence else 0.0,
            "directional_strongest_10m_share": directional_evidence["strongest_10m_share"] if directional_evidence else 0.0,
        }

    @staticmethod
    def _address_group(devices: list[Device], entity_rows: list[dict[str, list[dict]]]) -> dict | None:
        groups: dict[tuple[str, str, str], dict[str, float]] = {}

        for device, entities in zip(devices, entity_rows):
            houses = AttributionEngine._entity_values(entities, ("house_details",))
            roads = AttributionEngine._entity_values(entities, ("road",))
            localities = AttributionEngine._entity_values(entities, ("locality", "sub_locality"))
            for house, house_score in houses:
                for context, context_score in [*roads, *localities]:
                    groups.setdefault(("HOUSE", house, context), {})[device.device_id] = (house_score + context_score) / 2
            for road, road_score in roads:
                for locality, locality_score in localities:
                    groups.setdefault(("GALI", road, locality), {})[device.device_id] = (road_score + locality_score) / 2

        for scope, minimum in (("HOUSE", 2), ("GALI", 3)):
            candidates = [
                (key, scores) for key, scores in groups.items()
                if key[0] == scope and len(scores) >= minimum
            ]
            if candidates:
                key, scores = max(candidates, key=lambda item: (len(item[1]), sum(item[1].values())))
                return {
                    "scope": scope,
                    "affected_device_ids": sorted(scores),
                    "confidence": round(min(.9, sum(scores.values()) / len(scores)), 2),
                    "matched_components": list(key[1:]),
                }
        return None

    @staticmethod
    def _locality_group(devices: list[Device], entity_rows: list[dict[str, list[dict]]]) -> dict | None:
        groups: dict[str, dict[str, float]] = {}
        for device, entities in zip(devices, entity_rows):
            for locality, score in AttributionEngine._entity_values(entities, ("locality", "sub_locality")):
                groups.setdefault(locality, {})[device.device_id] = score
        candidates = [(locality, scores) for locality, scores in groups.items() if len(scores) >= 3]
        if not candidates:
            return None
        locality, scores = max(candidates, key=lambda item: (len(item[1]), sum(item[1].values())))
        return {
            "affected_device_ids": sorted(scores),
            "matched_components": [locality],
        }

    @staticmethod
    def _entity_values(entities: dict[str, list[dict]], keys: tuple[str, ...]) -> list[tuple[str, float]]:
        return [
            (normalized, float(item["confidence"]))
            for key in keys
            for item in entities.get(key, [])
            if (normalized := re.sub(r"[^a-z0-9]+", " ", str(item.get("text", "")).lower()).strip())
        ]

    @staticmethod
    def _csp_signal_confidence(share: float) -> float | None:
        return .8 if share >= .7 else None

    def _failure_time(
        self,
        device_id: str,
        cache: dict[str, tuple[str, str | None]],
    ) -> datetime:
        return timestamp(cache[device_id][1]) + timedelta(minutes=5)

    @staticmethod
    def _recovered(member_ids: list[str], cache: dict[str, tuple[str, str | None]]) -> list[str]:
        return sorted(device_id for device_id in member_ids if cache.get(device_id, ("UNKNOWN", None))[0] == "UP")

    def _map_devices(
        self,
        ids: list[str],
        member_ids: list[str],
        cache: dict[str, tuple[str, str | None]],
        polygon_ids: set[str],
    ) -> list[dict]:
        member_set = set(member_ids)
        devices = []
        for device_id in ids:
            device = self.inventory.devices.get(device_id)
            devices.append({
                "device_id": device_id,
                "csp_id": device.csp_id if device else None,
                "latitude": device.latitude if device else None,
                "longitude": device.longitude if device else None,
                "address": device.address if device else None,
                "csp_name": device.csp_name if device else None,
                "state": cache.get(device_id, ("UNKNOWN", None))[0],
                "last_ping_time": cache.get(device_id, ("UNKNOWN", None))[1],
                "member": device_id in member_set,
                "in_polygon": device_id in polygon_ids,
            })
        return devices

    @staticmethod
    def _result(
        outage_id: object,
        attribution: str,
        confidence: float,
        rule: str,
        evaluated_at: datetime,
        ongoing_time: int | float,
        groups: list[dict],
        devices: list[dict],
        review_ids: list[str],
        parent_evidence: list[dict] | None = None,
        polygon_evidence: dict | None = None,
        csp_signal: dict | None = None,
        spatial_evidence: str = "REVIEW",
        recovered_member_ids: list[str] | None = None,
        affected_device_ids: list[str] | None = None,
        ping_devices: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        if ping_devices is None:
            ping_devices = devices
        public = {
            "outage_id": outage_id,
            "attribution": attribution,
            "confidence": confidence,
            "affected_device_ids": affected_device_ids or [],
            "evaluated_at": evaluated_at.isoformat(),
            "device_pings": [{
                "device_id": device["device_id"],
                "status": device["state"],
                "latest_ping_at": device["last_ping_time"],
                "csp": device["csp_id"],
                "in_polygon": device["in_polygon"],
            } for device in ping_devices],
        }
        detail = {
            **public,
            "rule": rule,
            "ongoing_time": ongoing_time,
            "groups": groups,
            "devices": devices,
            "noise_device_ids": sorted(review_ids),
            "review_device_ids": sorted(review_ids),
            "recovered_member_ids": recovered_member_ids or [],
            "parent_evidence": parent_evidence or [],
            "polygon_evidence": polygon_evidence,
            "csp_signal": csp_signal,
            "policy_score": confidence if isinstance(confidence, float) else None,
            "spatial_evidence": spatial_evidence,
            "cause_likelihood": "LIKELY" if confidence >= .7 else "POSSIBLE" if confidence else "UNKNOWN",
            "confirmation_status": "MISSING",
            "confirmation_source": None,
        }
        return public, detail
