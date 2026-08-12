from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import shlex
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
BOOKING_TRUTH = WORKSPACE.parent / "booking_truth"
TIMEZONE = "Asia/Kolkata"
DAYS = 7
HOURS = DAYS * 24
MIN_MAPPING_COVERAGE = 0.90
MIN_REPORTABLE_ELIGIBLE_SHARE = 0.80
MIN_HALF_AFFECTED_CUSTOMER_HOURS = 100
MIN_COMMON_CELLS = 20
MIN_CHRONOLOGICAL_SPEARMAN = 0.50
MIN_EARLY_TO_LATE_LIFT = 1.50
MAX_SHIFT_RATE_CHANGE = 0.10
MAX_SHIFT_CONCENTRATION_CHANGE = 0.25
MAX_INCIDENT_DURATION_MINUTES = HOURS * 60

ACTIVE_BASE = "PROD_DB.DBT.ACTIVE_BASE"
T_DEVICE = "PROD_DB.PUBLIC.T_DEVICE"
INCIDENTS = "PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENTS"
IMPACTED = "PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENT_IMPACTED_DEVICE"

PROHIBITED_ARTIFACT_KEYS = {
    "account_id", "nasid", "device_id", "incident_id", "latitude", "longitude",
    "name", "mobile", "ip", "ip_address", "ssid", "payload", "params_json",
}
PROHIBITED_REPORT_TERMS = {
    "account_id", "device_id", "incident_id", "ip_address", "params_json", "ssid",
}

CSV_COLUMNS = (
    "hour_start_ist",
    "cell_id",
    "network_footprint_customers",
    "eligible_customers",
    "affected_customers",
    "affected_customer_rate",
    "distinct_incidents",
    "duration_minutes_mean",
    "duration_minutes_p50",
    "duration_minutes_max",
    "neighbor_reportable_cells",
    "neighbor_network_footprint_customers",
    "neighbor_eligible_customers",
    "neighbor_affected_customers",
    "neighbor_affected_customer_rate",
    "neighbor_distinct_incidents",
    "neighbor_duration_minutes_mean",
    "neighbor_duration_minutes_p50",
    "neighbor_duration_minutes_max",
)

QUERY_COLUMNS = {
    "analysis_variant",
    "grid_variant",
    "hour_start_ist",
    "cell_y",
    "cell_x",
    *CSV_COLUMNS[2:],
    "suppressed_cell_hours",
    "all_eligible_customer_hours",
    "reportable_eligible_customer_hours",
    "all_affected_customer_hours",
    "reportable_affected_customer_hours",
    "reportable_hours",
}


COMMON_SQL = f"""
WITH bounds AS (
  SELECT %s::TIMESTAMP_NTZ AS window_start_ist, %s::TIMESTAMP_NTZ AS window_end_ist
),
source_counts AS (
  SELECT COUNT(*) AS active_base_rows,
         COUNT_IF(source = 'CUSTOMER_V2') AS customer_v2_rows,
         COUNT_IF(source = 'CUSTOMER_V2' AND account_id IS NULL) AS missing_account_rows
  FROM {ACTIVE_BASE}
),
customer_rows AS (
  SELECT account_id, nasid, latitude, longitude, active_state,
         location_start_time, plan_expiry_time
  FROM {ACTIVE_BASE}
  WHERE source = 'CUSTOMER_V2' AND account_id IS NOT NULL
),
account_profile AS (
  SELECT account_id,
         COUNT(*) AS source_rows,
         COUNT_IF(nasid IS NULL) AS null_nasids,
         COUNT(DISTINCT nasid) AS nasid_values,
         COUNT_IF(latitude IS NULL OR longitude IS NULL) AS null_coordinates,
         COUNT(DISTINCT latitude) AS latitude_values,
         COUNT(DISTINCT longitude) AS longitude_values,
         COUNT(DISTINCT COALESCE(UPPER(TRIM(active_state)), '<NULL>')) AS active_state_values,
         MIN(UPPER(TRIM(active_state))) AS canonical_active_state,
         MIN(nasid)::VARCHAR AS nasid,
         MIN(latitude) AS latitude,
         MIN(longitude) AS longitude,
         MIN(location_start_time) AS location_start_time,
         MAX(plan_expiry_time) AS plan_expiry_time
  FROM customer_rows
  GROUP BY account_id
),
classified_accounts AS (
  SELECT *,
    CASE
      WHEN active_state_values <> 1 OR canonical_active_state IS NULL
        OR canonical_active_state <> 'ACTIVE'
        THEN 'inactive_or_state_conflict'
      WHEN null_nasids > 0 OR nasid_values <> 1
        THEN 'nasid_missing_or_conflict'
      WHEN null_coordinates > 0 OR latitude_values <> 1 OR longitude_values <> 1
        THEN 'coordinate_missing_or_conflict'
      WHEN latitude NOT BETWEEN 6 AND 38 OR longitude NOT BETWEEN 68 AND 98
        THEN 'coordinate_outside_india'
      ELSE 'eligible'
    END AS cohort_status
  FROM account_profile
),
eligible_accounts AS (
  SELECT account_id, nasid, latitude, longitude, location_start_time, plan_expiry_time
  FROM classified_accounts
  WHERE cohort_status = 'eligible'
),
customer_nasid_profile AS (
  SELECT nasid, COUNT(*) AS account_degree, MIN(account_id) AS account_id
  FROM eligible_accounts
  GROUP BY nasid
),
clean_customers AS (
  SELECT e.*
  FROM eligible_accounts e
  JOIN customer_nasid_profile p USING (nasid)
  WHERE p.account_degree = 1
),
valid_incidents AS (
  SELECT i.id AS incident_id,
         CONVERT_TIMEZONE('UTC', '{TIMEZONE}', i.first_fail_timestamp) AS start_ist,
         i.duration_minutes,
         i.status,
         i.is_closed,
         i.closed_at,
         DATEADD(minute, GREATEST(i.duration_minutes, 1),
                 CONVERT_TIMEZONE('UTC', '{TIMEZONE}', i.first_fail_timestamp)) AS end_ist
  FROM {INCIDENTS} i
  CROSS JOIN bounds b
  WHERE COALESCE(i._fivetran_deleted, FALSE) = FALSE
    AND i.id IS NOT NULL
    AND i.first_fail_timestamp IS NOT NULL
    AND i.duration_minutes IS NOT NULL
    AND i.duration_minutes >= 0
    AND i.first_fail_timestamp < CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_end_ist)
    AND DATEADD(minute, GREATEST(i.duration_minutes, 1), i.first_fail_timestamp)
        > CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_start_ist)
),
timestamp_sanity AS (
  SELECT COUNT(*) AS compared_incidents,
         COUNT_IF(created_at < first_fail_timestamp) AS reversed_clock_incidents,
         PERCENTILE_CONT(0.01) WITHIN GROUP (
           ORDER BY DATEDIFF(second, first_fail_timestamp, created_at)
         ) AS creation_lag_seconds_p01,
         MEDIAN(DATEDIFF(second, first_fail_timestamp, created_at)) AS creation_lag_seconds_p50,
         PERCENTILE_CONT(0.99) WITHIN GROUP (
           ORDER BY DATEDIFF(second, first_fail_timestamp, created_at)
         ) AS creation_lag_seconds_p99
  FROM {INCIDENTS} i
  CROSS JOIN bounds b
  WHERE COALESCE(i._fivetran_deleted, FALSE) = FALSE
    AND i.first_fail_timestamp IS NOT NULL AND i.created_at IS NOT NULL
    AND i.first_fail_timestamp >= CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_start_ist)
    AND i.first_fail_timestamp < CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_end_ist)
),
invalid_incidents AS (
  SELECT COUNT(*) AS invalid_duration_incidents
  FROM {INCIDENTS} i
  CROSS JOIN bounds b
  WHERE COALESCE(i._fivetran_deleted, FALSE) = FALSE
    AND i.first_fail_timestamp >= CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_start_ist)
    AND i.first_fail_timestamp < CONVERT_TIMEZONE('{TIMEZONE}', 'UTC', b.window_end_ist)
    AND (i.duration_minutes IS NULL OR i.duration_minutes < 0)
),
impacted_pairs AS (
  SELECT DISTINCT d.incident_id, NULLIF(TRIM(d.device_id), '') AS device_id
  FROM {IMPACTED} d
  WHERE COALESCE(d._fivetran_deleted, FALSE) = FALSE
    AND d.incident_id IS NOT NULL AND NULLIF(TRIM(d.device_id), '') IS NOT NULL
),
outage_pairs AS (
  SELECT p.device_id, i.incident_id, i.start_ist, i.end_ist, i.duration_minutes
  FROM valid_incidents i
  JOIN impacted_pairs p ON p.incident_id = i.incident_id
),
outage_devices AS (
  SELECT DISTINCT device_id FROM outage_pairs
),
inventory_rows AS (
  SELECT NULLIF(TRIM(device_id), '') AS device_id,
         NULLIF(TRIM(long_nas_id::VARCHAR), '') AS long_nasid,
         NULLIF(TRIM(nasid::VARCHAR), '') AS short_nasid
  FROM {T_DEVICE}
  WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE
    AND NULLIF(TRIM(device_id), '') IS NOT NULL
),
inventory_candidates AS (
  SELECT device_id,
         COALESCE(long_nasid, short_nasid) AS bridge_nasid,
         IFF(long_nasid IS NOT NULL AND short_nasid IS NOT NULL AND long_nasid <> short_nasid, 1, 0)
           AS row_conflict
  FROM inventory_rows
  WHERE long_nasid IS NOT NULL OR short_nasid IS NOT NULL
),
inventory_device_profile AS (
  SELECT device_id,
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
mapping_classification AS (
  SELECT o.device_id, d.bridge_nasid, c.account_id,
    CASE
      WHEN p.device_id IS NULL THEN 'no_inventory'
      WHEN p.conflict_rows > 0 OR p.nasid_degree <> 1 THEN 'ambiguous_device'
      WHEN n.device_degree <> 1 THEN 'ambiguous_nasid'
      WHEN c.bridge_nasid IS NULL THEN 'not_eligible_customer'
      WHEN c.account_degree <> 1 THEN 'ambiguous_customer_nasid'
      ELSE 'mapped'
    END AS mapping_status
  FROM outage_devices o
  LEFT JOIN inventory_device_profile p ON p.device_id = o.device_id
  LEFT JOIN device_unique_bridge d ON d.device_id = o.device_id
  LEFT JOIN inventory_nasid_profile n ON n.bridge_nasid = d.bridge_nasid
  LEFT JOIN (
    SELECT nasid AS bridge_nasid, account_degree, account_id FROM customer_nasid_profile
  ) c ON c.bridge_nasid = d.bridge_nasid
),
accepted_mapping AS (
  SELECT m.device_id, m.bridge_nasid, m.account_id
  FROM mapping_classification m
  WHERE m.mapping_status = 'mapped'
)
"""


