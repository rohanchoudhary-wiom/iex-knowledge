from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CellState:
    csp_id: str
    h3_id: str
    affected_devices: int
    eligible_devices: int
    affected_share: float
    state: str
