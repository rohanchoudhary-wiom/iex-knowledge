from __future__ import annotations

import argparse
import math
import platform
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, precision_score, recall_score

from acs_outage_feasibility import (
    assert_private_frame,
    calibration_error,
    connect_snowflake,
    file_hash,
    logistic_pipeline,
    native,
    sql_hash,
    write_json,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs" / "optical_outage_model_2026-08-12_v1"
DEFAULT_START = pd.Timestamp("2026-06-30 00:00:00")
DEFAULT_END = pd.Timestamp("2026-08-11 00:00:00")
RANDOM_SEED = 20260812
ALERT_BUDGET = 0.05

TIME_FEATURES = ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "elapsed_days"]
NON_OPTICAL_FEATURES = [
    "telemetry_hour_share_6h",
    "ping_success_share_6h",
    "ping_success_delta_vs_prior7d",
    "log1p_max_contiguous_misses_6h",
    "log1p_prior7d_outages",
]
OPTICAL_FEATURES = [
    "optical_median_6h",
    "optical_delta_vs_prior7d",
    "optical_oor_share_6h",
    "optical_spread_median_6h",
    "optical_valid_hour_share_6h",
]
ALL_FEATURES = TIME_FEATURES + NON_OPTICAL_FEATURES + OPTICAL_FEATURES
MODEL_FEATURES = {
    "time_only": TIME_FEATURES,
    "non_optical": TIME_FEATURES + NON_OPTICAL_FEATURES,
    "optical": TIME_FEATURES + OPTICAL_FEATURES,
    "combined": ALL_FEATURES,
}


