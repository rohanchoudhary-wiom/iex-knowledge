from dataclasses import dataclass
from datetime import datetime

from .outage import Outage


@dataclass(frozen=True, slots=True)
class AttributionEvent:
    event_id: str
    outages: tuple[Outage, ...]

    @property
    def start(self) -> datetime:
        return min(outage.trigger_time for outage in self.outages)

    @property
    def end(self) -> datetime:
        return max(outage.trigger_time for outage in self.outages)
