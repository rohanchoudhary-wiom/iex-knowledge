from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
from datetime import datetime
from importlib import metadata
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "outputs" / "acs_outage_feasibility_2026-08-11_v14" / "model_frame.csv.gz"
FROZEN_INPUT_SHA256 = "e71132a682a8ba5aa9699c83cb1db364f36531997570621443b28c8958f370de"
FROZEN_INPUT_CONTENT_SHA256 = "e84d5e4096ced722f0710606c78b84d0dc94688dca69a8a9f62f9b2a23e4e2ae"
FROZEN_PARENT_AUDIT_SHA256 = "7092265955d15b4ad7bdbf575aa8819972dc5746eace0f5c3f9bcf88a8bc0ed0"
STATUS = "FEASIBILITY_PILOT_ONLY"
DECISION_LABEL = "CURRENT_WINDOW_POST_HOC_EVENT_ALIGNED_DIAGNOSTIC_ONLY"
SEED = 20260811
BOOTSTRAP_REPLICATES = 2000
MIN_NEAR_ROWS = 3
MIN_FAR_ROWS = 9
MIN_INFERENCE_UNITS = 10
ALPHA = 0.05

LEAD_EDGES_MINUTES = [0.0, 60.0, 180.0, 360.0, 720.0, 1440.0]
LEAD_LABELS = ["(0,1]h", "(1,3]h", "(3,6]h", "(6,12]h", "(12,24]h"]
LEAD_MINIMUM_ROWS = dict(zip(LEAD_LABELS, [1, 1, 2, 3, 6]))

FEATURES = {
    "recent_reboot_1h": "I(boot_age_hours <= 1), missing when boot_age_hours is missing",
    "reboot_count_0_6h": "reboot_count_6h",
    "reboot_count_6_24h": "reboot_count_24h - reboot_count_6h",
    "reboot_rate_acceleration": "reboot_count_6h / 6 - (reboot_count_24h - reboot_count_6h) / 18",
    "reboot_recency_hours": "-boot_age_hours",
    "inform_staleness_minutes": "inform_staleness_minutes",
}

REQUIRED_COLUMNS = [
    "device_key",
    "prediction_time_utc",
    "split",
    "boot_age_hours",
    "reboot_count_6h",
    "reboot_count_24h",
    "inform_staleness_minutes",
    "outage_next_6h",
    "outage_next_24h",
    "time_to_next_outage_minutes",
]
PROHIBITED_OUTPUT_COLUMNS = {
    "account_id",
    "device_id",
    "device_key",
    "incident_id",
    "nasid",
    "serial_number",
    "mobile",
    "ip",
    "ip_address",
    "ssid",
    "params_json",
}


