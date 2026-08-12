from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
FRESH_HOURS = 24
MAX_BOOT_AGE_DAYS = 3652
MAX_TOTAL_WINDOW_DAYS = 31
MIN_COVERAGE = 0.25
MIN_DEVICES_WITH_3 = 20
MIN_VARYING_DEVICE_SHARE = 0.10
MIN_MINORITY_SHARE = 0.01
MIN_MINORITY_DEVICES = 10
MIN_TRANSITIONS = 5


@dataclass(frozen=True)
class Feature:
    name: str
    source: str
    reducer: str
    value_type: str = "numeric"
    canonical_path: str | None = None
    json_path: str | None = None
    special: str | None = None


@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime


FLAT_FIELDS = (
    "uptime_s",
    "cpu_pct",
    "memory_free_kb",
    "memory_total_kb",
    "temperature_c",
    "optical_rx_dbm",
    "optical_tx_dbm",
    "inform_time",
    "param_count",
)

FLAT_FEATURES = tuple(
    Feature(f"flat.{name}", "flat", "snapshot")
    for name in FLAT_FIELDS
    if name != "inform_time"
)

P1_FEATURES = (
    Feature(
        "p1.device_memory_free",
        "params_json",
        "min",
        canonical_path="DeviceInfo.MemoryStatus.Free",
        json_path="$.InternetGatewayDevice.DeviceInfo.MemoryStatus.Free",
    ),
    Feature(
        "p1.device_cpu_usage",
        "params_json",
        "max",
        canonical_path="DeviceInfo.ProcessStatus.CPUUsage",
        json_path="$.InternetGatewayDevice.DeviceInfo.ProcessStatus.CPUUsage",
    ),
    Feature(
        "p1.device_temperature",
        "params_json",
        "max",
        canonical_path="DeviceInfo.TemperatureStatus.TemperatureSensor.N.Value",
        json_path="$.InternetGatewayDevice.DeviceInfo.TemperatureStatus.TemperatureSensor.*.Value",
    ),
    Feature(
        "p1.device_uptime",
        "params_json",
        "min",
        canonical_path="DeviceInfo.UpTime",
        json_path="$.InternetGatewayDevice.DeviceInfo.UpTime",
    ),
    Feature(
        "p1.optical_rx_power",
        "params_json",
        "min",
        canonical_path="GX_OntOpticalParam.RXPower",
        json_path="$.InternetGatewayDevice.GX_OntOpticalParam.RXPower",
    ),
    Feature(
        "p1.optical_tx_power",
        "params_json",
        "max",
        canonical_path="GX_OntOpticalParam.TXPower",
        json_path="$.InternetGatewayDevice.GX_OntOpticalParam.TXPower",
    ),
    Feature(
        "p1.optical_temperature",
        "params_json",
        "max",
        canonical_path="GX_OntOpticalParam.TransceiverTemperature",
        json_path="$.InternetGatewayDevice.GX_OntOpticalParam.TransceiverTemperature",
    ),
    Feature(
        "p1.optical_bias_current",
        "params_json",
        "max",
        canonical_path="GX_OntOpticalParam.BiasCurrent",
        json_path="$.InternetGatewayDevice.GX_OntOpticalParam.BiasCurrent",
    ),
    Feature(
        "p1.optical_supply_voltage",
        "params_json",
        "min",
        canonical_path="GX_OntOpticalParam.SupplyVoltage",
        json_path="$.InternetGatewayDevice.GX_OntOpticalParam.SupplyVoltage",
    ),
    Feature(
        "p1.wifi_total_associations",
        "params_json",
        "sum",
        canonical_path="LANDevice.N.WLANConfiguration.N.TotalAssociations",
        json_path="$.InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.TotalAssociations",
    ),
    Feature(
        "p1.wifi_signal_strength",
        "params_json",
        "median",
        canonical_path="LANDevice.N.WLANConfiguration.N.AssociatedDevice.N.SignalStrength",
        json_path="$.InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.AssociatedDevice.*.SignalStrength",
    ),
    Feature(
        "p1.ppp_connection_status",
        "params_json",
        "set",
        value_type="connection_status",
        canonical_path="WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.ConnectionStatus",
        json_path="$.InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ConnectionStatus",
    ),
    Feature(
        "p1.ppp_last_connection_error",
        "params_json",
        "set",
        value_type="connection_error",
        canonical_path="WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.LastConnectionError",
        json_path="$.InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.LastConnectionError",
    ),
    Feature(
        "p1.ppp_uptime",
        "params_json",
        "min",
        canonical_path="WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.Uptime",
        json_path="$.InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Uptime",
    ),
    Feature(
        "p1.last_inform_age_seconds",
        "params_json",
        "age_seconds",
        canonical_path="_meta._lastInform",
        json_path="$._lastInform",
        special="last_inform",
    ),
    Feature(
        "p1.last_boot",
        "params_json",
        "boot_age_and_reset",
        canonical_path="_meta._lastBoot",
        json_path="$._lastBoot",
        special="last_boot",
    ),
)

