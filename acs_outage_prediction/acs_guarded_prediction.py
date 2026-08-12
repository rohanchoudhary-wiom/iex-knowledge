from __future__ import annotations

import argparse
import gzip
import hashlib
import math
import platform
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
FROZEN_HELPER_SHA256 = "7d85f3cbab6574a1433a909c1de2e91651af12487cf1c84e1eaf64ca1d33c4ef"


def local_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_HELPER_PATH = ROOT / "acs_outage_feasibility.py"
_HELPER_HASH = local_file_hash(_HELPER_PATH)
if _HELPER_HASH != FROZEN_HELPER_SHA256:
    raise RuntimeError(
        f"Canonical helper hash mismatch before import: expected {FROZEN_HELPER_SHA256}, observed {_HELPER_HASH}"
    )

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

from acs_outage_feasibility import (
    TIME_FEATURES,
    add_time_features,
    choose_threshold,
    clustered_pr_auc_delta,
    equal_device_pr_auc_delta,
    file_hash,
    logistic_pipeline,
    metric_summary,
    native,
    write_json,
)


DEFAULT_INPUT = ROOT / "outputs" / "acs_outage_feasibility_2026-08-11_v14" / "model_frame.csv.gz"
DEFAULT_PARENT_AUDIT = DEFAULT_INPUT.parent / "audit.json"
FROZEN_INPUT_SHA256 = "e71132a682a8ba5aa9699c83cb1db364f36531997570621443b28c8958f370de"
FROZEN_INPUT_CONTENT_SHA256 = "e84d5e4096ced722f0710606c78b84d0dc94688dca69a8a9f62f9b2a23e4e2ae"
FROZEN_PARENT_AUDIT_SHA256 = "7092265955d15b4ad7bdbf575aa8819972dc5746eace0f5c3f9bcf88a8bc0ed0"
STATUS = "EXPLORATORY_POST_HOC_ONLY"
ANALYSIS_VERSION = "guarded-prediction-v4"
GUARD_MINUTES = 30.0
HORIZONS = (6, 24)
SEED = 20260811
BOOTSTRAP_REPLICATES = 1000
MIN_HELDOUT_CLASS_DEVICES = 10
MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES = 20

NUMERIC_FEATURES = (
    "boot_age_hours",
    "reboot_count_6h",
    "reboot_count_24h",
    "inform_staleness_minutes",
)
MISSING_FEATURES = tuple(f"{feature}_missing" for feature in NUMERIC_FEATURES)
MODEL_FEATURES = NUMERIC_FEATURES + MISSING_FEATURES
REQUIRED_COLUMNS = (
    "device_key",
    "prediction_time_utc",
    "split",
    *MODEL_FEATURES,
    "outage_next_6h",
    "outage_next_24h",
    "time_to_next_outage_minutes",
)
PROHIBITED_MODEL_COLUMNS = {
    "device_key",
    "partner_group",
    "prediction_time_utc",
    "split",
    "outage_next_6h",
    "outage_next_24h",
    "time_to_next_outage_minutes",
    "next_outage_duration_minutes",
}
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
    "password",
}
FEATURE_DESCRIPTIONS = {
    "boot_age_hours": "Hours since the latest observed boot timestamp",
    "reboot_count_6h": "Observed boot-timestamp changes in the prior 6 hours",
    "reboot_count_24h": "Observed boot-timestamp changes in the prior 24 hours",
    "inform_staleness_minutes": "Minutes since the most recent strictly-prior ACS inform",
    "boot_age_hours_missing": "1 when boot_age_hours is unavailable, otherwise 0",
    "reboot_count_6h_missing": "1 when reboot_count_6h is unavailable, otherwise 0",
    "reboot_count_24h_missing": "1 when reboot_count_24h is unavailable, otherwise 0",
    "inform_staleness_minutes_missing": "1 when inform_staleness_minutes is unavailable, otherwise 0",
}


