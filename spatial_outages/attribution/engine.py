from collections import defaultdict
from datetime import timedelta

from .domain.cell_state import CellState
from .domain.event import AttributionEvent
from .domain.outage import Outage
from .domain.rule_context import RuleContext
from .domain.thresholds import Thresholds
from .rules import RuleEngine


def number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


class AttributionEngine:
    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.rules = RuleEngine()

    def classify(
        self, fleet: dict[tuple[str, str], set[str]], outages: list[Outage]
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        h3s_by_csp: dict[str, set[str]] = defaultdict(set)
        csps_by_h3: dict[str, set[str]] = defaultdict(set)
        for csp, h3_id in fleet:
            h3s_by_csp[csp].add(h3_id)
            csps_by_h3[h3_id].add(csp)

        state_rows, event_rows, evidence_rows, bucket_rows = [], [], [], []
        for event in self._group_events(outages):
            affected: dict[tuple[str, str], set[str]] = defaultdict(set)
            for outage in event.outages:
                for device, h3_id in outage.members.items():
                    affected[(outage.csp_id, h3_id)].add(device)

            affected_csps = {csp for csp, _ in affected}
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
                eligible = len(fleet[(csp, h3_id)])
                failed = len(affected[(csp, h3_id)])
                if failed > eligible:
                    raise ValueError(f"Affected devices exceed baseline for {csp}/{h3_id}")
                share = failed / eligible
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
                csp = outage.csp_id
                down_h3s = down_h3_by_csp[csp]
                member_h3s = set(outage.members.values())
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
                affected_h3_count = len(down_h3s)
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
                    noise_confidence=(
                        "HIGH"
                        if all(
                            states[(csp, h3_id)].state == "UP"
                            and states[(csp, h3_id)].affected_share
                            < self.thresholds.min_affected_share
                            for h3_id in member_h3s
                        )
                        else "MEDIUM"
                    ),
                    thresholds=self.thresholds,
                )
                decision = self.rules.evaluate(context)
                evidence_rows.append(
                    {
                        "outage_id": outage.outage_id,
                        "attribution_event_id": event.event_id,
                        "csp_id": csp,
                        "h3_id": target_h3,
                        "outage_start_ist": outage.start.isoformat(sep=" "),
                        "affected_devices": target.affected_devices,
                        "eligible_devices": target.eligible_devices,
                        "affected_share": number(target.affected_share),
                        "affected_h3_count": affected_h3_count,
                        "eligible_h3_count": eligible_h3_count,
                        "compared_csp_count": len(csps_by_h3[target_h3]),
                        "rule_matched": decision.rule,
                        "bucket": decision.bucket,
                        "confidence": decision.confidence,
                    }
                )
                bucket_rows.append(
                    {
                        "outage_id": outage.outage_id,
                        "bucket": decision.bucket,
                        "confidence": decision.confidence,
                    }
                )
        return state_rows, event_rows, evidence_rows, bucket_rows

    def _group_events(self, outages: list[Outage]) -> list[AttributionEvent]:
        groups: list[list[Outage]] = []
        window = timedelta(minutes=self.thresholds.overlap_window_minutes)
        for outage in outages:
            if not groups or outage.start - groups[-1][0].start > window:
                groups.append([outage])
            else:
                groups[-1].append(outage)
        return [
            AttributionEvent(f"AE{index:06d}", tuple(group))
            for index, group in enumerate(groups, 1)
        ]
