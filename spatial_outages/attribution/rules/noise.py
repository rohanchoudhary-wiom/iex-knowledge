from ..domain.decision import Decision
from ..domain.rule_context import RuleContext


class NoiseRule:
    def evaluate(self, context: RuleContext) -> Decision | None:
        if not context.down_h3s:
            return Decision("NOISE", "R1_NO_DOWN_CSP_H3", context.noise_confidence)
        return None
