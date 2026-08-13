from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Outage:
    outage_id: str
    csp_id: str
    start: datetime
    members: dict[str, str] = field(default_factory=dict)
