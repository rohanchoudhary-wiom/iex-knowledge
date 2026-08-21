import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from attribution import AttributionEngine, Inventory, StatusClient
from demo import DEMO_OUTAGES, demo_data


ROOT = Path(__file__).resolve().parent
DEFAULT_CUSTOMER_CSV = ROOT.parent / "data/input/outage_devices.csv"
DEFAULT_OUTAGE_URL = "https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN"
DEFAULT_STATUS_URL = "https://remote.i2e1.in/REMOTE/GetBatchDevicePing"


class Service:
    def __init__(
        self,
        engine: AttributionEngine,
        outage_reader: Callable[[], tuple[datetime, list[dict]]] | None = None,
        source: str = "LIVE",
        warning: str | None = None,
    ) -> None:
        self.engine, self.outage_reader, self.source = engine, outage_reader, source
        self.warning = warning
        self.results, self.lock = {}, threading.Lock()
        self.status = "demo" if source == "DEMO" else "starting"
        self.as_of = self.refreshed_at = self.error = None

    def evaluate(self, payload: object) -> dict:
        outage_id, devices, ongoing_time = validate_request(payload)
        public, detail = self.engine.evaluate(outage_id, devices, ongoing_time)
        with self.lock:
            self.results[str(outage_id)] = detail
        return public

    def refresh(self) -> None:
        if self.outage_reader is None:
            return
        as_of, outages = self.outage_reader()
        cache: dict[str, object] = {}

        def snapshot_status(device_ids: list[str]) -> dict[str, object]:
            missing = [device_id for device_id in device_ids if device_id not in cache]
            if missing:
                cache.update(self.engine.status_reader(missing))
            return {device_id: cache.get(device_id) for device_id in device_ids}

        engine = AttributionEngine(self.engine.inventory, snapshot_status, self.engine.min_provider_devices)
        fresh = {}
        for outage in outages:
            public, detail = engine.evaluate(
                outage["outage_id"], outage["devices"], outage["ongoing_time"], as_of
            )
            fresh[str(public["outage_id"])] = detail
        with self.lock:
            self.results = fresh
            self.status = "degraded" if self.warning else "ok"
            self.error = self.warning
            self.as_of = as_of.isoformat()
            self.refreshed_at = datetime.now(timezone.utc).isoformat()

    def fail(self, error: Exception) -> None:
        with self.lock:
            self.status = "stale" if self.results else "error"
            self.error = str(error)

    def map_data(self) -> dict:
        with self.lock:
            results = list(self.results.values())
            metadata = {
                "source": self.source,
                "status": self.status,
                "as_of": self.as_of,
                "refreshed_at": self.refreshed_at,
                "error": self.error,
            }
        return {**metadata, "results": sorted(results, key=lambda row: str(row["outage_id"]))}

    def health(self) -> dict:
        payload = self.map_data()
        return {
            "status": payload["status"],
            "source": payload["source"],
            "outages": len(payload["results"]),
            "as_of": payload["as_of"],
            "refreshed_at": payload["refreshed_at"],
            "error": payload["error"],
        }


def validate_request(payload: object) -> tuple[object, list[str], int | float]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    outage_id = payload.get("outage_id")
    if isinstance(outage_id, bool) or outage_id is None or (isinstance(outage_id, str) and not outage_id.strip()):
        raise ValueError("outage_id is required")
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices or any(not isinstance(value, str) or not value.strip() for value in devices):
        raise ValueError("devices must be a non-empty list of device IDs")
    devices = [value.strip() for value in devices]
    if len(devices) != len(set(devices)):
        raise ValueError("devices must not contain duplicates")
    recovered = payload.get("recovered_devices", [])
    if not isinstance(recovered, list) or any(not isinstance(value, str) or not value.strip() for value in recovered):
        raise ValueError("recovered_devices must be a list of device IDs")
    devices += [value.strip() for value in recovered if value.strip() not in devices]
    ongoing_time = payload.get("ongoing_time")
    if isinstance(ongoing_time, bool) or not isinstance(ongoing_time, (int, float)) or ongoing_time < 0:
        raise ValueError("ongoing_time must be non-negative seconds")
    return outage_id, devices, ongoing_time


