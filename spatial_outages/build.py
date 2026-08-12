from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import h3


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "acs_outage_prediction"))
from acs_outage_feasibility import connect_snowflake  # noqa: E402
from full_fleet_ping_outage_h3 import frame_sql  # noqa: E402


EVENT_COLUMNS = (
    "device_id",
    "h3_id",
    "eligible_devices",
    "ping_lack_minutes",
    "outage_start_ist",
    "outage_end_ist",
    "status",
)
HEX_COLUMNS = (
    "h3_id",
    "eligible_devices",
    "qualifying_events",
    "events_1_to_3_hours",
    "events_over_3_hours",
    "worst_affected_devices",
    "worst_affected_share",
    "longest_event_minutes",
    "latest_event_start_ist",
    "latest_event_end_ist",
    "severity",
)
SQL_TAIL = "SELECT c.*, a.*"


def outage_sql(start: datetime, end: datetime, observation_end: datetime) -> str:
    # ponytail: reuse the validated fleet CTE; extract a shared query only if it diverges.
    sql = frame_sql(start, end, observation_end)
    if sql.count(SQL_TAIL) != 1:
        raise RuntimeError("Validated fleet query has changed; cannot safely select event rows")
    prefix = sql.rsplit(SQL_TAIL, 1)[0]
    return prefix + """SELECT
  e.device_id,
  e.h3_cell_id AS h3_id,
  d.eligible_devices,
  ROUND(
    DATEDIFF(
      second,
      e.event_start_ist,
      LEAST(COALESCE(e.recovery_ist, b.observation_end_ist), b.month_end_ist)
    ) / 60.0,
    2
  ) AS ping_lack_minutes,
  e.event_start_ist AS outage_start_ist,
  LEAST(COALESCE(e.recovery_ist, b.observation_end_ist), b.month_end_ist) AS outage_end_ist,
  IFF(e.recovery_ist <= b.month_end_ist, 'recovered', 'month_end_censored') AS status
FROM eligible_events e
JOIN cell_denominators d USING (h3_cell_id)
CROSS JOIN bounds b
WHERE DATEDIFF(
  second,
  e.event_start_ist,
  LEAST(COALESCE(e.recovery_ist, b.observation_end_ist), b.month_end_ist)
) > 3600
  AND d.eligible_devices >= 5
"""


def concurrent_events(
    eligible_devices: int, outages: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime, int]]:
    threshold = math.ceil(eligible_devices * 0.7)
    changes: dict[datetime, int] = defaultdict(int)
    for start, end in outages:
        qualifying_start = start + timedelta(hours=1)
        if end > qualifying_start:
            changes[qualifying_start] += 1
            changes[end] -= 1

    count = 0
    event_start = None
    peak = 0
    events = []
    for time in sorted(changes):
        count += changes[time]
        if count > eligible_devices:
            raise RuntimeError("Concurrent device count exceeds the eligible denominator")
        if event_start is None and count >= threshold:
            event_start, peak = time, count
        elif event_start is not None and count < threshold:
            if time - event_start > timedelta(hours=1):
                events.append((event_start, time, peak))
            event_start = None
        elif event_start is not None:
            peak = max(peak, count)
    return events


