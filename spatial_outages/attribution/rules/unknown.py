from ..domain.decision import Decision
from ..domain.rule_context import RuleContext


class UnknownRule:
    def evaluate(self, context: RuleContext) -> Decision:
        if len(context.down_csps_here) >= 2 or context.eligible_h3_count < 2:
            rule = "R6_NO_CROSS_H3_COMPARISON"
        elif not context.neighboring_csps:
            rule = "R6_NO_NEIGHBOR_CSP"
        else:
            rule = "R6_AMBIGUOUS_PATTERN"
        return Decision("UNKNOWN", rule, "LOW", "LOCAL")