def decompressed_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_guarded_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    lead = pd.to_numeric(frame["time_to_next_outage_minutes"], errors="coerce")
    if bool(lead.dropna().le(0).any()) or bool(lead.dropna().gt(1440).any()):
        raise RuntimeError("Parent lead times fall outside the frozen (0, 1440] minute contract")
    for horizon in HORIZONS:
        expected = lead.le(horizon * 60.0).astype("int8")
        observed = pd.to_numeric(frame[f"outage_next_{horizon}h"], errors="raise").astype("int8")
        if not observed.equals(expected):
            raise RuntimeError(f"Parent {horizon}h labels do not match first-onset lead times")
    excluded = lead.gt(0) & lead.le(GUARD_MINUTES)
    guarded = frame.loc[~excluded].copy()
    guarded_lead = pd.to_numeric(guarded["time_to_next_outage_minutes"], errors="coerce")
    for horizon in HORIZONS:
        endpoint = horizon * 60.0
        outcome = f"outage_next_{horizon}h"
        guarded[outcome] = (guarded_lead.gt(GUARD_MINUTES) & guarded_lead.le(endpoint)).astype("int8")
        positives = guarded[outcome].eq(1)
        if bool((~guarded_lead.loc[positives].gt(GUARD_MINUTES)).any()) or bool(
            (~guarded_lead.loc[positives].le(endpoint)).any()
        ):
            raise RuntimeError(f"Guarded {horizon}h labels violate the frozen interval")

    support: dict[str, object] = {}
    for split in ("train", "validation", "test"):
        original_split = frame["split"].eq(split)
        part = guarded[guarded["split"].eq(split)]
        split_support: dict[str, object] = {
            "excluded_guard_rows": int((excluded & original_split).sum()),
            "eligible_rows": int(len(part)),
            "devices": int(part["device_key"].nunique()),
        }
        for horizon in HORIZONS:
            outcome = f"outage_next_{horizon}h"
            device_classes = part.groupby("device_key", observed=True)[outcome].nunique()
            split_support[f"{horizon}h"] = {
                "positive_rows": int(part[outcome].sum()),
                "control_rows": int(part[outcome].eq(0).sum()),
                "prevalence": float(part[outcome].mean()),
                "positive_devices": int(part.loc[part[outcome].eq(1), "device_key"].nunique()),
                "control_devices": int(part.loc[part[outcome].eq(0), "device_key"].nunique()),
                "mixed_class_devices": int(device_classes.eq(2).sum()),
            }
        support[split] = split_support
    return guarded, {
        "input_rows": int(len(frame)),
        "excluded_guard_rows": int(excluded.sum()),
        "eligible_rows": int(len(guarded)),
        "by_split": support,
    }


def random_forest_pipeline(seed: int) -> Pipeline:
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            (
                "model",
                RandomForestClassifier(
                    n_estimators=300,
                    max_depth=8,
                    min_samples_leaf=100,
                    max_features="sqrt",
                    class_weight=None,
                    random_state=seed,
                    n_jobs=1,
                ),
            ),
        ]
    )


def equal_device_brier_delta(
    frame: pd.DataFrame,
    outcome: str,
    probabilities: dict[str, np.ndarray],
    replicates: int,
    seed: int,
) -> dict[str, object]:
    y = frame[outcome].to_numpy(int)
    deltas: list[float] = []
    for indices in frame.groupby("_device_key", sort=False).indices.values():
        positions = np.asarray(indices, dtype=int)
        acs_error = np.square(y[positions] - probabilities["acs_plus_time"][positions]).mean()
        time_error = np.square(y[positions] - probabilities["time_only"][positions]).mean()
        deltas.append(float(acs_error - time_error))
    rng = np.random.default_rng(seed)
    bootstraps = [
        float(np.mean(rng.choice(deltas, len(deltas), replace=True))) for _ in range(replicates)
    ]
    return {
        "eligible_devices": len(deltas),
        "mean_per_device_brier_delta_vs_time_only": float(np.mean(deltas)),
        "devices_improved": int(np.sum(np.asarray(deltas) < 0)),
        "fraction_devices_improved": float(np.mean(np.asarray(deltas) < 0)),
        "requested_replicates": replicates,
        "ci_low": float(np.percentile(bootstraps, 2.5)),
        "ci_high": float(np.percentile(bootstraps, 97.5)),
    }