def roll_up(cells: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for cell, values in cells.items():
        eligible = int(values["eligible_devices"])
        events = concurrent_events(eligible, values["outages"])
        if not events:
            continue
        latest = max(events, key=lambda event: event[0])
        longest_minutes = max((end - start).total_seconds() / 60 for start, end, _ in events)
        worst = max(peak for _, _, peak in events)
        over_three = sum(end - start > timedelta(hours=3) for start, end, _ in events)
        rows.append(
            {
                "h3_id": cell,
                "eligible_devices": eligible,
                "qualifying_events": len(events),
                "events_1_to_3_hours": len(events) - over_three,
                "events_over_3_hours": over_three,
                "worst_affected_devices": worst,
                "worst_affected_share": round(worst / eligible, 4),
                "longest_event_minutes": round(longest_minutes, 2),
                "latest_event_start_ist": latest[0].isoformat(sep=" "),
                "latest_event_end_ist": latest[1].isoformat(sep=" "),
                "severity": "over_3_hours" if over_three else "1_to_3_hours",
            }
        )
    return sorted(rows, key=lambda row: str(row["h3_id"]))


def write_geojson(rows: list[dict[str, object]], path: Path) -> None:
    features = []
    for values in rows:
        cell = str(values["h3_id"])
        boundary = h3.cell_to_boundary(cell)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[lng, lat] for lat, lng in boundary + (boundary[0],)]],
                },
                "properties": values,
            }
        )
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def self_check() -> None:
    sql = outage_sql(datetime(2026, 7, 1), datetime(2026, 8, 1), datetime(2026, 8, 11))
    assert ") > 3600" in sql
    assert all(column in sql.lower() for column in EVENT_COLUMNS)
    boundary = h3.cell_to_boundary("8928308280fffff")
    assert len(boundary) == 6
    start = datetime(2026, 7, 1)
    short = concurrent_events(10, [(start, start + timedelta(minutes=150))] * 7)
    long = concurrent_events(10, [(start, start + timedelta(hours=5))] * 8)
    assert short == [(start + timedelta(hours=1), start + timedelta(minutes=150), 7)]
    assert long == [(start + timedelta(hours=1), start + timedelta(hours=5), 8)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export >1-hour ACS ping outages and H3 map data")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--observation-end", default="2026-08-11")
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0

    start, end, observation_end = map(
        datetime.fromisoformat, (args.start, args.end, args.observation_end)
    )
    if not start < end < observation_end:
        raise ValueError("Require start < end < observation-end")

    data_dir = ROOT / "data"
    public_dir = ROOT / "public"
    data_dir.mkdir(exist_ok=True)
    public_dir.mkdir(exist_ok=True)
    event_path = data_dir / "outages.csv"
    tmp_event_path = data_dir / ".outages.csv.tmp"
    hex_path = data_dir / "hex_outages.csv"
    tmp_hex_path = data_dir / ".hex_outages.csv.tmp"
    cells: dict[str, dict[str, object]] = defaultdict(
        lambda: {"eligible_devices": 0, "outages": []}
    )

    connection = connect_snowflake()
    cursor = connection.cursor()
    rows = 0
    try:
        cursor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1200")
        cursor.execute("ALTER SESSION SET QUERY_TAG = 'spatial_outages_read_only'")
        cursor.execute(outage_sql(start, end, observation_end))
        columns = tuple(str(item[0]).lower() for item in cursor.description)
        if columns != EVENT_COLUMNS:
            raise RuntimeError(f"Unexpected output schema: {columns}")
        with tmp_event_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EVENT_COLUMNS)
            while batch := cursor.fetchmany(10_000):
                for row in batch:
                    writer.writerow(row)
                    _, cell, eligible, _, start_ist, end_ist, _ = row
                    values = cells[str(cell)]
                    if values["eligible_devices"] not in (0, int(eligible)):
                        raise RuntimeError(f"Eligible denominator changed for {cell}")
                    values["eligible_devices"] = int(eligible)
                    values["outages"].append((start_ist, end_ist))
                    rows += 1
    finally:
        cursor.close()
        connection.close()

    if not rows:
        tmp_event_path.unlink(missing_ok=True)
        raise RuntimeError("No outages over one hour were returned")
    rollups = roll_up(cells)
    if not rollups:
        tmp_event_path.unlink(missing_ok=True)
        raise RuntimeError("No simultaneous hex outages met the 70% and duration thresholds")
    with tmp_hex_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rollups)
    os.replace(tmp_event_path, event_path)
    os.replace(tmp_hex_path, hex_path)
    write_geojson(rollups, public_dir / "outages.geojson")
    print(f"wrote {rows:,} device events to {event_path}")
    print(f"wrote {len(rollups):,} qualifying H3 rollups to {hex_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
