import base64
import csv
import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shapely import from_wkt, to_geojson


ROOT = Path(__file__).resolve().parent
PING_WORDS = 7


def ping_bitmap(row: dict[str, str], byte_count: int) -> str:
    bits = sum(
        int(row.get(f"ping_bits_{index}") or 0) << (60 * index)
        for index in range(PING_WORDS)
    )
    if not bits:
        return ""
    return base64.b64encode(bits.to_bytes(byte_count, "little")).decode()


def main() -> None:
    with (ROOT / "data/input/outage_devices.csv").open(newline="") as handle:
        device_rows = list(csv.DictReader(handle))
        csp_names = {
            row["csp_id"]: row["csp_name"]
            for row in device_rows
            if row["csp_name"]
        }
    with (ROOT / "data/output/outage_attributions.csv").open(newline="") as handle:
        attributions = {row["outage_id"]: row for row in csv.DictReader(handle)}
    with (ROOT / "data/output/outage_evidence.csv").open(newline="") as handle:
        evidence = {row["outage_id"]: row for row in csv.DictReader(handle)}

    features = []
    for outage_id, row in attributions.items():
        if not row["geometry"] or outage_id not in evidence:
            continue
        detail = evidence[outage_id]
        started_at = datetime.fromisoformat(detail["outage_trigger_time"]).replace(
            tzinfo=ZoneInfo("Asia/Kolkata")
        ).isoformat()
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(to_geojson(from_wkt(row["geometry"]))),
                "properties": {
                    "outage_id": outage_id,
                    "csp_id": detail["csp_id"],
                    "csp_name": csp_names.get(detail["csp_id"], detail["csp_id"]),
                    "started_at": started_at,
                    "area_km2": float(row["polygon_area_km2"]),
                    "unhealthy_devices": int(row["unhealthy_device_count"]),
                    "located_devices": int(row["located_unhealthy_device_count"]),
                    "root_cause": row["root_cause"],
                    "spatial_extent": row["spatial_extent"],
                    "confidence": row["confidence"],
                    "revision": int(row["revision"]),
                },
            }
        )

    features.sort(key=lambda feature: feature["properties"]["started_at"])
    starts = [feature["properties"]["started_at"] for feature in features]
    payload = {
        "type": "FeatureCollection",
        "metadata": {
            "generated_at": max(row["snapshot_at"] for row in attributions.values()),
            "min_started_at": min(starts),
            "max_started_at": max(starts),
            "timezone": "Asia/Kolkata",
            "time_semantics": "Outages whose trigger time falls inside the selected lookback window",
        },
        "features": features,
    }
    target = ROOT / "map_site/public/outages.geojson"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, separators=(",", ":")))

    first_by_device = {}
    for row in device_rows:
        first_by_device.setdefault(row["device_id"], row)
    located = [
        (device_id, row)
        for device_id, row in sorted(first_by_device.items())
        if row["latitude"] and row["longitude"]
    ]
    device_index = {device_id: index for index, (device_id, _) in enumerate(located)}
    ping_start = next((row.get("ping_start_hour_ist", "") for _, row in located if row.get("ping_start_hour_ist")), "")
    ping_hours = int(next((row.get("ping_hour_count", "0") for _, row in located if row.get("ping_hour_count")), "0"))
    byte_count = (ping_hours + 7) // 8
    devices = [
        [
            round(float(row["latitude"]), 4),
            round(float(row["longitude"]), 4),
            int(row["is_active"].lower() in {"true", "1"}),
            ping_bitmap(row, byte_count) if byte_count else "",
            row.get("customer_name", "").strip() or "Name unavailable",
            row.get("customer_address", "").strip() or "Address unavailable",
            csp_names.get(row["csp_id"], row["csp_id"]),
            row["csp_id"],
        ]
        for _, row in located
    ]
    feature_ids = {feature["properties"]["outage_id"] for feature in features}
    members = {}
    for row in device_rows:
        outage_id = row["outage_id"]
        index = device_index.get(row["device_id"])
        if outage_id in feature_ids and index is not None:
            members.setdefault(outage_id, set()).add(index)
    device_payload = {
        "metadata": {
            "hour_start_ist": (
                datetime.fromisoformat(ping_start).replace(tzinfo=ZoneInfo("Asia/Kolkata")).isoformat()
                if ping_start else ""
            ),
            "hour_count": ping_hours,
            "coordinates": "rounded to 4 decimal places; network identifiers and phone numbers excluded",
            "healthy_semantics": "valid hourly source row with TOTAL_PINGS_RECEIVED > 0",
        },
        "devices": devices,
        "members": {outage_id: sorted(indexes) for outage_id, indexes in members.items()},
    }
    device_target = target.with_name("devices.json.gz")
    serialized = json.dumps(device_payload, separators=(",", ":"))
    assert all(len(device) == 8 for device in devices)
    device_target.write_bytes(gzip.compress(serialized.encode(), compresslevel=9))
    target.with_name("devices.json").unlink(missing_ok=True)
    print(f"wrote {len(features):,} outage polygons to {target}")
    print(f"wrote {len(devices):,} device points to {device_target}")


if __name__ == "__main__":
    main()
