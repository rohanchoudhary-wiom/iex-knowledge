from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from acs_outage_feasibility import connect_snowflake, file_hash, sql_hash


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs" / "full_fleet_ping_outage_h3_2026-08-12_v1"
H3_RESOLUTION = 9
MIN_CELL_DEVICES = 5
MIN_MAPPING_COVERAGE = 0.90

CELL_COLUMNS = (
    "h3_cell_id",
    "h3_resolution",
    "eligible_devices",
    "affected_devices",
    "affected_device_share",
    "outage_events",
    "recovered_events",
    "right_censored_events",
    "recovered_outage_minutes",
    "recovered_duration_p50_minutes",
    "recovered_duration_p90_minutes",
)
PROHIBITED_COLUMNS = {
    "account_id",
    "device_id",
    "nasid",
    "latitude",
    "longitude",
    "device_key",
    "customer_id",
}


def native(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    try:
        return int(value) if value == int(value) else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return str(value)


def frame_sql(start: datetime, end: datetime, observation_end: datetime) -> str:
    values = {
        "scan_start": (start - timedelta(days=1)).isoformat(sep=" "),
        "start": start.isoformat(sep=" "),
        "end": end.isoformat(sep=" "),
        "observation_end": observation_end.isoformat(sep=" "),
    }
    return f"""
WITH bounds AS (
  SELECT
    '{values['scan_start']}'::TIMESTAMP_NTZ AS scan_start_ist,
    '{values['start']}'::TIMESTAMP_NTZ AS month_start_ist,
    '{values['end']}'::TIMESTAMP_NTZ AS month_end_ist,
    '{values['observation_end']}'::TIMESTAMP_NTZ AS observation_end_ist
),
normalized AS (
  SELECT
    UPPER(TRIM(device_id)) AS device_id,
    nas_id,
    hour_start_ist,
    CASE
      WHEN hour_end_ist = DATEADD(hour, 1, hour_start_ist)
        THEN DATEADD(hour, 1, hour_start_ist)
      WHEN DATE_PART(hour, hour_start_ist) = 23
       AND hour_end_ist = DATE_TRUNC(day, hour_start_ist)
        THEN DATEADD(hour, 1, hour_start_ist)
    END AS effective_end_ist,
    inserted_at,
    updated_at,
    total_pings_received,
    total_pings_missed,
    continuous_missed_ping_instances,
    max_pings_missed_in_continuous_instance,
    first_ping_ts_ist,
    last_ping_ts_ist,
    ping_bitmap,
    optical_min,
    optical_avg,
    optical_max,
    HASH(
      nas_id, total_pings_received, total_pings_missed,
      continuous_missed_ping_instances,
      max_pings_missed_in_continuous_instance,
      first_ping_ts_ist, last_ping_ts_ist, ping_bitmap,
      optical_min, optical_avg, optical_max
    ) AS value_hash
  FROM PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX, bounds
  WHERE hour_start_ist >= bounds.scan_start_ist
    AND hour_start_ist < bounds.observation_end_ist
    AND inserted_at IS NOT NULL
    AND NULLIF(TRIM(device_id), '') IS NOT NULL
    AND NOT REGEXP_LIKE(LEFT(UPPER(TRIM(device_id)), 2), '^[0-9]{{2}}$')
),
hour_quality AS (
  SELECT
    device_id,
    hour_start_ist,
    COUNT(DISTINCT IFF(effective_end_ist IS NOT NULL, value_hash, NULL)) AS value_variants,
    COUNT_IF(effective_end_ist IS NULL) AS invalid_end_rows,
    COUNT_IF(
      total_pings_received IS NULL OR total_pings_received < 0
      OR (total_pings_received = 0 AND (first_ping_ts_ist IS NOT NULL OR last_ping_ts_ist IS NOT NULL))
      OR (
        total_pings_received > 0
        AND (
          first_ping_ts_ist IS NULL OR last_ping_ts_ist IS NULL
          OR effective_end_ist IS NULL
          OR first_ping_ts_ist < hour_start_ist
          OR first_ping_ts_ist >= effective_end_ist
          OR last_ping_ts_ist < first_ping_ts_ist
          OR last_ping_ts_ist >= effective_end_ist
        )
      )
    ) AS invalid_endpoint_rows
  FROM normalized
  GROUP BY device_id, hour_start_ist
),
deduplicated_hours AS (
  SELECT n.*
  FROM normalized n
  JOIN hour_quality q USING (device_id, hour_start_ist)
  WHERE q.value_variants = 1
    AND q.invalid_end_rows = 0
    AND q.invalid_endpoint_rows = 0
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY n.device_id, n.hour_start_ist
    ORDER BY n.inserted_at, n.updated_at
  ) = 1
),
timeline_base AS (
  SELECT
    q.device_id,
    q.hour_start_ist,
    IFF(
      q.value_variants <> 1 OR q.invalid_end_rows > 0 OR q.invalid_endpoint_rows > 0,
      1, 0
    ) AS bad_hour,
    h.total_pings_received,
    h.first_ping_ts_ist,
    h.last_ping_ts_ist
  FROM hour_quality q
  LEFT JOIN deduplicated_hours h USING (device_id, hour_start_ist)
),
timeline AS (
  SELECT
    *,
    SUM(bad_hour) OVER (
      PARTITION BY device_id ORDER BY hour_start_ist
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS bad_hours_seen
  FROM timeline_base
),
device_bad_totals AS (
  SELECT device_id, SUM(bad_hour) AS total_bad_hours
  FROM timeline_base
  GROUP BY device_id
),
successes AS (
  SELECT
    t.device_id,
    t.hour_start_ist,
    t.first_ping_ts_ist,
    t.last_ping_ts_ist,
    t.bad_hours_seen,
    b.total_bad_hours
  FROM timeline t
  JOIN device_bad_totals b USING (device_id)
  WHERE t.bad_hour = 0 AND t.total_pings_received > 0
),
sequenced_successes AS (
  SELECT
    *,
    LEAD(first_ping_ts_ist) OVER (PARTITION BY device_id ORDER BY hour_start_ist) AS next_success_ist,
    LEAD(bad_hours_seen) OVER (PARTITION BY device_id ORDER BY hour_start_ist) AS next_bad_hours_seen
  FROM successes
),
candidate_events AS (
  SELECT
    s.device_id,
    DATEADD(minute, 5, s.last_ping_ts_ist) AS event_start_ist,
    s.next_success_ist AS recovery_ist,
    IFF(s.next_success_ist IS NULL, 'right_censored', 'recovered') AS event_status,
    IFF(
      s.next_success_ist IS NULL,
      NULL,
      DATEDIFF(second, DATEADD(minute, 5, s.last_ping_ts_ist), s.next_success_ist) / 60.0
    ) AS recovered_duration_minutes,
    IFF(
      COALESCE(s.next_bad_hours_seen, s.total_bad_hours) = s.bad_hours_seen,
      0, 1
    ) AS crosses_bad_hour
  FROM sequenced_successes s
  CROSS JOIN bounds b
  WHERE DATEADD(minute, 5, s.last_ping_ts_ist) >= b.month_start_ist
    AND DATEADD(minute, 5, s.last_ping_ts_ist) < b.month_end_ist
    AND (
      (s.next_success_ist IS NOT NULL
       AND DATEDIFF(second, s.last_ping_ts_ist, s.next_success_ist) >= 3900)
      OR
      (s.next_success_ist IS NULL
       AND DATEDIFF(second, s.last_ping_ts_ist, b.observation_end_ist) >= 3900)
    )
),
events AS (
  SELECT * FROM candidate_events WHERE crosses_bad_hour = 0
),
july_source_devices AS (
  SELECT DISTINCT h.device_id
  FROM deduplicated_hours h CROSS JOIN bounds b
  WHERE h.hour_start_ist >= b.month_start_ist
    AND h.hour_start_ist < b.month_end_ist
),
source_devices AS (
  SELECT DISTINCT device_id FROM deduplicated_hours
),
customer_rows AS (
  SELECT account_id, nasid::VARCHAR AS nasid, latitude, longitude, active_state,
         location_start_time, plan_expiry_time
  FROM PROD_DB.DBT.ACTIVE_BASE
  WHERE source = 'CUSTOMER_V2' AND account_id IS NOT NULL
),
account_profile AS (
  SELECT
    account_id,
    COUNT_IF(nasid IS NULL) AS null_nasids,
    COUNT(DISTINCT nasid) AS nasid_values,
    COUNT_IF(latitude IS NULL OR longitude IS NULL) AS null_coordinates,
    COUNT(DISTINCT latitude) AS latitude_values,
    COUNT(DISTINCT longitude) AS longitude_values,
    COUNT(DISTINCT COALESCE(UPPER(TRIM(active_state)), '<NULL>')) AS active_state_values,
    MIN(UPPER(TRIM(active_state))) AS canonical_active_state,
    MIN(nasid)::VARCHAR AS nasid,
    MIN(latitude)::FLOAT AS latitude,
    MIN(longitude)::FLOAT AS longitude,
    MIN(location_start_time) AS location_start_time,
    MAX(plan_expiry_time) AS plan_expiry_time
  FROM customer_rows
  GROUP BY account_id
),
eligible_accounts AS (
  SELECT account_id, nasid, latitude, longitude, location_start_time, plan_expiry_time
  FROM account_profile
  WHERE active_state_values = 1
    AND canonical_active_state = 'ACTIVE'
    AND null_nasids = 0 AND nasid_values = 1
    AND null_coordinates = 0 AND latitude_values = 1 AND longitude_values = 1
    AND latitude BETWEEN 6 AND 38 AND longitude BETWEEN 68 AND 98
),
customer_nasid_profile AS (
  SELECT nasid, COUNT(*) AS account_degree
  FROM eligible_accounts
  GROUP BY nasid
),
clean_customers AS (
  SELECT e.*
  FROM eligible_accounts e
  JOIN customer_nasid_profile p USING (nasid)
  WHERE p.account_degree = 1
),
inventory_rows AS (
  SELECT
    UPPER(TRIM(device_id)) AS device_id,
    NULLIF(TRIM(long_nas_id::VARCHAR), '') AS long_nasid,
    NULLIF(TRIM(nasid::VARCHAR), '') AS short_nasid
  FROM PROD_DB.PUBLIC.T_DEVICE
  WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE
    AND NULLIF(TRIM(device_id), '') IS NOT NULL
),
inventory_candidates AS (
  SELECT
    device_id,
    COALESCE(long_nasid, short_nasid) AS bridge_nasid,
    IFF(
      long_nasid IS NOT NULL AND short_nasid IS NOT NULL AND long_nasid <> short_nasid,
      1, 0
    ) AS row_conflict
  FROM inventory_rows
  WHERE long_nasid IS NOT NULL OR short_nasid IS NOT NULL
),
inventory_device_profile AS (
  SELECT
    device_id,
    COUNT_IF(row_conflict = 1) AS conflict_rows,
    COUNT(DISTINCT IFF(row_conflict = 0, bridge_nasid, NULL)) AS nasid_degree,
    MIN(IFF(row_conflict = 0, bridge_nasid, NULL)) AS bridge_nasid
  FROM inventory_candidates
  GROUP BY device_id
),
device_unique_bridge AS (
  SELECT device_id, bridge_nasid
  FROM inventory_device_profile
  WHERE conflict_rows = 0 AND nasid_degree = 1
),
inventory_nasid_profile AS (
  SELECT bridge_nasid, COUNT(*) AS device_degree
  FROM device_unique_bridge
  GROUP BY bridge_nasid
),
mapping AS (
  SELECT
    s.device_id,
    c.account_id,
    c.latitude,
    c.longitude,
    c.location_start_time,
    c.plan_expiry_time,
    CASE
      WHEN p.device_id IS NULL THEN 'no_inventory'
      WHEN p.conflict_rows > 0 OR p.nasid_degree <> 1 THEN 'ambiguous_device'
      WHEN n.device_degree <> 1 THEN 'ambiguous_nasid'
      WHEN c.nasid IS NULL THEN 'not_eligible_customer'
      ELSE 'mapped'
    END AS mapping_status
  FROM source_devices s
  LEFT JOIN inventory_device_profile p ON p.device_id = s.device_id
  LEFT JOIN device_unique_bridge d ON d.device_id = s.device_id
  LEFT JOIN inventory_nasid_profile n ON n.bridge_nasid = d.bridge_nasid
  LEFT JOIN clean_customers c ON c.nasid = d.bridge_nasid
),
mapped AS (
  SELECT
    *,
    H3_LATLNG_TO_CELL_STRING(latitude, longitude, {H3_RESOLUTION}) AS h3_cell_id
  FROM mapping
  WHERE mapping_status = 'mapped'
),
july_mapping AS (
  SELECT m.*
  FROM mapped m
  JOIN july_source_devices j USING (device_id)
),
july_service_devices AS (
  SELECT m.*
  FROM july_mapping m CROSS JOIN bounds b
  WHERE m.location_start_time IS NOT NULL
    AND m.plan_expiry_time IS NOT NULL
    AND m.location_start_time < b.month_end_ist
    AND m.plan_expiry_time >= b.month_start_ist
),
eligible_events AS (
  SELECT e.*, d.h3_cell_id
  FROM events e
  JOIN july_service_devices d USING (device_id)
  WHERE d.location_start_time <= e.event_start_ist
    AND d.plan_expiry_time >= e.event_start_ist
),
cell_denominators AS (
  SELECT h3_cell_id, COUNT(*) AS eligible_devices
  FROM july_service_devices
  GROUP BY h3_cell_id
),
cell_events AS (
  SELECT
    h3_cell_id,
    COUNT(DISTINCT device_id) AS affected_devices,
    COUNT(*) AS outage_events,
    COUNT_IF(event_status = 'recovered') AS recovered_events,
    COUNT_IF(event_status = 'right_censored') AS right_censored_events,
    SUM(IFF(event_status = 'recovered', recovered_duration_minutes, 0)) AS recovered_outage_minutes,
    MEDIAN(IFF(event_status = 'recovered', recovered_duration_minutes, NULL)) AS recovered_duration_p50_minutes,
    PERCENTILE_CONT(0.9) WITHIN GROUP (
      ORDER BY IFF(event_status = 'recovered', recovered_duration_minutes, NULL)
    ) AS recovered_duration_p90_minutes
  FROM eligible_events
  GROUP BY h3_cell_id
),
cell_summary AS (
  SELECT
    d.h3_cell_id,
    {H3_RESOLUTION} AS h3_resolution,
    d.eligible_devices,
    COALESCE(e.affected_devices, 0) AS affected_devices,
    COALESCE(e.affected_devices, 0) / d.eligible_devices::FLOAT AS affected_device_share,
    COALESCE(e.outage_events, 0) AS outage_events,
    COALESCE(e.recovered_events, 0) AS recovered_events,
    COALESCE(e.right_censored_events, 0) AS right_censored_events,
    COALESCE(e.recovered_outage_minutes, 0) AS recovered_outage_minutes,
    e.recovered_duration_p50_minutes,
    e.recovered_duration_p90_minutes
  FROM cell_denominators d
  LEFT JOIN cell_events e USING (h3_cell_id)
),
mapping_audit AS (
  SELECT
    COUNT(*) AS july_devices,
    COUNT_IF(m.mapping_status = 'mapped') AS mapped_devices,
    COUNT_IF(m.mapping_status = 'no_inventory') AS no_inventory_devices,
    COUNT_IF(m.mapping_status = 'ambiguous_device') AS ambiguous_device_devices,
    COUNT_IF(m.mapping_status = 'ambiguous_nasid') AS ambiguous_nasid_devices,
    COUNT_IF(m.mapping_status = 'not_eligible_customer') AS not_eligible_customer_devices
  FROM july_source_devices j
  JOIN mapping m USING (device_id)
),
h3_counts AS (
  SELECT h3_cell_id, COUNT(*) AS devices
  FROM july_mapping
  GROUP BY h3_cell_id
),
h3_density AS (
  SELECT
    COUNT(*) AS occupied_cells,
    MEDIAN(devices) AS devices_per_cell_p50,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY devices) AS devices_per_cell_p90,
    MAX(devices) AS devices_per_cell_max,
    SUM(IFF(devices >= 3, devices, 0)) AS devices_in_cells_ge_3,
    SUM(IFF(devices >= 5, devices, 0)) AS devices_in_cells_ge_5
  FROM h3_counts
),
event_audit AS (
  SELECT
    COUNT(*) AS outage_events,
    COUNT(DISTINCT device_id) AS affected_devices,
    COUNT_IF(event_status = 'recovered') AS recovered_events,
    COUNT_IF(event_status = 'right_censored') AS right_censored_events,
    SUM(IFF(event_status = 'recovered', recovered_duration_minutes, 0)) AS recovered_outage_minutes,
    MEDIAN(IFF(event_status = 'recovered', recovered_duration_minutes, NULL)) AS recovered_duration_p50_minutes,
    PERCENTILE_CONT(0.9) WITHIN GROUP (
      ORDER BY IFF(event_status = 'recovered', recovered_duration_minutes, NULL)
    ) AS recovered_duration_p90_minutes
  FROM eligible_events
),
privacy_audit AS (
  SELECT
    COUNT_IF(eligible_devices < {MIN_CELL_DEVICES}) AS suppressed_cells,
    SUM(IFF(eligible_devices < {MIN_CELL_DEVICES}, eligible_devices, 0)) AS suppressed_devices,
    COUNT_IF(eligible_devices >= {MIN_CELL_DEVICES}) AS reportable_cells,
    SUM(IFF(eligible_devices >= {MIN_CELL_DEVICES}, eligible_devices, 0)) AS reportable_devices
  FROM cell_summary
),
global_audit AS (
  SELECT
    (SELECT COUNT(*) FROM normalized n CROSS JOIN bounds b
      WHERE n.hour_start_ist >= b.month_start_ist AND n.hour_start_ist < b.month_end_ist)
      AS audit_july_source_rows,
    (SELECT COUNT(*) FROM deduplicated_hours h CROSS JOIN bounds b
      WHERE h.hour_start_ist >= b.month_start_ist AND h.hour_start_ist < b.month_end_ist)
      AS audit_july_retained_rows,
    (SELECT COUNT(*) FROM hour_quality q CROSS JOIN bounds b
      WHERE q.hour_start_ist >= b.month_start_ist AND q.hour_start_ist < b.month_end_ist
        AND (q.value_variants <> 1 OR q.invalid_end_rows > 0 OR q.invalid_endpoint_rows > 0))
      AS audit_july_quarantined_device_hours,
    m.july_devices AS audit_july_devices,
    m.mapped_devices AS audit_mapped_devices,
    m.mapped_devices / NULLIF(m.july_devices, 0)::FLOAT AS audit_mapping_coverage,
    m.no_inventory_devices AS audit_no_inventory_devices,
    m.ambiguous_device_devices AS audit_ambiguous_device_devices,
    m.ambiguous_nasid_devices AS audit_ambiguous_nasid_devices,
    m.not_eligible_customer_devices AS audit_not_eligible_customer_devices,
    (SELECT COUNT(*) FROM clean_customers) AS audit_clean_customer_accounts,
    (SELECT COUNT(*) FROM july_service_devices) AS audit_july_service_devices,
    h.occupied_cells AS audit_occupied_h3_cells,
    h.devices_per_cell_p50 AS audit_devices_per_cell_p50,
    h.devices_per_cell_p90 AS audit_devices_per_cell_p90,
    h.devices_per_cell_max AS audit_devices_per_cell_max,
    h.devices_in_cells_ge_3 AS audit_devices_in_cells_ge_3,
    h.devices_in_cells_ge_5 AS audit_devices_in_cells_ge_5,
    h.devices_in_cells_ge_3 / NULLIF(m.mapped_devices, 0)::FLOAT AS audit_device_share_cells_ge_3,
    h.devices_in_cells_ge_5 / NULLIF(m.mapped_devices, 0)::FLOAT AS audit_device_share_cells_ge_5,
    (SELECT COUNT_IF(NOT H3_IS_VALID_CELL(h3_cell_id) OR H3_GET_RESOLUTION(h3_cell_id) <> {H3_RESOLUTION})
       FROM july_mapping) AS audit_invalid_h3_devices,
    (SELECT COUNT_IF(crosses_bad_hour = 1) FROM candidate_events) AS audit_quarantined_candidate_events,
    e.outage_events AS audit_outage_events,
    e.affected_devices AS audit_affected_devices,
    e.recovered_events AS audit_recovered_events,
    e.right_censored_events AS audit_right_censored_events,
    e.recovered_outage_minutes AS audit_recovered_outage_minutes,
    e.recovered_duration_p50_minutes AS audit_recovered_duration_p50_minutes,
    e.recovered_duration_p90_minutes AS audit_recovered_duration_p90_minutes,
    p.suppressed_cells AS audit_suppressed_cells,
    p.suppressed_devices AS audit_suppressed_devices,
    p.reportable_cells AS audit_reportable_cells,
    p.reportable_devices AS audit_reportable_devices,
    p.reportable_devices / NULLIF((SELECT COUNT(*) FROM july_service_devices), 0)::FLOAT
      AS audit_reportable_device_share
  FROM mapping_audit m CROSS JOIN h3_density h CROSS JOIN event_audit e CROSS JOIN privacy_audit p
)
SELECT c.*, a.*
FROM cell_summary c CROSS JOIN global_audit a
WHERE c.eligible_devices >= {MIN_CELL_DEVICES}
ORDER BY c.h3_cell_id
"""


def self_check() -> None:
    previous = datetime(2026, 7, 1, 0, 0)
    assert (previous + timedelta(minutes=64, seconds=59) - previous).total_seconds() < 3900
    recovery = previous + timedelta(minutes=65)
    onset = previous + timedelta(minutes=5)
    assert (recovery - previous).total_seconds() == 3900
    assert (recovery - onset).total_seconds() == 3600
    assert not PROHIBITED_COLUMNS.intersection(CELL_COLUMNS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map full-population ping outages to H3 resolution 9")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--observation-end", default="2026-08-11")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    observation_end = datetime.fromisoformat(args.observation_end)
    if not start < end < observation_end:
        raise ValueError("Require start < end < observation-end")
    if any(value.time() != datetime.min.time() for value in (start, end, observation_end)):
        raise ValueError("All boundaries must be midnight IST")

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    sql = frame_sql(start, end, observation_end)

    connection = connect_snowflake()
    cursor = connection.cursor()
    tmp_csv = output / ".h3_cell_summary.csv.tmp"
    audit_values: dict[str, object] | None = None
    rows_written = 0
    try:
        cursor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1200")
        cursor.execute("ALTER SESSION SET QUERY_TAG = 'full_fleet_ping_outage_h3_v1_read_only'")
        cursor.execute(sql)
        query_id = str(cursor.sfqid)
        columns = [str(item[0]).lower() for item in cursor.description]
        if tuple(columns[: len(CELL_COLUMNS)]) != CELL_COLUMNS:
            raise RuntimeError(f"Unexpected cell schema: {columns[:len(CELL_COLUMNS)]}")
        if PROHIBITED_COLUMNS.intersection(columns):
            raise RuntimeError("Aggregate query exposed a prohibited identifier column")
        audit_columns = columns[len(CELL_COLUMNS) :]
        if not audit_columns or any(not name.startswith("audit_") for name in audit_columns):
            raise RuntimeError("Aggregate query did not return the expected audit fields")

        with tmp_csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CELL_COLUMNS)
            writer.writeheader()
            while batch := cursor.fetchmany(10_000):
                for values in batch:
                    row = dict(zip(columns, values))
                    current_audit = {
                        name.removeprefix("audit_"): native(row[name]) for name in audit_columns
                    }
                    if audit_values is None:
                        audit_values = current_audit
                    elif current_audit != audit_values:
                        raise RuntimeError("Audit fields changed across aggregate rows")
                    if int(row["h3_resolution"]) != H3_RESOLUTION:
                        raise RuntimeError("Non-resolution-9 H3 row returned")
                    if int(row["eligible_devices"]) < MIN_CELL_DEVICES:
                        raise RuntimeError("Sparse H3 row escaped suppression")
                    writer.writerow({name: native(row[name]) for name in CELL_COLUMNS})
                    rows_written += 1
    finally:
        cursor.close()
        connection.close()

    if audit_values is None or rows_written == 0:
        tmp_csv.unlink(missing_ok=True)
        raise RuntimeError("Snowflake returned no reportable H3 cells")
    mapping_coverage = float(audit_values["mapping_coverage"])
    if mapping_coverage < MIN_MAPPING_COVERAGE or int(audit_values["invalid_h3_devices"]) != 0:
        tmp_csv.unlink(missing_ok=True)
        raise RuntimeError("Mapping/H3 gate failed; detailed map was not exported")
    os.replace(tmp_csv, output / "h3_cell_summary.csv")

    audit: dict[str, object] = {
        "status": "FULL_POPULATION_H3_MAP_COMPLETE",
        "interpretation": "ping-defined telemetry outages; not confirmed customer-service outages",
        "window": {
            "start_ist": start,
            "end_ist_exclusive": end,
            "observation_end_ist": observation_end,
        },
        "outage_rule": {
            "ping_cadence_minutes": 5,
            "consecutive_missed_slots": 12,
            "minimum_success_gap_seconds": 3900,
            "event_start": "previous successful ping + 5 minutes",
            "event_end": "first later successful ping; otherwise right-censored",
        },
        "sampling": "none",
        "h3": {
            "resolution": H3_RESOLUTION,
            "minimum_exported_cell_devices": MIN_CELL_DEVICES,
        },
        "metrics": audit_values,
        "query_id": query_id,
        "sql_sha256": sql_hash(sql),
        "output_rows": {"h3_cell_summary.csv": rows_written},
        "privacy_check": "passed",
        "source_writes_attempted": False,
        "reproduction_command": (
            "python acs_outage_prediction/full_fleet_ping_outage_h3.py "
            f"--start {start:%Y-%m-%d} --end {end:%Y-%m-%d} "
            f"--observation-end {observation_end:%Y-%m-%d} --output-dir <new-empty-dir>"
        ),
    }
    (output / "audit.json").write_text(json.dumps(audit, indent=2, default=native) + "\n")
    report = f"""# Full-population H3 ping-outage map

- Population: {int(audit_values['july_devices']):,} unsampled July ping devices; {int(audit_values['mapped_devices']):,} mapped ({mapping_coverage:.2%}).
- Geography: H3 resolution {H3_RESOLUTION}; {int(audit_values['occupied_h3_cells']):,} occupied cells; median {float(audit_values['devices_per_cell_p50']):g} devices per occupied cell.
- Strict rule: 12 consecutive missed five-minute opportunities (at least 65 minutes between adjacent successes).
- Result: {int(audit_values['outage_events']):,} mapped, service-valid July-started telemetry outages across {int(audit_values['affected_devices']):,} devices; {int(audit_values['right_censored_events']):,} remain right-censored.
- Privacy: {int(audit_values['suppressed_cells']):,} cells below five eligible devices are omitted; exported cells cover {float(audit_values['reportable_device_share']):.2%} of eligible mapped devices.

These are ping-defined telemetry outages, not confirmed customer-service outages. Spatial incident clustering and predictor fitting are intentionally deferred.
"""
    (output / "report.md").write_text(report)
    audit["artifact_sha256"] = {
        path.name: file_hash(path)
        for path in sorted(output.iterdir())
        if path.name != "audit.json"
    }
    (output / "audit.json").write_text(json.dumps(audit, indent=2, default=native) + "\n")
    print(f"FULL_POPULATION_H3_MAP_COMPLETE: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
