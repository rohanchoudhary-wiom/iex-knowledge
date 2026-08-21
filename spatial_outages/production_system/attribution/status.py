import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable


DOWN_AFTER_SECONDS = 10 * 60
StatusReader = Callable[[list[str]], dict[str, object]]


class StatusClient:
    def __init__(self, url: str, timeout: float = 35, batch_size: int = 200) -> None:
        if not url:
            raise ValueError("batch device ping URL is required")
        self.url, self.timeout, self.batch_size = url, timeout, batch_size

    def __call__(self, device_ids: list[str]) -> dict[str, object]:
        statuses: dict[str, object] = {}
        for start in range(0, len(device_ids), self.batch_size):
            batch = device_ids[start:start + self.batch_size]
            request = urllib.request.Request(
                self.url,
                data=json.dumps({"deviceIds": batch}).encode(),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "IEX-Outage-Attribution/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"batch device ping returned HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"batch device ping is unavailable: {exc.reason}") from exc
            statuses.update(_normalize_status_response(payload))
        return {device_id: statuses.get(device_id) for device_id in device_ids}


def _normalize_status_response(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("status") != 0:
        raise RuntimeError("batch device ping reported failure")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("batch device ping response must contain data")
    if data.get("truncated") is True:
        raise RuntimeError("batch device ping response was truncated")
    rows = data.get("devices")
    if not isinstance(rows, list):
        raise ValueError("batch device ping response must contain a device list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("deviceId"), str):
            raise ValueError("batch device ping returned an invalid device row")
        result[row["deviceId"]] = row.get("latestPing")
    return result


def timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.strptime(value, "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (OverflowError, OSError, ValueError):
        return None


def device_state(last_ping: object, evaluated_at: datetime) -> tuple[str, str | None]:
    ping = timestamp(last_ping)
    if ping is None or ping > evaluated_at:
        return "UNKNOWN", None
    state = "DOWN" if (evaluated_at - ping).total_seconds() >= DOWN_AFTER_SECONDS else "UP"
    return state, ping.isoformat()
