from dataclasses import dataclass

from .thresholds import Thresholds


@dataclass(frozen=True, slots=True)
class RuleContext:
    down_h3s: frozenset[str]
    down_csps_here: frozenset[str]
    eligible_h3_count: int
    affected_h3_share: float
    neighboring_csps: frozenset[str]
    neighbor_up: bool
    cross_h3_comparison: bool
    shared_down_elsewhere: bool
    cause_confidence: str
    noise_confidence: str
    thresholds: Thresholds