def bh_adjust(p_values: list[float]) -> list[float]:
    adjusted = [float("nan")] * len(p_values)
    valid = sorted((index, value) for index, value in enumerate(p_values) if math.isfinite(value))
    valid.sort(key=lambda item: item[1])
    running = 1.0
    for rank in range(len(valid), 0, -1):
        index, value = valid[rank - 1]
        running = min(running, value * len(valid) / rank)
        adjusted[index] = running
    return adjusted


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def native(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [native(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(native(value), indent=2, sort_keys=True) + "\n")


def assert_private_frame(frame: pd.DataFrame) -> None:
    prohibited = PROHIBITED_OUTPUT_COLUMNS.intersection(str(column).lower() for column in frame.columns)
    if prohibited:
        raise RuntimeError(f"Privacy check rejected output columns: {sorted(prohibited)}")
    if any("json" in str(column).lower() or "payload" in str(column).lower() for column in frame.columns):
        raise RuntimeError("Privacy check rejected raw payload columns")


def bootstrap_mean_ci(values: np.ndarray, replicates: int, seed: int) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = rng.choice(values, size=(replicates, len(values)), replace=True).mean(axis=1)
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def mean_test(values: np.ndarray) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    if len(values) < MIN_INFERENCE_UNITS:
        return float("nan"), float("nan")
    if np.allclose(values, values[0]):
        return (0.0, 1.0) if np.allclose(values, 0.0) else (math.copysign(float("inf"), values[0]), 0.0)
    result = stats.ttest_1samp(values, popmean=0.0, nan_policy="raise")
    return float(result.statistic), float(result.pvalue)


def wilcoxon_sensitivity(values: np.ndarray) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    nonzero = values[~np.isclose(values, 0.0)]
    if len(values) < MIN_INFERENCE_UNITS or len(nonzero) == 0:
        return (0.0, 1.0) if len(values) >= MIN_INFERENCE_UNITS else (float("nan"), float("nan"))
    result = stats.wilcoxon(values, zero_method="wilcox", correction=False, alternative="two-sided", method="auto")
    return float(result.statistic), float(result.pvalue)


def add_derived_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if (result["reboot_count_24h"] < result["reboot_count_6h"]).any():
        raise RuntimeError("Frozen frame violates reboot_count_24h >= reboot_count_6h")
    boot_observed = result["boot_age_hours"].notna()
    result["recent_reboot_1h"] = np.nan
    result.loc[boot_observed, "recent_reboot_1h"] = (
        result.loc[boot_observed, "boot_age_hours"] <= 1.0
    ).astype(float)
    result["reboot_count_0_6h"] = result["reboot_count_6h"].astype(float)
    result["reboot_count_6_24h"] = (
        result["reboot_count_24h"] - result["reboot_count_6h"]
    ).astype(float)
    result["reboot_rate_acceleration"] = (
        result["reboot_count_6h"] / 6.0 - result["reboot_count_6_24h"] / 18.0
    )
    result["reboot_recency_hours"] = -result["boot_age_hours"]
    return result


def align_event_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise RuntimeError(f"Frozen frame is missing required columns: {missing}")
    if frame.duplicated(["device_key", "prediction_time_utc"]).any():
        raise RuntimeError("Frozen frame contains duplicate device-hour rows")
    if set(frame["split"].unique()) != {"train", "validation", "test"}:
        raise RuntimeError("Frozen frame does not contain the expected chronological splits")
    if (frame["inform_staleness_minutes"] <= 0).any():
        raise RuntimeError("Strictly-prior telemetry invariant failed")
    if (frame.loc[frame["boot_age_hours"].notna(), "boot_age_hours"] <= 0).any():
        raise RuntimeError("Strictly-prior boot-time invariant failed")

    positive = frame.loc[frame["outage_next_24h"].eq(1), REQUIRED_COLUMNS].copy()
    if positive["time_to_next_outage_minutes"].isna().any():
        raise RuntimeError("A 24-hour positive row lacks time to onset")
    lead = positive["time_to_next_outage_minutes"].astype(float)
    if not ((lead > 0) & (lead <= 1440)).all():
        raise RuntimeError("Event alignment found a non-future or beyond-24-hour onset")
    if not positive["outage_next_6h"].astype(bool).equals(lead.le(360)):
        raise RuntimeError("Six-hour outcome labels disagree with time to onset")

    positive["prediction_time_utc"] = pd.to_datetime(
        positive["prediction_time_utc"], errors="raise"
    )
    onset_raw = positive["prediction_time_utc"] + pd.to_timedelta(lead, unit="m")
    positive["event_onset_utc"] = onset_raw.dt.round("s")
    rounding_error_seconds = (onset_raw - positive["event_onset_utc"]).abs().dt.total_seconds()
    if float(rounding_error_seconds.max()) > 0.001:
        raise RuntimeError("Onset reconstruction exceeded source-second rounding tolerance")
    if not (positive["prediction_time_utc"] < positive["event_onset_utc"]).all():
        raise RuntimeError("Anchor-before-onset invariant failed")

    event_splits = positive.groupby(["device_key", "event_onset_utc"])["split"].nunique()
    if int(event_splits.max()) != 1:
        raise RuntimeError("A reconstructed device event crosses chronological splits")
    onset_splits = positive.groupby("event_onset_utc")["split"].nunique()
    if int(onset_splits.max()) != 1:
        raise RuntimeError("A shared onset crosses chronological splits")

    positive["lead_window"] = pd.cut(
        lead,
        bins=LEAD_EDGES_MINUTES,
        labels=LEAD_LABELS,
        right=True,
        include_lowest=False,
    )
    if positive["lead_window"].isna().any():
        raise RuntimeError("A positive row was not assigned to a frozen lead window")
    positive["period"] = np.where(lead <= 360, "near_0_6h", "far_6_24h")
    positive = add_derived_features(positive)

    event_key = ["device_key", "event_onset_utc"]
    onset_device_counts = positive.groupby("event_onset_utc")["device_key"].nunique()
    event_period_rows = (
        positive.groupby(event_key + ["split", "period"]).size().unstack("period", fill_value=0)
    )
    both_periods = event_period_rows["near_0_6h"].gt(0) & event_period_rows["far_6_24h"].gt(0)
    event_period_support = {
        "all_splits": {
            "both_periods": int(both_periods.sum()),
            "near_only": int((event_period_rows["near_0_6h"].gt(0) & event_period_rows["far_6_24h"].eq(0)).sum()),
            "far_only": int((event_period_rows["near_0_6h"].eq(0) & event_period_rows["far_6_24h"].gt(0)).sum()),
        }
    }
    for split in ("train", "validation", "test"):
        split_rows = event_period_rows[event_period_rows.index.get_level_values("split") == split]
        split_both = split_rows["near_0_6h"].gt(0) & split_rows["far_6_24h"].gt(0)
        event_period_support[split] = {
            "both_periods": int(split_both.sum()),
            "near_only": int((split_rows["near_0_6h"].gt(0) & split_rows["far_6_24h"].eq(0)).sum()),
            "far_only": int((split_rows["near_0_6h"].eq(0) & split_rows["far_6_24h"].gt(0)).sum()),
        }
    audit = {
        "positive_rows": len(positive),
        "positive_devices": int(positive["device_key"].nunique()),
        "positive_rows_by_split": {
            str(key): int(value) for key, value in positive.groupby("split").size().items()
        },
        "positive_devices_by_split": {
            str(key): int(value) for key, value in positive.groupby("split")["device_key"].nunique().items()
        },
        "reconstructed_device_events": int(positive[event_key].drop_duplicates().shape[0]),
        "reconstructed_device_events_by_split": {
            str(key): int(value)
            for key, value in positive.drop_duplicates(event_key).groupby("split").size().items()
        },
        "event_period_support": event_period_support,
        "reconstructed_onset_clusters": int(positive["event_onset_utc"].nunique()),
        "shared_onset_clusters": int((onset_device_counts > 1).sum()),
        "maximum_devices_per_onset_cluster": int(onset_device_counts.max()),
        "maximum_onset_rounding_error_seconds": float(rounding_error_seconds.max()),
        "split_crossing_device_events": 0,
        "split_crossing_onset_clusters": 0,
        "duration_or_closure_columns_loaded": False,
        "strict_prior_telemetry_asserted": True,
        "strict_anchor_before_onset_asserted": True,
    }
    return positive, audit


def event_contrasts(aligned: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    keys = ["device_key", "event_onset_utc", "split"]
    onset_device_counts = aligned.groupby("event_onset_utc")["device_key"].nunique()
    pieces: list[pd.DataFrame] = []
    support: dict[str, object] = {}
    for feature in FEATURES:
        observed = aligned.loc[aligned[feature].notna(), keys + ["period", feature]]
        grouped = observed.groupby(keys + ["period"], observed=True)[feature].agg(["mean", "size"])
        means = grouped["mean"].unstack("period")
        sizes = grouped["size"].unstack("period").fillna(0)
        eligible = (
            means.get("near_0_6h", pd.Series(index=means.index, dtype=float)).notna()
            & means.get("far_6_24h", pd.Series(index=means.index, dtype=float)).notna()
            & sizes.get("near_0_6h", pd.Series(0, index=means.index)).ge(MIN_NEAR_ROWS)
            & sizes.get("far_6_24h", pd.Series(0, index=means.index)).ge(MIN_FAR_ROWS)
        )
        selected = means.loc[eligible, ["near_0_6h", "far_6_24h"]].reset_index()
        selected["feature"] = feature
        selected["event_difference"] = selected["near_0_6h"] - selected["far_6_24h"]
        selected["onset_devices_all_positive"] = (
            selected["event_onset_utc"].map(onset_device_counts).astype(int)
        )
        eligible_observed_rows = len(observed.merge(selected[keys], on=keys, how="inner"))
        pieces.append(selected)
        near_observed = means.get("near_0_6h", pd.Series(index=means.index, dtype=float)).notna()
        far_observed = means.get("far_6_24h", pd.Series(index=means.index, dtype=float)).notna()
        both_observed = near_observed & far_observed
        near_supported = sizes.get("near_0_6h", pd.Series(0, index=means.index)).ge(MIN_NEAR_ROWS)
        far_supported = sizes.get("far_6_24h", pd.Series(0, index=means.index)).ge(MIN_FAR_ROWS)
        support[feature] = {
            "observed_positive_rows": len(observed),
            "observed_rows_in_supported_events": eligible_observed_rows,
            "observed_rows_outside_supported_events": len(observed) - eligible_observed_rows,
            "events_with_any_rows_in_both_periods": int(both_observed.sum()),
            "events_meeting_feature_specific_row_support": len(selected),
            "devices_meeting_feature_specific_row_support": int(selected["device_key"].nunique()),
            "excluded_events_after_both_periods_for_row_support": int(both_observed.sum() - len(selected)),
            "events_failing_near_row_support": int((both_observed & ~near_supported).sum()),
            "events_failing_far_row_support": int((both_observed & ~far_supported).sum()),
            "events_failing_both_row_supports": int((both_observed & ~near_supported & ~far_supported).sum()),
        }
    return pd.concat(pieces, ignore_index=True), support


def summarize_values(values: np.ndarray, replicates: int, seed: int) -> dict[str, object]:
    values = np.asarray(values, dtype=float)
    t_statistic, t_p_value = mean_test(values)
    wilcoxon_statistic, wilcoxon_p_value = wilcoxon_sensitivity(values)
    ci_low, ci_high = bootstrap_mean_ci(values, replicates, seed)
    return {
        "units": len(values),
        "mean_effect": float(np.mean(values)) if len(values) else None,
        "median_effect": float(np.median(values)) if len(values) else None,
        "fraction_positive": float(np.mean(values > 0)) if len(values) else None,
        "fraction_negative": float(np.mean(values < 0)) if len(values) else None,
        "t_statistic": t_statistic,
        "mean_test_p_value": t_p_value,
        "bootstrap_mean_ci_low": ci_low,
        "bootstrap_mean_ci_high": ci_high,
        "wilcoxon_statistic": wilcoxon_statistic,
        "wilcoxon_p_value": wilcoxon_p_value,
    }


def describe_values(values: np.ndarray) -> dict[str, object]:
    values = np.asarray(values, dtype=float)
    return {
        "units": len(values),
        "mean_effect": float(np.mean(values)) if len(values) else None,
        "median_effect": float(np.median(values)) if len(values) else None,
        "fraction_positive": float(np.mean(values > 0)) if len(values) else None,
        "fraction_negative": float(np.mean(values < 0)) if len(values) else None,
    }


def infer_contrasts(
    contrasts: pd.DataFrame, replicates: int, seed: int
) -> tuple[pd.DataFrame, dict[str, object]]:
    records: list[dict[str, object]] = []
    shared_support: dict[str, object] = {}
    for split_index, split in enumerate(("train", "validation", "test")):
        for feature_index, feature in enumerate(FEATURES):
            subset = contrasts[
                contrasts["split"].eq(split) & contrasts["feature"].eq(feature)
            ].copy()
            device_values = (
                subset.groupby("device_key")["event_difference"].mean().to_numpy(float)
            )
            onset_values = (
                subset.groupby("event_onset_utc")["event_difference"].mean().to_numpy(float)
            )
            eligible_onset_device_counts = subset.groupby("event_onset_utc")["device_key"].nunique()
            singleton = subset[subset["onset_devices_all_positive"].eq(1)]
            singleton_device_values = (
                singleton.groupby("device_key")["event_difference"].mean().to_numpy(float)
            )
            base_seed = seed + split_index * 100 + feature_index * 3
            row: dict[str, object] = {
                "split": split,
                "feature": feature,
                "eligible_events": len(subset),
                "shared_onset_clusters": int(
                    subset.loc[subset["onset_devices_all_positive"].gt(1), "event_onset_utc"].nunique()
                ),
                "singleton_events": len(singleton),
            }
            for prefix, values, offset in (
                ("device", device_values, 0),
                ("singleton_device", singleton_device_values, 2),
            ):
                summary = summarize_values(values, replicates, base_seed + offset)
                row.update({f"{prefix}_{key}": value for key, value in summary.items()})
            row.update(
                {
                    f"onset_cluster_{key}": value
                    for key, value in describe_values(onset_values).items()
                }
            )
            records.append(row)
            shared_support[f"{split}:{feature}"] = {
                "eligible_events": len(subset),
                "eligible_devices": len(device_values),
                "onset_clusters": len(onset_values),
                "shared_onset_clusters": int(
                    subset.loc[subset["onset_devices_all_positive"].gt(1), "event_onset_utc"].nunique()
                ),
                "maximum_eligible_devices_per_onset_cluster": int(eligible_onset_device_counts.max()),
                "singleton_events": len(singleton),
                "singleton_devices": len(singleton_device_values),
            }
    results = pd.DataFrame(records)
    for split in ("train", "validation", "test"):
        mask = results["split"].eq(split)
        for prefix in ("device", "singleton_device"):
            results.loc[mask, f"{prefix}_mean_test_q_value"] = bh_adjust(
                results.loc[mask, f"{prefix}_mean_test_p_value"].astype(float).tolist()
            )
            results.loc[mask, f"{prefix}_wilcoxon_q_value"] = bh_adjust(
                results.loc[mask, f"{prefix}_wilcoxon_p_value"].astype(float).tolist()
            )
    return results, shared_support


def trajectory_table(aligned: pd.DataFrame) -> pd.DataFrame:
    keys = ["device_key", "event_onset_utc", "split", "lead_window"]
    records: list[dict[str, object]] = []
    for feature in FEATURES:
        observed = aligned.loc[aligned[feature].notna(), keys + [feature]]
        grouped = observed.groupby(keys, observed=True)[feature].agg(["mean", "size"]).reset_index()
        grouped = grouped[grouped["size"] >= grouped["lead_window"].map(LEAD_MINIMUM_ROWS).astype(int)]
        for (split, lead_window), subset in grouped.groupby(["split", "lead_window"], observed=True):
            device_values = subset.groupby("device_key")["mean"].mean().to_numpy(float)
            records.append(
                {
                    "split": split,
                    "feature": feature,
                    "lead_window": str(lead_window),
                    "eligible_events": len(subset),
                    "eligible_devices": len(device_values),
                    "equal_device_mean": float(np.mean(device_values)) if len(device_values) else None,
                    "equal_device_median": float(np.median(device_values)) if len(device_values) else None,
                }
            )
    result = pd.DataFrame(records)
    result["lead_window"] = pd.Categorical(result["lead_window"], LEAD_LABELS, ordered=True)
    return result.sort_values(["split", "feature", "lead_window"]).reset_index(drop=True)


def interval_supports_direction(row: pd.Series, prefix: str, direction: float) -> bool:
    low = float(row[f"{prefix}_bootstrap_mean_ci_low"])
    high = float(row[f"{prefix}_bootstrap_mean_ci_high"])
    effect = float(row[f"{prefix}_mean_effect"])
    q_value = float(row[f"{prefix}_mean_test_q_value"])
    return bool(
        math.isfinite(q_value)
        and q_value < ALPHA
        and np.sign(effect) == direction
        and ((direction > 0 and low > 0) or (direction < 0 and high < 0))
    )


def classify_candidates(results: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    result = results.copy()
    result["passes_frozen_candidate_gate"] = False
    candidates: list[str] = []
    for feature in FEATURES:
        rows = result[result["feature"].eq(feature)].set_index("split")
        train_effect = float(rows.loc["train", "device_mean_effect"])
        direction = float(np.sign(train_effect))
        passed = direction != 0 and all(
            interval_supports_direction(rows.loc[split], prefix, direction)
            for split in ("train", "validation", "test")
            for prefix in ("device", "singleton_device")
        )
        result.loc[result["feature"].eq(feature), "passes_frozen_candidate_gate"] = passed
        if passed:
            candidates.append(feature)
    return result, candidates


def render_report(audit: dict[str, object], results: pd.DataFrame) -> str:
    candidates = audit["candidate_features"]
    outcome = (
        "A temporally stable event-aligned escalation candidate was found: " + ", ".join(candidates) + "."
        if candidates
        else "No feature passed the frozen temporal-stability and singleton-onset sensitivity gate."
    )
    lines = [
        "# ACS event-aligned window diagnostic",
        "",
        "## Decision",
        "",
        f"**{DECISION_LABEL}** — status remains `{STATUS}`.",
        "",
        outcome,
        "This post-hoc, case-only diagnostic does not establish outage prediction or specificity against non-outage controls and does not change the canonical v14 decision.",
        "",
        "## Frozen analysis",
        "",
        "- Descriptive lead windows: `(0,1]`, `(1,3]`, `(3,6]`, `(6,12]`, `(12,24]` hours before onset.",
        f"- Primary contrast: hourly-row means in `(0,6]` minus `(6,24]` hours within each reconstructed device event; at least {MIN_NEAR_ROWS} near and {MIN_FAR_ROWS} far observations per feature/event.",
        "- Repeated events are averaged within device, followed by an equal-device mean in each chronological split.",
        "- Primary inference is a two-sided one-sample mean test with a device bootstrap mean CI. Wilcoxon is a rank-location sensitivity, not the mean-effect test.",
        "- BH correction covers all six transformations separately in every split for the primary device and singleton-onset device inference. A candidate must pass both in one direction in train, validation, and test.",
        "- Equal-onset aggregation is descriptive only because devices recur across onset clusters; it is not an inferential gate.",
        "",
        "## Results",
        "",
        "| Split | Signal | Device mean [95% CI] | q | Equal-onset mean (descriptive) | Singleton-device mean [95% CI] | q | Candidate |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in results.itertuples(index=False):
        def effect(prefix: str) -> str:
            return (
                f"{getattr(row, prefix + '_mean_effect'):.4f} "
                f"[{getattr(row, prefix + '_bootstrap_mean_ci_low'):.4f}, "
                f"{getattr(row, prefix + '_bootstrap_mean_ci_high'):.4f}]"
            )

        lines.append(
            f"| {row.split} | `{row.feature}` | {effect('device')} | {row.device_mean_test_q_value:.4g} "
            f"| {row.onset_cluster_mean_effect:.4f} "
            f"| {effect('singleton_device')} | {row.singleton_device_mean_test_q_value:.4g} "
            f"| {'yes' if row.passes_frozen_candidate_gate else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Support and scope",
            "",
            f"- Positive rows: {audit['alignment']['positive_rows']}; reconstructed device events: {audit['alignment']['reconstructed_device_events']}; onset clusters: {audit['alignment']['reconstructed_onset_clusters']}.",
            f"- Events with both broad periods: {audit['alignment']['event_period_support']['all_splits']['both_periods']}/{audit['alignment']['reconstructed_device_events']}; events passing the 3/9 support rule: {sum(item['events'] for item in audit['primary_support_by_split'].values())}. Split support was "
            + ", ".join(
                f"{split} {values['events']} events/{values['devices']} devices"
                for split, values in audit["primary_support_by_split"].items()
            )
            + ".",
            f"- Shared onset clusters: {audit['alignment']['shared_onset_clusters']}; maximum devices per onset: {audit['alignment']['maximum_devices_per_onset_cluster']}.",
            f"- Descriptive lead-window minimum rows were `{json.dumps(audit['protocol']['descriptive_lead_window_minimum_rows'], sort_keys=True)}`.",
            "- The frame has no incident identifier. Nearest-second onset is a documented proxy; equal-onset summaries are descriptive and the singleton-only device analysis is the shared-onset sensitivity.",
            "- These are six predeclared transformations of the four selected v14 numeric features, not six newly discovered ACS source parameters.",
            "- Reused devices across splits measure temporal stability, not independent replication or unseen-device generalization.",
            "- A survivor would still require a frozen matched non-outage control analysis and untouched post-2026-08-10 data before predictor language.",
            "",
            "## Provenance",
            "",
            f"- Parent v14 model-frame SHA-256: `{audit['parent']['model_frame_sha256']}`.",
            f"- Parent v14 decompressed-frame SHA-256: `{audit['parent']['decompressed_model_frame_sha256']}`.",
            f"- Parent v14 audit SHA-256: `{audit['parent']['audit_sha256']}`.",
            f"- Analysis script SHA-256: `{audit['analysis_script_sha256']}`.",
            f"- Reproducible command template: `{audit['run_contract']['reproducible_command_template']}`.",
            f"- Seed: {audit['run_contract']['seed']}; bootstrap replicates: {audit['run_contract']['bootstrap_replicates']}.",
            f"- Software versions: `{json.dumps(native(audit['software_versions']), sort_keys=True)}`.",
            f"- Output hashes available at report render: `{json.dumps(native(audit['artifact_sha256']), sort_keys=True)}`. The report hash is added to `audit.json` after rendering.",
            "- Database query IDs for this command: none. Parent query IDs remain inherited provenance only.",
            "",
        ]
    )
    return "\n".join(lines)


def self_check() -> None:
    onset = pd.Timestamp("2026-01-02 00:00:30")
    lead = np.arange(30, 1440, 60, dtype=float)
    base = pd.DataFrame(
        {
            "device_key": "D0001",
            "prediction_time_utc": [onset - pd.Timedelta(minutes=float(value)) for value in lead],
            "split": "train",
            "boot_age_hours": lead / 60.0,
            "reboot_count_6h": (lead <= 360).astype(int),
            "reboot_count_24h": 1,
            "inform_staleness_minutes": 1.0,
            "outage_next_6h": (lead <= 360).astype(int),
            "outage_next_24h": 1,
            "time_to_next_outage_minutes": lead,
        }
    )
    toy = pd.concat(
        [
            base.assign(
                device_key=f"D{index:04d}",
                split=split,
                prediction_time_utc=base["prediction_time_utc"] + pd.Timedelta(days=index * 10),
            )
            for index, split in enumerate(("train", "validation", "test"), start=1)
        ],
        ignore_index=True,
    )
    aligned, alignment = align_event_rows(toy)
    contrasts, _support = event_contrasts(aligned)
    assert alignment["reconstructed_device_events"] == 3
    assert set(contrasts["feature"]) == set(FEATURES)
    assert float(contrasts.loc[contrasts["feature"].eq("reboot_count_0_6h"), "event_difference"].iloc[0]) == 1.0
    assert np.allclose(bh_adjust([0.01, 0.04, 0.03]), [0.03, 0.04, 0.04])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen ACS event-aligned window diagnostic")
    parser.add_argument("--input-frame", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--self-check-only", action="store_true")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    self_check()
    if args.self_check_only:
        print("self-check passed")
        return 0
    if args.output_dir is None:
        raise ValueError("--output-dir is required unless --self-check-only is used")
    if args.bootstrap_replicates < 1000:
        raise ValueError("Require at least 1000 bootstrap replicates")
    input_path = args.input_frame.resolve()
    if file_hash(input_path) != FROZEN_INPUT_SHA256:
        raise RuntimeError("Input is not the frozen v14 model frame")
    parent_audit = input_path.parent / "audit.json"
    if file_hash(parent_audit) != FROZEN_PARENT_AUDIT_SHA256:
        raise RuntimeError("Parent audit does not match the frozen v14 artifact")
    output = args.output_dir.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError(f"Output directory must be absent or empty: {output}")

    frame = pd.read_csv(input_path, usecols=REQUIRED_COLUMNS)
    aligned, alignment_audit = align_event_rows(frame)
    contrasts, feature_support = event_contrasts(aligned)
    results, inference_support = infer_contrasts(
        contrasts, args.bootstrap_replicates, args.seed
    )
    results, candidates = classify_candidates(results)
    trajectories = trajectory_table(aligned)

    primary_support_by_split: dict[str, dict[str, int]] = {}
    for split in ("train", "validation", "test"):
        split_results = results[results["split"].eq(split)]
        if split_results["eligible_events"].nunique() != 1 or split_results["device_units"].nunique() != 1:
            raise RuntimeError("Feature-specific support unexpectedly differs within a split")
        primary_support_by_split[split] = {
            "events": int(split_results["eligible_events"].iloc[0]),
            "devices": int(split_results["device_units"].iloc[0]),
            "singleton_events": int(split_results["singleton_events"].min()),
            "singleton_devices": int(split_results["singleton_device_units"].min()),
        }

    assert_private_frame(results)
    assert_private_frame(trajectories)
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / "window_results.csv"
    trajectory_path = output / "lead_window_trajectory.csv"
    results.to_csv(results_path, index=False)
    trajectories.to_csv(trajectory_path, index=False)

    audit: dict[str, object] = {
        "status": STATUS,
        "decision": DECISION_LABEL,
        "artifact_version": "v2",
        "candidate_features": candidates,
        "canonical_v14_decision_unchanged": True,
        "analysis_script_sha256": file_hash(Path(__file__).resolve()),
        "parent": {
            "model_frame_path": "acs_outage_prediction/outputs/acs_outage_feasibility_2026-08-11_v14/model_frame.csv.gz",
            "model_frame_sha256": FROZEN_INPUT_SHA256,
            "decompressed_model_frame_sha256": FROZEN_INPUT_CONTENT_SHA256,
            "audit_sha256": FROZEN_PARENT_AUDIT_SHA256,
            "inherited_query_ids": json.loads(parent_audit.read_text()).get("query_ids", {}),
        },
        "query_ids": {},
        "privacy_check": {
            "passed": True,
            "aggregate_outputs_only": True,
            "direct_or_pseudonymous_device_identifiers_exported": False,
            "absolute_local_paths_exported": False,
            "raw_json_or_payload_exported": False,
        },
        "run_contract": {
            "reproducible_command_template": (
                "python acs_outage_prediction/acs_window_analysis.py "
                "--input-frame acs_outage_prediction/outputs/acs_outage_feasibility_2026-08-11_v14/model_frame.csv.gz "
                "--output-dir <new-empty-output-dir>"
            ),
            "seed": args.seed,
            "bootstrap_replicates": args.bootstrap_replicates,
            "bootstrap_ci": "percentile 95% CI for the equal-unit mean",
            "mean_test": "two-sided one-sample t-test against zero",
            "wilcoxon_sensitivity": "two-sided; zero_method=wilcox; correction=false; method=auto",
            "multiplicity": "BH across all six transformations separately within each split for primary-device and singleton-device inference",
            "minimum_inference_units": MIN_INFERENCE_UNITS,
            "alpha": ALPHA,
        },
        "protocol": {
            "lead_windows_hours": LEAD_LABELS,
            "descriptive_lead_window_minimum_rows": LEAD_MINIMUM_ROWS,
            "primary_near_window": "0 < onset - anchor <= 6 hours",
            "primary_far_window": "6 < onset - anchor <= 24 hours",
            "minimum_nonmissing_rows_per_event": {"near": MIN_NEAR_ROWS, "far": MIN_FAR_ROWS},
            "aggregation": "hourly-row mean within event-band; near minus far; mean across events within device; equal-device mean within split",
            "event_key": "device_key plus reconstructed onset rounded to nearest second",
            "features": FEATURES,
            "candidate_gate": "same training direction; q<0.05 and bootstrap mean CI excludes zero in train, validation, and test for primary-device and singleton-device inference",
            "onset_cluster_role": "equal-onset descriptive summary only; no inferential independence claim because devices recur across onsets",
            "case_only": True,
            "non_outage_control": False,
            "duration_or_closure_used": False,
        },
        "alignment": alignment_audit,
        "feature_support": feature_support,
        "inference_support": inference_support,
        "primary_support_by_split": primary_support_by_split,
        "output_counts": {
            "window_result_rows": len(results),
            "trajectory_rows": len(trajectories),
            "tested_transformations": len(FEATURES),
            "chronological_splits": 3,
        },
        "software_versions": {
            "python": platform.python_version(),
            "numpy": metadata.version("numpy"),
            "pandas": metadata.version("pandas"),
            "scipy": metadata.version("scipy"),
        },
        "artifact_sha256": {
            results_path.name: file_hash(results_path),
            trajectory_path.name: file_hash(trajectory_path),
        },
    }
    report_path = output / "report.md"
    report_path.write_text(render_report(audit, results))
    audit["artifact_sha256"][report_path.name] = file_hash(report_path)
    write_json(output / "audit.json", audit)
    print(json.dumps({"decision": DECISION_LABEL, "candidate_features": candidates, "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