def parse_outage_feed(payload: object) -> tuple[datetime, list[dict]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("outages"), list):
        raise ValueError("outage feed must contain an outages list")
    as_of_raw = payload.get("as_of")
    if isinstance(as_of_raw, bool) or not isinstance(as_of_raw, (int, float)):
        raise ValueError("outage feed as_of must be an epoch timestamp")
    try:
        as_of = datetime.fromtimestamp(as_of_raw, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("outage feed as_of is invalid") from exc
    if abs((datetime.now(timezone.utc) - as_of).total_seconds()) > 600:
        raise ValueError("outage feed as_of is more than 10 minutes from server time")
    if payload.get("count") != len(payload["outages"]):
        raise ValueError("outage feed count does not match outages")
    outages, seen = [], set()
    for row in payload["outages"]:
        outage_id, devices, ongoing_time = validate_request(row)
        key = str(outage_id)
        if key in seen:
            raise ValueError(f"outage feed duplicates outage_id {outage_id}")
        seen.add(key)
        outages.append({"outage_id": outage_id, "devices": devices, "ongoing_time": ongoing_time})
    return as_of, outages


def fetch_open_outages(url: str, timeout: float = 10) -> tuple[datetime, list[dict]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse_outage_feed(json.load(response))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"outage feed returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"outage feed is unavailable: {exc.reason}") from exc


def ensure_inventory_fresh(path: Path, max_age_hours: float) -> None:
    if max_age_hours <= 0:
        raise ValueError("inventory max age must be positive")
    age_seconds = time.time() - path.stat().st_mtime
    if age_seconds < -300:
        raise ValueError("Customer V2 snapshot modification time is in the future")
    if age_seconds > max_age_hours * 3600:
        raise ValueError(
            f"Customer V2 snapshot is {age_seconds / 3600:.1f} hours old; refresh it before startup"
        )


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            body = (ROOT / "static/index.html").read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif self.path == "/health":
            health = self.server.service.health()
            self._json(200 if health["status"] in {"ok", "demo"} else 503, health)
        elif self.path == "/map-data":
            self._json(200, self.server.service.map_data())
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/outage_attribution":
                self._json(200, self.server.service.evaluate(payload))
            elif self.path == "/mock/get_device_status" and self.server.demo:
                device_ids = payload.get("device_ids") if isinstance(payload, dict) else None
                if not isinstance(device_ids, list) or any(not isinstance(value, str) for value in device_ids):
                    raise ValueError("device_ids must be a list")
                now = datetime.now(timezone.utc)
                self._json(200, {"devices": [
                    {
                        "device_id": device_id,
                        "last_ping_time": (
                            (now - timedelta(seconds=self.server.mock_ages[device_id])).isoformat()
                            if self.server.mock_ages.get(device_id) is not None else None
                        ),
                    }
                    for device_id in device_ids
                ]})
            else:
                self._json(404, {"error": "not_found"})
        except ValueError as exc:
            self._json(400, {"error": "invalid_request", "message": str(exc)})
        except Exception as exc:
            self._json(503, {"error": "dependency_unavailable", "message": str(exc)})

    def _read_json(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 < length <= 2_000_000:
            raise ValueError("request body is empty or too large")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(ThreadingHTTPServer):
    service: Service
    demo: bool
    mock_ages: dict[str, int | None]
    refresh_seconds: float
    stop_refresh: threading.Event


def build_server(
    host: str,
    port: int,
    customer_csv: Path,
    status_url: str | None,
    outage_url: str,
    refresh_seconds: float,
    inventory_max_age_hours: float,
    demo: bool,
    allow_missing_status: bool,
) -> Server:
    server = Server((host, port), Handler)
    server.demo, server.refresh_seconds, server.stop_refresh = demo, refresh_seconds, threading.Event()
    try:
        if demo:
            inventory, server.mock_ages = demo_data()

            def statuses(device_ids: list[str]) -> dict[str, object]:
                now = datetime.now(timezone.utc)
                return {
                    device_id: (
                        now - timedelta(seconds=server.mock_ages[device_id])
                        if server.mock_ages.get(device_id) is not None else None
                    )
                    for device_id in device_ids
                }

            server.service = Service(AttributionEngine(inventory, statuses), source="DEMO")
            for outage in DEMO_OUTAGES:
                server.service.evaluate(outage)
        else:
            if status_url is None and not allow_missing_status:
                raise ValueError("--status-url is required")
            ensure_inventory_fresh(customer_csv, inventory_max_age_hours)
            inventory, server.mock_ages = Inventory.from_csv(customer_csv), {}
            warning = None if status_url else "batch device ping is not configured; device states and attribution remain UNKNOWN"
            server.service = Service(
                AttributionEngine(inventory, StatusClient(status_url) if status_url else lambda device_ids: {}),
                lambda: fetch_open_outages(outage_url),
                warning=warning,
            )
            server.service.refresh()
    except BaseException:
        server.server_close()
        raise
    return server


def refresh_loop(server: Server) -> None:
    while not server.stop_refresh.wait(server.refresh_seconds):
        try:
            server.service.refresh()
        except Exception as exc:
            server.service.fail(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="IEX outage attribution service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--customer-csv", type=Path, default=DEFAULT_CUSTOMER_CSV)
    parser.add_argument("--status-url", default=os.environ.get("GET_DEVICE_STATUS_URL", DEFAULT_STATUS_URL))
    parser.add_argument("--outage-url", default=os.environ.get("OUTAGE_SOURCE_URL", DEFAULT_OUTAGE_URL))
    parser.add_argument("--refresh-seconds", type=float, default=60)
    parser.add_argument("--inventory-max-age-hours", type=float, default=24)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--allow-missing-status", action="store_true")
    args = parser.parse_args()
    if args.refresh_seconds <= 0:
        parser.error("--refresh-seconds must be positive")
    try:
        server = build_server(
            args.host,
            args.port,
            args.customer_csv,
            args.status_url,
            args.outage_url,
            args.refresh_seconds,
            args.inventory_max_age_hours,
            args.demo,
            args.allow_missing_status,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    refresh_thread = None
    if not args.demo:
        refresh_thread = threading.Thread(target=refresh_loop, args=(server,), daemon=True)
        refresh_thread.start()
    print(f"serving http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop_refresh.set()
        if refresh_thread:
            refresh_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
