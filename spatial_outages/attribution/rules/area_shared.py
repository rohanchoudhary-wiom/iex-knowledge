from ..domain.decision import Decision
from ..domain.rule_context import RuleContext


class AreaSharedRule:
    def evaluate(self, context: RuleContext) -> Decision | None:
        if (
            len(context.down_csps_here) >= 2
            and context.cross_h3_comparison
            and not context.shared_down_elsewhere
        ):
            return Decision("UNKNOWN", "R2_MULTI_CSP_ONE_H3", "LOW", "LOCAL")
        return None