FEATURES = FLAT_FEATURES + P1_FEATURES


def parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) > 10**12:
            number /= 1000
        try:
            result = datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip().strip('"')
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def decode_json(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def leaf_objects(value: object) -> list[dict[str, object]]:
    value = decode_json(value)
    if isinstance(value, list):
        return [leaf for item in value for leaf in leaf_objects(item)]
    return [value] if isinstance(value, dict) and ("_value" in value or "_timestamp" in value) else []


def scalar(value: object) -> object:
    value = decode_json(value)
    if isinstance(value, dict) and "_value" in value:
        return value["_value"]
    return value


def category(value: object, kind: str) -> str | None:
    text = re.sub(r"\s+", "_", ("" if value is None else str(value)).strip().upper())
    if not text:
        return None
    if kind == "connection_status":
        return "connected" if text == "CONNECTED" else "non_connected"
    no_error = {"ERROR_NONE", "NONE", "NO_ERROR", "NOERROR", "0"}
    return "no_error" if text in no_error else "any_error"


def reduce_values(values: list[object], feature: Feature) -> float | str | None:
    if feature.value_type == "numeric":
        numeric = [item for item in (number(value) for value in values) if item is not None]
        if not numeric:
            return None
        reducers = {
            "min": min,
            "max": max,
            "sum": sum,
            "median": statistics.median,
        }
        return float(reducers[feature.reducer](numeric))
    categories = {item for item in (category(value, feature.value_type) for value in values) if item}
    if not categories:
        return None
    if feature.value_type == "connection_status":
        return "connected" if categories == {"connected"} else "non_connected"
    return "no_error" if categories == {"no_error"} else "any_error"


def new_state() -> dict[str, object]:
    return {
        "present_rows": 0,
        "leaf_values_seen": 0,
        "fresh_leaf_values": 0,
        "stale_leaf_values": 0,
        "future_leaf_values": 0,
        "invalid_timestamp_leaf_values": 0,
        "invalid_value_leaf_values": 0,
        "observations": [],
        "by_device": defaultdict(list),
        "boot_entries": defaultdict(list),
        "boot_uptime_pairs": [],
    }


def add_observation(
    state: dict[str, object], device: object, inform_time: datetime, value: float | str
) -> None:
    state["observations"].append(value)
    if device is not None:
        state["by_device"][device].append((inform_time, value))


def extract_regular(
    raw: object, inform_time: datetime, feature: Feature, state: dict[str, object]
) -> float | str | None:
    leaves = leaf_objects(raw)
    if leaves:
        state["present_rows"] += 1
    fresh: list[object] = []
    for leaf in leaves:
        state["leaf_values_seen"] += 1
        observed_at = parse_datetime(leaf.get("_timestamp"))
        if observed_at is None:
            state["invalid_timestamp_leaf_values"] += 1
            continue
        age = inform_time - observed_at
        if age < timedelta(0):
            state["future_leaf_values"] += 1
            continue
        if age > timedelta(hours=FRESH_HOURS):
            state["stale_leaf_values"] += 1
            continue
        raw_value = leaf.get("_value")
        if (
            number(raw_value) is None
            if feature.value_type == "numeric"
            else category(raw_value, feature.value_type) is None
        ):
            state["invalid_value_leaf_values"] += 1
            continue
        state["fresh_leaf_values"] += 1
        fresh.append(raw_value)
    return reduce_values(fresh, feature)


def extract_meta(
    raw: object,
    inform_time: datetime,
    feature: Feature,
    state: dict[str, object],
) -> tuple[float | None, datetime | None]:
    if raw is not None:
        state["present_rows"] += 1
        state["leaf_values_seen"] += 1
    observed_at = parse_datetime(scalar(raw))
    if observed_at is None:
        if raw is not None:
            state["invalid_timestamp_leaf_values"] += 1
        return None, None
    age = inform_time - observed_at
    if age < timedelta(0):
        state["future_leaf_values"] += 1
        return None, None
    max_age = timedelta(days=MAX_BOOT_AGE_DAYS) if feature.special == "last_boot" else timedelta(hours=FRESH_HOURS)
    if age > max_age:
        state["stale_leaf_values"] += 1
        return None, None
    state["fresh_leaf_values"] += 1
    return age.total_seconds(), observed_at


def audit_rows(rows: Iterable[dict[str, object]]) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    states = {feature.name: new_state() for feature in FEATURES}
    devices: set[object] = set()
    row_count = invalid_inform_rows = null_device_rows = 0
    first_inform = last_inform = None
    p1_by_name = {feature.name: feature for feature in P1_FEATURES}
    for row in rows:
        row_count += 1
        device = row.get("device_id")
        if device is None:
            null_device_rows += 1
        else:
            devices.add(device)
        inform_time = parse_datetime(row.get("inform_time"))
        if inform_time is None:
            invalid_inform_rows += 1
            continue
        first_inform = inform_time if first_inform is None else min(first_inform, inform_time)
        last_inform = inform_time if last_inform is None else max(last_inform, inform_time)

        flat_uptime = number(row.get("uptime_s"))
        for feature in FLAT_FEATURES:
            state = states[feature.name]
            raw = row.get(feature.name.removeprefix("flat."))
            if raw is not None:
                state["present_rows"] += 1
                state["leaf_values_seen"] += 1
            value = number(raw)
            if value is None:
                if raw is not None:
                    state["invalid_value_leaf_values"] += 1
                continue
            state["fresh_leaf_values"] += 1
            add_observation(state, device, inform_time, value)

        for name, feature in p1_by_name.items():
            state = states[name]
            raw = row.get(name)
            if feature.special:
                value, event_time = extract_meta(raw, inform_time, feature, state)
                if value is None:
                    continue
                add_observation(state, device, inform_time, value)
                if feature.special == "last_boot" and device is not None and event_time is not None:
                    state["boot_entries"][device].append((inform_time, event_time, flat_uptime))
                    if flat_uptime is not None:
                        state["boot_uptime_pairs"].append((value, flat_uptime))
            else:
                value = extract_regular(raw, inform_time, feature, state)
                if value is not None:
                    add_observation(state, device, inform_time, value)

    return states, {
        "rows": row_count,
        "devices": len(devices),
        "null_device_rows": null_device_rows,
        "invalid_inform_time_rows": invalid_inform_rows,
        "first_inform_utc": first_inform.isoformat() if first_inform else None,
        "last_inform_utc": last_inform.isoformat() if last_inform else None,
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower))


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in pairs)
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else None


