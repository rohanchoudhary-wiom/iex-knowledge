from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
BOOKING_TRUTH = WORKSPACE.parent / "booking_truth"
DEFAULT_SOURCE_AUDIT = ROOT / "outputs" / "acs_parameter_source_audit_2026-08-11" / "audit.json"
FROZEN_SOURCE_AUDIT_SHA256 = "8b7a51393074978f3fb72740cc73046be0c85d863970642ef334d9d0fd906489"

MASTER_DEVICE = "PROD_DB.MASTER_DB_READ_DBO.T_DEVICE"
PUBLIC_DEVICE = "PROD_DB.PUBLIC.T_DEVICE"
ACTIVE_BASE = "PROD_DB.DBT.ACTIVE_BASE"
INCIDENTS = "PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENTS"
IMPACTED = "PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.INCIDENT_IMPACTED_DEVICE"

STATUS = "FEASIBILITY_PILOT_ONLY"
SOURCE_WHITELIST_VERSION = "p1-2026-08-11"
SERVICE_TIMEZONE = "Asia/Kolkata"
LOOKBACK_HOURS = 24
TELEMETRY_CHUNK_DAYS = 7
MAX_LABEL_HORIZON_HOURS = 24
MAX_BOOT_AGE_DAYS = 3652
MIN_COVERAGE = 0.25
MIN_TRAIN_DEVICES = 20
MIN_DEVICE_OBSERVATIONS = 3
MIN_VARYING_DEVICE_SHARE = 0.10
MIN_BOOT_TRANSITIONS = 5
MIN_HELDOUT_FEATURE_DEVICES = 20
MIN_HELDOUT_CLASS_DEVICES = 10
MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES = 20
MAX_ABSOLUTE_CALIBRATION_GAP = 0.05
MAX_EXPECTED_CALIBRATION_ERROR = 0.10
BOOTSTRAP_REPLICATES = 1000
RANDOM_SEED = 20260811
MIN_VALID_BOOTSTRAP_FRACTION = 0.95

ACS_FEATURES = (
    "boot_age_hours",
    "reboot_count_1h",
    "reboot_count_6h",
    "reboot_count_24h",
    "inform_staleness_minutes",
)
TIME_FEATURES = ("hour_sin", "hour_cos", "dow_sin", "dow_cos", "elapsed_days")
PROHIBITED_COLUMNS = {
    "account_id",
    "device_id",
    "incident_id",
    "nasid",
    "serial_number",
    "mobile",
    "ip",
    "ip_address",
    "ssid",
    "params_json",
    "last_boot_timestamp",
}

REFERENCE_EVIDENCE = {
    "mapping": {
        "acs_devices": 660,
        "unique_noncolliding_serials": 644,
        "exact_master_pon_matches": 251,
        "one_to_one_master_matches": 251,
        "one_to_one_public_device_nas_bridges": 249,
        "strict_customer_v2_mapped_devices": 81,
        "formal_outage_observed_devices": 77,
        "mapping_coverage": 0.1227,
        "ambiguous_mappings_excluded": 2,
    },
    "source_readiness": {
        "sampled_snapshots": 82_867,
        "ordinary_leaf_middle_fresh_coverage": 0.00589,
        "ordinary_leaf_late_fresh_coverage": 0.00350,
        "last_inform_has_no_within_device_variation": True,
        "flat_dynamic_fields_have_no_late_within_device_variation": True,
        "flat_optical_coverage": 0.0,
        "only_source_ready_p1_candidate": "$._lastBoot",
    },
    "snowflake_query_ids": {
        "cross_inventory_audit": "01c6512b-0002-7659-0009-01fa2671b17e",
        "canonical_mapping_audit": "01c6512c-0002-7674-0009-01fa2671abbe",
        "mapped_outcome_hour_audit": "01c6512c-0002-7749-0009-01fa2671c18a",
    },
}


def normalize_serial(value: object) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return normalized or None


def one_to_one_pairs(pairs: list[tuple[object, object]]) -> list[tuple[object, object]]:
    left: dict[object, set[object]] = defaultdict(set)
    right: dict[object, set[object]] = defaultdict(set)
    for a, b in pairs:
        if a is not None and b is not None:
            left[a].add(b)
            right[b].add(a)
    return sorted((a, next(iter(bs))) for a, bs in left.items() if len(bs) == 1 and len(right[next(iter(bs))]) == 1)


def ordinary_leaf_is_fresh(value_time: object, inform_time: object) -> bool:
    value = pd.to_datetime(value_time, utc=True, errors="coerce")
    inform = pd.to_datetime(inform_time, utc=True, errors="coerce")
    return bool(pd.notna(value) and pd.notna(inform) and value <= inform and inform - value <= pd.Timedelta(hours=24))


def valid_boot_time(value: object, inform_time: object) -> pd.Timestamp | pd.NaT:
    boot = pd.to_datetime(value, utc=True, errors="coerce")
    inform = pd.to_datetime(inform_time, utc=True, errors="coerce")
    if pd.isna(boot) or pd.isna(inform) or boot > inform or inform - boot > pd.Timedelta(days=MAX_BOOT_AGE_DAYS):
        return pd.NaT
    return boot.tz_convert(None)


def boot_transition_flags(boot_times: list[object]) -> list[bool]:
    flags: list[bool] = []
    previous: pd.Timestamp | None = None
    for value in boot_times:
        current = pd.to_datetime(value, errors="coerce")
        changed = bool(pd.notna(current) and previous is not None and current != previous)
        flags.append(changed)
        if pd.notna(current):
            previous = current
    return flags


def strict_prior_index(times: np.ndarray, anchor: np.datetime64) -> int | None:
    index = int(np.searchsorted(times, anchor, side="left")) - 1
    if index < 0 or anchor - times[index] > np.timedelta64(LOOKBACK_HOURS, "h"):
        return None
    return index


def future_outcome(anchor: pd.Timestamp, starts: list[pd.Timestamp], durations: list[float], horizon_hours: int) -> tuple[int, float | None, float | None]:
    candidates = [(start, duration) for start, duration in zip(starts, durations) if anchor < start <= anchor + pd.Timedelta(hours=horizon_hours)]
    if not candidates:
        return 0, None, None
    start, duration = min(candidates, key=lambda item: item[0])
    return 1, (start - anchor).total_seconds() / 60, duration


def outcome_observation_end(anchor_end_exclusive: pd.Timestamp) -> pd.Timestamp:
    last_scheduled_anchor = anchor_end_exclusive.floor("h") - pd.Timedelta(hours=1)
    return last_scheduled_anchor + pd.Timedelta(hours=MAX_LABEL_HORIZON_HOURS)


def bounded_outage_duration(
    start: pd.Timestamp,
    duration_minutes: float,
    observation_end: pd.Timestamp,
    closure_complete: bool,
) -> tuple[pd.Timestamp, float, bool]:
    reported_end = start + pd.to_timedelta(duration_minutes, unit="m")
    unavailable = bool(not closure_complete or reported_end > observation_end)
    risk_end = min(reported_end, observation_end)
    return risk_end, (float("nan") if unavailable else duration_minutes), unavailable


def inside_active_outage(anchor: pd.Timestamp, starts: list[pd.Timestamp], ends: list[pd.Timestamp]) -> bool:
    return any(start <= anchor < end for start, end in zip(starts, ends))


def split_horizon_isolated(
    anchor: pd.Timestamp, split: str, next_split_start: object, horizon_hours: int = 24
) -> bool:
    if split == "test":
        return True
    boundary = pd.to_datetime(next_split_start, errors="coerce")
    return bool(pd.notna(boundary) and anchor + pd.Timedelta(hours=horizon_hours) < boundary)


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


