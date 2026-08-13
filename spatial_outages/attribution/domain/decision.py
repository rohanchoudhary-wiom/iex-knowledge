from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Decision:
    bucket: str
    rule: str
    confidence: str
