from ..domain.decision import Decision
from ..domain.rule_context import RuleContext
from .area_shared import AreaSharedRule
from .isp_olt import IspOltRule
from .local_csp_fault import LocalCspFaultRule
from .noise import NoiseRule
from .regional import RegionalRule
from .unknown import UnknownRule


class RuleEngine:
    def __init__(self) -> None:
        self.rules = (
            NoiseRule(),
            AreaSharedRule(),
            RegionalRule(),
            IspOltRule(),
            LocalCspFaultRule(),
            UnknownRule(),
        )

    def evaluate(self, context: RuleContext) -> Decision:
        for rule in self.rules:
            if decision := rule.evaluate(context):
                return decision
        raise AssertionError("UnknownRule must always return a decision")