def summarize(feature: Feature, state: dict[str, object], row_count: int) -> dict[str, object]:
    observations = state["observations"]
    by_device = state["by_device"]
    device_sequences = {
        device: sorted(sequence, key=lambda item: item[0]) for device, sequence in by_device.items()
    }
    devices_ge3 = {device: sequence for device, sequence in device_sequences.items() if len(sequence) >= 3}
    if feature.source == "flat":
        result: dict[str, object] = {
            "observed_row_coverage": len(observations) / row_count if row_count else 0.0,
            "observed_rows": len(observations),
            "present_rows": state["present_rows"],
            "devices_with_observations": len(device_sequences),
            "devices_with_3_observations": len(devices_ge3),
            "invalid_values": state["invalid_value_leaf_values"],
            "freshness_verified": False,
        }
    else:
        result = {
            "fresh_row_coverage": len(observations) / row_count if row_count else 0.0,
            "fresh_rows": len(observations),
            "present_rows": state["present_rows"],
            "devices_with_fresh_values": len(device_sequences),
            "devices_with_3_fresh_observations": len(devices_ge3),
            "leaf_values_seen": state["leaf_values_seen"],
            "fresh_leaf_values": state["fresh_leaf_values"],
            "stale_leaf_values": state["stale_leaf_values"],
            "future_leaf_values": state["future_leaf_values"],
            "invalid_timestamp_leaf_values": state["invalid_timestamp_leaf_values"],
            "invalid_value_leaf_values": state["invalid_value_leaf_values"],
            "freshness_verified": True,
        }
    if feature.value_type == "numeric":
        values = [float(value) for value in observations]
        q1, q3 = percentile(values, 0.25), percentile(values, 0.75)
        varying = sum(
            1 for sequence in devices_ge3.values() if max(value for _, value in sequence) > min(value for _, value in sequence)
        )
        result.update(
            iqr=(q3 - q1) if q1 is not None and q3 is not None else None,
            varying_devices=varying,
            varying_device_share=varying / len(devices_ge3) if devices_ge3 else 0.0,
        )
    else:
        counts = Counter(str(value) for value in observations)
        dominant = counts.most_common(1)[0][0] if counts else None
        minority_devices = sum(
            1 for sequence in device_sequences.values() if any(value != dominant for _, value in sequence)
        )
        transitions = 0
        transition_devices = 0
        for sequence in device_sequences.values():
            count = sum(current[1] != previous[1] for previous, current in zip(sequence, sequence[1:]))
            transitions += count
            transition_devices += count > 0
        result.update(
            category_levels=len(counts),
            minority_share=(1 - max(counts.values()) / sum(counts.values())) if counts else 0.0,
            minority_devices=minority_devices,
            transitions=transitions,
            transition_devices=transition_devices,
        )
    if feature.special == "last_boot":
        boot_transitions = boot_transition_devices = paired_transitions = uptime_resets = 0
        for entries in state["boot_entries"].values():
            entries = sorted(entries, key=lambda item: item[0])
            device_transitions = 0
            for previous, current in zip(entries, entries[1:]):
                if current[1] == previous[1]:
                    continue
                boot_transitions += 1
                device_transitions += 1
                if previous[2] is not None and current[2] is not None:
                    paired_transitions += 1
                    uptime_resets += current[2] < previous[2]
            boot_transition_devices += device_transitions > 0
        pairs = state["boot_uptime_pairs"]
        result.update(
            boot_timestamp_transitions=boot_transitions,
            boot_transition_devices=boot_transition_devices,
            transitions_with_paired_uptime=paired_transitions,
            transitions_with_uptime_reset=uptime_resets,
            uptime_reset_share=uptime_resets / paired_transitions if paired_transitions else None,
            boot_age_uptime_pairs=len(pairs),
            boot_age_uptime_pearson=pearson(pairs),
            boot_age_uptime_median_absolute_difference_seconds=(
                statistics.median(abs(boot_age - uptime) for boot_age, uptime in pairs) if pairs else None
            ),
        )
    return result