def choose_threshold(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    if len(np.unique(y_true)) != 2:
        raise ValueError("Validation threshold requires both outcome classes")
    precision, recall, thresholds = precision_recall_curve(y_true, probabilities)
    precision, recall = precision[:-1], recall[:-1]
    denominator = precision + recall
    f1 = np.divide(2 * precision * recall, denominator, out=np.zeros_like(denominator), where=denominator > 0)
    maximum = float(np.max(f1))
    return float(np.max(thresholds[np.isclose(f1, maximum, rtol=1e-12, atol=1e-15)]))


def calibration_error(y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for low, high in zip(edges[:-1], edges[1:]):
        mask = (probabilities >= low) & (probabilities < high if high < 1 else probabilities <= high)
        if mask.any():
            total += mask.mean() * abs(float(y_true[mask].mean()) - float(probabilities[mask].mean()))
    return float(total)


def metric_summary(y_true: np.ndarray, probabilities: np.ndarray, threshold: float, warning_minutes: np.ndarray) -> dict[str, float | int | None]:
    predicted = probabilities >= threshold
    true_warnings = warning_minutes[(predicted == 1) & (y_true == 1)]
    negatives = y_true == 0
    return {
        "rows": int(len(y_true)),
        "positives": int(y_true.sum()),
        "prevalence": float(y_true.mean()),
        "pr_auc": float(average_precision_score(y_true, probabilities)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "specificity": float(np.mean(predicted[negatives] == 0)) if negatives.any() else None,
        "alert_rate": float(predicted.mean()),
        "brier": float(brier_score_loss(y_true, probabilities)),
        "mean_probability": float(probabilities.mean()),
        "calibration_gap": float(probabilities.mean() - y_true.mean()),
        "expected_calibration_error_10bin": calibration_error(y_true, probabilities),
        "threshold_selected_on_validation": float(threshold),
        "true_positive_warning_minutes_median": float(np.nanmedian(true_warnings)) if len(true_warnings) else None,
        "true_positive_warning_minutes_mean": float(np.nanmean(true_warnings)) if len(true_warnings) else None,
    }


def self_check() -> None:
    assert normalize_serial(" ab-c:12 ") == "ABC12"
    assert utc_to_service_time("2026-01-01T00:00:00Z") == pd.Timestamp("2026-01-01T05:30:00")
    assert one_to_one_pairs([(1, "a"), (2, "b"), (2, "c"), (3, "a")]) == []
    assert one_to_one_pairs([(1, "a"), (2, "b")]) == [(1, "a"), (2, "b")]
    assert ordinary_leaf_is_fresh("2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z")
    assert not ordinary_leaf_is_fresh("2026-01-02T00:00:00Z", "2026-01-01T12:00:00Z")
    assert boot_transition_flags(["2026-01-01", "2026-01-01", None, "2026-01-02"]) == [False, False, False, True]
    times = np.array(["2026-01-01T00", "2026-01-01T01"], dtype="datetime64[h]")
    assert strict_prior_index(times, np.datetime64("2026-01-01T01")) == 0
    assert strict_prior_index(times, np.datetime64("2026-01-02T02")) is None
    anchor = pd.Timestamp("2026-01-01T00:00:00")
    assert future_outcome(anchor, [anchor, anchor + pd.Timedelta(hours=2)], [10, 20], 6) == (1, 120.0, 20)
    assert future_outcome(anchor, [anchor + pd.Timedelta(hours=6)], [20], 6)[0] == 1
    assert future_outcome(anchor, [anchor + pd.Timedelta(hours=24)], [20], 24)[0] == 1
    assert future_outcome(anchor, [anchor + pd.Timedelta(hours=6, seconds=1)], [20], 6)[0] == 0
    observation_end = outcome_observation_end(anchor + pd.Timedelta(hours=1))
    assert observation_end == anchor + pd.Timedelta(hours=24)
    assert outcome_observation_end(anchor + pd.Timedelta(hours=1, minutes=30)) == observation_end
    bounded_end, bounded_duration, unavailable = bounded_outage_duration(
        anchor + pd.Timedelta(hours=23), 120.0, observation_end, True
    )
    assert bounded_end == observation_end and math.isnan(bounded_duration) and unavailable
    complete_end, complete_duration, unavailable = bounded_outage_duration(
        anchor + pd.Timedelta(hours=22), 60.0, observation_end, True
    )
    assert complete_end == anchor + pd.Timedelta(hours=23) and complete_duration == 60.0 and not unavailable
    open_end, open_duration, unavailable = bounded_outage_duration(
        anchor, 60.0, observation_end, False
    )
    assert open_end == anchor + pd.Timedelta(hours=1) and math.isnan(open_duration) and unavailable
    assert inside_active_outage(anchor, [anchor - pd.Timedelta(minutes=1)], [anchor + pd.Timedelta(minutes=1)])
    assert not inside_active_outage(anchor, [anchor - pd.Timedelta(minutes=1)], [anchor])
    assert split_horizon_isolated(anchor, "train", anchor + pd.Timedelta(hours=25))
    assert not split_horizon_isolated(anchor, "train", anchor + pd.Timedelta(hours=24))
    assert split_horizon_isolated(anchor, "test", None)
    assert np.allclose(bh_adjust([0.01, 0.04, 0.03]), [0.03, 0.04, 0.04])
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.6, 0.9])
    assert choose_threshold(y, p) == 0.6
    metric = metric_summary(y, p, 0.5, np.array([np.nan, np.nan, 30.0, 60.0]))
    assert metric["pr_auc"] == 1.0 and metric["specificity"] == 1.0 and metric["alert_rate"] == 0.5
    mapping_passed, _checks = mapping_reproduction(
        {"source_device_count": 660, "one_to_one_serial_candidates": 644},
        {
            "exact_master_matches": 251,
            "one_to_one_master_matches": 251,
            "one_to_one_public_bridges": 249,
            "strict_customer_v2_mappings": 81,
        },
    )
    assert mapping_passed
    grouped, grouping_audit = finalize_output_groups(
        pd.DataFrame(
            {
                "_device_key": [2, 2, 5, 5],
                "device_key": ["old", "old", "old", "old"],
                "partner_group": ["P01", "P01", "P01", "P01"],
            }
        )
    )
    assert grouped["device_key"].tolist() == ["D0001", "D0001", "D0002", "D0002"]
    assert grouping_audit["pseudonyms_contiguous"] and set(grouped["partner_group"]) == {"OTHER"}
    assert not any(feature.startswith("inform_count_") for feature in ACS_FEATURES)
    risk_features = pd.DataFrame(
        {
            "_device_key": [1, 1],
            "prediction_time_utc": [anchor, anchor + pd.Timedelta(hours=2)],
            "split": ["test", "test"],
            "_next_split_start_utc": [pd.NaT, pd.NaT],
        }
    )
    risk_outages = pd.DataFrame(
        {
            "acs_key": [1, 1],
            "incident_key": [1, 2],
            "start_utc": [anchor - pd.Timedelta(hours=1), anchor + pd.Timedelta(hours=3)],
            "end_utc": [anchor + pd.Timedelta(hours=1), anchor + pd.Timedelta(hours=4)],
            "duration_minutes": [120.0, 60.0],
            "onset_in_service": [False, False],
        }
    )
    risk_frame, risk_audit = attach_outcomes(risk_features, risk_outages)
    assert len(risk_frame) == 1 and risk_audit["active_outage_excluded_rows"] == 1
    assert int(risk_frame.iloc[0]["outage_next_6h"]) == 0
    try:
        assert_private_frame(pd.DataFrame({"device_id": ["forbidden"]}))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Privacy check accepted a direct identifier")


def load_env(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_utc_argument(value: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="raise")
    return timestamp.tz_convert(None)


def utc_to_service_time(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="raise")
    return timestamp.tz_convert(SERVICE_TIMEZONE).tz_localize(None)


def native(value: Any) -> Any:
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


def sql_hash(sql: str) -> str:
    return hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_source_audit(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"Frozen source-audit artifact not found: {path}")
    source = json.loads(path.read_text())
    windows = source.get("source", {}).get("windows", {})
    configuration = source.get("configuration", {})
    expected_thresholds = {
        "categorical_levels_each_window": 2,
        "categorical_minority_devices_each_window": 10,
        "categorical_minority_share_each_window": 0.01,
        "categorical_or_boot_transitions_each_window": 5,
        "coverage_each_window": 0.25,
        "flat_devices_with_3_observations_each_window": 20,
        "numeric_iqr_positive_each_window": True,
        "numeric_varying_device_share_each_window": 0.1,
        "p1_devices_with_3_fresh_observations_each_window": 20,
    }
    expected_flat_fields = [
        "uptime_s",
        "cpu_pct",
        "memory_free_kb",
        "memory_total_kb",
        "temperature_c",
        "optical_rx_dbm",
        "optical_tx_dbm",
        "inform_time",
        "param_count",
    ]
    expected_windows = [
        {"name": "early", "start": "2026-06-02T00:00:00+00:00", "end": "2026-06-05T00:00:00+00:00"},
        {"name": "middle", "start": "2026-07-05T00:00:00+00:00", "end": "2026-07-08T00:00:00+00:00"},
        {"name": "late", "start": "2026-08-07T00:00:00+00:00", "end": "2026-08-10T00:00:00+00:00"},
    ]
    artifact_sha256 = file_hash(path)
    checks = {
        "artifact_sha256": artifact_sha256 == FROZEN_SOURCE_AUDIT_SHA256,
        "schema_version": source.get("schema_version") == 1,
        "status": source.get("status") == "SOURCE_TRANSFER_SCREEN_COMPLETE",
        "decision": source.get("decision") == "CONTINUE_TO_MAPPED_COHORT_AUDIT",
        "eligible_flat_features": source.get("eligible_flat_features") == [],
        "eligible_p1_features": source.get("eligible_p1_features") == ["p1.last_boot"],
        "sampled_snapshots": sum(int(item.get("rows", 0)) for item in windows.values()) == 82_867,
        "audit_windows": configuration.get("windows") == expected_windows,
        "source_table": configuration.get("table") == "acs_raw_dump",
        "timezone": configuration.get("timezone") == "UTC"
        and source.get("source", {}).get("session_time_zone") == "+00:00",
        "window_end": configuration.get("window_end") == "exclusive",
        "flat_fields": configuration.get("flat_fields") == expected_flat_fields,
        "p1_whitelist_size": configuration.get("p1_whitelist_size") == 16,
        "freshness_hours": configuration.get("freshness_hours") == 24,
        "maximum_boot_age_days": configuration.get("maximum_boot_age_days") == MAX_BOOT_AGE_DAYS,
        "thresholds": configuration.get("thresholds") == expected_thresholds,
        "feature_count": len(source.get("features", [])) == 24,
        "privacy": source.get("privacy", {}).get("passed") is True,
    }
    return {
        "passed": all(checks.values()),
        "artifact_name": path.name,
        "artifact_sha256": artifact_sha256,
        "source_table": configuration.get("table"),
        "checks": checks,
        "sampled_snapshots": sum(int(item.get("rows", 0)) for item in windows.values()),
        "audit_windows_utc": expected_windows,
        "authorized_json_reads": ["$._lastBoot"],
        "authorized_non_json_fields": ["inform_time"],
        "ordinary_p1_candidates": "failed frozen source-readiness audit and are not queried",
    }


def import_mysql_connector(site_packages: Path | None):
    inserted: str | None = None
    if site_packages is not None:
        inserted = str(site_packages.resolve())
        if not site_packages.is_dir():
            raise FileNotFoundError(f"MySQL site-packages directory not found: {site_packages}")
        sys.path.insert(0, inserted)
    try:
        import mysql.connector as connector
    finally:
        if inserted is not None:
            sys.path.remove(inserted)
    version = str(getattr(connector, "__version__", "0"))
    match = re.match(r"(\d+)", version)
    if match is None or int(match.group(1)) < 8:
        raise RuntimeError(f"mysql.connector >= 8 is required; imported {version}. Use --mysql-site-packages.")
    return connector


def connect_mysql(connector):
    required = ("ACS_DUMP_DB_HOST", "ACS_DUMP_DB_USER", "ACS_DUMP_DB_PASSWORD", "ACS_DUMP_DB_NAME")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing MySQL environment variables: {', '.join(missing)}")
    connection = connector.connect(
        host=os.environ["ACS_DUMP_DB_HOST"],
        port=int(os.environ.get("ACS_DUMP_DB_PORT", "3306")),
        user=os.environ["ACS_DUMP_DB_USER"],
        password=os.environ["ACS_DUMP_DB_PASSWORD"],
        database=os.environ["ACS_DUMP_DB_NAME"],
        connection_timeout=20,
        read_timeout=900,
    )
    cursor = connection.cursor()
    cursor.execute("SET SESSION time_zone = '+00:00'")
    cursor.close()
    connection.start_transaction(readonly=True)
    return connection


def mysql_session_timezone(connection) -> str:
    cursor = connection.cursor()
    cursor.execute("SELECT @@session.time_zone")
    value = str(cursor.fetchone()[0])
    cursor.close()
    if value not in {"+00:00", "UTC"}:
        raise RuntimeError(f"MySQL session timezone must be UTC, observed {value!r}")
    return value


def connect_snowflake(network_timeout: int = 300):
    helper_path = str(BOOKING_TRUTH)
    sys.path.insert(0, helper_path)
    try:
        from data_lib.data_fetch.wiom_data import WiomData
    finally:
        sys.path.remove(helper_path)
    client = WiomData("snowflake")
    client._connection_params.update(
        login_timeout=20,
        network_timeout=network_timeout,
        ocsp_response_cache_filename=str(ROOT / ".ocsp_cache.json"),
    )
    return client._connect()


def mysql_identifier_candidates(connection, table: str, end: pd.Timestamp) -> tuple[list[tuple[int, str, str]], dict[str, int], str]:
    sql = f"""
        SELECT device_id, serial_number
        FROM `{table}`
        WHERE inform_time < %s
        GROUP BY device_id, serial_number
    """
    cursor = connection.cursor(dictionary=True)
    cursor.execute(sql, (end.to_pydatetime(),))
    rows = cursor.fetchall()
    cursor.close()

    device_serials: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        device = str(row["device_id"] or "").strip()
        serial = normalize_serial(row["serial_number"])
        if device:
            device_serials[device]
            if serial:
                device_serials[device].add(serial)
    single = [(device, next(iter(serials))) for device, serials in device_serials.items() if len(serials) == 1]
    serial_devices: dict[str, set[str]] = defaultdict(set)
    for device, serial in single:
        serial_devices[serial].add(device)
    accepted = sorted((device, serial) for device, serial in single if len(serial_devices[serial]) == 1)
    candidates = [(index, device, serial) for index, (device, serial) in enumerate(accepted, 1)]
    audit = {
        "source_device_count": len(device_serials),
        "source_device_serial_pairs": len(rows),
        "devices_without_serial": sum(len(values) == 0 for values in device_serials.values()),
        "devices_with_conflicting_serials": sum(len(values) > 1 for values in device_serials.values()),
        "serials_shared_by_multiple_devices": sum(len(values) > 1 for values in serial_devices.values()),
        "one_to_one_serial_candidates": len(candidates),
    }
    return candidates, audit, sql_hash(sql)


def fetch_mapping(connection, candidates: list[tuple[int, str, str]]) -> tuple[pd.DataFrame, dict[str, int], str, str]:
    values_sql = ",".join(["(%s,%s)"] * len(candidates))
    parameters = [value for key, _device, serial in candidates for value in (key, serial)]
    sql = f"""
        WITH acs(acs_key, serial_norm) AS (
          SELECT column1::INTEGER, column2::VARCHAR FROM VALUES {values_sql}
        ), master_pairs AS (
          SELECT DISTINCT a.acs_key, NULLIF(TRIM(m.device_id::VARCHAR), '') AS warehouse_device
          FROM acs a
          JOIN {MASTER_DEVICE} m
            ON a.serial_norm = REGEXP_REPLACE(UPPER(TRIM(m.pon_serial::VARCHAR)), '[^A-Z0-9]', '')
          WHERE COALESCE(m._fivetran_deleted, FALSE) = FALSE
            AND NULLIF(TRIM(m.device_id::VARCHAR), '') IS NOT NULL
        ), master_key_degree AS (
          SELECT acs_key, COUNT(DISTINCT warehouse_device) AS degree FROM master_pairs GROUP BY acs_key
        ), master_device_degree AS (
          SELECT warehouse_device, COUNT(DISTINCT acs_key) AS degree FROM master_pairs GROUP BY warehouse_device
        ), master_one AS (
          SELECT p.acs_key, p.warehouse_device
          FROM master_pairs p
          JOIN master_key_degree k ON k.acs_key = p.acs_key AND k.degree = 1
          JOIN master_device_degree d ON d.warehouse_device = p.warehouse_device AND d.degree = 1
        ), public_rows AS (
          SELECT m.acs_key, m.warehouse_device,
                 NULLIF(TRIM(COALESCE(p.long_nas_id::VARCHAR, p.nasid::VARCHAR)), '') AS nasid,
                 IFF(p.long_nas_id IS NOT NULL AND p.nasid IS NOT NULL
                     AND TRIM(p.long_nas_id::VARCHAR) <> TRIM(p.nasid::VARCHAR), 1, 0) AS row_conflict
          FROM master_one m
          JOIN {PUBLIC_DEVICE} p ON NULLIF(TRIM(p.device_id::VARCHAR), '') = m.warehouse_device
          WHERE COALESCE(p._fivetran_deleted, FALSE) = FALSE
        ), public_profile AS (
          SELECT acs_key, warehouse_device, MAX(row_conflict) AS has_conflict,
                 COUNT(DISTINCT IFF(row_conflict = 0, nasid, NULL)) AS nasid_degree,
                 MIN(IFF(row_conflict = 0, nasid, NULL)) AS nasid
          FROM public_rows GROUP BY acs_key, warehouse_device
        ), public_one_pre AS (
          SELECT acs_key, warehouse_device, nasid FROM public_profile
          WHERE has_conflict = 0 AND nasid_degree = 1 AND nasid IS NOT NULL
        ), public_nasid_degree AS (
          SELECT nasid, COUNT(DISTINCT warehouse_device) AS degree FROM public_one_pre GROUP BY nasid
        ), public_one AS (
          SELECT p.* FROM public_one_pre p JOIN public_nasid_degree n ON n.nasid = p.nasid AND n.degree = 1
        ), customer_rows AS (
          SELECT account_id, NULLIF(TRIM(nasid::VARCHAR), '') AS nasid,
                 COALESCE(UPPER(TRIM(active_state::VARCHAR)), '<NULL>') AS active_state,
                 location_start_time, plan_expiry_time,
                 NULLIF(TRIM(COALESCE(long_lco_id::VARCHAR, lco_id::VARCHAR)), '') AS partner_key
          FROM {ACTIVE_BASE}
          WHERE source = 'CUSTOMER_V2' AND account_id IS NOT NULL
        ), customer_accounts AS (
          SELECT account_id, COUNT_IF(nasid IS NULL) AS null_nasids,
                 COUNT(DISTINCT nasid) AS nasid_degree,
                 COUNT(DISTINCT active_state) AS state_degree, MIN(active_state) AS active_state,
                 MIN(nasid) AS nasid, MIN(location_start_time) AS service_start,
                 MAX(plan_expiry_time) AS service_end,
                 IFF(COUNT(DISTINCT partner_key) = 1, MIN(partner_key), NULL) AS partner_key
          FROM customer_rows GROUP BY account_id
        ), customer_one_pre AS (
          SELECT * FROM customer_accounts
          WHERE null_nasids = 0 AND nasid_degree = 1 AND state_degree = 1 AND active_state = 'ACTIVE'
        ), customer_nasid_degree AS (
          SELECT nasid, COUNT(*) AS degree FROM customer_one_pre GROUP BY nasid
        ), clean_customers AS (
          SELECT c.* FROM customer_one_pre c JOIN customer_nasid_degree n ON n.nasid = c.nasid AND n.degree = 1
        ), accepted AS (
          SELECT p.acs_key, p.warehouse_device, p.nasid, c.service_start, c.service_end, c.partner_key
          FROM public_one p JOIN clean_customers c ON c.nasid = p.nasid
        )
        SELECT a.*,
               (SELECT COUNT(*) FROM acs) AS uploaded_acs_keys,
               (SELECT COUNT(DISTINCT acs_key) FROM master_pairs) AS exact_master_matches,
               (SELECT COUNT(*) FROM master_one) AS one_to_one_master_matches,
               (SELECT COUNT(*) FROM public_one) AS one_to_one_public_bridges,
               (SELECT COUNT(*) FROM accepted) AS strict_customer_v2_mappings
        FROM accepted a ORDER BY a.acs_key
    """
    cursor = connection.cursor()
    cursor.execute(sql, parameters)
    rows = cursor.fetchall()
    query_id = cursor.sfqid
    columns = [column[0].lower() for column in cursor.description]
    cursor.close()
    frame = pd.DataFrame(rows, columns=columns)
    counts = {
        "uploaded_acs_keys": len(candidates),
        "exact_master_matches": int(frame.iloc[0]["exact_master_matches"]) if not frame.empty else 0,
        "one_to_one_master_matches": int(frame.iloc[0]["one_to_one_master_matches"]) if not frame.empty else 0,
        "one_to_one_public_bridges": int(frame.iloc[0]["one_to_one_public_bridges"]) if not frame.empty else 0,
        "strict_customer_v2_mappings": len(frame),
    }
    if not frame.empty:
        if frame["acs_key"].nunique() != len(frame) or frame["warehouse_device"].nunique() != len(frame) or frame["nasid"].nunique() != len(frame):
            raise RuntimeError("Canonical mapping did not preserve the one-to-one invariant")
        frame = frame[["acs_key", "warehouse_device", "service_start", "service_end", "partner_key"]]
    return frame, counts, query_id, sql_hash(sql)


def mapping_reproduction(
    identifier_audit: dict[str, int], mapping_audit: dict[str, int]
) -> tuple[bool, dict[str, dict[str, int | bool]]]:
    expected = {
        "source_device_count": int(REFERENCE_EVIDENCE["mapping"]["acs_devices"]),
        "one_to_one_serial_candidates": int(
            REFERENCE_EVIDENCE["mapping"]["unique_noncolliding_serials"]
        ),
        "exact_master_matches": int(REFERENCE_EVIDENCE["mapping"]["exact_master_pon_matches"]),
        "one_to_one_master_matches": int(
            REFERENCE_EVIDENCE["mapping"]["one_to_one_master_matches"]
        ),
        "one_to_one_public_bridges": int(
            REFERENCE_EVIDENCE["mapping"]["one_to_one_public_device_nas_bridges"]
        ),
        "strict_customer_v2_mappings": int(
            REFERENCE_EVIDENCE["mapping"]["strict_customer_v2_mapped_devices"]
        ),
    }
    actual = {**identifier_audit, **mapping_audit}
    checks = {
        name: {"expected": value, "actual": int(actual.get(name, -1)), "passed": int(actual.get(name, -1)) == value}
        for name, value in expected.items()
    }
    return all(bool(item["passed"]) for item in checks.values()), checks


def fetch_telemetry(connection, table: str, mapping: pd.DataFrame, candidates: list[tuple[int, str, str]], start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    source_device = {key: device for key, device, _serial in candidates}
    mapped_devices = [source_device[int(key)] for key in mapping["acs_key"]]
    placeholders = ",".join(["%s"] * len(mapped_devices))
    sql = f"""
        SELECT device_id, inform_time,
               JSON_UNQUOTE(JSON_EXTRACT(params_json, '$._lastBoot')) AS last_boot
        FROM `{table}`
        WHERE device_id IN ({placeholders})
          AND inform_time >= %s AND inform_time < %s
        ORDER BY device_id, inform_time
    """
    rows: list[dict[str, object]] = []
    chunk_start = start - pd.Timedelta(hours=LOOKBACK_HOURS)
    while chunk_start < end:
        chunk_end = min(chunk_start + pd.Timedelta(days=TELEMETRY_CHUNK_DAYS), end)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            sql,
            mapped_devices + [chunk_start.to_pydatetime(), chunk_end.to_pydatetime()],
        )
        rows.extend(cursor.fetchall())
        cursor.close()
        chunk_start = chunk_end
    telemetry = pd.DataFrame(rows)
    if telemetry.empty:
        return telemetry, sql_hash(sql)
    device_to_key = {device: key for key, device, _serial in candidates}
    telemetry["acs_key"] = telemetry.pop("device_id").astype(str).map(device_to_key)
    telemetry["inform_time"] = pd.to_datetime(telemetry["inform_time"], utc=True, errors="coerce").dt.tz_convert(None)
    telemetry = telemetry.dropna(subset=["acs_key", "inform_time"]).sort_values(["acs_key", "inform_time"])
    telemetry = telemetry.drop_duplicates(["acs_key", "inform_time"], keep="last")
    telemetry["last_boot"] = [valid_boot_time(value, inform) for value, inform in zip(telemetry["last_boot"], telemetry["inform_time"])]
    telemetry["reboot_event"] = False
    for _key, indices in telemetry.groupby("acs_key", sort=False).groups.items():
        positions = list(indices)
        telemetry.loc[positions, "reboot_event"] = boot_transition_flags(telemetry.loc[positions, "last_boot"].tolist())
    return telemetry, sql_hash(sql)


def partner_groups(mapping: pd.DataFrame) -> dict[int, str]:
    counts = mapping["partner_key"].fillna("<MISSING>").value_counts()
    retained = sorted(str(value) for value, count in counts.items() if value != "<MISSING>" and count >= 5)
    labels = {value: f"P{index:02d}" for index, value in enumerate(retained, 1)}
    return {int(row.acs_key): labels.get(str(row.partner_key), "OTHER") for row in mapping.itertuples()}


def finalize_output_groups(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    result = frame.copy()
    observed_keys = sorted(int(value) for value in result["_device_key"].unique())
    pseudonyms = {key: f"D{index:04d}" for index, key in enumerate(observed_keys, 1)}
    result["device_key"] = result["_device_key"].astype(int).map(pseudonyms)
    before = result.groupby("partner_group")["_device_key"].nunique().astype(int).to_dict()
    small = {str(group) for group, count in before.items() if group != "OTHER" and count < 5}
    if small:
        result.loc[result["partner_group"].isin(small), "partner_group"] = "OTHER"
    after = result.groupby("partner_group")["_device_key"].nunique().astype(int).to_dict()
    return result, {
        "observed_devices": len(observed_keys),
        "pseudonyms_contiguous": sorted(result["device_key"].unique())
        == [f"D{index:04d}" for index in range(1, len(observed_keys) + 1)],
        "partner_device_counts_before_observed_collapse": before,
        "partner_groups_collapsed_below_five_devices": sorted(small),
        "partner_device_counts_after_observed_collapse": after,
    }


def anchor_grid(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DatetimeIndex, pd.Timestamp, pd.Timestamp]:
    hours = pd.date_range(start.ceil("h"), end.floor("h"), freq="h", inclusive="left")
    if len(hours) < 10:
        raise RuntimeError("Analysis window has too few complete UTC anchor hours")
    train_index = int(math.floor(len(hours) * 0.60))
    validation_index = int(math.floor(len(hours) * 0.80))
    return hours, hours[train_index], hours[validation_index]


def build_feature_frame(
    mapping: pd.DataFrame,
    telemetry: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, int | str]]:
    hours, train_end, validation_end = anchor_grid(start, end)
    partner_by_key = partner_groups(mapping)
    rows: list[dict[str, object]] = []
    frame_audit: dict[str, int | str] = defaultdict(int)
    frame_audit["service_interval_timezone"] = SERVICE_TIMEZONE
    frame_audit["service_rule"] = "anchor and full 24-hour label horizon must be inside service"
    telemetry_by_key = {int(key): group.reset_index(drop=True) for key, group in telemetry.groupby("acs_key", sort=False)}
    for mapped in mapping.itertuples():
        key = int(mapped.acs_key)
        device = telemetry_by_key.get(key)
        if device is None or device.empty:
            frame_audit["mapped_devices_without_telemetry"] += 1
            continue
        service_start = pd.to_datetime(mapped.service_start, errors="coerce")
        service_end = pd.to_datetime(mapped.service_end, errors="coerce")
        if pd.isna(service_start) or pd.isna(service_end) or service_end < service_start:
            frame_audit["mapped_devices_with_invalid_service_interval"] += 1
            continue
        times = device["inform_time"].to_numpy(dtype="datetime64[ns]")
        boots = device["last_boot"].to_numpy(dtype="datetime64[ns]")
        reboot_times = device.loc[device["reboot_event"], "inform_time"].to_numpy(dtype="datetime64[ns]")
        for anchor in hours:
            frame_audit["candidate_device_hours"] += 1
            service_anchor = utc_to_service_time(anchor)
            service_horizon_end = utc_to_service_time(anchor + pd.Timedelta(hours=24))
            if service_anchor < service_start:
                frame_audit["before_service_start_rows"] += 1
                continue
            if service_horizon_end > service_end:
                frame_audit["incomplete_24h_service_horizon_rows"] += 1
                continue
            frame_audit["full_24h_in_service_rows"] += 1
            anchor64 = anchor.to_datetime64()
            latest = strict_prior_index(times, anchor64)
            if latest is None:
                frame_audit["no_strictly_prior_inform_within_24h_rows"] += 1
                continue
            split = "train" if anchor < train_end else "validation" if anchor < validation_end else "test"
            row: dict[str, object] = {
                "_device_key": key,
                "device_key": f"D{key:04d}",
                "partner_group": partner_by_key[key],
                "prediction_time_utc": anchor,
                "split": split,
                "_next_split_start_utc": (
                    train_end if split == "train" else validation_end if split == "validation" else pd.NaT
                ),
                "_latest_inform_utc": pd.Timestamp(times[latest]),
                "inform_staleness_minutes": float((anchor64 - times[latest]) / np.timedelta64(1, "m")),
            }
            if not np.isnat(boots[latest]):
                row["boot_age_hours"] = float((anchor64 - boots[latest]) / np.timedelta64(1, "h"))
            else:
                row["boot_age_hours"] = float("nan")
            for window in (1, 6, 24):
                lower = anchor64 - np.timedelta64(window, "h")
                row[f"reboot_count_{window}h"] = int(np.searchsorted(reboot_times, anchor64, side="left") - np.searchsorted(reboot_times, lower, side="left"))
            rows.append(row)
            frame_audit["retained_preoutcome_rows"] += 1
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("No mapped hourly anchors had a strictly prior inform within 24 hours")
    if frame.duplicated(["_device_key", "prediction_time_utc"]).any():
        raise RuntimeError("Feature construction produced duplicate device-hour rows")
    boundaries = {
        "anchor_start_utc": hours[0].isoformat(),
        "train_end_validation_start_utc": train_end.isoformat(),
        "validation_end_test_start_utc": validation_end.isoformat(),
        "anchor_end_exclusive_utc": (hours[-1] + pd.Timedelta(hours=1)).isoformat(),
    }
    return frame, boundaries, dict(frame_audit)


def feature_gate(frame: pd.DataFrame) -> tuple[list[str], list[dict[str, object]]]:
    train = frame[frame["split"] == "train"]
    results: list[dict[str, object]] = []
    for feature in ACS_FEATURES:
        observed = train[train[feature].notna()]
        devices_with_three = int((observed.groupby("_device_key")["_latest_inform_utc"].nunique() >= MIN_DEVICE_OBSERVATIONS).sum())
        values = observed[feature].astype(float)
        iqr = float(values.quantile(0.75) - values.quantile(0.25)) if len(values) else 0.0
        per_device_range = observed.groupby("_device_key")[feature].agg(lambda series: float(series.max() - series.min()))
        varying_devices = int((per_device_range > 0).sum())
        eligible_devices = int(len(per_device_range))
        varying_share = varying_devices / eligible_devices if eligible_devices else 0.0
        transition_devices = int(observed.groupby("_device_key")["reboot_count_24h"].max().gt(0).sum())
        transition_share = transition_devices / eligible_devices if eligible_devices else 0.0
        # Hourly anchors make the rolling one-hour windows disjoint, so their sum
        # counts observed boot-time changes without repeatedly counting one event.
        transitions = int(observed["reboot_count_1h"].sum()) if eligible_devices else 0
        if feature == "boot_age_hours":
            variation_pass = transition_share >= MIN_VARYING_DEVICE_SHARE and transitions >= MIN_BOOT_TRANSITIONS
        else:
            variation_pass = varying_share >= MIN_VARYING_DEVICE_SHARE
        coverage = len(observed) / len(train) if len(train) else 0.0
        passed = coverage >= MIN_COVERAGE and devices_with_three >= MIN_TRAIN_DEVICES and iqr > 0 and variation_pass
        results.append(
            {
                "feature": feature,
                "train_coverage": coverage,
                "train_non_null_rows": len(observed),
                "train_devices_with_at_least_three_fresh_informs": devices_with_three,
                "train_iqr": iqr,
                "train_devices_with_nonzero_range": varying_devices,
                "train_varying_device_share": varying_share,
                "boot_transition_devices": transition_devices,
                "boot_transition_device_share": transition_share,
                "boot_transitions_observed": transitions,
                "eligible": passed,
            }
        )
    return [str(row["feature"]) for row in results if row["eligible"]], results


def fetch_outages(connection, mapping: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, str, str]:
    values_sql = ",".join(["(%s,%s)"] * len(mapping))
    parameters = [
        value
        for row in mapping.itertuples()
        for value in (int(row.acs_key), str(row.warehouse_device))
    ]
    sql = f"""
        WITH mapped(acs_key, warehouse_device) AS (
          SELECT column1::INTEGER, column2::VARCHAR FROM VALUES {values_sql}
        ), impacted_pairs AS (
          SELECT DISTINCT incident_id, NULLIF(TRIM(device_id::VARCHAR), '') AS warehouse_device
          FROM {IMPACTED}
          WHERE COALESCE(_fivetran_deleted, FALSE) = FALSE
            AND incident_id IS NOT NULL AND NULLIF(TRIM(device_id::VARCHAR), '') IS NOT NULL
        ), incident_rows AS (
          SELECT i.id, i.first_fail_timestamp, i.duration_minutes,
                 IFF(UPPER(TRIM(i.status::VARCHAR)) = 'CLOSED'
                     AND COALESCE(i.is_closed, FALSE) = TRUE
                     AND i.closed_at IS NOT NULL
                     AND i.closed_at::TIMESTAMP_NTZ >= i.first_fail_timestamp::TIMESTAMP_NTZ
                     AND i.closed_at::TIMESTAMP_NTZ <= %s::TIMESTAMP_NTZ,
                     TRUE, FALSE) AS closure_complete
          FROM {INCIDENTS} i
          WHERE COALESCE(i._fivetran_deleted, FALSE) = FALSE
            AND i.id IS NOT NULL AND i.first_fail_timestamp IS NOT NULL
            AND i.duration_minutes IS NOT NULL AND i.duration_minutes >= 0
        )
        SELECT DISTINCT m.acs_key, i.id AS incident_key,
               i.first_fail_timestamp::TIMESTAMP_NTZ AS start_utc,
               GREATEST(i.duration_minutes, 1)::FLOAT AS reported_duration_minutes,
               i.closure_complete
        FROM mapped m
        JOIN impacted_pairs d ON d.warehouse_device = m.warehouse_device
        JOIN incident_rows i ON i.id = d.incident_id
        WHERE i.first_fail_timestamp <= %s::TIMESTAMP_NTZ
          AND DATEADD(minute, GREATEST(i.duration_minutes, 1), i.first_fail_timestamp) > %s::TIMESTAMP_NTZ
        ORDER BY m.acs_key, start_utc
    """
    observation_end = outcome_observation_end(end)
    cursor = connection.cursor()
    cursor.execute(
        sql,
        parameters
        + [
            observation_end.to_pydatetime(),
            observation_end.to_pydatetime(),
            start.to_pydatetime(),
        ],
    )
    rows = cursor.fetchall()
    query_id = cursor.sfqid
    columns = [column[0].lower() for column in cursor.description]
    cursor.close()
    frame = pd.DataFrame(rows, columns=columns)
    if not frame.empty:
        frame["start_utc"] = pd.to_datetime(frame["start_utc"], utc=True, errors="coerce").dt.tz_convert(None)
        frame["reported_duration_minutes"] = pd.to_numeric(
            frame["reported_duration_minutes"], errors="coerce"
        )
        frame["closure_complete"] = frame["closure_complete"].fillna(False).astype(bool)
        frame = frame.dropna(
            subset=["start_utc", "reported_duration_minutes"]
        ).drop_duplicates(["acs_key", "incident_key"])
        bounded = [
            bounded_outage_duration(
                start_utc, float(duration), observation_end, bool(closure_complete)
            )
            for start_utc, duration, closure_complete in zip(
                frame["start_utc"],
                frame["reported_duration_minutes"],
                frame["closure_complete"],
            )
        ]
        frame["duration_administratively_censored"] = (
            frame["start_utc"]
            + pd.to_timedelta(frame["reported_duration_minutes"], unit="m")
            > observation_end
        )
        frame["end_utc"] = [value[0] for value in bounded]
        frame["duration_minutes"] = [value[1] for value in bounded]
        frame["duration_unavailable"] = [value[2] for value in bounded]
        frame["duration_closure_unverified"] = ~frame["closure_complete"]
        frame = frame.drop(columns=["reported_duration_minutes"])
        service_start = {
            int(row.acs_key): pd.to_datetime(row.service_start, errors="coerce")
            for row in mapping.itertuples()
        }
        service_end = {
            int(row.acs_key): pd.to_datetime(row.service_end, errors="coerce")
            for row in mapping.itertuples()
        }
        onset_local = frame["start_utc"].map(utc_to_service_time)
        recovery_local = frame["end_utc"].map(utc_to_service_time)
        lower = frame["acs_key"].astype(int).map(service_start)
        upper = frame["acs_key"].astype(int).map(service_end)
        frame["onset_in_service"] = onset_local.ge(lower) & onset_local.le(upper)
        frame["incident_overlaps_service"] = recovery_local.gt(lower) & onset_local.le(upper)
    return frame, query_id, sql_hash(sql)


def attach_outcomes(features: pd.DataFrame, outages: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    events = {int(key): group.sort_values(["start_utc", "incident_key"]) for key, group in outages.groupby("acs_key", sort=False)} if not outages.empty else {}
    keep: list[int] = []
    labels: dict[int, dict[str, object]] = {}
    boundary_purged = active_outage_excluded = 0
    boundary_by_split: dict[str, int] = defaultdict(int)
    active_by_split: dict[str, int] = defaultdict(int)
    for index, row in features.iterrows():
        anchor = pd.Timestamp(row["prediction_time_utc"])
        if not split_horizon_isolated(
            anchor, str(row["split"]), row["_next_split_start_utc"], 24
        ):
            boundary_purged += 1
            boundary_by_split[str(row["split"])] += 1
            continue
        group = events.get(int(row["_device_key"]))
        if group is None:
            keep.append(index)
            labels[index] = {
                "outage_next_6h": 0,
                "outage_next_24h": 0,
                "time_to_next_outage_minutes": float("nan"),
                "next_outage_duration_minutes": float("nan"),
            }
            continue
        active_starts = group["start_utc"].tolist()
        active_ends = group["end_utc"].tolist()
        if inside_active_outage(anchor, active_starts, active_ends):
            active_outage_excluded += 1
            active_by_split[str(row["split"])] += 1
            continue
        label_events = group[group["onset_in_service"]]
        starts = label_events["start_utc"].tolist()
        durations = label_events["duration_minutes"].astype(float).tolist()
        outcome_24 = future_outcome(anchor, starts, durations, 24)
        outcome_6 = future_outcome(anchor, starts, durations, 6)
        keep.append(index)
        labels[index] = {
            "outage_next_6h": outcome_6[0],
            "outage_next_24h": outcome_24[0],
            "time_to_next_outage_minutes": outcome_24[1] if outcome_24[1] is not None else float("nan"),
            "next_outage_duration_minutes": outcome_24[2] if outcome_24[2] is not None else float("nan"),
        }
    result = features.loc[keep].copy()
    label_frame = pd.DataFrame.from_dict(labels, orient="index").loc[keep]
    for column in label_frame:
        result[column] = label_frame[column].to_numpy()
    if result.empty or result.duplicated(["_device_key", "prediction_time_utc"]).any():
        raise RuntimeError("Outcome construction did not preserve unique eligible device-hours")
    unavailable_positive = result["outage_next_24h"].eq(1) & result[
        "next_outage_duration_minutes"
    ].isna()
    return result, {
        "input_feature_rows": len(features),
        "boundary_purged_rows": boundary_purged,
        "boundary_purged_rows_by_split": dict(boundary_by_split),
        "active_outage_excluded_rows": active_outage_excluded,
        "active_outage_excluded_rows_by_split": dict(active_by_split),
        "eligible_labelled_rows": len(result),
        "duration_unavailable_positive_rows": int(unavailable_positive.sum()),
        "duration_unavailable_positive_devices": int(
            result.loc[unavailable_positive, "_device_key"].nunique()
        ),
        "complete_duration_positive_rows": int(
            (result["outage_next_24h"].eq(1) & ~unavailable_positive).sum()
        ),
    }


def class_support(frame: pd.DataFrame, selected: list[str]) -> tuple[bool, dict[str, object]]:
    result: dict[str, object] = {}
    passed = True
    for split in ("train", "validation", "test"):
        part = frame[frame["split"] == split]
        observed_devices = int(part.loc[part[selected].notna().any(axis=1), "_device_key"].nunique())
        split_result: dict[str, object] = {
            "rows": len(part),
            "devices": int(part["_device_key"].nunique()),
            "feature_observed_devices": observed_devices,
        }
        for horizon in (6, 24):
            outcome = f"outage_next_{horizon}h"
            positive_devices = int(part.loc[part[outcome] == 1, "_device_key"].nunique())
            control_devices = int(part.loc[part[outcome] == 0, "_device_key"].nunique())
            split_result[f"{horizon}h"] = {
                "positive_rows": int(part[outcome].sum()),
                "control_rows": int((part[outcome] == 0).sum()),
                "positive_devices": positive_devices,
                "control_devices": control_devices,
                "prevalence": float(part[outcome].mean()) if len(part) else None,
            }
            if not len(part) or part[outcome].nunique() != 2:
                passed = False
            if split in ("validation", "test") and (
                observed_devices < MIN_HELDOUT_FEATURE_DEVICES
                or positive_devices < MIN_HELDOUT_CLASS_DEVICES
                or control_devices < MIN_HELDOUT_CLASS_DEVICES
            ):
                passed = False
        result[split] = split_result
    return passed, result


def add_time_features(frame: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
    result = frame.copy()
    timestamp = pd.to_datetime(result["prediction_time_utc"])
    result["hour_sin"] = np.sin(2 * np.pi * timestamp.dt.hour / 24)
    result["hour_cos"] = np.cos(2 * np.pi * timestamp.dt.hour / 24)
    result["dow_sin"] = np.sin(2 * np.pi * timestamp.dt.dayofweek / 7)
    result["dow_cos"] = np.cos(2 * np.pi * timestamp.dt.dayofweek / 7)
    result["elapsed_days"] = (timestamp - origin).dt.total_seconds() / 86400
    return result


def logistic_pipeline(columns: list[str], seed: int) -> Pipeline:
    preprocessing = ColumnTransformer(
        [("numeric", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), columns)],
        remainder="drop",
    )
    return Pipeline(
        [
            ("preprocess", preprocessing),
            ("model", LogisticRegression(C=1.0, solver="liblinear", max_iter=1000, random_state=seed)),
        ]
    )


def clustered_pr_auc_delta(frame: pd.DataFrame, outcome: str, probabilities: dict[str, np.ndarray], replicates: int, seed: int) -> dict[str, object]:
    device_indices = {key: np.flatnonzero(frame["_device_key"].to_numpy() == key) for key in frame["_device_key"].unique()}
    devices = np.array(list(device_indices))
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {
        "pr_auc_vs_time_only": [],
        "pr_auc_vs_prevalence": [],
        "brier_vs_time_only": [],
        "brier_vs_prevalence": [],
    }
    y = frame[outcome].to_numpy(int)
    for _ in range(replicates):
        chosen = rng.choice(devices, len(devices), replace=True)
        indices = np.concatenate([device_indices[key] for key in chosen])
        if len(np.unique(y[indices])) != 2:
            continue
        acs_score = average_precision_score(y[indices], probabilities["acs_plus_time"][indices])
        samples["pr_auc_vs_time_only"].append(acs_score - average_precision_score(y[indices], probabilities["time_only"][indices]))
        samples["pr_auc_vs_prevalence"].append(acs_score - average_precision_score(y[indices], probabilities["prevalence"][indices]))
        acs_brier = brier_score_loss(y[indices], probabilities["acs_plus_time"][indices])
        samples["brier_vs_time_only"].append(
            acs_brier - brier_score_loss(y[indices], probabilities["time_only"][indices])
        )
        samples["brier_vs_prevalence"].append(
            acs_brier - brier_score_loss(y[indices], probabilities["prevalence"][indices])
        )
    result: dict[str, object] = {"requested_replicates": replicates}
    for name, values in samples.items():
        result[name] = {
            "valid_replicates": len(values),
            "ci_low": float(np.percentile(values, 2.5)) if values else None,
            "ci_high": float(np.percentile(values, 97.5)) if values else None,
        }
    return result


def equal_device_pr_auc_delta(
    frame: pd.DataFrame,
    outcome: str,
    probabilities: dict[str, np.ndarray],
    replicates: int,
    seed: int,
) -> dict[str, object]:
    y = frame[outcome].to_numpy(int)
    deltas: list[float] = []
    for _key, indices in frame.groupby("_device_key", sort=False).indices.items():
        positions = np.asarray(indices, dtype=int)
        if len(np.unique(y[positions])) != 2:
            continue
        deltas.append(
            float(
                average_precision_score(y[positions], probabilities["acs_plus_time"][positions])
                - average_precision_score(y[positions], probabilities["time_only"][positions])
            )
        )
    rng = np.random.default_rng(seed)
    bootstraps = [
        float(np.mean(rng.choice(deltas, len(deltas), replace=True)))
        for _ in range(replicates)
    ] if deltas else []
    return {
        "eligible_devices": len(deltas),
        "mean_per_device_pr_auc_delta_vs_time_only": float(np.mean(deltas)) if deltas else None,
        "median_per_device_pr_auc_delta_vs_time_only": float(np.median(deltas)) if deltas else None,
        "devices_improved": int(np.sum(np.asarray(deltas) > 0)) if deltas else 0,
        "fraction_devices_improved": float(np.mean(np.asarray(deltas) > 0)) if deltas else None,
        "requested_replicates": replicates,
        "ci_low": float(np.percentile(bootstraps, 2.5)) if bootstraps else None,
        "ci_high": float(np.percentile(bootstraps, 97.5)) if bootstraps else None,
    }


def evaluate_models(frame: pd.DataFrame, selected: list[str], origin: pd.Timestamp, replicates: int, seed: int) -> dict[str, object]:
    data = add_time_features(frame, origin)
    train = data["split"].eq("train")
    validation = data["split"].eq("validation")
    test = data["split"].eq("test")
    time_trend_cap_days = float(data.loc[train, "elapsed_days"].max())
    data["elapsed_days"] = data["elapsed_days"].clip(upper=time_trend_cap_days)
    results: dict[str, object] = {}
    for horizon in (6, 24):
        outcome = f"outage_next_{horizon}h"
        warning = data.loc[test, "time_to_next_outage_minutes"].to_numpy(float)
        validation_warning = data.loc[validation, "time_to_next_outage_minutes"].to_numpy(float)
        probabilities: dict[str, np.ndarray] = {}
        validation_probabilities: dict[str, np.ndarray] = {}
        for name, columns in (("time_only", list(TIME_FEATURES)), ("acs_plus_time", list(TIME_FEATURES) + selected)):
            model = logistic_pipeline(columns, seed + horizon)
            model.fit(data.loc[train, columns], data.loc[train, outcome])
            validation_probabilities[name] = model.predict_proba(data.loc[validation, columns])[:, 1]
            probabilities[name] = model.predict_proba(data.loc[test, columns])[:, 1]
        prevalence = float(data.loc[train, outcome].mean())
        validation_probabilities["prevalence"] = np.full(validation.sum(), prevalence)
        probabilities["prevalence"] = np.full(test.sum(), prevalence)
        model_metrics: dict[str, object] = {}
        validation_diagnostics: dict[str, object] = {}
        for name in ("prevalence", "time_only", "acs_plus_time"):
            threshold = choose_threshold(data.loc[validation, outcome].to_numpy(int), validation_probabilities[name])
            model_metrics[name] = metric_summary(data.loc[test, outcome].to_numpy(int), probabilities[name], threshold, warning)
            validation_diagnostics[name] = metric_summary(
                data.loc[validation, outcome].to_numpy(int), validation_probabilities[name], threshold, validation_warning
            )
        delta_time = float(model_metrics["acs_plus_time"]["pr_auc"] - model_metrics["time_only"]["pr_auc"])
        delta_prevalence = float(model_metrics["acs_plus_time"]["pr_auc"] - model_metrics["prevalence"]["pr_auc"])
        test_frame = data.loc[test].reset_index(drop=True)
        uncertainty = clustered_pr_auc_delta(test_frame, outcome, probabilities, replicates, seed + horizon)
        equal_device = equal_device_pr_auc_delta(
            test_frame, outcome, probabilities, replicates, seed + 100 + horizon
        )
        ranking_supported = bool(
            delta_time > 0
            and delta_prevalence > 0
            and uncertainty["pr_auc_vs_time_only"]["ci_low"] is not None
            and uncertainty["pr_auc_vs_time_only"]["ci_low"] > 0
            and uncertainty["pr_auc_vs_prevalence"]["ci_low"] is not None
            and uncertainty["pr_auc_vs_prevalence"]["ci_low"] > 0
            and uncertainty["pr_auc_vs_time_only"]["valid_replicates"]
            >= math.ceil(replicates * MIN_VALID_BOOTSTRAP_FRACTION)
            and uncertainty["pr_auc_vs_prevalence"]["valid_replicates"]
            >= math.ceil(replicates * MIN_VALID_BOOTSTRAP_FRACTION)
        )
        acs_metrics = model_metrics["acs_plus_time"]
        relative_probability_quality_supported = bool(
            acs_metrics["brier"] <= model_metrics["time_only"]["brier"]
            and acs_metrics["brier"] <= model_metrics["prevalence"]["brier"]
            and acs_metrics["expected_calibration_error_10bin"]
            <= model_metrics["time_only"]["expected_calibration_error_10bin"]
            and acs_metrics["expected_calibration_error_10bin"]
            <= model_metrics["prevalence"]["expected_calibration_error_10bin"]
            and uncertainty["brier_vs_time_only"]["ci_high"] is not None
            and uncertainty["brier_vs_time_only"]["ci_high"] < 0
            and uncertainty["brier_vs_prevalence"]["ci_high"] is not None
            and uncertainty["brier_vs_prevalence"]["ci_high"] < 0
        )
        absolute_probability_quality_supported = bool(
            abs(float(acs_metrics["calibration_gap"])) <= MAX_ABSOLUTE_CALIBRATION_GAP
            and acs_metrics["expected_calibration_error_10bin"]
            <= MAX_EXPECTED_CALIBRATION_ERROR
        )
        probability_quality_supported = bool(
            relative_probability_quality_supported
            and absolute_probability_quality_supported
        )
        within_device_ranking_supported = bool(
            equal_device["eligible_devices"] >= MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES
            and equal_device["ci_low"] is not None
            and equal_device["ci_low"] > 0
        )
        results[f"{horizon}h"] = {
            "time_trend_cap_days_from_training": time_trend_cap_days,
            "models": model_metrics,
            "validation_threshold_selection_diagnostics": validation_diagnostics,
            "incremental_test_pr_auc": {"vs_time_only": delta_time, "vs_prevalence": delta_prevalence},
            "conditional_test_device_cluster_bootstrap": uncertainty,
            "equal_device_test_diagnostic": equal_device,
            "incremental_pr_auc_ranking_supported": ranking_supported,
            "relative_probability_quality_supported": relative_probability_quality_supported,
            "absolute_probability_quality_supported": absolute_probability_quality_supported,
            "probability_quality_supported": probability_quality_supported,
            "within_device_ranking_supported": within_device_ranking_supported,
            "useful_predictive_signal_supported": (
                ranking_supported
                and probability_quality_supported
                and within_device_ranking_supported
            ),
        }
    return results


def feature_results(frame: pd.DataFrame, selected: list[str], seed: int, replicates: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for horizon in (6, 24):
        outcome = f"outage_next_{horizon}h"
        for feature in selected:
            row: dict[str, object] = {"feature": feature, "horizon_hours": horizon}
            for split in ("train", "validation", "test"):
                part = frame[frame["split"] == split]
                row[f"{split}_coverage"] = float(part[feature].notna().mean())
                pos = part.loc[part[outcome] == 1, feature].dropna()
                neg = part.loc[part[outcome] == 0, feature].dropna()
                row[f"{split}_positive_median"] = float(pos.median()) if len(pos) else float("nan")
                row[f"{split}_control_median"] = float(neg.median()) if len(neg) else float("nan")
            test = frame[(frame["split"] == "test") & frame[feature].notna()]
            device = test.groupby(["_device_key", outcome])[feature].median().unstack()
            paired = (device.get(1, pd.Series(dtype=float)) - device.get(0, pd.Series(dtype=float))).dropna().to_numpy(float)
            train_values = frame.loc[(frame["split"] == "train") & frame[feature].notna(), feature]
            train_iqr = float(train_values.quantile(0.75) - train_values.quantile(0.25)) if len(train_values) else float("nan")
            effect = float(np.median(paired) / train_iqr) if len(paired) and train_iqr > 0 else float("nan")
            boots = [float(np.median(rng.choice(paired, len(paired), replace=True)) / train_iqr) for _ in range(replicates)] if len(paired) and train_iqr > 0 else []
            if len(paired) >= 10 and np.any(paired != 0):
                p_value = float(stats.wilcoxon(paired, zero_method="wilcox").pvalue)
            else:
                p_value = float("nan")
            split_directions = []
            for split in ("train", "validation", "test"):
                part = frame[(frame["split"] == split) & frame[feature].notna()]
                by_device = part.groupby(["_device_key", outcome])[feature].median().unstack()
                differences = (by_device.get(1, pd.Series(dtype=float)) - by_device.get(0, pd.Series(dtype=float))).dropna()
                split_directions.append(int(np.sign(differences.median())) if len(differences) else 0)
            partner_directions = []
            for _partner, part in test.groupby("partner_group"):
                if part["_device_key"].nunique() < 5:
                    continue
                by_device = part.groupby(["_device_key", outcome])[feature].median().unstack()
                differences = (by_device.get(1, pd.Series(dtype=float)) - by_device.get(0, pd.Series(dtype=float))).dropna()
                if len(differences) >= 5:
                    partner_directions.append(int(np.sign(differences.median())))
            duration_rows = test[
                test[outcome].eq(1) & test["next_outage_duration_minutes"].notna()
            ]
            duration = (
                duration_rows.groupby("_device_key")[
                    [feature, "next_outage_duration_minutes"]
                ]
                .median()
                .dropna()
            )
            correlation = stats.spearmanr(duration[feature], duration["next_outage_duration_minutes"]) if len(duration) >= 10 and duration[feature].nunique() > 1 else None
            row.update(
                paired_test_devices=len(paired),
                standardized_median_device_difference=effect,
                effect_ci_low=float(np.percentile(boots, 2.5)) if boots else float("nan"),
                effect_ci_high=float(np.percentile(boots, 97.5)) if boots else float("nan"),
                device_p_value=p_value,
                direction_stable_across_splits=len(set(split_directions)) == 1 and split_directions[0] != 0,
                split_directions=json.dumps(split_directions),
                partner_groups_evaluated=len(partner_directions),
                partner_direction_agreement=(
                    float(np.mean(np.array(partner_directions) == int(np.sign(effect))))
                    if len(partner_directions) >= 2 and math.isfinite(effect)
                    else float("nan")
                ),
                duration_positive_devices=len(duration),
                duration_spearman=float(correlation.statistic) if correlation is not None else float("nan"),
                duration_p_value=float(correlation.pvalue) if correlation is not None else float("nan"),
            )
            rows.append(row)
    result = pd.DataFrame(rows)
    result["device_q_bh"] = bh_adjust(result["device_p_value"].astype(float).tolist())
    result["duration_q_bh"] = bh_adjust(result["duration_p_value"].astype(float).tolist())
    return result


def assert_private_frame(frame: pd.DataFrame) -> None:
    prohibited = PROHIBITED_COLUMNS.intersection(str(column).lower() for column in frame.columns)
    if prohibited:
        raise RuntimeError(f"Privacy check rejected output columns: {sorted(prohibited)}")
    if any("json" in str(column).lower() or "payload" in str(column).lower() for column in frame.columns):
        raise RuntimeError("Privacy check rejected raw payload columns")


def render_report(audit: dict[str, object]) -> str:
    def formatted(value: object) -> str:
        return f"{float(value):.4f}" if value is not None else "not available"

    decision = str(audit.get("decision", "RUN_FAILED"))
    mapping = audit.get("mapping_audit", {})
    gate = audit.get("feature_gate", {})
    frame_audit = audit.get("feature_frame_preoutcome_audit", {})
    incident_audit = audit.get("formal_incident_audit", {})
    outcome_audit = audit.get("outcome_frame_audit", {})
    lines = [
        "# ACS outage feasibility pilot",
        "",
        "## Decision",
        "",
        f"**{decision}** — status remains `{STATUS}`.",
        "",
        "The prior correlation pilot is superseded. This command uses only the exact inventory bridge, strictly prior telemetry, chronological partitions, and formal UTC-valued incident onsets.",
        "",
        "## Gates",
        "",
        f"- Frozen source-audit artifact: {audit.get('source_gate', {}).get('passed', False)}.",
        f"- Exact mapping reproduction: {audit.get('mapping_reproduction_gate', {}).get('passed', False)}.",
        f"- Service-eligible formal-outage overlap reproduction: {audit.get('service_eligible_formal_outage_overlap_reproduction_gate', {}).get('passed', False)}.",
        f"- Canonical mapped cohort: {mapping.get('strict_customer_v2_mappings', 0)} devices.",
        f"- Frozen source-ready parameter: `$._lastBoot`; selected mapped-training features: {', '.join(gate.get('selected_features', [])) or 'none'}.",
        f"- Mapped-training gate: {gate.get('passed', False)}.",
        f"- Chronological class/device-support gate: {audit.get('class_support_gate', {}).get('passed', False)}.",
        f"- Complete 24-hour in-service candidate rows: {frame_audit.get('full_24h_in_service_rows', 0)}; retained pre-outcome rows with recent telemetry: {frame_audit.get('retained_preoutcome_rows', 0)}.",
        f"- Formal-duration observation boundary: {incident_audit.get('duration_observation_end_utc', 'not reached')}; unavailable durations: {incident_audit.get('duration_unavailable_incident_device_rows', 0)} incident-device rows ({incident_audit.get('duration_closure_unverified_incident_device_rows', 0)} closure-unverified; {incident_audit.get('duration_administratively_censored_incident_device_rows', 0)} also end after the boundary), affecting {outcome_audit.get('duration_unavailable_positive_rows', 0)} positive device-hours across {outcome_audit.get('duration_unavailable_positive_devices', 0)} devices.",
        "",
        "## Interpretation",
        "",
    ]
    if audit.get("model_evaluation"):
        for horizon, result in audit["model_evaluation"].items():
            acs = result["models"]["acs_plus_time"]
            time = result["models"]["time_only"]
            prevalence = result["models"]["prevalence"]
            lines.append(
                f"- {horizon}: test PR-AUC ACS+time {acs['pr_auc']:.4f}, time-only {time['pr_auc']:.4f}, prevalence {prevalence['pr_auc']:.4f}; pooled device-clustered ranking support: {result['incremental_pr_auc_ranking_supported']}."
            )
            quality = "passed" if result["probability_quality_supported"] else "did not pass"
            lines.append(
                f"  Probability quality {quality}: prevalence {acs['prevalence']:.4f}, mean probability {acs['mean_probability']:.4f}, Brier {acs['brier']:.4f} (time-only {time['brier']:.4f}; prevalence {prevalence['brier']:.4f}), ECE {acs['expected_calibration_error_10bin']:.4f}."
            )
            lines.append(
                f"  Validation-threshold operation: alert rate {acs['alert_rate']:.4f}, precision {acs['precision']:.4f}, recall {acs['recall']:.4f}, specificity {acs['specificity']:.4f}. Within-device ranking support: {result['within_device_ranking_supported']}; useful predictive signal: {result['useful_predictive_signal_supported']}."
            )
            equal_device = result["equal_device_test_diagnostic"]
            lines.append(
                f"  Equal-device AP delta versus time-only: mean {formatted(equal_device['mean_per_device_pr_auc_delta_vs_time_only'])}, 95% CI [{formatted(equal_device['ci_low'])}, {formatted(equal_device['ci_high'])}], across {equal_device['eligible_devices']} mixed-class devices; {equal_device['devices_improved']} improved."
            )
            warning_median = acs["true_positive_warning_minutes_median"]
            warning_mean = acs["true_positive_warning_minutes_mean"]
            lines.append(
                "  True-positive warning time: median "
                + (f"{warning_median:.1f}" if warning_median is not None else "not available")
                + " minutes; mean "
                + (f"{warning_mean:.1f}" if warning_mean is not None else "not available")
                + " minutes. Bootstrap intervals condition on the fitted model and resample test devices only."
            )
        individual = audit.get("individual_feature_evidence", {})
        lines.append(
            f"- Individual-feature evidence: {individual.get('fdr_significant_rows', 0)} of {individual.get('tested_rows', 0)} rows survived FDR; stable parameter evidence: {individual.get('fdr_significant_and_direction_stable', False)}."
        )
        duration = audit.get("duration_feature_evidence", {})
        lines.append(
            f"- Closure-verified duration diagnostic: {duration.get('fdr_significant_rows', 0)} of {duration.get('tested_rows', 0)} rows survived FDR; minimum adjusted q {formatted(duration.get('minimum_q_bh'))}; {duration.get('test_devices', 0)} test devices. This complete-case result is descriptive for the query snapshot, not all incidents."
        )
    else:
        lines.append("No predictive comparison was authorized beyond the failed mandatory gate.")
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- Analysis script SHA-256: `{audit.get('analysis_script_sha256', 'not recorded')}`.",
            f"- Command template: `{audit.get('run_contract', {}).get('reproducible_command_template', 'not recorded')}`.",
            f"- Analysis window UTC: `{json.dumps(native(audit.get('analysis_window_utc', {})), sort_keys=True)}`.",
            f"- Outcome observation contract: `{json.dumps(native(audit.get('outcome_observation_contract', {})), sort_keys=True)}`.",
            f"- Source-audit windows UTC: `{json.dumps(native(audit.get('source_gate', {}).get('audit_windows_utc', [])), sort_keys=True)}`.",
            f"- Source-audit SHA-256: `{audit.get('source_gate', {}).get('artifact_sha256', 'not recorded')}`.",
            f"- Split boundaries UTC: `{json.dumps(native(audit.get('split_boundaries', {})), sort_keys=True)}`.",
            f"- Timezone contract: `{json.dumps(native(audit.get('timezone_contract', {})), sort_keys=True)}`.",
            f"- Whitelist version: `{audit.get('source_whitelist_version', 'not recorded')}`.",
            f"- Gate thresholds: `{json.dumps(native(audit.get('thresholds', {})), sort_keys=True)}`.",
            f"- Query IDs: `{json.dumps(native(audit.get('query_ids', {})), sort_keys=True)}`.",
            f"- SQL SHA-256 values: `{json.dumps(native(audit.get('sql_sha256', {})), sort_keys=True)}`.",
            f"- Software versions: `{json.dumps(native(audit.get('software_versions', {})), sort_keys=True)}`.",
            f"- Output counts: `{json.dumps(native(audit.get('output_counts', {})), sort_keys=True)}`.",
            f"- Model/result SHA-256 values available at report render: `{json.dumps(native(audit.get('artifact_sha256', {})), sort_keys=True)}`. The report hash is added to `audit.json` after rendering.",
        ]
    )
    lines.extend(
        [
            "",
            "All metrics are conditional on mapped device-hours whose complete 24-hour label horizon lies inside the recorded service interval and that have a strictly prior ACS inform within 24 hours.",
            "",
            "Any current-window association is exploratory, not causal or confirmed. The 81/660 reference mapping coverage and severe temporal prevalence/device drift prevent fleet-wide claims; confirmation requires the unchanged analysis on data after 2026-08-10 that was untouched during development.",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(output: Path, audit: dict[str, object], model_frame: pd.DataFrame | None = None, results: pd.DataFrame | None = None) -> None:
    written: list[Path] = []
    if model_frame is not None:
        assert_private_frame(model_frame)
    if results is not None:
        assert_private_frame(results)
    if model_frame is not None:
        path = output / "model_frame.csv.gz"
        model_frame.to_csv(path, index=False, compression="gzip")
        written.append(path)
    if results is not None:
        path = output / "feature_results.csv"
        results.to_csv(path, index=False)
        written.append(path)
    audit["artifact_sha256"] = {path.name: file_hash(path) for path in written}
    report_path = output / "report.md"
    report_path.write_text(render_report(audit))
    written.append(report_path)
    audit["artifact_sha256"][report_path.name] = file_hash(report_path)
    write_json(output / "audit.json", audit)


def run(args: argparse.Namespace) -> int:
    start, end = parse_utc_argument(args.start), parse_utc_argument(args.end)
    if end <= start or end - start > pd.Timedelta(days=120):
        raise ValueError("Require a positive UTC window no longer than 120 days")
    output = args.output_dir.resolve()
    if output.exists() and not output.is_dir():
        raise NotADirectoryError(f"Output path exists and is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"Output directory must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    load_env(args.env_file.resolve())
    table = os.environ.get("ACS_TABLE_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise RuntimeError("ACS_TABLE_NAME must contain only letters, digits, and underscores")
    source_gate = validate_source_audit(args.source_audit.resolve())
    source_gate["checks"]["runtime_source_table"] = table == source_gate["source_table"]
    source_gate["passed"] = bool(source_gate["passed"] and source_gate["checks"]["runtime_source_table"])
    duration_observation_end = outcome_observation_end(end)
    run_started_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)

    audit: dict[str, object] = {
        "status": STATUS,
        "decision": "RUNNING",
        "analysis_script_sha256": file_hash(Path(__file__).resolve()),
        "run_contract": {
            "entry_point": "acs_outage_prediction/acs_outage_feasibility.py",
            "output_directory_name": output.name,
            "reproducible_command_template": (
                f"python acs_outage_prediction/acs_outage_feasibility.py --start {args.start} "
                f"--end {args.end} --output-dir <new-output-dir> "
                "--source-audit acs_outage_prediction/outputs/acs_parameter_source_audit_2026-08-11/audit.json "
                "--mysql-site-packages <mysql-connector-site-packages>"
            ),
            "source_access": "read-only queries; MySQL session timezone setting only",
            "source_writes_attempted": False,
        },
        "analysis_window_utc": {"start_inclusive": start, "end_exclusive": end},
        "outcome_observation_contract": {
            "last_scheduled_anchor_utc": end.floor("h") - pd.Timedelta(hours=1),
            "maximum_label_horizon_hours": MAX_LABEL_HORIZON_HOURS,
            "duration_observation_end_utc": duration_observation_end,
            "run_started_utc": run_started_utc,
            "source_query_after_observation_end": bool(
                run_started_utc >= duration_observation_end
            ),
            "complete_duration_rule": "status=CLOSED, is_closed=true, closed_at between onset and the observation boundary, and reported recovery no later than the observation boundary",
        },
        "timezone_contract": {
            "mysql_inform_time": "UTC",
            "snowflake_first_fail_timestamp": "UTC-valued TIMESTAMP_NTZ assumption; no Asia/Kolkata conversion",
            "active_base_service_interval": "Asia/Kolkata-local TIMESTAMP_NTZ; UTC anchors and incident onsets are explicitly converted before comparison",
        },
        "source_whitelist_version": SOURCE_WHITELIST_VERSION,
        "source_gate": source_gate,
        "source_tables": {
            "acs_telemetry": table,
            "master_device": MASTER_DEVICE,
            "public_device": PUBLIC_DEVICE,
            "active_customer_base": ACTIVE_BASE,
            "incidents": INCIDENTS,
            "incident_impacted_device": IMPACTED,
        },
        "reference_evidence": REFERENCE_EVIDENCE,
        "thresholds": {
            "minimum_train_coverage": MIN_COVERAGE,
            "minimum_train_devices": MIN_TRAIN_DEVICES,
            "minimum_fresh_informs_per_device": MIN_DEVICE_OBSERVATIONS,
            "minimum_varying_device_share": MIN_VARYING_DEVICE_SHARE,
            "minimum_true_boot_transitions": MIN_BOOT_TRANSITIONS,
            "minimum_heldout_feature_devices": MIN_HELDOUT_FEATURE_DEVICES,
            "minimum_heldout_positive_devices": MIN_HELDOUT_CLASS_DEVICES,
            "minimum_heldout_control_devices": MIN_HELDOUT_CLASS_DEVICES,
            "minimum_equal_device_mixed_class_devices": MIN_EQUAL_DEVICE_DIAGNOSTIC_DEVICES,
            "maximum_absolute_calibration_gap": MAX_ABSOLUTE_CALIBRATION_GAP,
            "maximum_expected_calibration_error_10bin": MAX_EXPECTED_CALIBRATION_ERROR,
            "minimum_valid_bootstrap_fraction": MIN_VALID_BOOTSTRAP_FRACTION,
        },
        "query_ids": {},
        "sql_sha256": {},
        "frozen_evaluation": {
            "chronological_split": [0.60, 0.20, 0.20],
            "time_only_terms": list(TIME_FEATURES),
            "elapsed_time_rule": "linear elapsed_days is capped at the last training anchor before validation/test scoring",
            "model": "L2 logistic regression, C=1, liblinear",
            "imputation": "training median plus explicit per-feature missing indicator",
            "threshold_rule": "validation F1 maximum; ties use the higher threshold",
            "primary_incremental_metric": "test PR-AUC over both prevalence and time-only baselines",
            "cluster_unit": "pseudonymous test device; conditional on the fitted model",
            "label_horizon_boundary_purge_hours": 24,
            "duration_outcome_rule": "duration is closure-verified only when status=CLOSED, is_closed=true, closed_at lies from onset through the maximum label-tail boundary, and reported recovery is no later than that boundary; administratively censored or closure-unverified values are missing from complete-case duration correlation",
            "equal_device_diagnostic": "macro mean of per-device test AP(ACS+time) minus AP(time-only)",
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "seed": RANDOM_SEED,
        },
    }

    if not source_gate["passed"]:
        audit.update(
            decision="NO_GO_SOURCE_AUDIT_REPRODUCTION",
            failure="Frozen source-readiness artifact did not reproduce the exact whitelist contract",
        )
        write_outputs(output, audit)
        return 2
    if run_started_utc < duration_observation_end:
        audit.update(
            decision="NO_GO_OUTCOME_WINDOW_NOT_MATURE",
            failure="The fixed maximum label-tail boundary has not yet elapsed",
        )
        write_outputs(output, audit)
        return 2

    mysql = None
    snowflake = None
    try:
        connector = import_mysql_connector(args.mysql_site_packages)
        mysql = connect_mysql(connector)
        audit["timezone_contract"]["mysql_session_time_zone_observed"] = mysql_session_timezone(mysql)
        snowflake = connect_snowflake()
        candidates, mysql_mapping, identifier_hash = mysql_identifier_candidates(mysql, table, end)
        audit["mysql_identifier_audit"] = mysql_mapping
        audit["sql_sha256"]["mysql_identifier_candidates"] = identifier_hash
        if not candidates:
            audit.update(decision="NO_GO_FOR_MAPPING", failure="No one-to-one ACS serial candidates in the selected window")
            write_outputs(output, audit)
            return 2

        mapping, mapping_counts, mapping_qid, mapping_hash = fetch_mapping(snowflake, candidates)
        mapping_counts.update(
            {
                "master_one_to_one_stage_exclusions": max(
                    0,
                    mapping_counts["exact_master_matches"]
                    - mapping_counts["one_to_one_master_matches"],
                ),
                "public_bridge_stage_exclusions": max(
                    0,
                    mapping_counts["one_to_one_master_matches"]
                    - mapping_counts["one_to_one_public_bridges"],
                ),
                "strict_customer_v2_stage_exclusions": max(
                    0,
                    mapping_counts["one_to_one_public_bridges"]
                    - mapping_counts["strict_customer_v2_mappings"],
                ),
            }
        )
        audit["mapping_audit"] = mapping_counts
        audit["query_ids"]["canonical_mapping"] = mapping_qid
        audit["sql_sha256"]["canonical_mapping"] = mapping_hash
        if mapping.empty:
            audit.update(decision="NO_GO_FOR_MAPPING", failure="No canonical mappings reached strict active CUSTOMER_V2")
            write_outputs(output, audit)
            return 2
        mapping_passed, mapping_checks = mapping_reproduction(mysql_mapping, mapping_counts)
        audit["mapping_reproduction_gate"] = {
            "passed": mapping_passed,
            "checks": mapping_checks,
        }
        if not mapping_passed:
            audit.update(
                decision="NO_GO_MAPPING_REPRODUCTION",
                failure="Live identity stages did not exactly reproduce the frozen canonical counts",
            )
            write_outputs(output, audit)
            return 2

        telemetry, telemetry_hash = fetch_telemetry(mysql, table, mapping, candidates, start, end)
        audit["sql_sha256"]["mysql_mapped_last_boot"] = telemetry_hash
        if telemetry.empty:
            audit.update(decision="NO_GO_FOR_MODEL_FRAME", failure="Mapped cohort has no bounded ACS telemetry")
            write_outputs(output, audit)
            return 2
        audit["telemetry_audit"] = {
            "rows": len(telemetry),
            "mapped_devices_with_rows": int(telemetry["acs_key"].nunique()),
            "mapped_devices_without_rows": int(len(mapping) - telemetry["acs_key"].nunique()),
            "first_inform_utc": telemetry["inform_time"].min(),
            "last_inform_utc": telemetry["inform_time"].max(),
            "valid_last_boot_rows": int(telemetry["last_boot"].notna().sum()),
            "valid_last_boot_coverage": float(telemetry["last_boot"].notna().mean()),
            "boot_transition_events": int(telemetry["reboot_event"].sum()),
        }
        features, boundaries, feature_frame_audit = build_feature_frame(
            mapping, telemetry, start, end
        )
        selected, gate_rows = feature_gate(features)
        audit["split_boundaries"] = boundaries
        audit["feature_frame_preoutcome_audit"] = feature_frame_audit
        missing_features = [f"{feature}_missing" for feature in selected]
        model_features = selected + missing_features
        audit["feature_gate"] = {
            "passed": bool(selected),
            "selected_features": selected,
            "explicit_missingness_features": missing_features,
            "model_features": model_features,
            "candidates": gate_rows,
        }
        if not selected:
            audit.update(decision="NO_GO_FOR_MODEL_FRAME", failure="No mapped-training feature passed the frozen non-outcome gate")
            write_outputs(output, audit)
            return 2

        outages, outage_qid, outage_hash = fetch_outages(snowflake, mapping, start, end)
        audit["query_ids"]["formal_incidents"] = outage_qid
        audit["sql_sha256"]["formal_incidents"] = outage_hash
        audit["formal_incident_audit"] = {
            "raw_bounded_incident_device_rows": len(outages),
            "raw_bounded_devices_with_incidents": int(outages["acs_key"].nunique()) if not outages.empty else 0,
            "service_overlapping_incident_device_rows": int(outages["incident_overlaps_service"].sum()) if not outages.empty else 0,
            "service_overlapping_devices": int(outages.loc[outages["incident_overlaps_service"], "acs_key"].nunique()) if not outages.empty else 0,
            "onset_in_service_incident_device_rows": int(outages["onset_in_service"].sum()) if not outages.empty else 0,
            "onset_in_service_devices": int(outages.loc[outages["onset_in_service"], "acs_key"].nunique()) if not outages.empty else 0,
            "distinct_incidents": int(outages["incident_key"].nunique()) if not outages.empty else 0,
            "first_onset_utc": outages["start_utc"].min() if not outages.empty else None,
            "last_onset_utc": outages["start_utc"].max() if not outages.empty else None,
            "duration_observation_end_utc": outcome_observation_end(end),
            "duration_unavailable_incident_device_rows": int(outages["duration_unavailable"].sum()) if not outages.empty else 0,
            "duration_unavailable_onset_in_service_rows": int((outages["duration_unavailable"] & outages["onset_in_service"]).sum()) if not outages.empty else 0,
            "duration_unavailable_onset_in_service_devices": int(outages.loc[outages["duration_unavailable"] & outages["onset_in_service"], "acs_key"].nunique()) if not outages.empty else 0,
            "duration_available_incident_device_rows": int((~outages["duration_unavailable"]).sum()) if not outages.empty else 0,
            "duration_closure_unverified_incident_device_rows": int(outages["duration_closure_unverified"].sum()) if not outages.empty else 0,
            "duration_administratively_censored_incident_device_rows": int(outages["duration_administratively_censored"].sum()) if not outages.empty else 0,
            "duration_rule": "available only when status=CLOSED, is_closed=true, closed_at lies from onset through the maximum label-tail boundary, and reported recovery is no later than that boundary; otherwise closure-unverified and/or administratively censored, so missing for complete-case correlation",
            "risk_interval_rule": "half-open onset plus max(reported duration, one minute), capped at the maximum label-tail boundary; closure status does not redefine the interval",
        }
        overlap_outages = (
            outages[
                (outages["start_utc"] < end)
                & (outages["end_utc"] > start)
                & outages["onset_in_service"]
            ]
            if not outages.empty
            else outages
        )
        overlap_devices = int(overlap_outages["acs_key"].nunique()) if not overlap_outages.empty else 0
        audit["service_eligible_formal_outage_overlap_reproduction_gate"] = {
            "passed": overlap_devices
            == int(REFERENCE_EVIDENCE["mapping"]["formal_outage_observed_devices"]),
            "expected_devices": int(
                REFERENCE_EVIDENCE["mapping"]["formal_outage_observed_devices"]
            ),
            "actual_devices": overlap_devices,
            "overlap_rule": "incident overlaps the base analysis interval and its UTC onset falls inside the Asia/Kolkata-local service interval",
        }
        if not audit["service_eligible_formal_outage_overlap_reproduction_gate"]["passed"]:
            audit.update(
                decision="NO_GO_SERVICE_ELIGIBLE_OUTAGE_OVERLAP_REPRODUCTION",
                failure="Service-eligible formal-outage overlap did not reproduce the frozen mapped-device count",
            )
            write_outputs(output, audit)
            return 2
        labelled, outcome_audit = attach_outcomes(features, outages)
        audit["outcome_frame_audit"] = outcome_audit
        isolation_checks = {
            split: bool(
                all(
                    split_horizon_isolated(
                        pd.Timestamp(row["prediction_time_utc"]),
                        str(row["split"]),
                        row["_next_split_start_utc"],
                        24,
                    )
                    for _index, row in labelled[labelled["split"] == split].iterrows()
                )
            )
            for split in ("train", "validation", "test")
        }
        audit["chronological_isolation_gate"] = {
            "passed": all(isolation_checks.values()),
            "checks": isolation_checks,
            "rule": "earlier-split anchor + 24 hours must be strictly before the next split start",
        }
        if not all(isolation_checks.values()):
            audit.update(
                decision="NO_GO_CHRONOLOGICAL_ISOLATION",
                failure="At least one retained anchor can label an event in a later partition",
            )
            write_outputs(output, audit)
            return 2
        post_selected, post_gate_rows = feature_gate(labelled)
        post_gate_passed = all(feature in post_selected for feature in selected)
        audit["post_exclusion_feature_gate"] = {
            "passed": post_gate_passed,
            "prelabel_selected_features_retained": [
                feature for feature in selected if feature in post_selected
            ],
            "post_exclusion_eligible_features": post_selected,
            "selection_was_not_adapted_from_outcomes": True,
            "candidates": post_gate_rows,
        }
        if not post_gate_passed:
            audit.update(
                decision="NO_GO_POST_EXCLUSION_FEATURE_GATE",
                failure="A frozen prelabel feature failed the same gate after mandatory anchor exclusions",
            )
            write_outputs(output, audit)
            return 2
        for feature, missing in zip(selected, missing_features):
            labelled[missing] = labelled[feature].isna().astype("int8")
        labelled, output_group_audit = finalize_output_groups(labelled)
        audit["output_group_audit"] = output_group_audit
        support_passed, support = class_support(labelled, selected)
        audit["class_support_gate"] = {"passed": support_passed, "splits": support}
        if not support_passed:
            audit.update(decision="NO_GO_FOR_CHRONOLOGICAL_EVALUATION", failure="A chronological split lacks the frozen independent-device or class support")
            write_outputs(output, audit)
            return 2

        evaluation = evaluate_models(
            labelled,
            model_features,
            start.ceil("h"),
            BOOTSTRAP_REPLICATES,
            RANDOM_SEED,
        )
        results = feature_results(
            labelled, selected, RANDOM_SEED, BOOTSTRAP_REPLICATES
        )
        ranking_supported = bool(
            evaluation["6h"]["incremental_pr_auc_ranking_supported"]
            and evaluation["24h"]["incremental_pr_auc_ranking_supported"]
        )
        useful_supported = bool(
            evaluation["6h"]["useful_predictive_signal_supported"]
            and evaluation["24h"]["useful_predictive_signal_supported"]
        )
        supported_rows = (
            results["device_q_bh"].lt(0.05)
            & results["direction_stable_across_splits"]
            & results["effect_ci_low"].mul(results["effect_ci_high"]).gt(0)
            & results["partner_groups_evaluated"].ge(2)
            & results["partner_direction_agreement"].eq(1.0)
        )
        supported_parameters = sorted(
            str(feature)
            for feature, part in results[supported_rows].groupby("feature")
            if {6, 24}.issubset(set(part["horizon_hours"].astype(int)))
        )
        individual_supported = bool(supported_parameters)
        audit["model_evaluation"] = evaluation
        audit["individual_feature_evidence"] = {
            "fdr_significant_and_direction_stable": individual_supported,
            "fdr_significant_rows": int(results["device_q_bh"].lt(0.05).sum()),
            "multi_horizon_fdr_time_partner_stable_parameters": supported_parameters,
            "tested_rows": len(results),
        }
        duration_q = results["duration_q_bh"].dropna().astype(float)
        audit["duration_feature_evidence"] = {
            "estimand": "closure-verified complete-case association in the current query snapshot",
            "tested_rows": int(len(duration_q)),
            "fdr_significant_rows": int(duration_q.lt(0.05).sum()),
            "minimum_q_bh": float(duration_q.min()) if len(duration_q) else None,
            "test_devices": int(results["duration_positive_devices"].min()) if len(results) else 0,
            "generalizes_to_all_incidents": False,
        }
        if useful_supported and individual_supported:
            audit["decision"] = "CURRENT_WINDOW_INCREMENTAL_SIGNAL"
        elif ranking_supported:
            audit["decision"] = "CURRENT_WINDOW_EXPLORATORY_ROW_RANKING_SIGNAL_ONLY"
        else:
            audit["decision"] = "USEFUL_ACS_SIGNAL_NOT_DEMONSTRATED"
        audit["output_counts"] = {
            "model_frame_rows": len(labelled),
            "model_frame_devices": int(labelled["_device_key"].nunique()),
            "canonical_mapped_devices": len(mapping),
            "mapped_devices_with_telemetry": int(telemetry["acs_key"].nunique()),
            "canonical_devices_reaching_model_frame": int(labelled["_device_key"].nunique()),
            "feature_result_rows": len(results),
        }
        audit["privacy_check"] = "passed"
        audit["software_versions"] = {
            "python": platform.python_version(),
            "numpy": metadata.version("numpy"),
            "pandas": metadata.version("pandas"),
            "scipy": metadata.version("scipy"),
            "scikit_learn": metadata.version("scikit-learn"),
            "mysql_connector": getattr(connector, "__version__", "unknown"),
            "snowflake_connector": metadata.version("snowflake-connector-python"),
        }
        export_columns = [
            "device_key",
            "partner_group",
            "prediction_time_utc",
            "split",
            *model_features,
            "outage_next_6h",
            "outage_next_24h",
            "time_to_next_outage_minutes",
            "next_outage_duration_minutes",
        ]
        export = labelled[export_columns].copy()
        write_outputs(output, audit, export, results)
        return 0
    except Exception as error:
        audit.update(
            decision="RUN_FAILED",
            error_type=type(error).__name__,
            failure_detail=str(error)[:500],
            failure_stage="after_source_gate",
            source_writes_attempted=False,
        )
        try:
            write_outputs(output, audit)
        except Exception:
            pass
        raise
    finally:
        if mysql is not None:
            try:
                mysql.rollback()
            except Exception:
                pass
            try:
                mysql.close()
            except Exception:
                pass
        if snowflake is not None:
            try:
                snowflake.close()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the gated ACS outage feasibility pilot (read-only sources).")
    parser.add_argument("--start", help="UTC anchor-window start, inclusive (ISO-8601).")
    parser.add_argument("--end", help="UTC anchor-window end, exclusive (ISO-8601).")
    parser.add_argument("--output-dir", type=Path, help="New or empty derived-output directory.")
    parser.add_argument("--env-file", type=Path, default=WORKSPACE / ".env")
    parser.add_argument(
        "--source-audit",
        type=Path,
        default=DEFAULT_SOURCE_AUDIT,
        help="Frozen acs_parameter_audit.py audit.json artifact.",
    )
    parser.add_argument("--mysql-site-packages", type=Path, help="Optional directory containing a modern mysql.connector; added only during import.")
    parser.add_argument("--self-check", action="store_true", help="Run pure in-memory checks and exit without database access.")
    args = parser.parse_args()
    if not args.self_check and (not args.start or not args.end or args.output_dir is None):
        parser.error("--start, --end, and --output-dir are required unless --self-check is used")
    return args


def main() -> int:
    args = parse_args()
    self_check()
    if args.self_check:
        print("self-check passed")
        return 0
    output_was_nonempty = bool(
        args.output_dir is not None
        and args.output_dir.exists()
        and (not args.output_dir.is_dir() or any(args.output_dir.iterdir()))
    )
    try:
        return run(args)
    except Exception as error:
        if args.output_dir is not None and not output_was_nonempty:
            output = args.output_dir.resolve()
            if output.exists() and not output.is_dir():
                print(f"{type(error).__name__}: {error}", file=sys.stderr)
                return 1
            output.mkdir(parents=True, exist_ok=True)
            if (output / "audit.json").exists():
                print(f"{type(error).__name__}: {error}", file=sys.stderr)
                return 1
            failure = {
                "status": STATUS,
                "decision": "RUN_FAILED",
                "error_type": type(error).__name__,
                "failure_detail": str(error)[:500],
                "analysis_window_utc": {
                    "start_inclusive": args.start,
                    "end_exclusive": args.end,
                },
                "source_writes_attempted": False,
                "reference_evidence": REFERENCE_EVIDENCE,
            }
            write_outputs(output, failure)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
