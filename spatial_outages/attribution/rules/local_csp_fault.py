from ..domain.decision import Decision
from ..domain.rule_context import RuleContext


class LocalCspFaultRule:
    def evaluate(self, context: RuleContext) -> Decision | None:
        if (
            context.down_h3s
            and len(context.down_csps_here) < 2
            and context.eligible_h3_count >= 2
            and context.affected_h3_share < context.thresholds.almost_all_h3_share
            and context.neighbor_up
        ):
            return Decision("CSP_SIDE", "R5_NEIGHBOR_CSP_UP", context.cause_confidence, "LOCAL")
        return None
