from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Decision:
    root_cause: str
    rule: str
    confidence: str
    spatial_extent: str
