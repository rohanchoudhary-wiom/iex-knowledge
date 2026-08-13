from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Thresholds:
    min_affected_share: float = 0.7
    overlap_window_minutes: int = 30
    almost_all_h3_share: float = 0.8

    def __post_init__(self) -> None:
        if self.overlap_window_minutes < 0:
            raise ValueError("overlap window cannot be negative")
        if not 0 < self.min_affected_share <= 1 or not 0 < self.almost_all_h3_share <= 1:
            raise ValueError("share thresholds must be in (0, 1]")
