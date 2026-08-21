import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    csp_id: str
    latitude: float | None
    longitude: float | None
    address: str | None = None
    csp_name: str | None = None


class Inventory:
    def __init__(self, devices: dict[str, Device], outage_failure_times: dict[tuple[str, str], datetime] | None = None) -> None:
        if not devices:
            raise ValueError("Customer V2 inventory is empty")
        self.devices = devices
        self.outage_failure_times = outage_failure_times or {}
        self.by_csp: dict[str, list[str]] = defaultdict(list)
        for device in devices.values():
            self.by_csp[device.csp_id].append(device.device_id)
        for ids in self.by_csp.values():
            ids.sort()

    @classmethod
    def from_csv(cls, path: Path) -> "Inventory":
        devices: dict[str, Device] = {}
        outage_failure_times: dict[tuple[str, str], datetime] = {}
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"device_id", "csp_id", "latitude", "longitude"}
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")
            for line, row in enumerate(reader, 2):
                active = row.get("is_active", "true").strip().lower()
                if active not in {"true", "1", "false", "0"}:
                    raise ValueError(f"{path}:{line} has invalid is_active")
                if active in {"false", "0"}:
                    continue
                device_id, csp_id = row["device_id"].strip(), row["csp_id"].strip()
                if not device_id or not csp_id:
                    raise ValueError(f"{path}:{line} requires device_id and csp_id")
                latitude, longitude = _coordinates(path, line, row)
                device = Device(
                    device_id,
                    csp_id,
                    latitude,
                    longitude,
                    row.get("customer_address", "").strip() or None,
                    row.get("csp_name", "").strip() or None,
                )
                previous = devices.setdefault(device_id, device)
                if previous != device:
                    raise ValueError(f"{path}:{line} conflicts with device {device_id!r}")
                outage_id = row.get("outage_id", "").strip()
                failed_at = row.get("member_first_fail_at_ist", "").strip()
                if outage_id and failed_at:
                    key = (_outage_key(outage_id), device_id)
                    value = _failure_time(path, line, failed_at)
                    previous_time = outage_failure_times.setdefault(key, value)
                    if previous_time != value:
                        raise ValueError(f"{path}:{line} conflicts with outage failure time for {device_id!r}")
        return cls(devices, outage_failure_times)


def _coordinates(path: Path, line: int, row: dict[str, str]) -> tuple[float | None, float | None]:
    latitude, longitude = row["latitude"].strip(), row["longitude"].strip()
    if not latitude and not longitude:
        return None, None
    if not latitude or not longitude:
        raise ValueError(f"{path}:{line} has a partial location")
    try:
        point = float(latitude), float(longitude)
    except ValueError as exc:
        raise ValueError(f"{path}:{line} has an invalid location") from exc
    if not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180:
        raise ValueError(f"{path}:{line} has an out-of-range location")
    return point


def _outage_key(value: object) -> str:
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def _failure_time(path: Path, line: int, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path}:{line} has an invalid member_first_fail_at_ist") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return parsed.astimezone(timezone.utc)
