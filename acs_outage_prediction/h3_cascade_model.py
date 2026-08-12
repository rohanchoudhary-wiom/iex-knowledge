from __future__ import annotations

import argparse
import json
import platform
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, precision_score, recall_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from acs_outage_feasibility import calibration_error, connect_snowflake, file_hash, native, sql_hash
from full_fleet_ping_outage_h3 import frame_sql as mapping_sql


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs" / "h3_cascade_model_2026-08-12_v2"
DEFAULT_START = datetime(2026, 7, 1)
DEFAULT_TRAIN_END = datetime(2026, 7, 23)
DEFAULT_VALIDATION_END = datetime(2026, 8, 2)
DEFAULT_END = datetime(2026, 8, 11)
DEFAULT_OBSERVATION_END = datetime(2026, 8, 12)
RANDOM_SEED = 20260812

NON_OPTICAL_FEATURES = (
    "early_miss_share",
    "current_outage_share",
    "early_share_delta_15m",
    "outage_share_delta_15m",
    "neighbor_early_share",
    "neighbor_outage_share",
    "neighbor_max_early_share",
    "log1p_eligible_devices",
    "hour_sin",
    "hour_cos",
)
OPTICAL_FEATURES = (
    "optical_median_6h",
    "optical_cell_median_shift_vs_prior6h",
    "optical_oor_share_6h",
    "optical_spread_median_6h",
    "optical_valid_hour_share_6h",
)
FEATURES = NON_OPTICAL_FEATURES + OPTICAL_FEATURES


def roster_sql(start: datetime, end: datetime, observation_end: datetime) -> str:
    prefix = mapping_sql(start, end, observation_end).split("eligible_events AS (", 1)[0]
    return "CREATE OR REPLACE TEMP TABLE CASCADE_ROSTER AS\n" + prefix + """
cell_population AS (
  SELECT h3_cell_id, COUNT(*) AS population_devices
  FROM july_service_devices
  GROUP BY h3_cell_id
  HAVING COUNT(*) >= 20
)
SELECT d.*
FROM july_service_devices d
JOIN cell_population p USING (h3_cell_id)
"""


def ping_hours_sql(start: datetime, end: datetime) -> str:
    scan_start = (start - timedelta(hours=13)).isoformat(sep=" ")
    scan_end = (end + timedelta(hours=1)).isoformat(sep=" ")
    return f"""
CREATE OR REPLACE TEMP TABLE CASCADE_PING_HOURS AS
WITH normalized AS (
  SELECT
    UPPER(TRIM(h.device_id)) AS device_id,
    h.hour_start_ist,
    CASE
      WHEN h.hour_end_ist = DATEADD(hour, 1, h.hour_start_ist)
        THEN DATEADD(hour, 1, h.hour_start_ist)
      WHEN DATE_PART(hour, h.hour_start_ist) = 23
       AND h.hour_end_ist = DATE_TRUNC(day, h.hour_start_ist)
        THEN DATEADD(hour, 1, h.hour_start_ist)
    END AS effective_end_ist,
    h.total_pings_received,
    h.total_pings_missed,
    h.continuous_missed_ping_instances,
    h.max_pings_missed_in_continuous_instance,
    h.first_ping_ts_ist,
    h.last_ping_ts_ist,
    h.ping_bitmap,
    h.optical_min,
    h.optical_avg,
    h.optical_max,
    HASH(
      h.nas_id, h.total_pings_received, h.total_pings_missed,
      h.continuous_missed_ping_instances,
      h.max_pings_missed_in_continuous_instance,
      h.first_ping_ts_ist, h.last_ping_ts_ist, h.ping_bitmap,
      h.optical_min, h.optical_avg, h.optical_max
    ) AS value_hash
  FROM PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX h
  JOIN CASCADE_ROSTER r ON r.device_id = UPPER(TRIM(h.device_id))
  WHERE h.hour_start_ist >= '{scan_start}'::TIMESTAMP_NTZ
    AND h.hour_start_ist < '{scan_end}'::TIMESTAMP_NTZ
    AND h.inserted_at IS NOT NULL
),
quality AS (
  SELECT
    device_id,
    hour_start_ist,
    COUNT(DISTINCT IFF(effective_end_ist IS NOT NULL, value_hash, NULL)) AS variants,
    COUNT_IF(
      effective_end_ist IS NULL
      OR total_pings_received IS NULL OR total_pings_received < 0
      OR (total_pings_received = 0 AND (first_ping_ts_ist IS NOT NULL OR last_ping_ts_ist IS NOT NULL))
      OR (
        total_pings_received > 0 AND (
          first_ping_ts_ist IS NULL OR last_ping_ts_ist IS NULL
          OR first_ping_ts_ist < hour_start_ist OR first_ping_ts_ist >= effective_end_ist
          OR last_ping_ts_ist < first_ping_ts_ist OR last_ping_ts_ist >= effective_end_ist
        )
      )
      OR ping_bitmap IS NULL OR LENGTH(ping_bitmap) <> 12
      OR REGEXP_LIKE(ping_bitmap, '[^01]')
      OR REGEXP_COUNT(ping_bitmap, '1') <> total_pings_received
    ) AS invalid_rows,
    MIN(ping_bitmap) AS bitmap,
    MIN(IFF(
      optical_min >= -50 AND optical_max < 0
      AND optical_min <= optical_avg AND optical_avg <= optical_max,
      optical_avg, NULL
    )) AS valid_optical_avg,
    MIN(IFF(
      optical_min >= -50 AND optical_max < 0
      AND optical_min <= optical_avg AND optical_avg <= optical_max,
      optical_max - optical_min, NULL
    )) AS valid_optical_spread
  FROM normalized
  GROUP BY device_id, hour_start_ist
)
SELECT
  device_id,
  hour_start_ist,
  IFF(variants = 1 AND invalid_rows = 0, bitmap, NULL) AS bitmap,
  IFF(variants = 1 AND invalid_rows = 0, 0, 1) AS bad_hour,
  IFF(variants = 1 AND invalid_rows = 0, valid_optical_avg, NULL) AS valid_optical_avg,
  IFF(variants = 1 AND invalid_rows = 0, valid_optical_spread, NULL) AS valid_optical_spread
FROM quality
"""