def evaluate_family(
    frame: pd.DataFrame,
    family: str,
    builder: Callable[[list[str], int], Pipeline],
) -> dict[str, object]:
    origin = pd.to_datetime(frame["prediction_time_utc"]).min()
    data = add_time_features(frame, origin).rename(columns={"device_key": "_device_key"})
    train = data["split"].eq("train")
    validation = data["split"].eq("validation")
    test = data["split"].eq("test")
    time_trend_cap = float(data.loc[train, "elapsed_days"].max())
    data["elapsed_days"] = data["elapsed_days"].clip(upper=time_trend_cap)
    results: dict[str, object] = {}
    for horizon in HORIZONS:
        outcome = f"outage_next_{horizon}h"
        validation_probabilities: dict[str, np.ndarray] = {}
        test_probabilities: dict[str, np.ndarray] = {}
        for name, columns in (
            ("time_only", list(TIME_FEATURES)),
            ("acs_plus_time", list(TIME_FEATURES) + list(MODEL_FEATURES)),
        ):
            model = builder(columns, SEED + horizon)
            model.fit(data.loc[train, columns], data.loc[train, outcome])
            validation_probabilities[name] = model.predict_proba(data.loc[validation, columns])[:, 1]
            test_probabilities[name] = model.predict_proba(data.loc[test, columns])[:, 1]
        prevalence = float(data.loc[train, outcome].mean())
        validation_probabilities["prevalence"] = np.full(int(validation.sum()), prevalence)
        test_probabilities["prevalence"] = np.full(int(test.sum()), prevalence)

        model_metrics: dict[str, object] = {}
        validation_metrics: dict[str, object] = {}
        test_y = data.loc[test, outcome].to_numpy(int)
        validation_y = data.loc[validation, outcome].to_numpy(int)
        test_warning = data.loc[test, "time_to_next_outage_minutes"].to_numpy(float)
        validation_warning = data.loc[validation, "time_to_next_outage_minutes"].to_numpy(float)
        for name in ("prevalence", "time_only", "acs_plus_time"):
            threshold = choose_threshold(validation_y, validation_probabilities[name])
            model_metrics[name] = metric_summary(
                test_y, test_probabilities[name], threshold, test_warning
            )
            model_metrics[name]["alert_hours_per_device_day"] = 24.0 * float(
                model_metrics[name]["alert_rate"]
            )
            validation_metrics[name] = metric_summary(
                validation_y, validation_probabilities[name], threshold, validation_warning
            )

        test_frame = data.loc[test].reset_index(drop=True)
        uncertainty = clustered_pr_auc_delta(
            test_frame, outcome, test_probabilities, BOOTSTRAP_REPLICATES, SEED + horizon
        )
        equal_ap = equal_device_pr_auc_delta(
            test_frame, outcome, test_probabilities, BOOTSTRAP_REPLICATES, SEED + 100 + horizon
        )
        equal_brier = equal_device_brier_delta(
            test_frame, outcome, test_probabilities, BOOTSTRAP_REPLICATES, SEED + 200 + horizon
        )
        pooled_delta = float(
            model_metrics["acs_plus_time"]["pr_auc"] - model_metrics["time_only"]["pr_auc"]
        )
        pooled_supported = bool(
            pooled_delta > 0
            and uncertainty["pr_auc_vs_time_only"]["ci_low"] is not None
            and uncertainty["pr_auc_vs_time_only"]["ci_low"] > 0
        )
        equal_ap_supported = bool(equal_ap["ci_low"] is not None and equal_ap["ci_low"] > 0)
        equal_brier_supported = bool(
            equal_brier["ci_high"] is not None and equal_brier["ci_high"] < 0
        )
        results[f"{horizon}h"] = {
            "family": family,
            "models": model_metrics,
            "validation_threshold_selection_diagnostics": validation_metrics,
            "pooled_pr_auc_delta_vs_time_only": pooled_delta,
            "conditional_test_device_cluster_bootstrap": uncertainty,
            "equal_device_average_precision_delta": equal_ap,
            "equal_device_brier_delta": equal_brier,
            "pooled_ranking_supported": pooled_supported,
            "equal_device_ranking_supported": equal_ap_supported,
            "equal_device_probability_improvement_supported": equal_brier_supported,
            "consistent_device_aware_signal": bool(
                pooled_supported and equal_ap_supported and equal_brier_supported
            ),
            "time_trend_cap_days_from_training": time_trend_cap,
        }
    return results


