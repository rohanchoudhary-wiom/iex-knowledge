from datetime import datetime, timedelta, timezone

from .models import Device, Inventory, _outage_key
from .spatial import (
    anchored_time_groups,
    cluster_stability,
    convex_hull,
    distance_m,
    radius_core,
    radius_profile,
    strongest_window,
    variable_density_clusters,
)
from .status import StatusReader, device_state, timestamp


class AttributionEngine:
    def __init__(self, inventory: Inventory, status_reader: StatusReader, min_provider_devices: int = 5) -> None:
        self.inventory, self.status_reader = inventory, status_reader
        self.min_provider_devices = min_provider_devices

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
            return self._result(outage_id, "UNKNOWN", "LOW", "CUSTOMER_V2_MISSING", evaluated_at, ongoing_time, [], [], missing)

        target_csps = {device.csp_id for device in members}
        target_csp = next(iter(target_csps)) if len(target_csps) == 1 else None
        csp_signal = None
        if target_csp:
            csp_ids = self.inventory.by_csp[target_csp]
            csp_states = states(csp_ids)
            csp_down = sum(state == "DOWN" for state, _ in csp_states.values())
            csp_down_share = csp_down / len(csp_ids)
            gate_threshold = .75 if len(csp_ids) >= 50 else .8
            score = self._csp_signal_confidence(csp_down_share)
            csp_signal = {
                "csp_id": target_csp,
                "down": csp_down,
                "eligible": len(csp_ids),
                "down_share": round(csp_down_share, 4),
                "gate_threshold": gate_threshold,
                "policy_score": score,
            }
            if csp_down_share >= gate_threshold:
                devices = self._map_devices(csp_ids, member_ids, cache)
                return self._result(
                    outage_id, "ISP_OLT_CSP_SIDE", score, "CSP_DOWN_SHARE", evaluated_at, ongoing_time,
                    [], devices, missing, [csp_signal], csp_signal=csp_signal, spatial_evidence="NOT_APPLICABLE",
                    recovered_member_ids=self._recovered(known_member_ids, cache),
                )

        member_states = states(known_member_ids)
        located_down = [
            device for device in members
            if member_states[device.device_id][0] == "DOWN" and device.latitude is not None
        ]
        failure_evidence = {
            device.device_id: self._failure_time(outage_id, device.device_id, cache)
            for device in located_down
        }
        failure_times = {device_id: value[0] for device_id, value in failure_evidence.items()}
        failure_sources = {device_id: value[1] for device_id, value in failure_evidence.items()}
        located_down = [device for device in located_down if failure_times[device.device_id] is not None]
        refined: list[tuple[list[Device], bool]] = []
        for timed in anchored_time_groups(located_down, failure_times):
            for component in variable_density_clusters(timed):
                refined.extend(self._refine(component))

        groups, review_ids, visible_ids = [], [], set(known_member_ids)
        for index, (component, reclustered) in enumerate(refined, 1):
            group, comparison_ids = self._group(
                outage_id, index, component, target_csp, cache, states, failure_times, failure_sources, reclustered
            )
            groups.append(group)
            visible_ids.update(comparison_ids)
            if not group["supported"]:
                review_ids.extend(group["member_ids"])

        supported = [group for group in groups if group["supported"]]
        causes = {group["attribution"] for group in supported}
        if len(causes) == 1 and "UNKNOWN" not in causes:
            attribution = next(iter(causes))
            confidence = .8 if attribution == "CSP_SPECIFIC_LOCAL" else "MEDIUM"
            rule = next(group["decision_rule"] for group in supported)
        else:
            attribution, confidence = "UNKNOWN", "LOW"
            if not located_down:
                rule = "NO_CURRENT_DOWN_MEMBERS"
            elif not supported:
                rule = "SUB_OUTAGE_REVIEW_ONLY"
            elif causes == {"UNKNOWN"} and all(group["decision_rule"] == "NO_QUALIFIED_PEER" for group in supported):
                rule = "NO_QUALIFIED_PEER"
            elif len(causes) > 1:
                rule = "LOCAL_GROUPS_DISAGREE"
            else:
                rule = "LOCAL_PATTERN_AMBIGUOUS"
            if located_down:
                attribution, rule = self._low_confidence_cause(target_csp, supported)

        devices = self._map_devices(sorted(visible_ids), member_ids, cache)
        local_evidence = next((group["local_csp_evidence"] for group in supported if group["local_csp_evidence"]), None)
        spatial_evidence = "SUPPORTED" if supported else "REVIEW"
        return self._result(
            outage_id, attribution, confidence, rule, evaluated_at, ongoing_time, groups, devices,
            sorted(set(review_ids + missing)), polygon_evidence=local_evidence, csp_signal=csp_signal,
            spatial_evidence=spatial_evidence, recovered_member_ids=self._recovered(known_member_ids, cache),
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
        target_csp: str | None,
        cache: dict[str, tuple[str, str | None]],
        states,
        failure_times: dict[str, datetime],
        failure_sources: dict[str, str],
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
        times = [failure_times[device.device_id] for device in component]
        sources = {failure_sources[device.device_id] for device in component}
        anchor, window_end = min(times), min(times) + timedelta(minutes=30)
        providers, comparison_ids, local_evidence = [], [], None
        cause, decision_rule = "UNKNOWN", "SPATIAL_REVIEW"

        if supported:
            comparison = [
                device for device in self.inventory.devices.values()
                if device.latitude is not None and distance_m(center, device) <= radii["r90"]
            ]
            comparison_ids = [device.device_id for device in comparison]
            comparison_states = states(comparison_ids)
            for csp_id in sorted({device.csp_id for device in comparison}):
                ids = [device.device_id for device in comparison if device.csp_id == csp_id]
                concurrent_down = sum(
                    comparison_states[device_id][0] == "DOWN"
                    and anchor <= self._failure_time(outage_id, device_id, cache)[0] <= window_end
                    for device_id in ids
                )
                unknown = sum(comparison_states[device_id][0] == "UNKNOWN" for device_id in ids)
                nonconcurrent = sum(comparison_states[device_id][0] == "DOWN" for device_id in ids) - concurrent_down
                providers.append({
                    "csp_id": csp_id,
                    "down": concurrent_down,
                    "up": sum(comparison_states[device_id][0] == "UP" for device_id in ids),
                    "nonconcurrent_down": nonconcurrent,
                    "unknown": unknown,
                    "eligible": len(ids),
                    "down_share": round(concurrent_down / len(ids), 4),
                    "up_share": round(sum(comparison_states[device_id][0] == "UP" for device_id in ids) / len(ids), 4),
                    "qualified": len(ids) >= self.min_provider_devices,
                    "eligibility_reason": "ELIGIBLE" if len(ids) >= self.min_provider_devices else "BELOW_MINIMUM",
                })
            local_evidence = self._local_csp_evidence(target_csp, providers, core, radii, tails)
            if local_evidence and local_evidence["matched"]:
                cause, decision_rule = "CSP_SPECIFIC_LOCAL", "LOCAL_CSP_ISOLATION"
            else:
                cause = self._local_cause(target_csp, providers)
                decision_rule = "LOCAL_PROVIDER_PATTERN" if cause != "UNKNOWN" else "NO_QUALIFIED_PEER" if not any(provider["qualified"] and provider["csp_id"] != target_csp for provider in providers) else "MIXED_PROVIDER_PATTERN"

        return {
            "sub_outage_id": f"{outage_id}:{index}",
            "member_ids": [device.device_id for device in component],
            "boundary_member_ids": [device.device_id for device in core],
            "tail_device_ids": [device.device_id for device in tails],
            "boundary": convex_hull(core),
            "center": [round(center[0], 6), round(center[1], 6)],
            "radii_m": radii,
            "supported": supported,
            "evidence_grade": "SUPPORTED" if supported else "REVIEW",
            "review_reasons": reasons,
            "reclustered": reclustered,
            "stability": cluster_stability(component),
            "timing": {
                "source": next(iter(sources)) if len(sources) == 1 else "MIXED_V3_AND_LAST_PING_PROXY",
                "window_start": anchor.isoformat(),
                "window_end": window_end.isoformat(),
                "failure_span_minutes": round((max(times) - min(times)).total_seconds() / 60, 1),
                "strongest_10m_count": strongest_window(times),
                "strongest_10m_share": round(strongest_window(times) / len(times), 4),
            },
            "attribution": cause,
            "decision_rule": decision_rule,
            "cause_likelihood": "LIKELY" if cause != "UNKNOWN" else "UNKNOWN",
            "confirmation_status": "MISSING",
            "providers": providers,
            "local_csp_evidence": local_evidence,
        }, comparison_ids

    @staticmethod
    def _local_csp_evidence(
        target_csp: str | None,
        providers: list[dict],
        core: list[Device],
        radii: dict[str, float],
        tails: list[Device],
    ) -> dict | None:
        if not target_csp:
            return None
        target = next((provider for provider in providers if provider["csp_id"] == target_csp), None)
        peers = [provider for provider in providers if provider["csp_id"] != target_csp]
        if not target:
            return None
        peer_devices = sum(provider["eligible"] for provider in peers)
        peer_up = sum(provider["up"] for provider in peers)
        peer_up_share = peer_up / peer_devices if peer_devices else 0
        matched = sum(provider["eligible"] for provider in providers) > 20 and peer_devices > 10 and target["down_share"] >= .8 and peer_up_share >= .8
        return {
            "matched": matched,
            "polygon_devices": sum(provider["eligible"] for provider in providers),
            "target_csp": target_csp,
            "target_devices": target["eligible"],
            "target_down": target["down"],
            "target_down_share": target["down_share"],
            "peer_devices": peer_devices,
            "peer_up": peer_up,
            "peer_up_share": round(peer_up_share, 4),
            "boundary": convex_hull(core),
            "member_radii_m": radii,
            "tail_device_ids": [device.device_id for device in tails],
        }

    @staticmethod
    def _local_cause(target_csp: str | None, providers: list[dict]) -> str:
        qualified = [provider for provider in providers if provider["qualified"]]
        if sum(provider["down_share"] >= .7 for provider in qualified) >= 2:
            return "PREMISE_POWER"
        target = next((provider for provider in qualified if provider["csp_id"] == target_csp), None)
        peers = [provider for provider in qualified if provider["csp_id"] != target_csp]
        if target and target["down_share"] >= .7 and peers and all(
            provider["down_share"] < .2 and provider["up_share"] >= .8 for provider in peers
        ):
            return "FIBRE_CUT"
        return "UNKNOWN"

    @staticmethod
    def _low_confidence_cause(target_csp: str | None, groups: list[dict]) -> tuple[str, str]:
        impacted_csps = {
            provider["csp_id"]
            for group in groups
            for provider in group["providers"]
            if provider["qualified"] and provider["down"] >= 2
        }
        if len(impacted_csps) >= 2 or target_csp is None:
            return "PREMISE_POWER", "LOW_CONFIDENCE_MULTI_CSP_SIGNAL"
        return "FIBRE_CUT", "LOW_CONFIDENCE_SINGLE_CSP_SIGNAL"

    @staticmethod
    def _csp_signal_confidence(share: float) -> float | None:
        return .9 if share >= .8 else .8 if share >= .75 else .75 if share >= .7 else .6 if share >= .65 else .5 if share >= .6 else None

    def _failure_time(
        self,
        outage_id: object,
        device_id: str,
        cache: dict[str, tuple[str, str | None]],
    ) -> tuple[datetime, str]:
        v3_time = self.inventory.outage_failure_times.get((_outage_key(outage_id), device_id))
        if v3_time:
            return v3_time, "OUTAGE_MEMBER_V3"
        # Detection V3 defines first failure as last successful ping plus five minutes.
        return timestamp(cache[device_id][1]) + timedelta(minutes=5), "LAST_PING_PROXY"

    @staticmethod
    def _recovered(member_ids: list[str], cache: dict[str, tuple[str, str | None]]) -> list[str]:
        return sorted(device_id for device_id in member_ids if cache.get(device_id, ("UNKNOWN", None))[0] == "UP")

    def _map_devices(self, ids: list[str], member_ids: list[str], cache: dict[str, tuple[str, str | None]]) -> list[dict]:
        member_set = set(member_ids)
        return [
            {
                "device_id": device_id,
                "csp_id": self.inventory.devices[device_id].csp_id,
                "latitude": self.inventory.devices[device_id].latitude,
                "longitude": self.inventory.devices[device_id].longitude,
                "address": self.inventory.devices[device_id].address,
                "csp_name": self.inventory.devices[device_id].csp_name,
                "state": cache.get(device_id, ("UNKNOWN", None))[0],
                "last_ping_time": cache.get(device_id, ("UNKNOWN", None))[1],
                "member": device_id in member_set,
            }
            for device_id in ids
        ]

    @staticmethod
    def _result(
        outage_id: object,
        attribution: str,
        confidence: str | float,
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
    ) -> tuple[dict, dict]:
        public = {"outage_id": outage_id, "attribution": attribution, "confidence": confidence}
        detail = {
            **public,
            "rule": rule,
            "evaluated_at": evaluated_at.isoformat(),
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
            "cause_likelihood": "POSSIBLE" if confidence == "LOW" and attribution != "UNKNOWN" else "LIKELY" if attribution != "UNKNOWN" else "UNKNOWN",
            "confirmation_status": "MISSING",
            "confirmation_source": None,
        }
        return public, detail