def day_frame_sql(day: datetime) -> str:
    day_end = day + timedelta(days=1)
    grid_start = day - timedelta(hours=3)
    return f"""
WITH hour_grid AS (
  SELECT DATEADD(hour, ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1,
                 '{grid_start.isoformat(sep=' ')}'::TIMESTAMP_NTZ) AS hour_start_ist
  FROM TABLE(GENERATOR(ROWCOUNT => 29))
),
device_hours AS (
  SELECT
    r.device_id,
    r.h3_cell_id,
    r.location_start_time,
    r.plan_expiry_time,
    g.hour_start_ist,
    COALESCE(h.bitmap, '000000000000') AS bitmap,
    COALESCE(h.bad_hour, 0) AS bad_hour
  FROM CASCADE_ROSTER r
  CROSS JOIN hour_grid g
  LEFT JOIN CASCADE_PING_HOURS h
    ON h.device_id = r.device_id AND h.hour_start_ist = g.hour_start_ist
),
hour_lag AS (
  SELECT
    *,
    LAG(bitmap) OVER (PARTITION BY device_id ORDER BY hour_start_ist) AS previous_bitmap,
    LAG(bad_hour) OVER (PARTITION BY device_id ORDER BY hour_start_ist) AS previous_bad
  FROM device_hours
),
quarter_state AS (
  SELECT
    h.device_id,
    h.h3_cell_id,
    h.location_start_time,
    h.plan_expiry_time,
    DATEADD(minute, 15 * (q.i + 1), h.hour_start_ist) AS anchor_ist,
    IFF(
      SUBSTR(h.bitmap, 3 * q.i + 1, 3) = '000'
      AND SUBSTR(h.previous_bitmap || h.bitmap, 3 * q.i + 4, 12) <> '000000000000',
      1, 0
    ) AS early_miss_15m,
    IFF(
      SUBSTR(h.previous_bitmap || h.bitmap, 3 * q.i + 4, 12) = '000000000000',
      1, 0
    ) AS outage_60m,
    IFF(q.i = 3, h.bad_hour, GREATEST(h.previous_bad, h.bad_hour)) AS state_bad
  FROM hour_lag h
  CROSS JOIN (SELECT column1::INTEGER AS i FROM VALUES (0), (1), (2), (3)) q
  WHERE h.previous_bitmap IS NOT NULL
),
cell_state_base AS (
  SELECT
    q.h3_cell_id,
    q.anchor_ist,
    COUNT(*) AS eligible_devices,
    COUNT_IF(q.early_miss_15m = 1) AS early_miss_devices,
    COUNT_IF(q.outage_60m = 1) AS outage_devices,
    COUNT_IF(q.state_bad = 1) AS bad_devices
  FROM quarter_state q
  WHERE q.anchor_ist >= DATEADD(hour, -1, '{day.isoformat(sep=' ')}'::TIMESTAMP_NTZ)
    AND q.anchor_ist < '{day_end.isoformat(sep=' ')}'::TIMESTAMP_NTZ
    AND q.location_start_time <= DATEADD(hour, -12, DATE_TRUNC(hour, q.anchor_ist))
    AND q.plan_expiry_time >= DATEADD(hour, 1, q.anchor_ist)
  GROUP BY q.h3_cell_id, q.anchor_ist
  HAVING COUNT(*) >= 20
),
cell_state AS (
  SELECT
    *,
    early_miss_devices / eligible_devices::FLOAT AS early_miss_share,
    outage_devices / eligible_devices::FLOAT AS current_outage_share,
    LAG(anchor_ist) OVER (
      PARTITION BY h3_cell_id ORDER BY anchor_ist
    ) AS previous_anchor_ist,
    LAG(early_miss_devices / eligible_devices::FLOAT) OVER (
      PARTITION BY h3_cell_id ORDER BY anchor_ist
    ) AS previous_early_share,
    LAG(outage_devices / eligible_devices::FLOAT) OVER (
      PARTITION BY h3_cell_id ORDER BY anchor_ist
    ) AS previous_outage_share
  FROM cell_state_base
),
trigger_flags AS (
  SELECT
    *,
    IFF(
      bad_devices = 0
      AND early_miss_devices >= CEIL(0.10 * eligible_devices)
      AND outage_devices < CEIL(0.10 * eligible_devices),
      1, 0
    ) AS raw_trigger
  FROM cell_state
),
trigger_episodes AS (
  SELECT t.*
  FROM trigger_flags t
  WHERE t.raw_trigger = 1
    AND t.anchor_ist >= '{day.isoformat(sep=' ')}'::TIMESTAMP_NTZ
    AND t.anchor_ist < '{day_end.isoformat(sep=' ')}'::TIMESTAMP_NTZ
    AND NOT EXISTS (
      SELECT 1
      FROM trigger_flags p
      WHERE p.h3_cell_id = t.h3_cell_id
        AND p.raw_trigger = 1
        AND p.anchor_ist >= DATEADD(minute, -60, t.anchor_ist)
        AND p.anchor_ist < t.anchor_ist
    )
),
neighbor_cells AS (
  SELECT t.h3_cell_id, t.anchor_ist, f.value::VARCHAR AS neighbor_h3_cell_id
  FROM trigger_episodes t,
  LATERAL FLATTEN(INPUT => H3_GRID_DISK(t.h3_cell_id, 1)) f
  WHERE f.value::VARCHAR <> t.h3_cell_id
),
neighbor_features AS (
  SELECT
    n.h3_cell_id,
    n.anchor_ist,
    SUM(s.early_miss_devices) / NULLIF(SUM(s.eligible_devices), 0)::FLOAT AS neighbor_early_share,
    SUM(s.outage_devices) / NULLIF(SUM(s.eligible_devices), 0)::FLOAT AS neighbor_outage_share,
    MAX(s.early_miss_share) AS neighbor_max_early_share
  FROM neighbor_cells n
  JOIN cell_state s
    ON s.h3_cell_id = n.neighbor_h3_cell_id
   AND s.anchor_ist = n.anchor_ist
   AND s.bad_devices = 0
  GROUP BY n.h3_cell_id, n.anchor_ist
),
trigger_devices AS (
  SELECT t.h3_cell_id, t.anchor_ist, t.eligible_devices, q.device_id
  FROM trigger_episodes t
  JOIN quarter_state q USING (h3_cell_id, anchor_ist)
  WHERE q.location_start_time <= DATEADD(hour, -12, DATE_TRUNC(hour, q.anchor_ist))
    AND q.plan_expiry_time >= DATEADD(hour, 1, q.anchor_ist)
),
trigger_optical_features AS (
  SELECT
    t.h3_cell_id,
    t.anchor_ist,
    MEDIAN(IFF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist)),
      h.valid_optical_avg, NULL
    )) AS optical_median_6h,
    MEDIAN(IFF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist)),
      h.valid_optical_avg, NULL
    )) - MEDIAN(IFF(
      h.hour_start_ist < DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist)),
      h.valid_optical_avg, NULL
    )) AS optical_cell_median_shift_vs_prior6h,
    COUNT_IF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist))
      AND (h.valid_optical_avg < -25 OR h.valid_optical_avg > -8)
    ) / NULLIF(COUNT_IF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist))
      AND h.valid_optical_avg IS NOT NULL
    ), 0)::FLOAT AS optical_oor_share_6h,
    MEDIAN(IFF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist)),
      h.valid_optical_spread, NULL
    )) AS optical_spread_median_6h,
    COUNT_IF(
      h.hour_start_ist >= DATEADD(hour, -6, DATE_TRUNC(hour, t.anchor_ist))
      AND h.valid_optical_avg IS NOT NULL
    ) / (MAX(t.eligible_devices) * 6)::FLOAT AS optical_valid_hour_share_6h
  FROM trigger_devices t
  LEFT JOIN CASCADE_PING_HOURS h
    ON h.device_id = t.device_id
   AND h.hour_start_ist >= DATEADD(hour, -12, DATE_TRUNC(hour, t.anchor_ist))
   AND h.hour_start_ist < DATE_TRUNC(hour, t.anchor_ist)
  GROUP BY t.h3_cell_id, t.anchor_ist
),
trigger_features AS (
  SELECT
    t.h3_cell_id,
    t.anchor_ist,
    t.eligible_devices,
    t.early_miss_share,
    t.current_outage_share,
    IFF(
      DATEDIFF(minute, t.previous_anchor_ist, t.anchor_ist) = 15,
      t.early_miss_share - t.previous_early_share, NULL
    ) AS early_share_delta_15m,
    IFF(
      DATEDIFF(minute, t.previous_anchor_ist, t.anchor_ist) = 15,
      t.current_outage_share - t.previous_outage_share, NULL
    ) AS outage_share_delta_15m,
    n.neighbor_early_share,
    n.neighbor_outage_share,
    n.neighbor_max_early_share,
    LN(1 + t.eligible_devices) AS log1p_eligible_devices,
    SIN(2 * PI() * DATE_PART(hour, t.anchor_ist) / 24) AS hour_sin,
    COS(2 * PI() * DATE_PART(hour, t.anchor_ist) / 24) AS hour_cos,
    o.optical_median_6h,
    o.optical_cell_median_shift_vs_prior6h,
    o.optical_oor_share_6h,
    o.optical_spread_median_6h,
    o.optical_valid_hour_share_6h
  FROM trigger_episodes t
  LEFT JOIN neighbor_features n USING (h3_cell_id, anchor_ist)
  LEFT JOIN trigger_optical_features o USING (h3_cell_id, anchor_ist)
),
trigger_bit_windows AS (
  SELECT
    t.*,
    p.bitmap || c.bitmap || n.bitmap AS bitmap_3h,
    GREATEST(p.bad_hour, c.bad_hour, n.bad_hour) AS target_bad
  FROM trigger_devices t
  JOIN device_hours p
    ON p.device_id = t.device_id
   AND p.hour_start_ist = DATEADD(hour, -1, DATE_TRUNC(hour, t.anchor_ist))
  JOIN device_hours c
    ON c.device_id = t.device_id
   AND c.hour_start_ist = DATE_TRUNC(hour, t.anchor_ist)
  JOIN device_hours n
    ON n.device_id = t.device_id
   AND n.hour_start_ist = DATEADD(hour, 1, DATE_TRUNC(hour, t.anchor_ist))
),
future_checkpoints AS (
  SELECT
    t.h3_cell_id,
    t.anchor_ist,
    t.eligible_devices,
    k.i AS future_slot,
    COUNT(*) AS observed_devices,
    COUNT_IF(t.target_bad = 1) AS bad_devices,
    COUNT_IF(
      SUBSTR(t.bitmap_3h, 1 + FLOOR(DATE_PART(minute, t.anchor_ist) / 5) + k.i, 12)
        = '000000000000'
    ) AS future_outage_devices
  FROM trigger_bit_windows t
  CROSS JOIN (
    SELECT column1::INTEGER AS i
    FROM VALUES (1), (2), (3), (4), (5), (6), (7), (8), (9), (10), (11), (12)
  ) k
  GROUP BY t.h3_cell_id, t.anchor_ist, t.eligible_devices, k.i
),
labels AS (
  SELECT
    h3_cell_id,
    anchor_ist,
    MAX(IFF(future_outage_devices >= CEIL(0.70 * eligible_devices), 1, 0)) AS cascade_next_60m,
    MIN(IFF(future_outage_devices >= CEIL(0.70 * eligible_devices), future_slot * 5, NULL))
      AS warning_minutes
  FROM future_checkpoints
  GROUP BY h3_cell_id, anchor_ist, eligible_devices
  HAVING COUNT(*) = 12
    AND MIN(observed_devices) = eligible_devices
    AND MAX(observed_devices) = eligible_devices
    AND MAX(bad_devices) = 0
)
SELECT
  f.h3_cell_id,
  f.anchor_ist,
  l.cascade_next_60m,
  l.warning_minutes,
  f.eligible_devices,
  f.early_miss_share,
  f.current_outage_share,
  f.early_share_delta_15m,
  f.outage_share_delta_15m,
  f.neighbor_early_share,
  f.neighbor_outage_share,
  f.neighbor_max_early_share,
  f.log1p_eligible_devices,
  f.hour_sin,
  f.hour_cos,
  f.optical_median_6h,
  f.optical_cell_median_shift_vs_prior6h,
  f.optical_oor_share_6h,
  f.optical_spread_median_6h,
  f.optical_valid_hour_share_6h
FROM trigger_features f
JOIN labels l USING (h3_cell_id, anchor_ist)
ORDER BY f.anchor_ist, f.h3_cell_id
"""


