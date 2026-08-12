"""Superseded historical paired-window pilot.

Use ``acs_outage_feasibility.py`` for the contracted exact-mapping,
strict-prior, chronological analysis. This file remains only to preserve the
earlier exploratory method and its null result.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql
import scipy
from dotenv import load_dotenv
from scipy import stats


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
BOOKING_TRUTH = WORKSPACE.parent / "booking_truth"
OUTPUT = ROOT / "outputs" / "acs_correlation_pilot_2026-08-11"
LEAD_HOURS = 1
FEATURE_COLUMNS = (
    "uptime_latest",
    "uptime_change",
    "cpu_mean",
    "cpu_max",
    "cpu_change",
    "memory_free_pct_mean",
    "memory_free_pct_min",
    "memory_free_pct_change",
    "temperature_mean",
    "temperature_max",
    "temperature_change",
    "optical_rx_mean",
    "optical_rx_min",
    "optical_rx_change",
    "optical_tx_mean",
    "optical_tx_max",
    "optical_tx_change",
    "param_count_mean",
    "param_count_change",
    "inform_count",
    "staleness_minutes",
)


def normalize(value: object) -> str | None:
    result = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return result or None


def bh_adjust(values: list[float]) -> list[float]:
    result = [float("nan")] * len(values)
    valid = [(i, p) for i, p in enumerate(values) if np.isfinite(p)]
    running = 1.0
    for rank, (index, p) in reversed(list(enumerate(sorted(valid, key=lambda x: x[1]), 1))):
        running = min(running, p * len(valid) / rank)
        result[index] = running
    return result


def rank_biserial(differences: np.ndarray) -> float:
    differences = differences[np.isfinite(differences) & (differences != 0)]
    if not len(differences):
        return 0.0
    ranks = stats.rankdata(np.abs(differences))
    return float((ranks[differences > 0].sum() - ranks[differences < 0].sum()) / ranks.sum())


def merge_episodes(events: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    episodes: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for row in events.sort_values("start_utc").itertuples():
        start = row.start_utc
        end = max(row.end_utc, start + timedelta(minutes=1))
        if episodes and start <= episodes[-1][1] + timedelta(hours=6):
            episodes[-1] = (episodes[-1][0], max(episodes[-1][1], end))
        else:
            episodes.append((start, end))
    return episodes


def self_check() -> None:
    assert np.allclose(bh_adjust([0.01, 0.04, 0.03]), [0.03, 0.04, 0.04])
    assert rank_biserial(np.array([1.0, 2.0, -1.0])) > 0
    sample = pd.DataFrame(
        {
            "start_utc": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 02:00", "2026-01-02 00:00"]),
            "end_utc": pd.to_datetime(["2026-01-01 01:00", "2026-01-01 03:00", "2026-01-02 01:00"]),
        }
    )
    assert len(merge_episodes(sample)) == 2


def feature_summary(frame: pd.DataFrame, end: pd.Timestamp) -> dict[str, float]:
    frame = frame.sort_values("inform_time")
    result: dict[str, float] = {
        "inform_count": float(len(frame)),
        "staleness_minutes": float((end - frame["inform_time"].max()).total_seconds() / 60),
    }

    def add(source: str, name: str, measures: tuple[str, ...]) -> None:
        values = pd.to_numeric(frame[source], errors="coerce").dropna()
        for measure in measures:
            if values.empty:
                result[f"{name}_{measure}"] = float("nan")
            elif measure == "latest":
                result[f"{name}_{measure}"] = float(values.iloc[-1])
            elif measure == "mean":
                result[f"{name}_{measure}"] = float(values.mean())
            elif measure == "min":
                result[f"{name}_{measure}"] = float(values.min())
            elif measure == "max":
                result[f"{name}_{measure}"] = float(values.max())
            elif measure == "change":
                result[f"{name}_{measure}"] = float(values.iloc[-1] - values.iloc[0]) if len(values) > 1 else float("nan")

    add("uptime_s", "uptime", ("latest", "change"))
    add("cpu_pct", "cpu", ("mean", "max", "change"))
    add("memory_free_pct", "memory_free_pct", ("mean", "min", "change"))
    add("temperature_c", "temperature", ("mean", "max", "change"))
    add("optical_rx_dbm", "optical_rx", ("mean", "min", "change"))
    add("optical_tx_dbm", "optical_tx", ("mean", "max", "change"))
    add("param_count", "param_count", ("mean", "change"))
    return result


def main() -> None:
    self_check()
    load_dotenv(WORKSPACE / ".env")
    table = os.environ["ACS_TABLE_NAME"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise SystemExit("Unsafe ACS_TABLE_NAME")

    mysql = pymysql.connect(
        host=os.environ["ACS_DUMP_DB_HOST"],
        port=int(os.environ.get("ACS_DUMP_DB_PORT", "3306")),
        user=os.environ["ACS_DUMP_DB_USER"],
        password=os.environ["ACS_DUMP_DB_PASSWORD"],
        database=os.environ["ACS_DUMP_DB_NAME"],
        connect_timeout=20,
        read_timeout=120,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with mysql.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute(f"SELECT DISTINCT device_id, serial_number FROM `{table}`")
        identifiers = cursor.fetchall()
        cursor.execute(
            f"SELECT COUNT(*) rows_n, MIN(inform_time) time_min, MAX(inform_time) time_max FROM `{table}`"
        )
        acs_audit = cursor.fetchone()

    serials: dict[str, set[object]] = defaultdict(set)
    for row in identifiers:
        serials[str(row["device_id"])].add(row["serial_number"])
    device_to_key: dict[str, int] = {}
    acs_values: list[tuple[int, str | None]] = []
    for key, (device_id, seen) in enumerate(sorted(serials.items()), 1):
        non_null = {value for value in seen if value is not None}
        device_to_key[device_id] = key
        acs_values.append((key, normalize(next(iter(non_null))) if len(non_null) == 1 else None))

    sys.path.insert(0, str(BOOKING_TRUTH))
    from data_lib.data_fetch.wiom_data import WiomData

    snowflake = WiomData("snowflake")
    snowflake._connection_params.update(
        login_timeout=20,
        network_timeout=120,
        ocsp_response_cache_filename=str(ROOT / ".ocsp_cache.json"),
    )
    sf = snowflake._connect()
    cursor = sf.cursor()
    placeholders = ",".join(["(%s,%s)"] * len(acs_values))
    parameters = [value for row in acs_values for value in row]
    mapping_sql = f"""
        WITH acs(acs_key, serial_norm) AS (
          SELECT column1::INTEGER, column2::TEXT FROM VALUES {placeholders}
        ), raw_map AS (
          SELECT DISTINCT a.acs_key, t.device_id, COALESCE(t.long_nas_id, t.nasid) nasid
          FROM acs a
          JOIN prod_db.master_db_read_dbo.t_device t
            ON a.serial_norm = REGEXP_REPLACE(UPPER(TRIM(t.pon_serial)), '[^A-Z0-9]', '')
          WHERE a.serial_norm IS NOT NULL
            AND COALESCE(t._fivetran_deleted, FALSE) = FALSE
        ), unique_map AS (
          SELECT * FROM raw_map
          WHERE device_id IS NOT NULL AND nasid IS NOT NULL
          QUALIFY COUNT(DISTINCT device_id) OVER (PARTITION BY acs_key) = 1
              AND COUNT(DISTINCT acs_key) OVER (PARTITION BY device_id) = 1
              AND COUNT(DISTINCT device_id) OVER (PARTITION BY nasid) = 1
        ), clean_customer AS (
          SELECT account_id, MIN(nasid) nasid, MIN(location_start_time) location_start_time,
                 MAX(plan_expiry_time) plan_expiry_time
          FROM prod_db.dbt.active_base
          WHERE source = 'CUSTOMER_V2' AND UPPER(active_state) = 'ACTIVE' AND nasid IS NOT NULL
          GROUP BY account_id
          HAVING COUNT(DISTINCT nasid) = 1
        )
        SELECT u.acs_key, u.device_id, u.nasid, c.location_start_time, c.plan_expiry_time,
               (SELECT COUNT(DISTINCT acs_key) FROM raw_map) pon_matches,
               (SELECT COUNT(DISTINCT acs_key) FROM unique_map) unique_matches
        FROM unique_map u JOIN clean_customer c ON c.nasid = u.nasid
        QUALIFY COUNT(*) OVER (PARTITION BY u.nasid) = 1
    """
    cursor.execute(mapping_sql, parameters)
    mapping = cursor.fetchall()
    mapping_columns = [column[0].lower() for column in cursor.description]
    mapped = pd.DataFrame(mapping, columns=mapping_columns)
    if mapped.empty:
        raise SystemExit("No CUSTOMER_V2 devices passed the PON_SERIAL bridge")

    outage_values = [
        (int(row.acs_key), str(row.device_id), str(row.location_start_time), str(row.plan_expiry_time))
        for row in mapped.itertuples()
    ]
    outage_placeholders = ",".join(["(%s,%s,%s,%s)"] * len(outage_values))
    outage_parameters = [value for row in outage_values for value in row]
    outage_sql = f"""
        WITH mapped(acs_key, device_id, location_start_time, plan_expiry_time) AS (
          SELECT column1::INTEGER, column2::TEXT, column3::TIMESTAMP_NTZ, column4::TIMESTAMP_NTZ
          FROM VALUES {outage_placeholders}
        )
        SELECT DISTINCT m.acs_key, i.incident_id,
          CONVERT_TIMEZONE('Asia/Kolkata', 'UTC', i.incident_start_ist)::TIMESTAMP_NTZ start_utc,
          CONVERT_TIMEZONE('Asia/Kolkata', 'UTC', COALESCE(i.closed_at_ist, i.incident_start_ist))::TIMESTAMP_NTZ end_utc
        FROM mapped m
        JOIN prod_db.dbt.stg_ix_incident_impacted_device d ON d.device_id = m.device_id
        JOIN prod_db.dbt.stg_ix_incidents i ON i.incident_id = d.incident_id
        WHERE i.incident_start_ist >= CONVERT_TIMEZONE('UTC', 'Asia/Kolkata', %s::TIMESTAMP_NTZ)
          AND i.incident_start_ist <= CONVERT_TIMEZONE('UTC', 'Asia/Kolkata', %s::TIMESTAMP_NTZ)
          AND m.location_start_time <= i.incident_start_ist
          AND m.plan_expiry_time >= i.incident_start_ist
    """
    cursor.execute(outage_sql, outage_parameters + [acs_audit["time_min"], acs_audit["time_max"]])
    outage_rows = cursor.fetchall()
    outage_columns = [column[0].lower() for column in cursor.description]
    cursor.close()
    sf.close()
    outages = pd.DataFrame(outage_rows, columns=outage_columns)
    outages["start_utc"] = pd.to_datetime(outages["start_utc"])
    outages["end_utc"] = pd.to_datetime(outages["end_utc"])
    outage_keys = sorted(map(int, outages["acs_key"].unique()))

    key_to_device = {key: device for device, key in device_to_key.items() if key in outage_keys}
    device_ids = list(key_to_device.values())
    mysql_placeholders = ",".join(["%s"] * len(device_ids))
    telemetry_sql = f"""
        SELECT device_id, inform_time, uptime_s, cpu_pct, memory_free_kb, memory_total_kb,
               temperature_c, optical_rx_dbm, optical_tx_dbm, param_count
        FROM `{table}`
        WHERE device_id IN ({mysql_placeholders}) AND inform_time BETWEEN %s AND %s
        ORDER BY device_id, inform_time
    """
    with mysql.cursor() as cursor:
        cursor.execute(telemetry_sql, device_ids + [acs_audit["time_min"], acs_audit["time_max"]])
        telemetry = pd.DataFrame(cursor.fetchall())
    mysql.close()
    telemetry["acs_key"] = telemetry.pop("device_id").map(device_to_key)
    telemetry["inform_time"] = pd.to_datetime(telemetry["inform_time"])
    total_memory = pd.to_numeric(telemetry["memory_total_kb"], errors="coerce")
    free_memory = pd.to_numeric(telemetry["memory_free_kb"], errors="coerce")
    telemetry["memory_free_pct"] = np.where(total_memory > 0, 100 * free_memory / total_memory, np.nan)

    observations: list[dict[str, object]] = []
    durations: dict[int, float] = {}
    for acs_key in outage_keys:
        device_events = outages[outages["acs_key"] == acs_key]
        episodes = merge_episodes(device_events)
        device_data = telemetry[telemetry["acs_key"] == acs_key]
        for start, end in reversed(episodes):
            case_end = start - timedelta(hours=LEAD_HOURS)
            case_start = case_end - timedelta(hours=6)
            control_end = case_end - timedelta(days=7)
            control_start = control_end - timedelta(hours=6)
            if control_start < pd.Timestamp(acs_audit["time_min"]):
                continue
            if any(other_start < control_end and other_end > control_start for other_start, other_end in episodes):
                continue
            if any(
                (other_start, other_end) != (start, end) and other_start < case_end and other_end > case_start
                for other_start, other_end in episodes
            ):
                continue
            case = device_data[(device_data["inform_time"] >= case_start) & (device_data["inform_time"] < case_end)]
            control = device_data[(device_data["inform_time"] >= control_start) & (device_data["inform_time"] < control_end)]
            if len(case) < 3 or len(control) < 3:
                continue
            observations.append({"acs_key": acs_key, "condition": "control", **feature_summary(control, control_end)})
            observations.append({"acs_key": acs_key, "condition": "pre_outage", **feature_summary(case, case_end)})
            durations[acs_key] = max((end - start).total_seconds() / 60, 1.0)
            break

    frame = pd.DataFrame(observations)
    if frame.empty:
        raise SystemExit("No devices had both pre-outage and control telemetry windows")
    case = frame[frame["condition"] == "pre_outage"].set_index("acs_key")
    control = frame[frame["condition"] == "control"].set_index("acs_key")
    rng = np.random.default_rng(20260811)
    results: list[dict[str, object]] = []
    for feature in FEATURE_COLUMNS:
        joined = pd.concat([control[feature].rename("control"), case[feature].rename("case")], axis=1).dropna()
        row: dict[str, object] = {"feature": feature, "paired_n": len(joined)}
        if len(joined) >= 20:
            differences = (joined["case"] - joined["control"]).to_numpy(float)
            if np.any(differences != 0):
                wilcoxon = stats.wilcoxon(differences, alternative="two-sided", zero_method="wilcox")
                p_value = float(wilcoxon.pvalue)
                statistic = float(wilcoxon.statistic)
            else:
                p_value, statistic = 1.0, 0.0
            boot = [rank_biserial(differences[rng.integers(0, len(differences), len(differences))]) for _ in range(1000)]
            row.update(
                control_median=float(joined["control"].median()),
                pre_outage_median=float(joined["case"].median()),
                median_difference=float(np.median(differences)),
                wilcoxon_w=statistic,
                occurrence_p=p_value,
                rank_biserial=rank_biserial(differences),
                rank_biserial_ci_low=float(np.percentile(boot, 2.5)),
                rank_biserial_ci_high=float(np.percentile(boot, 97.5)),
                difference_shapiro_p=float(stats.shapiro(differences).pvalue)
                if len(differences) >= 3 and np.ptp(differences) > 0
                else float("nan"),
            )
        else:
            row["occurrence_p"] = float("nan")

        duration_data = case[[feature]].copy()
        duration_data["duration_minutes"] = pd.Series(durations)
        duration_data = duration_data.dropna()
        row["duration_n"] = len(duration_data)
        if len(duration_data) >= 20 and duration_data[feature].nunique() > 1:
            correlation = stats.spearmanr(duration_data[feature], duration_data["duration_minutes"])
            row["duration_spearman_rho"] = float(correlation.statistic)
            row["duration_p"] = float(correlation.pvalue)
        else:
            row["duration_p"] = float("nan")
        results.append(row)

    occurrence_q = bh_adjust([float(row.get("occurrence_p", float("nan"))) for row in results])
    duration_q = bh_adjust([float(row.get("duration_p", float("nan"))) for row in results])
    for row, oq, dq in zip(results, occurrence_q, duration_q):
        row["occurrence_q_bh"] = oq
        row["duration_q_bh"] = dq
    result_frame = pd.DataFrame(results).sort_values(
        ["occurrence_q_bh", "rank_biserial"], ascending=[True, False], na_position="last"
    )

    prohibited = {"device_id", "nasid", "serial_number", "mobile", "ip", "ssid_2g", "ssid_5g"}
    assert not prohibited.intersection(map(str.lower, result_frame.columns))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result_frame.to_csv(OUTPUT / "feature_results.csv", index=False)
    significant = result_frame[result_frame["occurrence_q_bh"] < 0.05]
    duration_significant = result_frame[result_frame["duration_q_bh"] < 0.05]
    audit = {
        "analysis_date": "2026-08-11",
        "status": "ANALYSIS_COMPLETE",
        "acs_rows": int(acs_audit["rows_n"]),
        "acs_devices": len(serials),
        "pon_serial_matches": int(mapped["pon_matches"].iloc[0]),
        "one_to_one_matches": int(mapped["unique_matches"].iloc[0]),
        "customer_v2_matches": int(mapped["acs_key"].nunique()),
        "customer_v2_devices_with_outages": len(outage_keys),
        "paired_devices_analyzed": int(case.index.nunique()),
        "formal_incident_rows": int(len(outages)),
        "design": "same-device six-hour window ending one hour before outage versus a matched control window seven days earlier",
        "minimum_informs_per_window": 3,
        "features_tested": int(result_frame["occurrence_p"].notna().sum()),
        "occurrence_fdr_significant": int(len(significant)),
        "duration_fdr_significant": int(len(duration_significant)),
        "software": {"python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__, "scipy": scipy.__version__},
        "privacy_check": "passed",
    }
    (OUTPUT / "audit.json").write_text(json.dumps(audit, indent=2))

    def top_lines(data: pd.DataFrame, q_column: str, effect_column: str) -> list[str]:
        if data.empty:
            return ["- No feature survived Benjamini-Hochberg correction at q < 0.05."]
        return [
            f"- `{row.feature}`: effect {getattr(row, effect_column):.3f}, q = {getattr(row, q_column):.4g}, n = {int(row.paired_n if q_column == 'occurrence_q_bh' else row.duration_n)}"
            for row in data.sort_values(q_column).head(8).itertuples()
        ]

    report = "\n".join(
        [
            "# ACS outage correlation pilot",
            "",
            "## Result",
            "",
            f"The corrected `PON_SERIAL` bridge linked {audit['pon_serial_matches']} ACS devices to warehouse inventory. After one-to-one and `CUSTOMER_V2` checks, {audit['customer_v2_matches']} devices remained; {audit['customer_v2_devices_with_outages']} had formal outages and {audit['paired_devices_analyzed']} supplied both analysis windows.",
            "",
            "This is an exploratory same-device association analysis, not a causal result or a production prediction model.",
            "",
            "## Outage-occurrence associations",
            "",
            *top_lines(significant, "occurrence_q_bh", "rank_biserial"),
            "",
            "Positive rank-biserial effects mean the feature tended to be higher in the six-hour window ending one hour before an outage than in the same device's control window seven days earlier; negative effects mean lower.",
            "",
            "## Outage-duration associations",
            "",
            *top_lines(duration_significant, "duration_q_bh", "duration_spearman_rho"),
            "",
            "## Method and limitations",
            "",
            "- Outages came from formal incident and impacted-device staging tables; all ACS measurements ended at least one hour before episode onset.",
            "- Incident rows within six hours were merged into one outage episode. Each device contributed at most one paired episode.",
            "- Wilcoxon signed-rank tests and rank-biserial effects were used because paired telemetry differences were not assumed normal. Duration used Spearman correlation.",
            "- Benjamini-Hochberg correction was applied separately to occurrence and duration feature families.",
            f"- Only {audit['paired_devices_analyzed']} devices were analyzable, below the requested 100–200 because only 78 mapped devices belong to the strict `CUSTOMER_V2` cohort.",
            "- Missing fields were left missing; no imputation or `params_json` mining was performed.",
            "- No direct identifiers, exact locations, IPs, SSIDs, or raw parameter payloads were exported.",
        ]
    )
    (OUTPUT / "report.md").write_text(report + "\n")
    print(json.dumps(audit))


if __name__ == "__main__":
    main()
