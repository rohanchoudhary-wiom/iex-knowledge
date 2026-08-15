import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .domain.outage import Device, Outage


INPUT_COLUMNS = (
    "device_id",
    "csp_id",
    "latitude",
    "longitude",
    "h3_id",
    "outage_id",
    "member_first_fail_at_ist",
)
OPTIONAL_INPUT_COLUMNS = ("is_active", "outage_trigger_time")
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
        "outage_trigger_time", "down_devices_csp", "eligible_devices_csp",
        "csp_down_share", "affected_devices", "eligible_devices",
        "affected_share", "affected_h3_count", "eligible_h3_count",
        "compared_csp_count", "rule_matched", "root_cause", "spatial_extent",
        "confidence", "geometry_quality",
    ),
    "outage_attributions.csv": (
        "outage_id", "geometry", "geometry_version", "polygon_area_km2",
        "unhealthy_device_count", "located_unhealthy_device_count",
        "healthy_device_count", "root_cause", "sub_cause", "spatial_extent",
        "event_pattern", "confidence", "cause_distribution", "evidence",
        "expected_restoration_class", "revision", "revised_at", "is_final",
        "engine_version", "snapshot_at",
    ),
}


def _time(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def _location(path: Path, line: int, row: dict[str, str]) -> tuple[float, float] | None:
    latitude, longitude = row["latitude"].strip(), row["longitude"].strip()
    if not latitude and not longitude:
        return None
    if not latitude or not longitude:
        raise ValueError(f"{path}:{line} has a partial location")
    try:
        point = float(latitude), float(longitude)
    except ValueError as exc:
        raise ValueError(f"{path}:{line} has an invalid location") from exc
    if not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180:
        raise ValueError(f"{path}:{line} has an out-of-range location")
    return point


def read_input(path: Path) -> tuple[dict[str, Device], list[Outage]]:
    fleet: dict[str, Device] = {}
    outages: dict[str, Outage] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(INPUT_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, 2):
            device, csp = row["device_id"].strip(), row["csp_id"].strip()
            if not device or not csp:
                raise ValueError(f"{path}:{line} requires device_id and csp_id")
            location = _location(path, line, row)
            active = row.get("is_active", "true").strip().lower()
            if active not in {"true", "false", "1", "0"}:
                raise ValueError(f"{path}:{line} has invalid is_active: {active!r}")
            snapshot = Device(
                csp,
                row["h3_id"].strip() or None,
                location[0] if location else None,
                location[1] if location else None,
                active in {"true", "1"},
            )
            previous = fleet.setdefault(device, snapshot)
            if previous != snapshot:
                raise ValueError(f"{path}:{line} gives device {device!r} conflicting registry data")

            outage_id = row["outage_id"].strip()
            failure = row["member_first_fail_at_ist"].strip()
            trigger = row.get("outage_trigger_time", "").strip()
            if not outage_id:
                if failure or trigger:
                    raise ValueError(f"{path}:{line} has partial outage fields")
                continue
            if not failure:
                raise ValueError(f"{path}:{line} has a missing outage member time")
            failed_at = _time(failure, "member_first_fail_at_ist")
            trigger_at = _time(trigger, "outage_trigger_time") if trigger else failed_at
            outage = outages.setdefault(outage_id, Outage(outage_id, csp, trigger_at))
            if outage.csp_id != csp:
                raise ValueError(f"Outage {outage_id!r} belongs to multiple CSP IDs")
            if trigger:
                if outage.trigger_time != trigger_at:
                    raise ValueError(f"Outage {outage_id!r} has conflicting trigger times")
            else:
                outage.trigger_time = min(outage.trigger_time, failed_at)
            outage.members.add(device)
            if snapshot.h3_id:
                previous_h3 = outage.h3_ids.setdefault(device, snapshot.h3_id)
                if previous_h3 != snapshot.h3_id:
                    raise ValueError(f"Outage {outage_id!r} gives device {device!r} conflicting H3 values")
            if location:
                previous_location = outage.locations.setdefault(device, location)
                if previous_location != location:
                    raise ValueError(f"Outage {outage_id!r} gives device {device!r} conflicting locations")
    if not fleet:
        raise ValueError("No device rows were supplied")
    if not outages:
        raise ValueError("No outage rows were supplied")
    return fleet, sorted(outages.values(), key=lambda outage: (outage.trigger_time, outage.outage_id))


def _versioned(path: Path, rows: list[dict]) -> list[dict]:
    previous: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="") as handle:
            previous = {row["outage_id"]: row for row in csv.DictReader(handle)}
    ids = [row["outage_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Attribution output must contain one row per outage_id")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    content = set(OUTPUTS[path.name]) - {"revision", "revised_at", "snapshot_at"}
    for row in rows:
        old = previous.get(row["outage_id"])
        if old and all(str(row.get(column, "")) == old.get(column, "") for column in content):
            row.update({column: old[column] for column in ("revision", "revised_at", "snapshot_at")})
        else:
            row["revision"] = int(old["revision"]) + 1 if old else 1
            row["revised_at"] = now
            row["snapshot_at"] = now
    return sorted(rows, key=lambda row: row["outage_id"])


def write_outputs(output_dir: Path, rows: tuple[list[dict], ...]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for (name, columns), values in zip(OUTPUTS.items(), rows, strict=True):
        values = _versioned(output_dir / name, values) if name == "outage_attributions.csv" else values
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