def logistic_pipeline(features: tuple[str, ...]) -> Pipeline:
    return Pipeline(
        [
            (
                "prepare",
                ColumnTransformer(
                    [("numeric", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), list(features))],
                    remainder="drop",
                ),
            ),
            ("model", LogisticRegression(C=1.0, max_iter=1_000, random_state=RANDOM_SEED)),
        ]
    )


def threshold_for_recall(y: np.ndarray, scores: np.ndarray, minimum_recall: float = 0.90) -> float:
    candidates = np.unique(scores)
    eligible = [value for value in candidates if recall_score(y, scores >= value, zero_division=0) >= minimum_recall]
    return float(max(eligible)) if eligible else 0.0


def model_metrics(y: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, float | int]:
    alerts = scores >= threshold
    false_alerts = int(np.sum(alerts & (y == 0)))
    negatives = int(np.sum(y == 0))
    return {
        "rows": int(len(y)),
        "positives": int(y.sum()),
        "prevalence": float(y.mean()),
        "pr_auc": float(average_precision_score(y, scores)),
        "brier": float(brier_score_loss(y, scores)),
        "ece_10bin": float(calibration_error(y, scores)),
        "alert_rate": float(alerts.mean()),
        "precision": float(precision_score(y, alerts, zero_division=0)),
        "recall": float(recall_score(y, alerts, zero_division=0)),
        "false_alerts": false_alerts,
        "false_alert_reduction_vs_rule": float(1 - false_alerts / negatives) if negatives else 0.0,
    }