def safe_correlation(x: pd.Series, y: pd.Series, method: str) -> float | None:
    valid = x.notna() & y.notna()
    if int(valid.sum()) < 3 or x.loc[valid].nunique() < 2 or y.loc[valid].nunique() < 2:
        return None
    if method == "spearman":
        value = stats.spearmanr(x.loc[valid], y.loc[valid]).statistic
    else:
        value = np.corrcoef(x.loc[valid], y.loc[valid])[0, 1]
    return float(value) if math.isfinite(float(value)) else None


def feature_signal_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for split in ("train", "validation", "test"):
        part = frame[frame["split"].eq(split)].copy()
        for horizon in HORIZONS:
            outcome = f"outage_next_{horizon}h"
            for feature in MODEL_FEATURES:
                observed = part[["device_key", feature, outcome]].dropna()
                x_centered = observed[feature] - observed.groupby("device_key")[feature].transform("mean")
                y_centered = observed[outcome] - observed.groupby("device_key")[outcome].transform("mean")
                rows.append(
                    {
                        "status": STATUS,
                        "split": split,
                        "horizon": f"(30m,{horizon}h]",
                        "feature": feature,
                        "role": "measured" if feature in NUMERIC_FEATURES else "missingness flag",
                        "description": FEATURE_DESCRIPTIONS[feature],
                        "complete_rows": int(len(observed)),
                        "coverage": float(part[feature].notna().mean()),
                        "positive_median": float(observed.loc[observed[outcome].eq(1), feature].median()),
                        "control_median": float(observed.loc[observed[outcome].eq(0), feature].median()),
                        "pooled_spearman_r": safe_correlation(observed[feature], observed[outcome], "spearman"),
                        "within_device_centered_r": safe_correlation(x_centered, y_centered, "pearson"),
                    }
                )
    return pd.DataFrame(rows)


