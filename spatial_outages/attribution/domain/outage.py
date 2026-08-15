from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Device:
    csp_id: str
    h3_id: str | None
    latitude: float | None
    longitude: float | None
    is_active: bool


@dataclass(slots=True)
class Outage:
    outage_id: str
    csp_id: str
    trigger_time: datetime
    members: set[str] = field(default_factory=set)
    h3_ids: dict[str, str] = field(default_factory=dict)
    locations: dict[str, tuple[float, float]] = field(default_factory=dict)