AUDIT_SQL = COMMON_SQL + f"""
SELECT b.window_start_ist, b.window_end_ist,
       s.active_base_rows, s.customer_v2_rows, s.missing_account_rows,
       (SELECT COUNT(*) FROM account_profile) AS customer_v2_accounts,
       (SELECT COUNT_IF(cohort_status = 'inactive_or_state_conflict') FROM classified_accounts)
         AS inactive_or_state_conflict_accounts,
       (SELECT COUNT_IF(cohort_status = 'nasid_missing_or_conflict') FROM classified_accounts)
         AS nasid_missing_or_conflict_accounts,
       (SELECT COUNT_IF(cohort_status = 'coordinate_missing_or_conflict') FROM classified_accounts)
         AS coordinate_missing_or_conflict_accounts,
       (SELECT COUNT_IF(cohort_status = 'coordinate_outside_india') FROM classified_accounts)
         AS coordinate_outside_india_accounts,
       (SELECT COUNT(*) FROM eligible_accounts) AS eligible_accounts_before_nasid_uniqueness,
       (SELECT COUNT(*) FROM eligible_accounts e JOIN customer_nasid_profile p USING (nasid)
         WHERE p.account_degree > 1) AS shared_nasid_accounts,
       (SELECT COUNT(*) FROM clean_customers) AS clean_customer_accounts,
       (SELECT COUNT(*) FROM valid_incidents) AS valid_incidents,
       (SELECT COUNT(DISTINCT incident_id) FROM valid_incidents) AS valid_distinct_incidents,
       (SELECT invalid_duration_incidents FROM invalid_incidents) AS invalid_duration_incidents,
       (SELECT COUNT_IF(start_ist < b.window_start_ist) FROM valid_incidents) AS carry_in_incidents,
       (SELECT COUNT_IF(start_ist < b.window_start_ist AND end_ist >= b.window_end_ist)
          FROM valid_incidents) AS incidents_spanning_full_window,
       (SELECT COUNT_IF(duration_minutes > {HOURS * 60}) FROM valid_incidents) AS incidents_over_seven_days,
       (SELECT COUNT_IF(duration_minutes > {HOURS * 60}
                        AND UPPER(status) = 'ACTIVE' AND COALESCE(is_closed, FALSE) = FALSE
                        AND closed_at IS NULL) FROM valid_incidents) AS long_active_open_incidents,
       (SELECT COUNT_IF(duration_minutes > {HOURS * 60}
                        AND UPPER(status) = 'CLOSED' AND COALESCE(is_closed, FALSE) = TRUE
                        AND closed_at IS NOT NULL) FROM valid_incidents) AS long_closed_consistent_incidents,
       (SELECT COUNT_IF(
          status IS NULL OR is_closed IS NULL
          OR (UPPER(status) = 'CLOSED') <> is_closed
          OR COALESCE(is_closed, FALSE) <> (closed_at IS NOT NULL)
        ) FROM valid_incidents) AS status_closure_contradictions,
       (SELECT compared_incidents FROM timestamp_sanity) AS clock_compared_incidents,
       (SELECT reversed_clock_incidents FROM timestamp_sanity) AS reversed_clock_incidents,
       (SELECT creation_lag_seconds_p01 FROM timestamp_sanity) AS creation_lag_seconds_p01,
       (SELECT creation_lag_seconds_p50 FROM timestamp_sanity) AS creation_lag_seconds_p50,
       (SELECT creation_lag_seconds_p99 FROM timestamp_sanity) AS creation_lag_seconds_p99,
       (SELECT COUNT(*) FROM outage_pairs) AS incident_device_pairs,
       (SELECT COUNT(*) FROM outage_devices) AS outage_devices_total,
       (SELECT COUNT_IF(mapping_status = 'no_inventory') FROM mapping_classification) AS no_inventory_devices,
       (SELECT COUNT_IF(mapping_status = 'ambiguous_device') FROM mapping_classification) AS ambiguous_device_devices,
       (SELECT COUNT_IF(mapping_status = 'ambiguous_nasid') FROM mapping_classification) AS ambiguous_nasid_devices,
       (SELECT COUNT_IF(mapping_status = 'not_eligible_customer') FROM mapping_classification)
         AS not_eligible_customer_devices,
       (SELECT COUNT_IF(mapping_status = 'ambiguous_customer_nasid') FROM mapping_classification)
         AS ambiguous_customer_nasid_devices,
       (SELECT COUNT(*) FROM accepted_mapping) AS mapped_devices,
       (SELECT COUNT(DISTINCT device_id) FROM accepted_mapping) AS mapped_distinct_devices,
       (SELECT COUNT(DISTINCT bridge_nasid) FROM accepted_mapping) AS mapped_distinct_nasids,
       (SELECT COUNT(DISTINCT account_id) FROM accepted_mapping) AS mapped_distinct_accounts
FROM bounds b CROSS JOIN source_counts s
"""


