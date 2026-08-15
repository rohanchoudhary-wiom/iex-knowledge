from ..domain.decision import Decision
from ..domain.rule_context import RuleContext


class RegionalRule:
    def evaluate(self, context: RuleContext) -> Decision | None:
        if (
            len(context.down_csps_here) >= 2
            and context.cross_h3_comparison
            and context.shared_down_elsewhere
        ):
            return Decision("UNKNOWN", "R3_MULTI_CSP_MULTI_H3", "LOW", "REGIONAL")
        return None