def flatten_model_results(evaluations: dict[str, dict[str, object]]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for family, family_results in evaluations.items():
        for horizon_key, result in family_results.items():
            for model_name, metrics in result["models"].items():
                row = {
                    "status": STATUS,
                    "family": family,
                    "horizon": f"(30m,{horizon_key}]",
                    "model": model_name,
                    **metrics,
                    "pooled_pr_auc_delta_vs_same_family_time_only": None,
                    "pooled_delta_ci_low": None,
                    "pooled_delta_ci_high": None,
                    "equal_device_ap_delta": None,
                    "equal_device_ap_ci_low": None,
                    "equal_device_ap_ci_high": None,
                    "fraction_mixed_devices_improved_ap": None,
                    "equal_device_brier_delta": None,
                    "equal_device_brier_ci_low": None,
                    "equal_device_brier_ci_high": None,
                    "fraction_devices_improved_brier": None,
                    "consistent_device_aware_signal": None,
                }
                if model_name == "acs_plus_time":
                    pooled = result["conditional_test_device_cluster_bootstrap"]["pr_auc_vs_time_only"]
                    equal_ap = result["equal_device_average_precision_delta"]
                    equal_brier = result["equal_device_brier_delta"]
                    row.update(
                        {
                            "pooled_pr_auc_delta_vs_same_family_time_only": result[
                                "pooled_pr_auc_delta_vs_time_only"
                            ],
                            "pooled_delta_ci_low": pooled["ci_low"],
                            "pooled_delta_ci_high": pooled["ci_high"],
                            "equal_device_ap_delta": equal_ap[
                                "mean_per_device_pr_auc_delta_vs_time_only"
                            ],
                            "equal_device_ap_ci_low": equal_ap["ci_low"],
                            "equal_device_ap_ci_high": equal_ap["ci_high"],
                            "fraction_mixed_devices_improved_ap": equal_ap[
                                "fraction_devices_improved"
                            ],
                            "equal_device_brier_delta": equal_brier[
                                "mean_per_device_brier_delta_vs_time_only"
                            ],
                            "equal_device_brier_ci_low": equal_brier["ci_low"],
                            "equal_device_brier_ci_high": equal_brier["ci_high"],
                            "fraction_devices_improved_brier": equal_brier[
                                "fraction_devices_improved"
                            ],
                            "consistent_device_aware_signal": result[
                                "consistent_device_aware_signal"
                            ],
                        }
                    )
                rows.append(row)
    return pd.DataFrame(rows)


def assert_aggregate_outputs(model_results: pd.DataFrame, signals: pd.DataFrame) -> dict[str, object]:
    checks = {
        "model_results_status": bool(
            "status" in model_results and model_results["status"].eq(STATUS).all()
        ),
        "feature_signal_status": bool("status" in signals and signals["status"].eq(STATUS).all()),
        "model_results_has_no_prohibited_columns": not bool(
            set(map(str.lower, model_results.columns)) & PROHIBITED_OUTPUT_COLUMNS
        ),
        "feature_signal_has_no_prohibited_columns": not bool(
            set(map(str.lower, signals.columns)) & PROHIBITED_OUTPUT_COLUMNS
        ),
        "model_results_is_aggregate": len(model_results) == 12,
        "feature_signal_is_aggregate": len(signals) == 48,
    }
    if not all(checks.values()):
        raise RuntimeError(f"Aggregate output/privacy assertion failed: {checks}")
    return checks


def fmt(value: object, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def make_report(
    support: dict[str, object],
    evaluations: dict[str, dict[str, object]],
    signals: pd.DataFrame,
) -> str:
    lines = [
        "# Guarded ACS prediction sensitivity",
        "",
        f"**Status:** `{STATUS}`",
        "",
        "This source-free run excludes formal outages in the first 30 minutes, then asks whether the frozen ACS feature bundle improves prediction over time alone. It is not confirmation because every current split was already inspected.",
        "",
        "## Risk set",
        "",
        "| Split | Excluded in first 30m | Eligible rows | (30m,6h] positives | (30m,24h] positives |",
        "|---|---:|---:|---:|---:|",
    ]
    for split in ("train", "validation", "test"):
        item = support["by_split"][split]
        lines.append(
            f"| {split} | {item['excluded_guard_rows']:,} | {item['eligible_rows']:,} | "
            f"{item['6h']['positive_rows']:,} ({item['6h']['prevalence']:.1%}) | "
            f"{item['24h']['positive_rows']:,} ({item['24h']['prevalence']:.1%}) |"
        )
    lines.extend(
        [
            "",
            "## Prediction result",
            "",
            "AP/PR-AUC measures ranking quality; its no-skill reference is the test positive rate. Positive AP deltas favor ACS. Negative Brier deltas favor ACS probability accuracy.",
            "",
            "| Horizon | Model | Time-only AP | ACS+time AP | Pooled AP delta (95% CI) | Equal-device AP delta (95% CI) | Equal-device Brier delta (95% CI) | ACS alert-hours/device-day | Device-aware gate? |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for family in ("logistic", "random_forest"):
        for horizon in HORIZONS:
            result = evaluations[family][f"{horizon}h"]
            time_metrics = result["models"]["time_only"]
            acs_metrics = result["models"]["acs_plus_time"]
            pooled = result["conditional_test_device_cluster_bootstrap"]["pr_auc_vs_time_only"]
            equal_ap = result["equal_device_average_precision_delta"]
            equal_brier = result["equal_device_brier_delta"]
            lines.append(
                f"| (30m,{horizon}h] | {family.replace('_', ' ')} | {time_metrics['pr_auc']:.3f} | "
                f"{acs_metrics['pr_auc']:.3f} | {result['pooled_pr_auc_delta_vs_time_only']:+.3f} "
                f"[{pooled['ci_low']:+.3f}, {pooled['ci_high']:+.3f}] | "
                f"{equal_ap['mean_per_device_pr_auc_delta_vs_time_only']:+.3f} "
                f"[{equal_ap['ci_low']:+.3f}, {equal_ap['ci_high']:+.3f}] | "
                f"{equal_brier['mean_per_device_brier_delta_vs_time_only']:+.3f} "
                f"[{equal_brier['ci_low']:+.3f}, {equal_brier['ci_high']:+.3f}] | "
                f"{acs_metrics['alert_hours_per_device_day']:.1f} | "
                f"{'yes' if result['consistent_device_aware_signal'] else 'no'} |"
            )

    primary = evaluations["logistic"]["6h"]
    primary_acs = primary["models"]["acs_plus_time"]
    primary_time = primary["models"]["time_only"]
    verdict = (
        "The prespecified primary comparison passes the device-aware exploratory gate. It still requires untouched-future confirmation."
        if primary["consistent_device_aware_signal"]
        else "The prespecified primary comparison does not pass the device-aware predictive gate."
    )
    lines.extend(
        [
            "",
            f"**Primary verdict:** {verdict}",
            "",
            f"At its validation-selected threshold, primary ACS+time precision is {primary_acs['precision']:.1%}, recall is {primary_acs['recall']:.1%}, and it marks {primary_acs['alert_rate']:.1%} of device-hours versus {primary_time['alert_rate']:.1%} for time-only. Its Brier error is {primary_acs['brier']:.3f} versus {primary['models']['prevalence']['brier']:.3f} for the prevalence baseline, and it overpredicts absolute risk by {primary_acs['calibration_gap']:.3f}.",
            "",
            "## Exact input columns and test correlation",
            "",
            "These are descriptive correlations, not independent hypothesis tests. Pooled correlation mixes differences between devices; the within-device value asks whether a change for the same device tracks changing risk.",
            "",
            "| Exact column | Meaning | 6h pooled r | 6h within-device r | 24h pooled r | 24h within-device r |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    test = signals[signals["split"].eq("test")]
    for feature in MODEL_FEATURES:
        six = test[(test["feature"].eq(feature)) & (test["horizon"].eq("(30m,6h]"))].iloc[0]
        day = test[(test["feature"].eq(feature)) & (test["horizon"].eq("(30m,24h]"))].iloc[0]
        lines.append(
            f"| `{feature}` | {FEATURE_DESCRIPTIONS[feature]} | {fmt(six['pooled_spearman_r'])} | "
            f"{fmt(six['within_device_centered_r'])} | {fmt(day['pooled_spearman_r'])} | "
            f"{fmt(day['within_device_centered_r'])} |"
        )
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- This changes the outcome window, not the telemetry-history windows: prior 6-hour and 24-hour reboot history is already embedded in the exact count columns.",
            "- The three reboot measurements all derive from `$._lastBoot`; inform staleness is timing metadata, not a new device-health sensor.",
            "- Device bootstrap intervals are conditional on fitted models. Overlapping hourly anchors and shared incidents can make the effective sample smaller than the row count.",
            "- Large train/validation/test prevalence drift can damage calibration. Devices recur across splits, so this is temporal stability rather than unseen-device generalization.",
            "- A formal outage timestamp can lag real degradation. The action gap reduces, but does not eliminate, that proxy risk.",
            "- No model, horizon, or feature was selected from these test results. Confirmation requires this frozen protocol on genuinely untouched data after 2026-08-10.",
            "",
        ]
    )
    return "\n".join(lines)


def self_check() -> None:
    sample = pd.DataFrame(
        {
            "device_key": ["D1"] * 6,
            "prediction_time_utc": pd.date_range("2026-01-01", periods=6, freq="h"),
            "split": ["train"] * 6,
            **{feature: [0.0] * 6 for feature in MODEL_FEATURES},
            "outage_next_6h": [1, 1, 1, 1, 0, 0],
            "outage_next_24h": [1, 1, 1, 1, 1, 0],
            "time_to_next_outage_minutes": [10.0, 30.0, 31.0, 360.0, 361.0, np.nan],
        }
    )
    guarded, audit = make_guarded_frame(sample)
    assert audit["excluded_guard_rows"] == 2 and len(guarded) == 4
    assert guarded["outage_next_6h"].tolist() == [1, 1, 0, 0]
    assert guarded["outage_next_24h"].tolist() == [1, 1, 1, 0]
    assert not (set(MODEL_FEATURES) & PROHIBITED_MODEL_COLUMNS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen 30-minute guarded ACS prediction sensitivity")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--parent-audit", type=Path, default=DEFAULT_PARENT_AUDIT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return
    if args.output_dir is None:
        raise SystemExit("--output-dir is required unless --self-check is used")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise RuntimeError("Output directory must be new or empty")

    observed_hashes = {
        "parent_model_frame_gzip": file_hash(args.input),
        "parent_model_frame_content": decompressed_hash(args.input),
        "parent_audit": file_hash(args.parent_audit),
        "canonical_helper": local_file_hash(_HELPER_PATH),
    }
    expected_hashes = {
        "parent_model_frame_gzip": FROZEN_INPUT_SHA256,
        "parent_model_frame_content": FROZEN_INPUT_CONTENT_SHA256,
        "parent_audit": FROZEN_PARENT_AUDIT_SHA256,
        "canonical_helper": FROZEN_HELPER_SHA256,
    }
    if observed_hashes != expected_hashes:
        raise RuntimeError(f"Frozen input hash mismatch: expected {expected_hashes}, observed {observed_hashes}")

    frame = pd.read_csv(args.input, usecols=list(REQUIRED_COLUMNS))
    if set(frame["split"].unique()) != {"train", "validation", "test"}:
        raise RuntimeError("Parent frame does not contain the frozen three split labels")
    if frame.duplicated(["device_key", "prediction_time_utc"]).any():
        raise RuntimeError("Parent frame contains duplicate device-hour anchors")
    if set(MODEL_FEATURES) & PROHIBITED_MODEL_COLUMNS:
        raise RuntimeError("A prohibited column entered the model manifest")

    guarded, support = make_guarded_frame(frame)
    for split in ("train", "validation", "test"):
        for horizon in HORIZONS:
            part = guarded[guarded["split"].eq(split)]
            if part[f"outage_next_{horizon}h"].nunique() != 2:
                raise RuntimeError(f"{split} lacks both guarded {horizon}h classes")
            item = support["by_split"][split][f"{horizon}h"]
            if split in {"validation", "test"} and (
                item["positive_devices"] < MIN_HELDOUT_CLASS_DEVICES
                or item["control_devices"] < MIN_HELDOUT_CLASS_DEVICES
            ):
                raise RuntimeError(f"{split} lacks held-out device support for guarded {horizon}h")
            if split == "test" and item["mixed_class_devices"] < MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES:
                raise RuntimeError(f"Test lacks mixed-class device support for guarded {horizon}h")

    logistic_builder = lambda columns, seed: logistic_pipeline(columns, seed)
    forest_builder = lambda columns, seed: random_forest_pipeline(seed)
    evaluations = {
        "logistic": evaluate_family(guarded, "logistic", logistic_builder),
        "random_forest": evaluate_family(guarded, "random_forest", forest_builder),
    }
    signals = feature_signal_table(guarded)
    model_results = flatten_model_results(evaluations)
    privacy_checks = assert_aggregate_outputs(model_results, signals)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / "model_results.csv"
    signal_path = args.output_dir / "feature_signal.csv"
    report_path = args.output_dir / "report.md"
    audit_path = args.output_dir / "audit.json"
    model_results.to_csv(model_path, index=False)
    signals.to_csv(signal_path, index=False)
    report_path.write_text(make_report(support, evaluations, signals))
    output_hashes = {
        "model_results.csv": file_hash(model_path),
        "feature_signal.csv": file_hash(signal_path),
        "report.md": file_hash(report_path),
    }
    audit = {
        "status": STATUS,
        "analysis_version": ANALYSIS_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_access": "immutable local v14 model frame only; no database or network access",
        "source_writes_attempted": False,
        "protocol": {
            "guard_minutes": GUARD_MINUTES,
            "endpoints_minutes": {"6h": 360, "24h": 1440},
            "guard_failures": "excluded, never relabelled as controls",
            "split_assignments": "preserved from parent v14",
            "primary_contrast": "logistic ACS+time minus logistic time-only for (30m,6h]",
            "secondary_contrast": "logistic (30m,24h]",
            "nonlinear_sensitivity": "fixed untuned random forest at both endpoints",
            "model_features": list(MODEL_FEATURES),
            "time_features": list(TIME_FEATURES),
            "validation_threshold_rule": "maximum F1 on retained validation rows; ties choose higher threshold",
            "bootstrap_unit": "device; conditional on fitted models",
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "minimum_heldout_class_devices": MIN_HELDOUT_CLASS_DEVICES,
            "minimum_test_mixed_class_devices": MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES,
            "device_aware_gate": (
                "pooled AP delta device-bootstrap 95% lower bound > 0 AND equal-device AP delta "
                "device-bootstrap 95% lower bound > 0 AND equal-device Brier delta device-bootstrap "
                "95% upper bound < 0"
            ),
            "random_forest": {
                "n_estimators": 300,
                "max_depth": 8,
                "min_samples_leaf": 100,
                "max_features": "sqrt",
                "class_weight": None,
                "tuned": False,
            },
        },
        "parent_hashes_expected": expected_hashes,
        "parent_hashes_observed": observed_hashes,
        "implementation_hashes": {
            "analysis_script": file_hash(Path(__file__).resolve()),
            "protocol_spec": file_hash(
                ROOT
                / "openspec"
                / "changes"
                / "explore-guarded-acs-prediction"
                / "specs"
                / "acs-guarded-prediction-sensitivity"
                / "spec.md"
            ),
        },
        "support": support,
        "evaluations": evaluations,
        "output_hashes": output_hashes,
        "privacy": {
            "row_level_predictions_written": False,
            "direct_identifiers_written": False,
            "aggregate_outputs_only": True,
            "assertions": privacy_checks,
        },
        "software_versions": {
            "python": platform.python_version(),
            "numpy": metadata.version("numpy"),
            "pandas": metadata.version("pandas"),
            "scipy": metadata.version("scipy"),
            "scikit_learn": metadata.version("scikit-learn"),
        },
        "interpretation": {
            "current_window_can_confirm_predictor": False,
            "canonical_v14_decision_changed": False,
            "untouched_future_confirmation_required": True,
            "primary_consistent_device_aware_signal": evaluations["logistic"]["6h"][
                "consistent_device_aware_signal"
            ],
        },
        "reproducible_command_template": (
            "python acs_outage_prediction/acs_guarded_prediction.py "
            "--output-dir <new-empty-output-dir>"
        ),
    }
    write_json(audit_path, native(audit))
    print(report_path)


if __name__ == "__main__":
    main()