ANALYSIS_VARIANTS_SQL = f"""analysis_variants AS (
  SELECT 'all_incidents' AS analysis_variant, NULL::NUMBER AS maximum_duration_minutes,
         FALSE AS require_in_window_onset, FALSE AS clip_to_onset_half
  UNION ALL
  SELECT 'in_window_onsets', NULL::NUMBER, TRUE, TRUE
  UNION ALL
  SELECT 'in_window_onsets_max_7d', {MAX_INCIDENT_DURATION_MINUTES}::NUMBER, TRUE, TRUE
)"""

DESCRIPTIVE_VARIANTS_SQL = """analysis_variants AS (
  SELECT 'all_incidents' AS analysis_variant, NULL::NUMBER AS maximum_duration_minutes,
         FALSE AS require_in_window_onset, FALSE AS clip_to_onset_half
)"""

GATE_VARIANTS_SQL = f"""analysis_variants AS (
  SELECT 'in_window_onsets' AS analysis_variant, NULL::NUMBER AS maximum_duration_minutes,
         TRUE AS require_in_window_onset, TRUE AS clip_to_onset_half
  UNION ALL
  SELECT 'in_window_onsets_max_7d', {MAX_INCIDENT_DURATION_MINUTES}::NUMBER, TRUE, TRUE
)"""


SPATIAL_SQL = COMMON_SQL + f"""
, hours AS (
  SELECT DATEADD(hour, ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1, b.window_start_ist)
           AS hour_start_ist
  FROM bounds b, TABLE(GENERATOR(ROWCOUNT => {HOURS}))
),
{ANALYSIS_VARIANTS_SQL},
grid_variants AS (
  SELECT 'base' AS grid_variant, 0.0::FLOAT AS shift
  UNION ALL SELECT 'shifted', 0.005::FLOAT
),
grid_customers AS (
  SELECT v.grid_variant,
         FLOOR((c.latitude - v.shift) / 0.01)::INTEGER AS cell_y,
         FLOOR((c.longitude - v.shift) / 0.01)::INTEGER AS cell_x,
         c.account_id, c.location_start_time, c.plan_expiry_time
  FROM clean_customers c CROSS JOIN grid_variants v
),
denominators AS (
  SELECT g.grid_variant, h.hour_start_ist, g.cell_y, g.cell_x,
         COUNT(*) AS network_footprint_customers,
         COUNT_IF(g.location_start_time IS NOT NULL AND g.plan_expiry_time IS NOT NULL
                  AND g.location_start_time <= h.hour_start_ist
                  AND g.plan_expiry_time >= h.hour_start_ist) AS eligible_customers
  FROM grid_customers g CROSS JOIN hours h
  GROUP BY g.grid_variant, h.hour_start_ist, g.cell_y, g.cell_x
),
analysis_denominators AS (
  SELECT v.analysis_variant, d.*
  FROM analysis_variants v CROSS JOIN denominators d
),
mapped_events AS (
  SELECT v.analysis_variant, m.account_id, o.incident_id, o.start_ist,
         IFF(v.clip_to_onset_half,
             LEAST(
               o.end_ist,
               IFF(o.start_ist < DATEADD(hour, {HOURS // 2}, b.window_start_ist),
                   DATEADD(hour, {HOURS // 2}, b.window_start_ist),
                   b.window_end_ist)
             ),
             o.end_ist) AS end_ist,
         o.duration_minutes
  FROM outage_pairs o
  JOIN accepted_mapping m ON m.device_id = o.device_id
  CROSS JOIN analysis_variants v
  CROSS JOIN bounds b
  WHERE (v.maximum_duration_minutes IS NULL
         OR o.duration_minutes <= v.maximum_duration_minutes)
    AND (NOT v.require_in_window_onset
         OR (o.start_ist >= b.window_start_ist AND o.start_ist < b.window_end_ist))
),
event_customer_hours AS (
  SELECT DISTINCT e.analysis_variant, g.grid_variant, h.hour_start_ist, g.cell_y, g.cell_x,
         e.account_id, e.incident_id, e.duration_minutes
  FROM mapped_events e
  JOIN grid_customers g ON g.account_id = e.account_id
  JOIN hours h ON e.start_ist < DATEADD(hour, 1, h.hour_start_ist)
              AND e.end_ist > h.hour_start_ist
  WHERE g.location_start_time IS NOT NULL AND g.plan_expiry_time IS NOT NULL
    AND g.location_start_time <= h.hour_start_ist
    AND g.plan_expiry_time >= h.hour_start_ist
),
affected_cells AS (
  SELECT analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x,
         COUNT(DISTINCT account_id) AS affected_customers
  FROM event_customer_hours
  GROUP BY analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x
),
incident_cell_hours AS (
  SELECT DISTINCT analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x,
         incident_id, duration_minutes
  FROM event_customer_hours
  WHERE analysis_variant = 'all_incidents'
),
incident_cells AS (
  SELECT analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x,
         COUNT(*) AS distinct_incidents,
         AVG(duration_minutes) AS duration_minutes_mean,
         MEDIAN(duration_minutes) AS duration_minutes_p50,
         MAX(duration_minutes) AS duration_minutes_max
  FROM incident_cell_hours
  GROUP BY analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x
),
cell_hours AS (
  SELECT d.analysis_variant, d.grid_variant, d.hour_start_ist, d.cell_y, d.cell_x,
         d.network_footprint_customers, d.eligible_customers,
         COALESCE(a.affected_customers, 0) AS affected_customers,
         COALESCE(a.affected_customers, 0) / NULLIF(d.eligible_customers, 0) AS affected_customer_rate,
         COALESCE(i.distinct_incidents, 0) AS distinct_incidents,
         i.duration_minutes_mean, i.duration_minutes_p50, i.duration_minutes_max
  FROM analysis_denominators d
  LEFT JOIN affected_cells a
    USING (analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x)
  LEFT JOIN incident_cells i
    USING (analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x)
),
variant_stats AS (
  SELECT analysis_variant, grid_variant,
         COUNT_IF(eligible_customers < 5) AS suppressed_cell_hours,
         SUM(eligible_customers) AS all_eligible_customer_hours,
         SUM(IFF(eligible_customers >= 5, eligible_customers, 0)) AS reportable_eligible_customer_hours,
         SUM(affected_customers) AS all_affected_customer_hours,
         SUM(IFF(eligible_customers >= 5, affected_customers, 0)) AS reportable_affected_customer_hours
  FROM cell_hours GROUP BY analysis_variant, grid_variant
),
reportable AS (
  SELECT * FROM cell_hours WHERE eligible_customers >= 5
),
neighbor_counts AS (
  SELECT c.analysis_variant, c.grid_variant, c.hour_start_ist, c.cell_y, c.cell_x,
         COUNT(n.cell_y) AS neighbor_reportable_cells,
         COALESCE(SUM(n.network_footprint_customers), 0) AS neighbor_network_footprint_customers,
         COALESCE(SUM(n.eligible_customers), 0) AS neighbor_eligible_customers,
         COALESCE(SUM(n.affected_customers), 0) AS neighbor_affected_customers
  FROM reportable c
  LEFT JOIN reportable n
    ON n.analysis_variant = c.analysis_variant
   AND n.grid_variant = c.grid_variant AND n.hour_start_ist = c.hour_start_ist
   AND n.cell_y BETWEEN c.cell_y - 1 AND c.cell_y + 1
   AND n.cell_x BETWEEN c.cell_x - 1 AND c.cell_x + 1
   AND NOT (n.cell_y = c.cell_y AND n.cell_x = c.cell_x)
  WHERE c.analysis_variant = 'all_incidents'
  GROUP BY c.analysis_variant, c.grid_variant, c.hour_start_ist, c.cell_y, c.cell_x
),
neighbor_incident_set AS (
  SELECT DISTINCT c.analysis_variant, c.grid_variant, c.hour_start_ist, c.cell_y, c.cell_x,
         i.incident_id, i.duration_minutes
  FROM reportable c
  JOIN reportable n
    ON n.analysis_variant = c.analysis_variant
   AND n.grid_variant = c.grid_variant AND n.hour_start_ist = c.hour_start_ist
   AND n.cell_y BETWEEN c.cell_y - 1 AND c.cell_y + 1
   AND n.cell_x BETWEEN c.cell_x - 1 AND c.cell_x + 1
   AND NOT (n.cell_y = c.cell_y AND n.cell_x = c.cell_x)
  JOIN incident_cell_hours i
    ON i.analysis_variant = n.analysis_variant
   AND i.grid_variant = n.grid_variant AND i.hour_start_ist = n.hour_start_ist
   AND i.cell_y = n.cell_y AND i.cell_x = n.cell_x
  WHERE c.analysis_variant = 'all_incidents'
),
neighbor_incidents AS (
  SELECT analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x,
         COUNT(*) AS neighbor_distinct_incidents,
         AVG(duration_minutes) AS neighbor_duration_minutes_mean,
         MEDIAN(duration_minutes) AS neighbor_duration_minutes_p50,
         MAX(duration_minutes) AS neighbor_duration_minutes_max
  FROM neighbor_incident_set
  GROUP BY analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x
),
primary_output AS (
  SELECT r.analysis_variant, r.grid_variant, r.hour_start_ist, r.cell_y, r.cell_x,
         r.network_footprint_customers, r.eligible_customers, r.affected_customers,
         r.affected_customer_rate, r.distinct_incidents,
         r.duration_minutes_mean, r.duration_minutes_p50, r.duration_minutes_max,
         n.neighbor_reportable_cells, n.neighbor_network_footprint_customers,
         n.neighbor_eligible_customers, n.neighbor_affected_customers,
         n.neighbor_affected_customers / NULLIF(n.neighbor_eligible_customers, 0)
           AS neighbor_affected_customer_rate,
         COALESCE(ni.neighbor_distinct_incidents, 0) AS neighbor_distinct_incidents,
         ni.neighbor_duration_minutes_mean, ni.neighbor_duration_minutes_p50,
         ni.neighbor_duration_minutes_max,
         s.suppressed_cell_hours, s.all_eligible_customer_hours,
         s.reportable_eligible_customer_hours, s.all_affected_customer_hours,
         s.reportable_affected_customer_hours,
         1 AS reportable_hours
  FROM reportable r
  LEFT JOIN neighbor_counts n
    USING (analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x)
  LEFT JOIN neighbor_incidents ni
    USING (analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x)
  JOIN variant_stats s USING (analysis_variant, grid_variant)
  WHERE r.analysis_variant = 'all_incidents'
),
validation_output AS (
  SELECT r.analysis_variant, r.grid_variant,
         IFF(r.hour_start_ist < DATEADD(hour, {HOURS // 2}, b.window_start_ist),
             b.window_start_ist, DATEADD(hour, {HOURS // 2}, b.window_start_ist)) AS hour_start_ist,
         r.cell_y, r.cell_x,
         MAX(r.network_footprint_customers) AS network_footprint_customers,
         SUM(r.eligible_customers) AS eligible_customers,
         SUM(r.affected_customers) AS affected_customers,
         SUM(r.affected_customers) / NULLIF(SUM(r.eligible_customers), 0) AS affected_customer_rate,
         NULL::NUMBER AS distinct_incidents,
         NULL::FLOAT AS duration_minutes_mean,
         NULL::FLOAT AS duration_minutes_p50,
         NULL::FLOAT AS duration_minutes_max,
         NULL::NUMBER AS neighbor_reportable_cells,
         NULL::NUMBER AS neighbor_network_footprint_customers,
         NULL::NUMBER AS neighbor_eligible_customers,
         NULL::NUMBER AS neighbor_affected_customers,
         NULL::FLOAT AS neighbor_affected_customer_rate,
         NULL::NUMBER AS neighbor_distinct_incidents,
         NULL::FLOAT AS neighbor_duration_minutes_mean,
         NULL::FLOAT AS neighbor_duration_minutes_p50,
         NULL::FLOAT AS neighbor_duration_minutes_max,
         MAX(s.suppressed_cell_hours) AS suppressed_cell_hours,
         MAX(s.all_eligible_customer_hours) AS all_eligible_customer_hours,
         MAX(s.reportable_eligible_customer_hours) AS reportable_eligible_customer_hours,
         MAX(s.all_affected_customer_hours) AS all_affected_customer_hours,
         MAX(s.reportable_affected_customer_hours) AS reportable_affected_customer_hours,
         COUNT(*) AS reportable_hours
  FROM reportable r
  JOIN variant_stats s USING (analysis_variant, grid_variant)
  CROSS JOIN bounds b
  WHERE r.analysis_variant <> 'all_incidents'
  GROUP BY r.analysis_variant, r.grid_variant,
           IFF(r.hour_start_ist < DATEADD(hour, {HOURS // 2}, b.window_start_ist),
               b.window_start_ist, DATEADD(hour, {HOURS // 2}, b.window_start_ist)),
           r.cell_y, r.cell_x
)
SELECT * FROM primary_output
UNION ALL
SELECT * FROM validation_output
ORDER BY analysis_variant, grid_variant, hour_start_ist, cell_y, cell_x
"""