def day_bootstrap(
    frame: pd.DataFrame,
    scores: np.ndarray,
    baseline_scores: np.ndarray,
    replicates: int,
) -> dict[str, float | int | None]:
    days = frame["anchor_ist"].dt.date.to_numpy()
    unique_days = np.unique(days)
    rng = np.random.default_rng(RANDOM_SEED)
    y = frame["cascade_next_60m"].to_numpy(dtype=int)
    baseline = np.asarray(baseline_scores, dtype=float)
    if baseline.shape != scores.shape:
        raise ValueError("baseline scores must match model scores")
    ap_deltas: list[float] = []
    brier_deltas: list[float] = []
    for _ in range(replicates):
        sampled = rng.choice(unique_days, len(unique_days), replace=True)
        positions = np.concatenate([np.flatnonzero(days == day) for day in sampled])
        sampled_y = y[positions]
        if sampled_y.min() == sampled_y.max():
            continue
        ap_deltas.append(
            float(average_precision_score(sampled_y, scores[positions]) - average_precision_score(sampled_y, baseline[positions]))
        )
        brier_deltas.append(
            float(brier_score_loss(sampled_y, scores[positions]) - brier_score_loss(sampled_y, baseline[positions]))
        )
    return {
        "requested_replicates": replicates,
        "valid_replicates": len(ap_deltas),
        "pr_auc_delta": float(average_precision_score(y, scores) - average_precision_score(y, baseline)),
        "pr_auc_ci_low": float(np.percentile(ap_deltas, 2.5)) if ap_deltas else None,
        "pr_auc_ci_high": float(np.percentile(ap_deltas, 97.5)) if ap_deltas else None,
        "brier_delta": float(brier_score_loss(y, scores) - brier_score_loss(y, baseline)),
        "brier_ci_low": float(np.percentile(brier_deltas, 2.5)) if brier_deltas else None,
        "brier_ci_high": float(np.percentile(brier_deltas, 97.5)) if brier_deltas else None,
    }