def frame_sql(start: pd.Timestamp, end: pd.Timestamp, sample_modulus: int) -> str:
    hours = int((end - start).total_seconds() // 3600)
    if hours <= 0 or hours % 18:
        raise ValueError("The analysis interval must be a positive whole number of 18-hour blocks")
    anchors = hours // 3
    train_end = start + (end - start) * (4 / 6)
    validation_end = start + (end - start) * (5 / 6)
    values = {
        "start": start.isoformat(sep=" "),
        "end": end.isoformat(sep=" "),
        "train_end": train_end.isoformat(sep=" "),
        "validation_end": validation_end.isoformat(sep=" "),
    }
    return f"""
WITH params AS (
    SELECT
      '{values['start']}'::TIMESTAMP_NTZ AS start_ist,
      '{values['train_end']}'::TIMESTAMP_NTZ AS train_end_ist,
      '{values['validation_end']}'::TIMESTAMP_NTZ AS validation_end_ist,
      '{values['end']}'::TIMESTAMP_NTZ AS end_ist
),
normalized AS (
    SELECT
      UPPER(TRIM(device_id)) AS device_id,
      nas_id,
      hour_start_ist,
      hour_end_ist,
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
      max_pings_missed_in_continuous_instance,
      optical_min,
      optical_avg,
      optical_max,
      HASH(
        nas_id, total_pings_received, total_pings_missed,
        max_pings_missed_in_continuous_instance,
        optical_min, optical_avg, optical_max
      ) AS value_hash
    FROM PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX, params
    WHERE hour_start_ist >= DATEADD(day, -8, params.start_ist)
      AND hour_start_ist < params.end_ist
      AND inserted_at IS NOT NULL
      AND NULLIF(TRIM(device_id), '') IS NOT NULL
      AND MOD(ABS(HASH(UPPER(TRIM(device_id)))), {sample_modulus}) = 0
      AND NOT REGEXP_LIKE(LEFT(UPPER(TRIM(device_id)), 2), '^[0-9]{{2}}$')
),
key_quality AS (
    SELECT device_id, hour_start_ist, COUNT(DISTINCT value_hash) AS variants
    FROM normalized
    WHERE effective_end_ist IS NOT NULL
    GROUP BY device_id, hour_start_ist
),
hourly AS (
    SELECT
      n.*,
      IFF(
        optical_min >= -50 AND optical_max < 0
        AND optical_min <= optical_avg AND optical_avg <= optical_max,
        optical_avg, NULL
      ) AS valid_optical_avg,
      IFF(
        optical_min >= -50 AND optical_max < 0
        AND optical_min <= optical_avg AND optical_avg <= optical_max,
        optical_max - optical_min, NULL
      ) AS valid_optical_spread
    FROM normalized n
    JOIN key_quality q USING (device_id, hour_start_ist)
    WHERE q.variants = 1
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY n.device_id, n.hour_start_ist
      ORDER BY n.inserted_at, n.updated_at
    ) = 1
),
eligible_devices AS (
    SELECT device_id
    FROM hourly, params
    GROUP BY device_id, params.start_ist, params.end_ist
    HAVING MIN(hour_start_ist) < params.start_ist
       AND MAX(hour_start_ist) >= DATEADD(day, -1, params.end_ist)
),
device_keys AS (
    SELECT
      d.device_id,
      'D' || LPAD(DENSE_RANK() OVER (ORDER BY SHA2(d.device_id, 256))::VARCHAR, 6, '0') AS device_key
    FROM eligible_devices d
),
anchor_grid AS (
    SELECT DATEADD(hour, 3 * (ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1), p.start_ist) AS anchor_ist
    FROM params p, TABLE(GENERATOR(ROWCOUNT => {anchors}))
),
anchors_raw AS (
    SELECT
      d.device_id,
      d.device_key,
      g.anchor_ist,
      CASE
        WHEN g.anchor_ist < p.train_end_ist
         AND DATEADD(hour, 6, g.anchor_ist) < p.train_end_ist THEN 'train'
        WHEN g.anchor_ist >= p.train_end_ist
         AND g.anchor_ist < p.validation_end_ist
         AND DATEADD(hour, 6, g.anchor_ist) < p.validation_end_ist THEN 'validation'
        WHEN g.anchor_ist >= p.validation_end_ist
         AND g.anchor_ist < p.end_ist THEN 'test'
      END AS split
    FROM device_keys d CROSS JOIN anchor_grid g CROSS JOIN params p
),
anchors AS (
    SELECT * FROM anchors_raw WHERE split IS NOT NULL
),
active_anchors AS (
    SELECT a.device_id, a.device_key, a.anchor_ist, a.split
    FROM anchors a
    JOIN hourly h
      ON h.device_id = a.device_id
     AND h.effective_end_ist > DATEADD(hour, -24, a.anchor_ist)
     AND h.effective_end_ist <= a.anchor_ist
     AND h.inserted_at <= a.anchor_ist
     AND COALESCE(h.updated_at, h.inserted_at) <= a.anchor_ist
    GROUP BY a.device_id, a.device_key, a.anchor_ist, a.split
),
recent AS (
    SELECT
      a.device_id,
      a.device_key,
      a.anchor_ist,
      a.split,
      COUNT(h.hour_start_ist) / 6.0 AS telemetry_hour_share_6h,
      SUM(COALESCE(h.total_pings_received, 0)) / 72.0 AS ping_success_share_6h,
      LN(1 + COALESCE(MAX(h.max_pings_missed_in_continuous_instance), 0))
        AS log1p_max_contiguous_misses_6h,
      MEDIAN(h.valid_optical_avg) AS optical_median_6h,
      COUNT(h.valid_optical_avg) / 6.0 AS optical_valid_hour_share_6h,
      COUNT_IF(h.valid_optical_avg < -25 OR h.valid_optical_avg > -8)
        / NULLIF(COUNT(h.valid_optical_avg), 0)::FLOAT AS optical_oor_share_6h,
      MEDIAN(h.valid_optical_spread) AS optical_spread_median_6h
    FROM active_anchors a
    LEFT JOIN hourly h
      ON h.device_id = a.device_id
     AND h.effective_end_ist > DATEADD(hour, -6, a.anchor_ist)
     AND h.effective_end_ist <= a.anchor_ist
     AND h.inserted_at <= a.anchor_ist
     AND COALESCE(h.updated_at, h.inserted_at) <= a.anchor_ist
    GROUP BY a.device_id, a.device_key, a.anchor_ist, a.split
),
daily AS (
    SELECT
      device_id,
      CAST(hour_start_ist AS DATE) AS signal_date,
      MEDIAN(valid_optical_avg) AS optical_median,
      SUM(COALESCE(total_pings_received, 0)) AS received_pings,
      MAX(inserted_at) AS available_at,
      MAX(COALESCE(updated_at, inserted_at)) AS updated_at
    FROM hourly
    GROUP BY device_id, CAST(hour_start_ist AS DATE)
),
baseline AS (
    SELECT
      a.device_id,
      a.anchor_ist,
      MEDIAN(d.optical_median) AS prior7d_optical_median,
      IFF(
        COUNT(d.signal_date) > 0,
        SUM(COALESCE(d.received_pings, 0)) / (7 * 288.0),
        NULL
      ) AS prior7d_ping_success_share
    FROM active_anchors a
    LEFT JOIN daily d
      ON d.device_id = a.device_id
     AND d.signal_date >= DATEADD(day, -8, CAST(a.anchor_ist AS DATE))
     AND d.signal_date < DATEADD(day, -1, CAST(a.anchor_ist AS DATE))
     AND d.available_at <= a.anchor_ist
     AND d.updated_at <= a.anchor_ist
    GROUP BY a.device_id, a.anchor_ist
),
incidents_current AS (
    SELECT
      id AS incident_id,
      DATEADD(minute, 330, first_fail_timestamp) AS onset_ist,
      DATEADD(
        minute, GREATEST(COALESCE(duration_minutes, 1), 1),
        DATEADD(minute, 330, first_fail_timestamp)
      ) AS end_ist,
      DATEADD(minute, 330, created_at) AS created_at_ist
    FROM PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENTS
    WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE
      AND first_fail_timestamp IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY id ORDER BY updated_at DESC, _fivetran_synced DESC
    ) = 1
),
impacted_current AS (
    SELECT incident_id, UPPER(TRIM(device_id)) AS device_id
    FROM PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENT_IMPACTED_DEVICE
    WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE
      AND device_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY incident_id, UPPER(TRIM(device_id))
      ORDER BY updated_at DESC, _fivetran_synced DESC
    ) = 1
),
events AS (
    SELECT d.device_id, i.incident_id, i.onset_ist, i.end_ist, i.created_at_ist
    FROM incidents_current i
    JOIN impacted_current d USING (incident_id)
    JOIN device_keys k USING (device_id)
    CROSS JOIN params p
    WHERE i.onset_ist < DATEADD(hour, 6, p.end_ist)
      AND (i.end_ist > p.start_ist OR i.onset_ist >= DATEADD(day, -7, p.start_ist))
),
labels AS (
    SELECT
      a.device_id,
      a.anchor_ist,
      COUNT_IF(e.onset_ist <= a.anchor_ist AND e.end_ist > a.anchor_ist) AS active_outages,
      COUNT_IF(e.onset_ist > a.anchor_ist
            AND e.onset_ist <= DATEADD(hour, 2, a.anchor_ist)) AS guard_outages,
      IFF(COUNT_IF(e.onset_ist > DATEADD(hour, 2, a.anchor_ist)
                AND e.onset_ist <= DATEADD(hour, 6, a.anchor_ist)) > 0, 1, 0)
        AS outage_2h_6h,
      MIN(CASE
        WHEN e.onset_ist > DATEADD(hour, 2, a.anchor_ist)
         AND e.onset_ist <= DATEADD(hour, 6, a.anchor_ist)
        THEN e.onset_ist
      END) AS next_onset_ist,
      COUNT(DISTINCT CASE
        WHEN e.onset_ist >= DATEADD(day, -7, a.anchor_ist)
         AND e.onset_ist < a.anchor_ist
         AND e.created_at_ist <= a.anchor_ist
        THEN e.incident_id
      END) AS prior7d_outages
    FROM active_anchors a
    LEFT JOIN events e
      ON e.device_id = a.device_id
     AND e.onset_ist <= DATEADD(hour, 6, a.anchor_ist)
     AND (e.end_ist > a.anchor_ist OR e.onset_ist >= DATEADD(day, -7, a.anchor_ist))
    GROUP BY a.device_id, a.anchor_ist
),
framed AS (
    SELECT
      r.device_key,
      r.anchor_ist AS prediction_time_ist,
      r.split,
      SIN(2 * PI() * DATE_PART(hour, r.anchor_ist) / 24) AS hour_sin,
      COS(2 * PI() * DATE_PART(hour, r.anchor_ist) / 24) AS hour_cos,
      SIN(2 * PI() * (DAYOFWEEKISO(r.anchor_ist) - 1) / 7) AS dow_sin,
      COS(2 * PI() * (DAYOFWEEKISO(r.anchor_ist) - 1) / 7) AS dow_cos,
      DATEDIFF(second, p.start_ist, r.anchor_ist) / 86400.0 AS elapsed_days,
      r.telemetry_hour_share_6h,
      r.ping_success_share_6h,
      r.ping_success_share_6h - b.prior7d_ping_success_share
        AS ping_success_delta_vs_prior7d,
      r.log1p_max_contiguous_misses_6h,
      LN(1 + l.prior7d_outages) AS log1p_prior7d_outages,
      r.optical_median_6h,
      r.optical_median_6h - b.prior7d_optical_median AS optical_delta_vs_prior7d,
      r.optical_oor_share_6h,
      r.optical_spread_median_6h,
      r.optical_valid_hour_share_6h,
      l.outage_2h_6h,
      DATEDIFF(minute, r.anchor_ist, l.next_onset_ist) AS warning_minutes
    FROM recent r
    JOIN baseline b USING (device_id, anchor_ist)
    JOIN labels l USING (device_id, anchor_ist)
    CROSS JOIN params p
    WHERE l.active_outages = 0 AND l.guard_outages = 0
)
SELECT * FROM framed
ORDER BY prediction_time_ist, device_key
"""


def fetch_frame(sql: str) -> tuple[pd.DataFrame, str]:
    connection = connect_snowflake()
    cursor = connection.cursor()
    batches: list[pd.DataFrame] = []
    try:
        cursor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 600")
        cursor.execute("ALTER SESSION SET QUERY_TAG = 'optical_outage_model_v1_read_only'")
        cursor.execute(sql)
        columns = [str(item[0]).lower() for item in cursor.description]
        query_id = str(cursor.sfqid)
        while rows := cursor.fetchmany(100_000):
            batches.append(pd.DataFrame.from_records(rows, columns=columns))
        return pd.concat(batches, ignore_index=True) if batches else pd.DataFrame(columns=columns), query_id
    finally:
        cursor.close()
        connection.close()


def threshold_at_budget(probabilities: np.ndarray, budget: float = ALERT_BUDGET) -> float:
    if not 0 < budget < 1:
        raise ValueError("Alert budget must lie between zero and one")
    return float(np.quantile(probabilities, 1 - budget, method="higher"))


def metrics(y: np.ndarray, probabilities: np.ndarray, threshold: float, warning: np.ndarray) -> dict[str, float | int | None]:
    predicted = probabilities >= threshold
    true_warning = warning[predicted & (y == 1)]
    return {
        "rows": int(len(y)),
        "positives": int(y.sum()),
        "prevalence": float(y.mean()),
        "pr_auc": float(average_precision_score(y, probabilities)),
        "brier": float(brier_score_loss(y, probabilities)),
        "ece_10bin": calibration_error(y, probabilities),
        "calibration_gap": float(probabilities.mean() - y.mean()),
        "threshold": threshold,
        "alert_rate": float(predicted.mean()),
        "precision": float(precision_score(y, predicted, zero_division=0)),
        "recall": float(recall_score(y, predicted, zero_division=0)),
        "median_warning_minutes": float(np.nanmedian(true_warning)) if len(true_warning) else None,
    }


def bootstrap_delta(
    frame: pd.DataFrame,
    candidate: np.ndarray,
    baseline: np.ndarray,
    replicates: int,
    seed: int,
) -> dict[str, object]:
    y = frame["outage_2h_6h"].to_numpy(int)
    codes, devices = pd.factorize(frame["device_key"], sort=False)
    rng = np.random.default_rng(seed)
    ap: list[float] = []
    brier: list[float] = []
    for _ in range(replicates):
        counts = np.bincount(rng.integers(0, len(devices), len(devices)), minlength=len(devices))
        weights = counts[codes]
        if weights[y == 1].sum() == 0 or weights[y == 0].sum() == 0:
            continue
        ap.append(
            float(
                average_precision_score(y, candidate, sample_weight=weights)
                - average_precision_score(y, baseline, sample_weight=weights)
            )
        )
        brier.append(
            float(
                brier_score_loss(y, candidate, sample_weight=weights)
                - brier_score_loss(y, baseline, sample_weight=weights)
            )
        )
    return {
        "replicates": replicates,
        "valid_replicates": len(ap),
        "pr_auc_delta": float(average_precision_score(y, candidate) - average_precision_score(y, baseline)),
        "pr_auc_ci_low": float(np.percentile(ap, 2.5)) if ap else None,
        "pr_auc_ci_high": float(np.percentile(ap, 97.5)) if ap else None,
        "brier_delta": float(brier_score_loss(y, candidate) - brier_score_loss(y, baseline)),
        "brier_ci_low": float(np.percentile(brier, 2.5)) if brier else None,
        "brier_ci_high": float(np.percentile(brier, 97.5)) if brier else None,
    }


def equal_device_ap_delta(
    frame: pd.DataFrame,
    candidate: np.ndarray,
    baseline: np.ndarray,
    replicates: int,
    seed: int,
) -> dict[str, object]:
    y = frame["outage_2h_6h"].to_numpy(int)
    deltas: list[float] = []
    for positions in frame.groupby("device_key", sort=False).indices.values():
        index = np.asarray(positions, dtype=int)
        if len(np.unique(y[index])) == 2:
            deltas.append(
                float(
                    average_precision_score(y[index], candidate[index])
                    - average_precision_score(y[index], baseline[index])
                )
            )
    rng = np.random.default_rng(seed)
    samples = [float(np.mean(rng.choice(deltas, len(deltas), replace=True))) for _ in range(replicates)] if deltas else []
    return {
        "eligible_devices": len(deltas),
        "mean_delta": float(np.mean(deltas)) if deltas else None,
        "median_delta": float(np.median(deltas)) if deltas else None,
        "fraction_improved": float(np.mean(np.asarray(deltas) > 0)) if deltas else None,
        "ci_low": float(np.percentile(samples, 2.5)) if samples else None,
        "ci_high": float(np.percentile(samples, 97.5)) if samples else None,
    }


def train_models(frame: pd.DataFrame, replicates: int) -> tuple[dict[str, object], pd.DataFrame]:
    train = frame["split"].eq("train")
    validation = frame["split"].eq("validation")
    test = frame["split"].eq("test")
    outcome = "outage_2h_6h"
    test_y = frame.loc[test, outcome].to_numpy(int)
    warning = frame.loc[test, "warning_minutes"].to_numpy(float)
    probabilities: dict[str, np.ndarray] = {}
    results: dict[str, object] = {}
    coefficients: list[dict[str, object]] = []
    for model_name, columns in MODEL_FEATURES.items():
        model = logistic_pipeline(columns, RANDOM_SEED)
        model.fit(frame.loc[train, columns], frame.loc[train, outcome])
        validation_probability = model.predict_proba(frame.loc[validation, columns])[:, 1]
        test_probability = model.predict_proba(frame.loc[test, columns])[:, 1]
        threshold = threshold_at_budget(validation_probability)
        probabilities[model_name] = test_probability
        results[model_name] = {
            "features": columns,
            "validation_alert_budget": ALERT_BUDGET,
            "test": metrics(test_y, test_probability, threshold, warning),
        }
        for feature, coefficient in zip(columns, model.named_steps["model"].coef_[0]):
            coefficients.append(
                {"model": model_name, "feature": feature, "standardized_coefficient": float(coefficient)}
            )
    test_frame = frame.loc[test].reset_index(drop=True)
    comparisons = {
        "optical_vs_time": ("optical", "time_only"),
        "combined_vs_non_optical": ("combined", "non_optical"),
    }
    uncertainty: dict[str, object] = {}
    for index, (name, (candidate, baseline)) in enumerate(comparisons.items()):
        uncertainty[name] = {
            "device_cluster_bootstrap": bootstrap_delta(
                test_frame, probabilities[candidate], probabilities[baseline], replicates, RANDOM_SEED + index
            ),
            "equal_device_ap": equal_device_ap_delta(
                test_frame, probabilities[candidate], probabilities[baseline], replicates, RANDOM_SEED + 100 + index
            ),
        }
    results["comparisons"] = uncertainty
    return results, pd.DataFrame(coefficients)


def feature_profile(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for split, part in frame.groupby("split", sort=False):
        for feature in ALL_FEATURES:
            values = pd.to_numeric(part[feature], errors="coerce")
            rows.append(
                {
                    "split": split,
                    "feature": feature,
                    "rows": len(part),
                    "coverage": float(values.notna().mean()),
                    "mean": float(values.mean()) if values.notna().any() else None,
                    "median": float(values.median()) if values.notna().any() else None,
                    "std": float(values.std()) if values.notna().sum() > 1 else None,
                }
            )
    return pd.DataFrame(rows)


def render_report(audit: dict[str, object]) -> str:
    models = audit["models"]
    rows = [
        "# Optical outage model — exploratory development run",
        "",
        f"Decision: `{audit['decision']}`.",
        "",
        "This is an actual regularized logistic training run on a chronological development split. It is not confirmation because the dates were already inspected and the formal outage detector's dependence on ping/optical telemetry is unresolved.",
        "",
        "## Test metrics",
        "",
        "| Model | Features | PR-AUC | Brier | ECE | Alert rate | Precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("time_only", "non_optical", "optical", "combined"):
        result = models[name]
        item = result["test"]
        rows.append(
            f"| {name} | {len(result['features'])} | {item['pr_auc']:.4f} | {item['brier']:.4f} | "
            f"{item['ece_10bin']:.4f} | {item['alert_rate']:.3%} | {item['precision']:.3%} | {item['recall']:.3%} |"
        )
    comparison = models["comparisons"]["combined_vs_non_optical"]
    pooled = comparison["device_cluster_bootstrap"]
    equal = comparison["equal_device_ap"]
    rows.extend(
        [
            "",
            "## Optical incremental comparison",
            "",
            f"- Combined minus non-optical test PR-AUC: {pooled['pr_auc_delta']:+.4f} "
            f"(device-bootstrap 95% CI {pooled['pr_auc_ci_low']:+.4f} to {pooled['pr_auc_ci_high']:+.4f}).",
            f"- Combined minus non-optical Brier: {pooled['brier_delta']:+.4f} "
            f"(95% CI {pooled['brier_ci_low']:+.4f} to {pooled['brier_ci_high']:+.4f}; lower is better).",
            f"- Equal-device AP delta: {equal['mean_delta']:+.4f} "
            f"(95% CI {equal['ci_low']:+.4f} to {equal['ci_high']:+.4f}) across {equal['eligible_devices']} mixed-class devices.",
            "",
            "## Limits",
            "",
            "- Ping and outage-history performance may partly reproduce the outage detector rather than predict independent service failure.",
            "- Device timelines recur across splits; this tests temporal stability, not unseen-device generalization.",
            "- Shared incidents reduce the effective sample size beyond the device-bootstrap approximation.",
            "- A new post-2026-08-11 window is required before any predictor claim.",
            "",
        ]
    )
    return "\n".join(rows)


def self_check() -> None:
    assert len(ALL_FEATURES) == 15 and len(set(ALL_FEATURES)) == 15
    assert threshold_at_budget(np.arange(100), 0.05) == 95
    toy = pd.DataFrame(
        {
            "device_key": ["D1", "D1", "D2", "D2"],
            "outage_2h_6h": [0, 1, 0, 1],
        }
    )
    delta = bootstrap_delta(toy, np.array([0.1, 0.9, 0.2, 0.8]), np.full(4, 0.5), 20, 1)
    assert delta["valid_replicates"] == 20 and delta["pr_auc_delta"] > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the bounded optical outage logistic pilot")
    parser.add_argument("--start", default=str(DEFAULT_START))
    parser.add_argument("--end", default=str(DEFAULT_END))
    parser.add_argument("--sample-modulus", type=int, default=20, help="Stable hash sampling denominator")
    parser.add_argument("--bootstrap-replicates", type=int, default=200)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.sample_modulus < 1 or args.bootstrap_replicates < 20:
        parser.error("sample modulus must be positive and bootstraps must be at least 20")
    return args


def main() -> int:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    if end - start != pd.Timedelta(days=42):
        raise ValueError("v1 requires exactly six weeks: four train, one validation, one test")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    sql = frame_sql(start, end, args.sample_modulus)
    (output / "model_frame.sql").write_text(sql)
    frame, query_id = fetch_frame(sql)
    if frame.empty:
        raise RuntimeError("Snowflake returned an empty model frame")
    assert_private_frame(frame)
    frame["prediction_time_ist"] = pd.to_datetime(frame["prediction_time_ist"])
    numeric = ALL_FEATURES + ["outage_2h_6h", "warning_minutes"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if set(frame["split"].unique()) != {"train", "validation", "test"}:
        raise RuntimeError("All three chronological splits are required")
    for split, part in frame.groupby("split"):
        if part["outage_2h_6h"].nunique() != 2:
            raise RuntimeError(f"{split} lacks both outcome classes")
    time_trend_cap = float(frame.loc[frame["split"].eq("train"), "elapsed_days"].max())
    frame["elapsed_days"] = frame["elapsed_days"].clip(upper=time_trend_cap)
    profiles = feature_profile(frame)
    models, coefficients = train_models(frame, args.bootstrap_replicates)
    primary = models["comparisons"]["combined_vs_non_optical"]
    pooled = primary["device_cluster_bootstrap"]
    equal = primary["equal_device_ap"]
    combined = models["combined"]["test"]
    supported = bool(
        pooled["pr_auc_delta"] >= 0.01
        and pooled["pr_auc_ci_low"] is not None
        and pooled["pr_auc_ci_low"] > 0
        and pooled["brier_ci_high"] is not None
        and pooled["brier_ci_high"] < 0
        and equal["eligible_devices"] >= 20
        and equal["ci_low"] is not None
        and equal["ci_low"] > 0
        and abs(combined["calibration_gap"]) <= 0.05
        and combined["ece_10bin"] <= 0.10
    )
    if supported:
        decision = "CURRENT_DEVELOPMENT_INCREMENTAL_OPTICAL_SIGNAL"
    elif pooled["pr_auc_delta"] > 0:
        decision = "CURRENT_DEVELOPMENT_POOLED_OPTICAL_LIFT_ONLY"
    else:
        decision = "OPTICAL_INCREMENTAL_SIGNAL_NOT_DEMONSTRATED"
    split_summary = {
        split: {
            "rows": len(part),
            "devices": int(part["device_key"].nunique()),
            "positives": int(part["outage_2h_6h"].sum()),
            "prevalence": float(part["outage_2h_6h"].mean()),
            "first_anchor": part["prediction_time_ist"].min(),
            "last_anchor": part["prediction_time_ist"].max(),
        }
        for split, part in frame.groupby("split", sort=False)
    }
    audit: dict[str, object] = {
        "status": "EXPLORATORY_DEVELOPMENT_MODEL_ONLY",
        "decision": decision,
        "source": "PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX",
        "outcome": "formal device outage onset in (2h, 6h] IST; active and (0h, 2h] anchors excluded",
        "source_contract_assumptions": {
            "first_fail_timestamp": "UTC-valued TIMESTAMP_NTZ converted by +330 minutes",
            "availability": "normalized hour end, INSERTED_AT, and UPDATED_AT must all be <= anchor",
            "midnight_hour_end_bug": "normalize to HOUR_START_IST + 1 hour only for exact +1h rows or known same-day midnight defect",
            "valid_optical_range": "-50 <= min <= avg <= max < 0; provisional until source-owner confirmation",
            "ping_label_independence": "unconfirmed; non-optical model is detector-replication sensitivity only",
        },
        "window": {"start_ist": start, "end_ist": end, "split": "4 weeks / 1 week / 1 week"},
        "sampling": {"device_hash_modulus": args.sample_modulus, "anchor_cadence_hours": 3},
        "feature_count": len(ALL_FEATURES),
        "time_trend_cap_days_from_training": time_trend_cap,
        "feature_groups": {
            "time": TIME_FEATURES,
            "non_optical": NON_OPTICAL_FEATURES,
            "optical": OPTICAL_FEATURES,
        },
        "frame": {"rows": len(frame), "devices": int(frame["device_key"].nunique()), "splits": split_summary},
        "models": models,
        "promotion_gate": {
            "passed": supported,
            "rule": "combined-minus-non-optical AP >= .01 with device-bootstrap lower bound > 0; Brier upper bound < 0; equal-device AP lower bound > 0; calibrated",
        },
        "snowflake_query_id": query_id,
        "model_frame_sql_sha256": sql_hash(sql),
        "source_writes_attempted": False,
        "software_versions": {
            "python": platform.python_version(),
            "numpy": metadata.version("numpy"),
            "pandas": metadata.version("pandas"),
            "scikit_learn": metadata.version("scikit-learn"),
            "snowflake_connector": metadata.version("snowflake-connector-python"),
        },
    }
    profiles.to_csv(output / "feature_profile.csv", index=False)
    coefficients.to_csv(output / "coefficients.csv", index=False)
    model_rows = []
    for name in ("time_only", "non_optical", "optical", "combined"):
        model_rows.append({"model": name, "feature_count": len(models[name]["features"]), **models[name]["test"]})
    pd.DataFrame(model_rows).to_csv(output / "model_results.csv", index=False)
    write_json(output / "audit.json", audit)
    (output / "report.md").write_text(render_report(native(audit)))
    audit["artifact_sha256"] = {
        path.name: file_hash(path)
        for path in sorted(output.iterdir())
        if path.name != "audit.json"
    }
    write_json(output / "audit.json", audit)
    print(f"{decision}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