COMBINED_OUTPUT_SQL = """SELECT * FROM primary_output
UNION ALL
SELECT * FROM validation_output"""
DESCRIPTIVE_SQL = SPATIAL_SQL.replace(
    ANALYSIS_VARIANTS_SQL, DESCRIPTIVE_VARIANTS_SQL
).replace(COMBINED_OUTPUT_SQL, "SELECT * FROM primary_output")
GATE_SQL = SPATIAL_SQL.replace(
    ANALYSIS_VARIANTS_SQL, GATE_VARIANTS_SQL
).replace(COMBINED_OUTPUT_SQL, "SELECT * FROM validation_output")


def native(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    try:
        return int(value) if value == int(value) else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return str(value)


def rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average = (start + 1 + end) / 2
        for index, _ in ordered[start:end]:
            ranks[index] = average
        start = end
    return ranks


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else None


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    return pearson(rank([p[0] for p in pairs]), rank([p[1] for p in pairs]))


def relative_change(left: float, right: float) -> float | None:
    return abs(right - left) / abs(left) if left else (0.0 if right == 0 else None)


def cell_bin(value: float, shift: float = 0.0) -> int:
    return math.floor((value - shift) / 0.01)


def time_valid(customer: dict[str, object], hour: datetime) -> bool:
    start, end = customer.get("location_start_time"), customer.get("plan_expiry_time")
    return isinstance(start, datetime) and isinstance(end, datetime) and start <= hour <= end


def fixture_clean_customers(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[object, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if row.get("source") == "CUSTOMER_V2" and row.get("account_id") is not None:
            grouped[row["account_id"]].append(row)
    candidates: list[dict[str, object]] = []
    for account_id, group in grouped.items():
        nasids = {row.get("nasid") for row in group}
        coordinates = {(row.get("latitude"), row.get("longitude")) for row in group}
        states = {str(row.get("active_state") or "").strip().upper() for row in group}
        latitude, longitude = next(iter(coordinates))
        if (
            len(nasids) == len(coordinates) == len(states) == 1
            and None not in nasids
            and None not in (latitude, longitude)
            and states == {"ACTIVE"}
            and 6 <= float(latitude) <= 38
            and 68 <= float(longitude) <= 98
        ):
            candidates.append(
                {
                    "account_id": account_id,
                    "nasid": next(iter(nasids)),
                    "latitude": latitude,
                    "longitude": longitude,
                    "location_start_time": min(row["location_start_time"] for row in group),
                    "plan_expiry_time": max(row["plan_expiry_time"] for row in group),
                }
            )
    degrees: dict[object, int] = defaultdict(int)
    for row in candidates:
        degrees[row["nasid"]] += 1
    return [row for row in candidates if degrees[row["nasid"]] == 1]


def accepted_fixture_bridges(rows: list[dict[str, object]]) -> dict[str, str]:
    pairs: set[tuple[str, str]] = set()
    conflicted: set[str] = set()
    for row in rows:
        if row.get("deleted"):
            continue
        device = str(row.get("device_id") or "").strip()
        long_nasid = str(row.get("long_nasid") or "").strip()
        short_nasid = str(row.get("nasid") or "").strip()
        if not device or (not long_nasid and not short_nasid):
            continue
        if long_nasid and short_nasid and long_nasid != short_nasid:
            conflicted.add(device)
            continue
        pairs.add((device, long_nasid or short_nasid))
    by_device: dict[str, set[str]] = defaultdict(set)
    for device, nasid in pairs:
        by_device[device].add(nasid)
    device_unique = {device: next(iter(nasids)) for device, nasids in by_device.items()
                     if device not in conflicted and len(nasids) == 1}
    nasid_degree: dict[str, int] = defaultdict(int)
    for nasid in device_unique.values():
        nasid_degree[nasid] += 1
    return {device: nasid for device, nasid in device_unique.items() if nasid_degree[nasid] == 1}


def overlapping_hours(start: datetime, end: datetime) -> list[datetime]:
    hour = start.replace(minute=0, second=0, microsecond=0)
    result: list[datetime] = []
    while hour < end:
        if start < hour + timedelta(hours=1) and end > hour:
            result.append(hour)
        hour += timedelta(hours=1)
    return result


def self_check() -> None:
    hour = datetime(2026, 8, 4)
    base = {
        "source": "CUSTOMER_V2", "active_state": "Active", "latitude": 20.0,
        "longitude": 75.0, "location_start_time": hour, "plan_expiry_time": hour + timedelta(days=2),
    }
    rows = [
        {**base, "account_id": 1, "nasid": 101}, {**base, "account_id": 1, "nasid": 101},
        {**base, "account_id": 2, "nasid": 102},
        {**base, "account_id": 3, "nasid": 103}, {**base, "account_id": 3, "nasid": 104},
        {**base, "account_id": 4, "nasid": 105, "active_state": "Inactive"},
        {**base, "account_id": 5, "nasid": 106, "latitude": 0.0},
        {**base, "account_id": 6, "nasid": 102},
        {**base, "account_id": 7, "nasid": 107, "source": "T_STORE"},
        {**base, "account_id": 8, "nasid": 108},
        {**base, "account_id": 8, "nasid": 108, "longitude": 76.0},
        {**base, "account_id": 9, "nasid": 109},
        {**base, "account_id": 9, "nasid": 109, "active_state": "Inactive"},
        {**base, "account_id": 10, "nasid": 110, "active_state": None},
    ]
    clean = fixture_clean_customers(rows)
    assert {(row["account_id"], row["nasid"]) for row in clean} == {(1, 101)}
    assert time_valid(clean[0], hour) and time_valid(clean[0], hour + timedelta(days=2))
    assert not time_valid(clean[0], hour - timedelta(seconds=1))
    assert not time_valid({**clean[0], "plan_expiry_time": None}, hour)

    bridge_rows = [
        {"device_id": f"d{i}", "nasid": f"n{i}", "long_nasid": "", "deleted": False}
        for i in range(1, 11)
    ]
    assert len(accepted_fixture_bridges(bridge_rows[:9])) / 10 == MIN_MAPPING_COVERAGE
    assert len(accepted_fixture_bridges(bridge_rows[:8])) / 10 < MIN_MAPPING_COVERAGE
    bridge_rows += [
        {"device_id": "two", "nasid": "a", "long_nasid": "", "deleted": False},
        {"device_id": "two", "nasid": "b", "long_nasid": "", "deleted": False},
        {"device_id": "x", "nasid": "shared", "long_nasid": "", "deleted": False},
        {"device_id": "y", "nasid": "shared", "long_nasid": "", "deleted": False},
        {"device_id": "deleted", "nasid": "z", "long_nasid": "", "deleted": True},
    ]
    accepted = accepted_fixture_bridges(bridge_rows)
    assert "two" not in accepted and "x" not in accepted and "y" not in accepted and "deleted" not in accepted

    assert overlapping_hours(hour, hour + timedelta(hours=1)) == [hour]
    assert overlapping_hours(hour + timedelta(minutes=30), hour + timedelta(hours=1, minutes=30)) == [
        hour, hour + timedelta(hours=1)
    ]
    affected = {(1, h) for h in overlapping_hours(hour, hour + timedelta(hours=1))}
    affected |= {(1, h) for h in overlapping_hours(hour, hour + timedelta(hours=1))}
    assert len(affected) == 1
    neighbors = {(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)}
    assert len(neighbors) == 8 and (2, 0) not in neighbors
    assert cell_bin(20.004) == 2000 and cell_bin(20.006, 0.005) == 2000
    assert 5 >= 5 and not 4 >= 5
    assert spearman([(1, 1), (2, 2), (3, 3)]) == 1.0
    assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))
    assert not {"account_id", "nasid", "device_id", "incident_id", "latitude", "longitude"}.intersection(CSV_COLUMNS)

    gate_audit = {
        "outage_devices_total": 10, "mapped_devices": 9, "mapped_distinct_devices": 9,
        "mapped_distinct_nasids": 9, "mapped_distinct_accounts": 9,
    }
    assert mapping_gate(gate_audit) == (True, 0.9)
    assert not mapping_gate({**gate_audit, "mapped_devices": 8})[0]
    try:
        assert_private_structure({"account_id": "must never be exported"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Privacy guard did not reject an identifier key")
    try:
        assert_private_report("device_id must never leave the warehouse")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Report privacy guard did not reject an identifier field")

    period_fixture: dict[tuple[str, str], dict[tuple[int, int], list[int]]] = {}
    full_fixture: dict[str, dict[tuple[int, int], list[int]]] = {}
    for grid in ("base", "shifted"):
        early = {(i, 0): [840, 100 if i < 2 else 1, HOURS // 2] for i in range(20)}
        late = {(i, 0): [840, 100 if i < 2 else 1, HOURS // 2] for i in range(20)}
        period_fixture[(grid, "early")] = early
        period_fixture[(grid, "late")] = late
        full_fixture[grid] = {cell: [early[cell][0] + late[cell][0], early[cell][1] + late[cell][1]]
                              for cell in early}
    totals_fixture = {
        grid: {"all_eligible": 33_600, "reportable_eligible": 33_600,
               "all_affected": 436, "reportable_affected": 436,
               "suppressed_cell_hours": 0}
        for grid in ("base", "shifted")
    }
    assert evaluate_spatial(period_fixture, full_fixture, totals_fixture)["all_checks_pass"]


def connect():
    load_dotenv(WORKSPACE / ".env")
    sys.path.insert(0, str(BOOKING_TRUTH))
    from data_lib.data_fetch.wiom_data import WiomData

    db = WiomData("snowflake")
    db._connection_params.update(
        login_timeout=20,
        network_timeout=300,
        ocsp_response_cache_filename=str(ROOT / ".ocsp_cache.json"),
    )
    return db._connect()


def fetch_one(cursor, sql: str, parameters: tuple[object, ...] = ()) -> dict[str, object]:
    cursor.execute(sql, parameters)
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Expected one aggregate row")
    return {column[0].lower(): native(value) for column, value in zip(cursor.description, row)}


def default_window(cursor) -> tuple[datetime, datetime]:
    row = fetch_one(
        cursor,
        f"""
        WITH latest AS (
          SELECT DATE_TRUNC('day', MAX(CONVERT_TIMEZONE('UTC', '{TIMEZONE}', first_fail_timestamp))) AS end_ist
          FROM {INCIDENTS}
          WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE AND first_fail_timestamp IS NOT NULL
        )
        SELECT DATEADD(day, -{DAYS}, end_ist) AS start_ist, end_ist FROM latest
        """,
    )
    start, end = row["start_ist"], row["end_ist"]
    if not isinstance(start, str) or not isinstance(end, str):
        raise RuntimeError("Could not resolve the latest complete incident window")
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def mapping_gate(audit: dict[str, object]) -> tuple[bool, float]:
    total, mapped = int(audit["outage_devices_total"]), int(audit["mapped_devices"])
    coverage = mapped / total if total else 0.0
    invariant = len({
        int(audit["mapped_devices"]), int(audit["mapped_distinct_devices"]),
        int(audit["mapped_distinct_nasids"]), int(audit["mapped_distinct_accounts"]),
    }) == 1
    return total > 0 and coverage >= MIN_MAPPING_COVERAGE and invariant, coverage


def source_contract_gate(audit: dict[str, object]) -> bool:
    return (
        int(audit["valid_incidents"]) == int(audit["valid_distinct_incidents"])
        and int(audit["clock_compared_incidents"]) > 0
        and int(audit["reversed_clock_incidents"]) == 0
        and int(audit["status_closure_contradictions"]) == 0
    )


def top_decile_metrics(cells: dict[tuple[int, int], list[int]]) -> tuple[float | None, float | None]:
    usable = [(key, values) for key, values in cells.items() if values[0] > 0]
    total_eligible = sum(values[0] for _, values in usable)
    total_affected = sum(values[1] for _, values in usable)
    if not usable or not total_eligible or not total_affected:
        return None, None
    usable.sort(key=lambda item: (-item[1][1] / item[1][0], -item[1][0], item[0]))
    top = usable[: max(1, math.ceil(len(usable) * 0.10))]
    top_eligible = sum(values[0] for _, values in top)
    top_affected = sum(values[1] for _, values in top)
    overall_rate = total_affected / total_eligible
    top_rate = top_affected / top_eligible if top_eligible else 0.0
    return top_affected / total_affected, top_rate / overall_rate if overall_rate else None


def early_selected_late_metrics(
    early: dict[tuple[int, int], list[int]],
    late: dict[tuple[int, int], list[int]],
) -> tuple[float | None, float | None, int, int]:
    common = [
        (cell, early[cell]) for cell in set(early).intersection(late)
        if early[cell][0] > 0 and late[cell][0] > 0
    ]
    if not common:
        return None, None, 0, 0
    common.sort(key=lambda item: (-item[1][1] / item[1][0], -item[1][0], item[0]))
    selected = [cell for cell, _ in common[: max(1, math.ceil(len(common) * 0.10))]]
    selected_eligible = sum(late[cell][0] for cell in selected)
    selected_affected = sum(late[cell][1] for cell in selected)
    overall_eligible = sum(late[cell][0] for cell, _ in common)
    overall_affected = sum(late[cell][1] for cell, _ in common)
    if not selected_eligible or not overall_eligible or not overall_affected:
        return None, None, len(selected), len(common)
    lift = (selected_affected / selected_eligible) / (overall_affected / overall_eligible)
    concentration = selected_affected / overall_affected
    return lift, concentration, len(selected), len(common)


def shifted_hotspot_area_jaccard(
    base: dict[tuple[int, int], list[int]],
    shifted: dict[tuple[int, int], list[int]],
) -> float | None:
    def hot(cells: dict[tuple[int, int], list[int]]) -> set[tuple[int, int]]:
        usable = [(cell, values) for cell, values in cells.items() if values[0] > 0]
        usable.sort(key=lambda item: (-item[1][1] / item[1][0], -item[1][0], item[0]))
        return {cell for cell, _ in usable[: max(1, math.ceil(len(usable) * 0.10))]}

    base_hot, shifted_hot = hot(base), hot(shifted)
    if not base_hot or not shifted_hot:
        return None
    overlaps = sum(
        (shift_y, shift_x) in shifted_hot
        for base_y, base_x in base_hot
        for shift_y in (base_y - 1, base_y)
        for shift_x in (base_x - 1, base_x)
    )
    intersection_area = overlaps * 0.25
    union_area = len(base_hot) + len(shifted_hot) - intersection_area
    return intersection_area / union_area if union_area else None


def grid_period_metrics(
    early: dict[tuple[int, int], list[int]],
    late: dict[tuple[int, int], list[int]],
) -> dict[str, object]:
    complete = {
        cell for cell in set(early).intersection(late)
        if early[cell][2] == HOURS // 2 and late[cell][2] == HOURS // 2
    }
    early_complete = {cell: early[cell] for cell in complete}
    late_complete = {cell: late[cell] for cell in complete}
    pairs = [
        (early_complete[cell][1] / early_complete[cell][0],
         late_complete[cell][1] / late_complete[cell][0])
        for cell in sorted(complete)
    ]
    transfer_lift, transfer_concentration, selected, common = early_selected_late_metrics(
        early_complete, late_complete
    )
    early_concentration, early_lift = top_decile_metrics(early_complete)
    early_eligible = sum(value[0] for value in early_complete.values())
    late_eligible = sum(value[0] for value in late_complete.values())
    early_affected = sum(value[1] for value in early_complete.values())
    late_affected = sum(value[1] for value in late_complete.values())
    return {
        "complete_common_cells": len(complete),
        "early_eligible_customer_hours": early_eligible,
        "late_eligible_customer_hours": late_eligible,
        "early_affected_customer_hours": early_affected,
        "late_affected_customer_hours": late_affected,
        "early_rate": early_affected / early_eligible if early_eligible else None,
        "late_rate": late_affected / late_eligible if late_eligible else None,
        "chronological_spearman": spearman(pairs),
        "early_selected_cells": selected,
        "early_selected_late_lift": transfer_lift,
        "early_selected_late_concentration": transfer_concentration,
        "training_top_decile_lift_diagnostic": early_lift,
        "training_top_decile_concentration_diagnostic": early_concentration,
        "selection_common_cells": common,
    }


def evaluate_spatial(
    period_cells: dict[tuple[str, str], dict[tuple[int, int], list[int]]],
    full_cells: dict[str, dict[tuple[int, int], list[int]]],
    variant_totals: dict[str, dict[str, int]],
) -> dict[str, object]:
    grids = {
        grid: grid_period_metrics(period_cells[(grid, "early")], period_cells[(grid, "late")])
        for grid in ("base", "shifted")
    }
    for grid, metrics in grids.items():
        totals = variant_totals[grid]
        metrics["reportable_eligible_share"] = (
            totals["reportable_eligible"] / totals["all_eligible"] if totals["all_eligible"] else 0.0
        )

    base_early_rate, shifted_early_rate = grids["base"]["early_rate"], grids["shifted"]["early_rate"]
    base_late_rate, shifted_late_rate = grids["base"]["late_rate"], grids["shifted"]["late_rate"]
    base_concentration = grids["base"]["early_selected_late_concentration"]
    shifted_concentration = grids["shifted"]["early_selected_late_concentration"]
    early_rate_change = (
        relative_change(float(base_early_rate), float(shifted_early_rate))
        if base_early_rate is not None and shifted_early_rate is not None else None
    )
    late_rate_change = (
        relative_change(float(base_late_rate), float(shifted_late_rate))
        if base_late_rate is not None and shifted_late_rate is not None else None
    )
    concentration_change = (
        relative_change(float(base_concentration), float(shifted_concentration))
        if base_concentration is not None and shifted_concentration is not None else None
    )
    checks: dict[str, bool] = {}
    for grid, metrics in grids.items():
        checks.update({
            f"{grid}_reportable_share": (
                float(metrics["reportable_eligible_share"]) >= MIN_REPORTABLE_ELIGIBLE_SHARE
            ),
            f"{grid}_common_cell_support": int(metrics["complete_common_cells"]) >= MIN_COMMON_CELLS,
            f"{grid}_early_affected_support": (
                int(metrics["early_affected_customer_hours"]) >= MIN_HALF_AFFECTED_CUSTOMER_HOURS
            ),
            f"{grid}_late_affected_support": (
                int(metrics["late_affected_customer_hours"]) >= MIN_HALF_AFFECTED_CUSTOMER_HOURS
            ),
            f"{grid}_chronological_stability": (
                metrics["chronological_spearman"] is not None
                and float(metrics["chronological_spearman"]) >= MIN_CHRONOLOGICAL_SPEARMAN
            ),
            f"{grid}_early_selected_late_lift": (
                metrics["early_selected_late_lift"] is not None
                and float(metrics["early_selected_late_lift"]) >= MIN_EARLY_TO_LATE_LIFT
            ),
        })
    checks.update({
        "early_shift_rate_stability": (
            early_rate_change is not None and early_rate_change <= MAX_SHIFT_RATE_CHANGE
        ),
        "late_shift_rate_stability": (
            late_rate_change is not None and late_rate_change <= MAX_SHIFT_RATE_CHANGE
        ),
        "shift_late_concentration_stability": (
            concentration_change is not None
            and concentration_change <= MAX_SHIFT_CONCENTRATION_CHANGE
        ),
    })
    return {
        "grids": grids,
        "early_shift_rate_relative_change": early_rate_change,
        "late_shift_rate_relative_change": late_rate_change,
        "shift_late_oos_concentration_relative_change": concentration_change,
        "shift_hotspot_area_jaccard_diagnostic": shifted_hotspot_area_jaccard(
            full_cells["base"], full_cells["shifted"]
        ),
        "checks": checks,
        "all_checks_pass": all(checks.values()),
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    assert_private_structure(payload)
    path.write_text(json.dumps(payload, indent=2, default=native) + "\n")


def assert_private_structure(value: object) -> None:
    if isinstance(value, dict):
        prohibited = PROHIBITED_ARTIFACT_KEYS.intersection(map(str, value))
        if prohibited:
            raise RuntimeError(f"Prohibited identifier keys in aggregate artifact: {sorted(prohibited)}")
        for nested in value.values():
            assert_private_structure(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_private_structure(nested)


def assert_private_report(report: str) -> None:
    found = {term for term in PROHIBITED_REPORT_TERMS if term in report.lower()}
    if found:
        raise RuntimeError(f"Prohibited identifier fields in report: {sorted(found)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded CUSTOMER_V2 outage spatial pilot")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--end-date", help="Exclusive IST end date (YYYY-MM-DD); defaults to latest complete day")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0

    started = time.monotonic()
    connection = connect()
    cursor = connection.cursor()
    try:
        cursor.execute(f"ALTER SESSION SET TIMEZONE = '{TIMEZONE}'")
        cursor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 300")
        cursor.execute("ALTER SESSION SET QUERY_TAG = 'iex_customer_outage_spatial_pilot'")
        if args.end_date:
            end = datetime.strptime(args.end_date, "%Y-%m-%d")
            start = end - timedelta(days=DAYS)
        else:
            start, end = default_window(cursor)

        output = args.output_dir or ROOT / "outputs" / f"customer_outage_spatial_pilot_{end:%Y-%m-%d}"
        if output.exists():
            raise SystemExit(f"Output directory already exists: {output}")
        output.mkdir(parents=True)
        parameters = (start, end)
        audit = fetch_one(cursor, AUDIT_SQL, parameters)
        audit_query_id = cursor.sfqid
        mapping_passed, coverage = mapping_gate(audit)
        source_passed = source_contract_gate(audit)
        passed = mapping_passed and source_passed
        audit.update(
            analysis_window={"start_ist": start, "end_ist_exclusive": end, "hours": HOURS},
            source_tables={
                "active_base": ACTIVE_BASE, "inventory": T_DEVICE, "incidents": INCIDENTS,
                "impacted": IMPACTED,
            },
            source_time_contract={
                "assumption": "FIRST_FAIL_TIMESTAMP is UTC-valued TIMESTAMP_NTZ",
                "support": (
                    "warehouse convention plus an internal FIRST_FAIL_TIMESTAMP-to-CREATED_AT "
                    "clock-order check; not independent timezone proof"
                ),
            },
            mapping_coverage=coverage,
            mapping_gate_passed=mapping_passed,
            source_contract_gate_passed=source_passed,
            query_ids={"audit": audit_query_id},
            status="PREFLIGHT_PASSED" if passed else "PREFLIGHT_FAILED",
        )
        if not passed:
            audit["decision"] = "NO_GO_FOR_ACS"
            audit["privacy_check"] = "passed"
            write_json(output / "audit.json", audit)
            return 2

        if PROHIBITED_ARTIFACT_KEYS.intersection(CSV_COLUMNS):
            raise RuntimeError("CSV schema contains prohibited identifier fields")

        tmp_csv = output / ".cell_hour_outages.csv.tmp"
        period_cells: dict[tuple[str, str, str], dict[tuple[int, int], list[int]]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0, 0])
        )
        full_cells: dict[tuple[str, str], dict[tuple[int, int], list[int]]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0])
        )
        variant_totals: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        base_rows = 0
        seen_base: set[tuple[object, int, int]] = set()
        split = start + timedelta(hours=HOURS // 2)

        def execute_and_consume(sql: str, writer: csv.DictWriter | None = None) -> str:
            nonlocal base_rows
            cursor.execute(sql, parameters)
            query_id = cursor.sfqid
            columns = [column[0].lower() for column in cursor.description]
            if set(columns) != QUERY_COLUMNS:
                raise RuntimeError(f"Unexpected aggregate schema: {columns}")
            while batch := cursor.fetchmany(10_000):
                for values in batch:
                    row = {name: value for name, value in zip(columns, values)}
                    analysis = str(row["analysis_variant"])
                    variant = str(row["grid_variant"])
                    hour = row["hour_start_ist"]
                    cell_y, cell_x = int(row["cell_y"]), int(row["cell_x"])
                    eligible = int(row["eligible_customers"])
                    affected = int(row["affected_customers"])
                    rate = float(row["affected_customer_rate"] or 0)
                    if eligible < 5 or not 0 <= affected <= eligible or not 0 <= rate <= 1:
                        raise RuntimeError("Aggregate row failed denominator/rate validation")
                    if variant not in variant_totals[analysis]:
                        variant_totals[analysis][variant] = {
                            "suppressed_cell_hours": int(row["suppressed_cell_hours"]),
                            "all_eligible": int(row["all_eligible_customer_hours"]),
                            "reportable_eligible": int(row["reportable_eligible_customer_hours"]),
                            "all_affected": int(row["all_affected_customer_hours"]),
                            "reportable_affected": int(row["reportable_affected_customer_hours"]),
                        }
                    key = (cell_y, cell_x)
                    period = "early" if hour < split else "late"
                    period_cells[(analysis, variant, period)][key][0] += eligible
                    period_cells[(analysis, variant, period)][key][1] += affected
                    period_cells[(analysis, variant, period)][key][2] += int(row["reportable_hours"])
                    full_cells[(analysis, variant)][key][0] += eligible
                    full_cells[(analysis, variant)][key][1] += affected

                    if writer is not None and analysis == "all_incidents" and variant == "base":
                        unique = (hour, cell_y, cell_x)
                        if unique in seen_base:
                            raise RuntimeError("Duplicate base cell-hour row")
                        seen_base.add(unique)
                        base_rows += 1
                        writer.writerow({
                            "hour_start_ist": hour.isoformat(sep=" "),
                            "cell_id": f"b:{cell_y}:{cell_x}",
                            **{name: native(row[name]) for name in CSV_COLUMNS[2:]},
                        })
            return query_id

        try:
            with tmp_csv.open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                descriptive_query_id = execute_and_consume(DESCRIPTIVE_SQL, writer)
            gate_query_id = execute_and_consume(GATE_SQL)
            expected_analyses = {"all_incidents", "in_window_onsets", "in_window_onsets_max_7d"}
            if set(variant_totals) != expected_analyses or any(
                set(totals) != {"base", "shifted"} for totals in variant_totals.values()
            ) or base_rows == 0:
                raise RuntimeError("Spatial query returned incomplete grid variants")
            spatial_by_analysis = {
                analysis: evaluate_spatial(
                    {
                        (variant, period): period_cells[(analysis, variant, period)]
                        for variant in ("base", "shifted") for period in ("early", "late")
                    },
                    {variant: full_cells[(analysis, variant)] for variant in ("base", "shifted")},
                    variant_totals[analysis],
                )
                for analysis in sorted(expected_analyses)
            }
            os.replace(tmp_csv, output / "cell_hour_outages.csv")
        finally:
            tmp_csv.unlink(missing_ok=True)

        corrected_checks = {
            "in_window_onset_stability": spatial_by_analysis["in_window_onsets"]["all_checks_pass"],
            "in_window_onset_max_7d_stability": spatial_by_analysis["in_window_onsets_max_7d"]["all_checks_pass"],
        }
        decision = "GO_FOR_ACS" if all(corrected_checks.values()) else "NO_GO_FOR_ACS"
        audit["query_ids"].update({  # type: ignore[union-attr]
            "descriptive_spatial": descriptive_query_id,
            "corrected_gate": gate_query_id,
        })
        reproduce = [
            "python", "acs_outage_prediction/customer_outage_spatial_pilot.py",
            "--end-date", f"{end:%Y-%m-%d}",
        ]
        if args.output_dir:
            reproduce.extend(("--output-dir", str(args.output_dir)))
        audit.update(
            status="ANALYSIS_COMPLETE",
            decision=decision,
            reportable_base_cell_hours=base_rows,
            grid_counts=variant_totals,
            feasibility_thresholds={
                "minimum_mapping_coverage": MIN_MAPPING_COVERAGE,
                "minimum_reportable_eligible_share": MIN_REPORTABLE_ELIGIBLE_SHARE,
                "minimum_affected_customer_hours_per_half": MIN_HALF_AFFECTED_CUSTOMER_HOURS,
                "minimum_common_cells": MIN_COMMON_CELLS,
                "minimum_chronological_spearman": MIN_CHRONOLOGICAL_SPEARMAN,
                "minimum_early_selected_late_lift": MIN_EARLY_TO_LATE_LIFT,
                "maximum_shift_rate_relative_change": MAX_SHIFT_RATE_CHANGE,
                "maximum_shift_concentration_relative_change": MAX_SHIFT_CONCENTRATION_CHANGE,
            },
            spatial_evidence=spatial_by_analysis,
            corrected_gate_checks=corrected_checks,
            runtime_seconds=time.monotonic() - started,
            software_versions={
                "python": platform.python_version(),
                "snowflake_connector": metadata.version("snowflake-connector-python"),
            },
            output_rows={"cell_hour_outages_csv": base_rows},
            privacy_check="passed",
            reproduction_command=shlex.join(reproduce),
        )
        write_json(output / "audit.json", audit)
        all_base = spatial_by_analysis["all_incidents"]["grids"]["base"]
        onset = spatial_by_analysis["in_window_onsets"]
        onset_base, onset_shifted = onset["grids"]["base"], onset["grids"]["shifted"]
        short = spatial_by_analysis["in_window_onsets_max_7d"]
        short_base, short_shifted = short["grids"]["base"], short["grids"]["shifted"]
        report = f"""# CUSTOMER_V2 geographic outage pilot

## Decision

**{decision}**

The deterministic inventory bridge mapped {int(audit['mapped_devices']):,} of
{int(audit['outage_devices_total']):,} formal outage devices ({coverage:.1%}) in the
seven complete IST days from {start:%Y-%m-%d} through {(end - timedelta(days=1)):%Y-%m-%d}.

## Geographic evidence

- Early half: [{start:%Y-%m-%d %H:%M:%S}, {split:%Y-%m-%d %H:%M:%S}) IST
- Late half: [{split:%Y-%m-%d %H:%M:%S}, {end:%Y-%m-%d %H:%M:%S}) IST
- Base/shifted reportable denominator coverage: {float(onset_base['reportable_eligible_share']):.1%}, {float(onset_shifted['reportable_eligible_share']):.1%}
- All-overlap base early/late Spearman (diagnostic): {all_base['chronological_spearman']}
- Corrected in-window base/shifted Spearman: {onset_base['chronological_spearman']}, {onset_shifted['chronological_spearman']}
- Corrected max-7d base/shifted Spearman: {short_base['chronological_spearman']}, {short_shifted['chronological_spearman']}
- Corrected base/shifted early-selected, late-tested lifts: {onset_base['early_selected_late_lift']}, {onset_shifted['early_selected_late_lift']}
- Max-7d base/shifted early-selected, late-tested lifts: {short_base['early_selected_late_lift']}, {short_shifted['early_selected_late_lift']}
- In-window shifted-grid early/late rate changes and late concentration change: {onset['early_shift_rate_relative_change']}, {onset['late_shift_rate_relative_change']}, {onset['shift_late_oos_concentration_relative_change']}
- Max-7d shifted-grid early/late rate changes and late concentration change: {short['early_shift_rate_relative_change']}, {short['late_shift_rate_relative_change']}, {short['shift_late_oos_concentration_relative_change']}
- Shifted-grid hotspot-area Jaccard diagnostics: {onset['shift_hotspot_area_jaccard_diagnostic']}, {short['shift_hotspot_area_jaccard_diagnostic']}

## Interpretation and limits

This is descriptive outage localization and customer-impact measurement, not root-cause,
causal, or predictive evidence. Formal incidents supply the outage measure and therefore
cannot independently validate outage truth. The current CUSTOMER_V2 snapshot cannot prove
historical membership before `LOCATION_START_TIME`; each denominator also requires an
unexpired plan. The 0.01-degree cells are approximate and are not equal-area geography.
Carry-in incidents and intervals longer than seven days materially inflate the descriptive
outage level, so they are not used to establish temporal transfer. Exact hotspot footprints
remain boundary-sensitive even when aggregate localization transfers across time.

ACS parameter evaluation is {'authorized as the next feasibility stage but is not completed here' if decision == 'GO_FOR_ACS' else 'still deferred because at least one frozen geographic feasibility condition failed'}.
"""
        assert_private_report(report)
        (output / "report.md").write_text(report)
        return 0 if decision == "GO_FOR_ACS" else 3
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
