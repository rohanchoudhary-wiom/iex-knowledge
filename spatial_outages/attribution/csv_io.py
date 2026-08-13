import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .domain.outage import Outage


INPUT_COLUMNS = (
    "device_id",
    "csp_id",
    "h3_id",
    "outage_id",
    "member_first_fail_at_ist",
)
OUTPUTS = {
    "csp_h3_states.csv": (
        "attribution_event_id", "csp_id", "h3_id", "affected_devices",
        "eligible_devices", "affected_share", "csp_h3_state",
    ),
    "attribution_events.csv": (
        "attribution_event_id", "event_start_ist", "event_end_ist",
        "outage_count", "affected_csp_count", "affected_h3_count",
    ),
    "outage_evidence.csv": (
        "outage_id", "attribution_event_id", "csp_id", "h3_id",
        "outage_start_ist", "affected_devices", "eligible_devices",
        "affected_share", "affected_h3_count", "eligible_h3_count",
        "compared_csp_count", "rule_matched", "bucket", "confidence",
    ),
    "outage_buckets.csv": ("outage_id", "bucket", "confidence"),
}


def read_input(path: Path) -> tuple[dict[tuple[str, str], set[str]], list[Outage]]:
    fleet: dict[tuple[str, str], set[str]] = {}
    device_home: dict[str, tuple[str, str]] = {}
    outages: dict[str, Outage] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(INPUT_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, 2):
            device, csp, h3_id = (row[column].strip() for column in INPUT_COLUMNS[:3])
            if not device or not csp or not h3_id:
                raise ValueError(f"{path}:{line} requires device_id, csp_id, and h3_id")
            home = (csp, h3_id)
            if device in device_home and device_home[device] != home:
                raise ValueError(f"{path}:{line} gives device {device!r} conflicting CSP/H3 values")
            device_home[device] = home
            fleet.setdefault(home, set()).add(device)

            outage_id, failure = (row[column].strip() for column in INPUT_COLUMNS[3:])
            if not outage_id:
                if failure:
                    raise ValueError(f"{path}:{line} has partial outage fields")
                continue
            if not failure:
                raise ValueError(f"{path}:{line} has a missing outage time")
            try:
                failed_at = datetime.fromisoformat(failure)
            except ValueError as exc:
                raise ValueError(f"Invalid member_first_fail_at_ist: {failure!r}") from exc
            outage = outages.setdefault(outage_id, Outage(outage_id, csp, failed_at))
            if outage.csp_id != csp:
                raise ValueError(f"Outage {outage_id!r} belongs to multiple CSP IDs")
            outage.start = min(outage.start, failed_at)
            previous_h3 = outage.members.setdefault(device, h3_id)
            if previous_h3 != h3_id:
                raise ValueError(f"Outage {outage_id!r} gives device {device!r} conflicting H3 values")
    if not fleet:
        raise ValueError("No active device rows were supplied")
    if not outages:
        raise ValueError("No outage rows were supplied")
    return fleet, sorted(outages.values(), key=lambda outage: (outage.start, outage.outage_id))


def write_outputs(output_dir: Path, rows: tuple[list[dict], ...]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for (name, columns), values in zip(OUTPUTS.items(), rows):
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=output_dir)
        try:
            with os.fdopen(descriptor, "w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(values)
            os.replace(temporary, output_dir / name)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