def gate(feature: Feature, windows: dict[str, dict[str, object]]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for name, stats in windows.items():
        coverage = stats["observed_row_coverage"] if feature.source == "flat" else stats["fresh_row_coverage"]
        devices_with_3 = (
            stats["devices_with_3_observations"]
            if feature.source == "flat"
            else stats["devices_with_3_fresh_observations"]
        )
        if coverage < MIN_COVERAGE:
            reasons.append(f"{name}:coverage_below_{MIN_COVERAGE:g}")
        if devices_with_3 < MIN_DEVICES_WITH_3:
            qualifier = "observations" if feature.source == "flat" else "fresh_observations"
            reasons.append(f"{name}:devices_with_3_{qualifier}_below_{MIN_DEVICES_WITH_3}")
        if feature.value_type == "numeric":
            if stats["iqr"] is None or stats["iqr"] <= 0:
                reasons.append(f"{name}:iqr_not_positive")
            if stats["varying_device_share"] < MIN_VARYING_DEVICE_SHARE:
                reasons.append(f"{name}:varying_device_share_below_{MIN_VARYING_DEVICE_SHARE:g}")
        else:
            if stats["category_levels"] < 2:
                reasons.append(f"{name}:fewer_than_2_categories")
            if stats["minority_share"] < MIN_MINORITY_SHARE:
                reasons.append(f"{name}:minority_share_below_{MIN_MINORITY_SHARE:g}")
            if stats["minority_devices"] < MIN_MINORITY_DEVICES:
                reasons.append(f"{name}:minority_devices_below_{MIN_MINORITY_DEVICES}")
            if stats["transitions"] < MIN_TRANSITIONS:
                reasons.append(f"{name}:transitions_below_{MIN_TRANSITIONS}")
        if feature.special == "last_boot" and stats["boot_timestamp_transitions"] < MIN_TRANSITIONS:
            reasons.append(f"{name}:boot_transitions_below_{MIN_TRANSITIONS}")
    return not reasons, reasons


def parse_window(text: str) -> Window:
    match = re.fullmatch(r"([a-z][a-z0-9_-]*)=([^,]+),([^,]+)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("use NAME=START_UTC,END_UTC")
    start, end = parse_datetime(match.group(2)), parse_datetime(match.group(3))
    if start is None or end is None or start >= end:
        raise argparse.ArgumentTypeError("window timestamps must be valid UTC values with START < END")
    return Window(match.group(1), start, end)


def validate_windows(windows: list[Window]) -> None:
    if len(windows) != 3:
        raise ValueError("exactly three chronological --window values are required")
    if len({window.name for window in windows}) != len(windows):
        raise ValueError("window names must be unique")
    for previous, current in zip(windows, windows[1:]):
        if current.start < previous.end:
            raise ValueError("windows must be supplied in chronological, non-overlapping order")
    total_days = sum((window.end - window.start).total_seconds() for window in windows) / 86400
    if total_days > MAX_TOTAL_WINDOW_DAYS:
        raise ValueError(f"audit windows total {total_days:g} days; the source-only cap is {MAX_TOTAL_WINDOW_DAYS}")


def load_env(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"environment file not found: {path}")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def mysql_connector():
    try:
        import mysql.connector as connector
    except ImportError as exc:
        raise RuntimeError(
            "mysql-connector-python >=8 is required; run with a Python environment that already provides it"
        ) from exc
    version = tuple(int(part) for part in re.findall(r"\d+", connector.__version__)[:3])
    if version < (8,):
        raise RuntimeError(
            f"mysql-connector-python >=8 is required for caching_sha2_password (found {connector.__version__}); "
            "run with a compatible Python environment"
        )
    return connector


def select_sql(table: str) -> str:
    extracts = ",\n               ".join(
        f"JSON_EXTRACT(params_json, %s) AS `{feature.name}`" for feature in P1_FEATURES
    )
    flat = ", ".join(FLAT_FIELDS)
    return f"""
        SELECT device_id, {flat},
               {extracts}
        FROM `{table}`
        WHERE inform_time >= %s AND inform_time < %s
    """


def run_source_audit(windows: list[Window], env_file: Path) -> dict[str, object]:
    load_env(env_file)
    required = (
        "ACS_DUMP_DB_HOST",
        "ACS_DUMP_DB_NAME",
        "ACS_DUMP_DB_USER",
        "ACS_DUMP_DB_PASSWORD",
        "ACS_TABLE_NAME",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("missing environment variables: " + ", ".join(missing))
    table = os.environ["ACS_TABLE_NAME"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise ValueError("ACS_TABLE_NAME must contain only letters, digits, and underscores")

    connector = mysql_connector()
    connection = None
    feature_windows: dict[str, dict[str, dict[str, object]]] = {
        feature.name: {} for feature in FEATURES
    }
    source_windows: dict[str, dict[str, object]] = {}
    try:
        connection = connector.connect(
            host=os.environ["ACS_DUMP_DB_HOST"],
            port=int(os.environ.get("ACS_DUMP_DB_PORT", "3306")),
            user=os.environ["ACS_DUMP_DB_USER"],
            password=os.environ["ACS_DUMP_DB_PASSWORD"],
            database=os.environ["ACS_DUMP_DB_NAME"],
            connection_timeout=20,
            autocommit=False,
        )
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SET SESSION time_zone = '+00:00'")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SELECT VERSION() server_version, @@session.time_zone session_time_zone")
            source = cursor.fetchone()
        sql = select_sql(table)
        paths = [feature.json_path for feature in P1_FEATURES]
        for window in windows:
            with connection.cursor(dictionary=True, buffered=False) as cursor:
                cursor.execute(sql, paths + [window.start.replace(tzinfo=None), window.end.replace(tzinfo=None)])
                states, window_source = audit_rows(
                    row for batch in iter(lambda: cursor.fetchmany(2000), []) for row in batch
                )
            source_windows[window.name] = window_source
            for feature in FEATURES:
                feature_windows[feature.name][window.name] = summarize(
                    feature, states[feature.name], window_source["rows"]
                )
    except connector.Error as exc:
        error_number = getattr(exc, "errno", None) or "unknown"
        raise RuntimeError(f"read-only MySQL audit failed (error {error_number}); no output was written") from None
    finally:
        if connection is not None:
            connection.close()

    feature_results = []
    for feature in FEATURES:
        eligible, reasons = gate(feature, feature_windows[feature.name])
        item: dict[str, object] = {
            "feature": feature.name,
            "source": feature.source,
            "reducer": feature.reducer,
            "eligible_for_mapped_cohort_audit": eligible,
            "exclusion_reasons": reasons,
            "windows": feature_windows[feature.name],
        }
        if feature.canonical_path:
            item["canonical_path"] = feature.canonical_path
            item["json_path"] = feature.json_path
            item["freshness_semantics"] = (
                "plausible prior boot timestamp within ten years; derive only boot age and reset indicators"
                if feature.special == "last_boot"
                else "root event time must be strictly non-future and no more than 24 hours old"
                if feature.special == "last_inform"
                else "each retained leaf value uses its own strictly non-future _timestamp and a 24-hour maximum age"
            )
        else:
            item["freshness_semantics"] = (
                "coverage and variation screen only: the flat mirror has no independent value timestamp; "
                "the corresponding ordinary P1 leaf timestamp governs dynamic freshness downstream"
            )
        if feature.special == "last_boot":
            item["candidate_transforms"] = ["boot_age_seconds", "boot_reset_indicator"]
        feature_results.append(item)

    eligible_flat = [item["feature"] for item in feature_results if item["source"] == "flat" and item["eligible_for_mapped_cohort_audit"]]
    eligible_p1 = [item["feature"] for item in feature_results if item["source"] == "params_json" and item["eligible_for_mapped_cohort_audit"]]
    return {
        "schema_version": 1,
        "status": "SOURCE_TRANSFER_SCREEN_COMPLETE",
        "decision": "CONTINUE_TO_MAPPED_COHORT_AUDIT" if eligible_flat or eligible_p1 else "STOP_NO_SOURCE_ELIGIBLE_FEATURES",
        "scope": "Unlabelled source-transfer screen only; it does not authorize a correlation or predictor claim.",
        "configuration": {
            "table": table,
            "timezone": "UTC",
            "window_end": "exclusive",
            "windows": [
                {"name": window.name, "start": window.start.isoformat(), "end": window.end.isoformat()}
                for window in windows
            ],
            "flat_fields": list(FLAT_FIELDS),
            "p1_whitelist_size": len(P1_FEATURES),
            "freshness_hours": FRESH_HOURS,
            "maximum_boot_age_days": MAX_BOOT_AGE_DAYS,
            "thresholds": {
                "coverage_each_window": MIN_COVERAGE,
                "flat_devices_with_3_observations_each_window": MIN_DEVICES_WITH_3,
                "p1_devices_with_3_fresh_observations_each_window": MIN_DEVICES_WITH_3,
                "numeric_iqr_positive_each_window": True,
                "numeric_varying_device_share_each_window": MIN_VARYING_DEVICE_SHARE,
                "categorical_levels_each_window": 2,
                "categorical_minority_share_each_window": MIN_MINORITY_SHARE,
                "categorical_minority_devices_each_window": MIN_MINORITY_DEVICES,
                "categorical_or_boot_transitions_each_window": MIN_TRANSITIONS,
            },
        },
        "source": {
            "server_version": source["server_version"],
            "session_time_zone": source["session_time_zone"],
            "connector_version": connector.__version__,
            "python_version": sys.version.split()[0],
            "windows": source_windows,
        },
        "eligible_flat_features": eligible_flat,
        "eligible_p1_features": eligible_p1,
        "features": feature_results,
        "privacy": {
            "passed": True,
            "direct_identifiers_written": False,
            "full_params_json_written": False,
            "raw_values_written": False,
        },
    }


def privacy_check(audit: dict[str, object]) -> None:
    prohibited_keys = {"device_id", "serial_number", "ip", "ssid", "params_json", "raw_values"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            found = prohibited_keys.intersection(key.lower() for key in value)
            if found:
                raise ValueError("privacy check rejected output keys: " + ", ".join(sorted(found)))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(audit)


def self_check() -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    state = new_state()
    feature = next(item for item in P1_FEATURES if item.name == "p1.device_temperature")
    raw = json.dumps(
        [
            {"_value": "20", "_timestamp": (now - timedelta(hours=1)).isoformat()},
            {"_value": "25", "_timestamp": (now - timedelta(hours=2)).isoformat()},
            {"_value": "99", "_timestamp": (now - timedelta(hours=25)).isoformat()},
            {"_value": "88", "_timestamp": (now + timedelta(minutes=1)).isoformat()},
        ]
    )
    assert extract_regular(raw, now, feature, state) == 25
    assert state["fresh_leaf_values"] == 2 and state["stale_leaf_values"] == 1
    assert state["future_leaf_values"] == 1

    boot = next(item for item in P1_FEATURES if item.special == "last_boot")
    rows = []
    for device_number in range(25):
        initial_boot = now - timedelta(hours=2, seconds=device_number)
        reboot_time = now + timedelta(hours=2, minutes=-5)
        for index in range(4):
            inform = now + timedelta(hours=index)
            rebooted = index >= 2
            boot_time = reboot_time if rebooted else initial_boot
            uptime = 300 + (index - 2) * 3600 if rebooted else (inform - initial_boot).total_seconds()
            rows.append(
                {
                    "device_id": f"synthetic-{device_number}",
                    "inform_time": inform,
                    "uptime_s": uptime,
                    boot.name: json.dumps(boot_time.isoformat()),
                }
            )
    states, source = audit_rows(rows)
    summary = summarize(boot, states[boot.name], source["rows"])
    assert summary["boot_timestamp_transitions"] == 25
    assert summary["transitions_with_uptime_reset"] == 25
    assert summary["devices_with_3_fresh_observations"] == 25
    assert summary["boot_age_uptime_median_absolute_difference_seconds"] == 0
    eligible, reasons = gate(boot, {"synthetic": summary})
    assert eligible, reasons
    privacy_check({"features": [{"canonical_path": boot.canonical_path}], "privacy": {"passed": True}})


def write_json(path: Path, audit: dict[str, object], overwrite: bool) -> None:
    if path.suffix.lower() != ".json":
        raise ValueError("--output must name a .json file")
    if path.exists() and not overwrite:
        raise ValueError(f"output already exists: {path}; pass --overwrite to replace it")
    privacy_check(audit)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only, aggregate source screen for the frozen ACS flat and P1 parameter whitelist."
    )
    parser.add_argument(
        "--window",
        action="append",
        type=parse_window,
        help="repeat exactly three times as NAME=START_UTC,END_UTC (end exclusive)",
    )
    parser.add_argument("--output", type=Path, help="aggregate audit JSON path")
    parser.add_argument("--env-file", type=Path, default=ROOT.parent / ".env")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--self-check", action="store_true", help="run in memory; do not connect or write")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        print("self-check passed")
        return
    if not args.window or args.output is None:
        parser.error("--window (three times) and --output are required unless --self-check is used")
    try:
        validate_windows(args.window)
        audit = run_source_audit(args.window, args.env_file)
        write_json(args.output, audit, args.overwrite)
    except (AssertionError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"ACS parameter audit stopped: {exc}") from exc
    print(json.dumps({"status": audit["status"], "decision": audit["decision"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