def self_check() -> None:
    assert len(NON_OPTICAL_FEATURES) == 10
    assert len(OPTICAL_FEATURES) == 5
    assert len(FEATURES) == 15 and len(set(FEATURES)) == 15
    y = np.array([1, 0, 1, 0])
    scores = np.array([0.9, 0.8, 0.7, 0.1])
    threshold = threshold_for_recall(y, scores, 0.5)
    assert threshold == 0.9 and recall_score(y, scores >= threshold) == 0.5
    previous = "111111111111"
    current = "000000000000"
    assert (previous + current)[12:24] == "000000000000"
    assert {minute: 1 + minute // 5 + 1 for minute in (0, 15, 30, 45)} == {
        0: 2, 15: 5, 30: 8, 45: 11
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the retrospective H3 outage-cascade logistic pilot")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-replicates", type=int, default=1_000)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.bootstrap_replicates < 20:
        parser.error("bootstrap replicates must be at least 20")
    return args


def main() -> int:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    roster = roster_sql(DEFAULT_START, DEFAULT_END, DEFAULT_OBSERVATION_END)
    ping_hours = ping_hours_sql(DEFAULT_START, DEFAULT_END)

    connection = connect_snowflake(network_timeout=1_800)
    cursor = connection.cursor()
    batches: list[pd.DataFrame] = []
    day_query_ids: dict[str, str] = {}
    try:
        cursor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1800")
        cursor.execute("ALTER SESSION SET QUERY_TAG = 'h3_cascade_model_v2_retrospective'")
        cursor.execute(roster)
        roster_query_id = str(cursor.sfqid)
        cursor.execute(ping_hours)
        ping_hours_query_id = str(cursor.sfqid)
        cursor.execute(
            "SELECT COUNT(DISTINCT h3_cell_id), COUNT(*) FROM CASCADE_ROSTER"
        )
        population_cells, population_devices = map(int, cursor.fetchone())
        day = DEFAULT_START
        columns: list[str] = []
        while day < DEFAULT_END:
            daily_sql = day_frame_sql(day)
            cursor.execute(daily_sql)
            day_query_ids[f"{day:%Y-%m-%d}"] = str(cursor.sfqid)
            columns = [str(item[0]).lower() for item in cursor.description]
            rows = cursor.fetchall()
            if rows:
                batches.append(pd.DataFrame.from_records(rows, columns=columns))
            print(f"{day:%Y-%m-%d}: {len(rows):,} trigger episodes", flush=True)
            day += timedelta(days=1)
    finally:
        cursor.close()
        connection.close()
    frame = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame(columns=columns)
    if frame.empty:
        raise RuntimeError("No valid trigger episodes were returned")
    prohibited = {"device_id", "nasid", "account_id", "latitude", "longitude", "device_key"}
    if prohibited.intersection(frame.columns):
        raise RuntimeError("Model frame exposed a prohibited identity field")
    frame["anchor_ist"] = pd.to_datetime(frame["anchor_ist"])
    for column in (*FEATURES, "cascade_next_60m", "warning_minutes"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    anchor = frame["anchor_ist"]
    frame["split"] = np.select(
        [
            (anchor >= DEFAULT_START + timedelta(hours=1))
            & (anchor < DEFAULT_TRAIN_END - timedelta(hours=1)),
            (anchor >= DEFAULT_TRAIN_END + timedelta(hours=1))
            & (anchor < DEFAULT_VALIDATION_END - timedelta(hours=1)),
            (anchor >= DEFAULT_VALIDATION_END + timedelta(hours=1))
            & (anchor < DEFAULT_END - timedelta(hours=1)),
        ],
        ["train", "validation", "test"],
        default="purged",
    )
    frame = frame[~frame["split"].eq("purged")].copy()
    if set(frame["split"]) != {"train", "validation", "test"}:
        raise RuntimeError("All chronological splits are required")
    for split, part in frame.groupby("split"):
        if part["cascade_next_60m"].nunique() != 2:
            raise RuntimeError(f"{split} lacks both cascade classes")

    train = frame[frame["split"].eq("train")]
    validation = frame[frame["split"].eq("validation")]
    test = frame[frame["split"].eq("test")]
    pipeline = logistic_pipeline(FEATURES).fit(train, train["cascade_next_60m"].astype(int))
    non_optical_pipeline = logistic_pipeline(NON_OPTICAL_FEATURES).fit(
        train, train["cascade_next_60m"].astype(int)
    )
    validation_scores = pipeline.predict_proba(validation)[:, 1]
    non_optical_validation_scores = non_optical_pipeline.predict_proba(validation)[:, 1]
    validation_y = validation["cascade_next_60m"].to_numpy(dtype=int)
    threshold = threshold_for_recall(validation_y, validation_scores)
    non_optical_threshold = threshold_for_recall(
        validation_y, non_optical_validation_scores
    )
    test_scores = pipeline.predict_proba(test)[:, 1]
    non_optical_test_scores = non_optical_pipeline.predict_proba(test)[:, 1]
    y_test = test["cascade_next_60m"].to_numpy(dtype=int)
    baseline_probability = float(train["cascade_next_60m"].mean())
    test_metrics = model_metrics(y_test, test_scores, threshold)
    non_optical_test_metrics = model_metrics(
        y_test, non_optical_test_scores, non_optical_threshold
    )
    rule_metrics = {
        "rows": len(test),
        "positives": int(y_test.sum()),
        "precision": float(y_test.mean()),
        "recall": 1.0,
        "alert_rate": 1.0,
        "false_alerts": int(np.sum(y_test == 0)),
    }
    bootstrap = day_bootstrap(
        test,
        test_scores,
        np.full(len(test_scores), baseline_probability),
        args.bootstrap_replicates,
    )
    optical_bootstrap = day_bootstrap(
        test,
        test_scores,
        non_optical_test_scores,
        args.bootstrap_replicates,
    )
    coefficients = pd.DataFrame(
        {
            "feature": FEATURES,
            "standardized_coefficient": pipeline.named_steps["model"].coef_[0],
        }
    ).sort_values("standardized_coefficient", key=abs, ascending=False)
    split_summary = {
        split: {
            "rows": len(part),
            "positive_cascades": int(part["cascade_next_60m"].sum()),
            "prevalence": float(part["cascade_next_60m"].mean()),
            "cells": int(part["h3_cell_id"].nunique()),
            "days": int(part["anchor_ist"].dt.date.nunique()),
            "first_anchor": part["anchor_ist"].min(),
            "last_anchor": part["anchor_ist"].max(),
        }
        for split, part in frame.groupby("split", sort=False)
    }
    support_passed = bool(
        split_summary["train"]["positive_cascades"] >= 20 * len(FEATURES)
        and split_summary["test"]["positive_cascades"] >= 100
        and split_summary["test"]["days"] >= 20
    )
    performance_passed = bool(
        bootstrap["pr_auc_ci_low"] is not None
        and bootstrap["pr_auc_ci_low"] > 0
        and bootstrap["brier_ci_high"] is not None
        and bootstrap["brier_ci_high"] < 0
        and test_metrics["recall"] >= 0.90
        and test_metrics["false_alert_reduction_vs_rule"] >= 0.25
    )
    optical_point_estimate_gate_passed = bool(
        optical_bootstrap["pr_auc_ci_low"] is not None
        and optical_bootstrap["pr_auc_ci_low"] > 0
        and optical_bootstrap["brier_ci_high"] is not None
        and optical_bootstrap["brier_ci_high"] < 0
    )
    optical_incremental_passed = bool(
        support_passed and optical_point_estimate_gate_passed
    )
    decision = (
        "RETROSPECTIVE_CASCADE_RANKING_SIGNAL_ONLY"
        if support_passed and performance_passed
        else "CASCADE_MODEL_NOT_SUPPORTED"
    )
    audit: dict[str, object] = {
        "status": "CORRECTED_RETROSPECTIVE_DEVELOPMENT_SENSITIVITY_ONLY",
        "decision": decision,
        "production_blocker": (
            "HOURLY_DEVICE_PING_INFLUX final bitmaps arrive about 61 minutes after hour end; "
            "a live/raw per-ping source is required for a 15-minute trigger"
        ),
        "target": (
            "first H3-9 trigger episode with >=10% early 15-minute misses and <10% current "
            "strict outages; positive if >=70% of the frozen same-cell denominator is "
            "simultaneously in strict 60-minute outage within the next 60 minutes"
        ),
        "features": {
            "non_optical": list(NON_OPTICAL_FEATURES),
            "optical": list(OPTICAL_FEATURES),
        },
        "window": {
            "start_ist": DEFAULT_START,
            "train_end_ist": DEFAULT_TRAIN_END,
            "validation_end_ist": DEFAULT_VALIDATION_END,
            "end_ist": DEFAULT_END,
            "observation_end_ist": DEFAULT_OBSERVATION_END,
            "boundary_purge_hours_each_side": 1,
        },
        "population": {
            "minimum_cell_devices": 20,
            "cells": population_cells,
            "devices": population_devices,
        },
        "frame": {"rows": len(frame), "splits": split_summary},
        "model": {
            "type": "L2-regularized logistic regression",
            "validation_threshold_for_90pct_recall": threshold,
            "train_climatology": baseline_probability,
            "combined_test": test_metrics,
            "non_optical_validation_threshold_for_90pct_recall": non_optical_threshold,
            "non_optical_test": non_optical_test_metrics,
            "test_rule_baseline": rule_metrics,
            "day_cluster_bootstrap_vs_climatology": bootstrap,
            "day_cluster_bootstrap_combined_vs_non_optical": optical_bootstrap,
        },
        "gates": {
            "support_passed": support_passed,
            "performance_passed": performance_passed,
            "optical_incremental_assessable": support_passed,
            "optical_point_estimate_gate_passed": optical_point_estimate_gate_passed,
            "optical_incremental_passed": optical_incremental_passed,
            "production_latency_passed": False,
            "support_thresholds": {
                "train_positive_cascades": 20 * len(FEATURES),
                "test_positive_cascades": 100,
                "test_days": 20,
            },
        },
        "source_contract": {
            "absent_device_hour": (
                "interpreted as twelve missed ping opportunities for a time-valid roster device"
            ),
            "explicit_zero_ping_hour": (
                "valid only when both ping endpoint timestamps are null"
            ),
            "optical_window": (
                "six completed event-time hours versus the preceding six; final hourly "
                "values were not available live at the anchor"
            ),
            "minimum_service_history_hours": 12,
            "optical_shift_interpretation": (
                "difference between pooled cell-device-hour medians, not paired within-device change"
            ),
        },
        "limitations": [
            "The trigger, label, and optical summaries use final event-time rows, not values available at the simulated anchor.",
            "The customer and device mapping is a current-state snapshot with service-time bounds.",
            "Day-cluster bootstrap preserves within-day dependence but does not merge adjacent-cell incidents.",
            "The model is conditional on a 10% trigger; coverage of all 70% cascades is not established.",
            "The corrected feature set reused dates exposed by v1, so this is development sensitivity rather than untouched confirmation.",
        ],
        "query_ids": {
            "temporary_roster": roster_query_id,
            "temporary_ping_hours": ping_hours_query_id,
            "daily_frames": day_query_ids,
        },
        "sql_sha256": {
            "temporary_roster": sql_hash(roster),
            "temporary_ping_hours": sql_hash(ping_hours),
            "daily_frame_template": sql_hash(day_frame_sql(DEFAULT_START)),
        },
        "source_writes_attempted": False,
        "warehouse_computation": "session-scoped temporary tables only; automatically dropped on disconnect",
        "privacy_check": "passed; trigger-level H3/time frame was not written",
        "software_versions": {
            "python": platform.python_version(),
            "numpy": metadata.version("numpy"),
            "pandas": metadata.version("pandas"),
            "scikit_learn": metadata.version("scikit-learn"),
        },
    }
    coefficients.to_csv(output / "coefficients.csv", index=False)
    (output / "audit.json").write_text(json.dumps(native(audit), indent=2) + "\n")
    report = f"""# H3 outage-cascade logistic pilot

## Decision

**{decision}** — retrospective event-time simulation only.

The frame contains {len(frame):,} first-trigger episodes across {population_cells:,} H3-9 cells and {population_devices:,} mapped devices. The model has 10 ping/spatial/time features plus 5 optical features. Test prevalence was {float(test_metrics['prevalence']):.3%} ({int(test_metrics['positives']):,}/{int(test_metrics['rows']):,}).

## Chronological development test

- Alert-all 10% rule: precision {float(rule_metrics['precision']):.2%}, recall 100%, {int(rule_metrics['false_alerts']):,} false alerts.
- Combined 15-feature model: PR-AUC {float(test_metrics['pr_auc']):.4f}, precision {float(test_metrics['precision']):.2%}, recall {float(test_metrics['recall']):.2%}, false-alert reduction {float(test_metrics['false_alert_reduction_vs_rule']):.2%}.
- Non-optical 10-feature model: PR-AUC {float(non_optical_test_metrics['pr_auc']):.4f}, precision {float(non_optical_test_metrics['precision']):.2%}, recall {float(non_optical_test_metrics['recall']):.2%}.
- Model minus climatology PR-AUC: {float(bootstrap['pr_auc_delta']):+.4f} (day-bootstrap 95% CI {bootstrap['pr_auc_ci_low']} to {bootstrap['pr_auc_ci_high']}).
- Model minus climatology Brier: {float(bootstrap['brier_delta']):+.6f} (95% CI {bootstrap['brier_ci_low']} to {bootstrap['brier_ci_high']}; lower is better).
- Optical incremental PR-AUC: {float(optical_bootstrap['pr_auc_delta']):+.4f} (95% CI {optical_bootstrap['pr_auc_ci_low']} to {optical_bootstrap['pr_auc_ci_high']}).

## Gates

- Support: {'passed' if support_passed else 'failed'} — training has {split_summary['train']['positive_cascades']:,}/{20 * len(FEATURES):,} required positive cascades; test has {split_summary['test']['positive_cascades']:,}/100 across {split_summary['test']['days']:,}/20 required days.
- Chronological test-period performance: {'passed' if performance_passed else 'failed'} — requires positive PR-AUC and Brier confidence bounds, at least 90% recall, and at least 25% fewer false alerts.
- Incremental optical contribution: {'passed' if optical_incremental_passed else 'not assessable because support failed' if not support_passed else 'not demonstrated'}.
- Production latency: failed.

## Hard limit

`HOURLY_DEVICE_PING_INFLUX` arrives roughly 61 minutes after hour-end, so this cannot power a live 15-minute warning. A raw per-ping stream with arrival timestamps is required before production validation. Missing device-hour rows are interpreted as twelve missed opportunities for a device with at least 12 hours of service tenure. V1 exposed the same dates before this corrected feature set, so this is not an untouched confirmation. The result is conditional on the 10% trigger and does not establish coverage of all 70% cascades.
"""
    (output / "report.md").write_text(report)
    audit["artifact_sha256"] = {
        path.name: file_hash(path) for path in sorted(output.iterdir()) if path.name != "audit.json"
    }
    (output / "audit.json").write_text(json.dumps(native(audit), indent=2) + "\n")
    print(f"{decision}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
