from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJS_ROOT = ROOT.parent.parent
BOOKING_TRUTH_ROOT = PROJS_ROOT / "booking_truth"
DATA_DIR = ROOT / "data"


SIGNAL_TERMS = (
    "DBM",
    "ONT",
    "OLT",
    "ONU",
    "PON",
    "OPTIC",
    "OPTICAL",
    "SIGNAL",
    "RX",
    "TX",
    "POWER",
    "FIBER",
    "FIBRE",
    "ROUTER",
    "NETBOX",
    "MAC",
    "SERIAL",
    "DEVICE",
    "JC",
    "SPLITTER",
)

VALUE_TERMS = (
    "DBM",
    "OPTIC",
    "OPTICAL",
    "SIGNAL",
    "RX",
    "TX",
    "POWER",
    "ONT",
    "OLT",
    "PON",
)

CONTEXT_TERMS = (
    "ACCOUNT",
    "CUSTOMER",
    "CONNECTION",
    "ROUTER",
    "NETBOX",
    "MAC",
    "SERIAL",
    "DEVICE",
    "PLAN",
    "STATUS",
    "STATE",
    "ADDED",
    "CREATED",
    "UPDATED",
    "TIME",
    "DATE",
    "LAT",
    "LNG",
    "LONG",
)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def full_name(schema: str, table: str) -> str:
    return f"prod_db.{quote_ident(schema)}.{quote_ident(table)}"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def has_any(value: str, terms: tuple[str, ...]) -> bool:
    upper = value.upper()
    for term in terms:
        if term == "RX":
            if re.search(r"(^|_)RX(_|POWER|$)|RXPOWER", upper):
                return True
            continue
        if term == "TX":
            if re.search(r"(^|_)TX(_|POWER|$)|TXPOWER", upper):
                return True
            continue
        if term == "ONT":
            if re.search(r"(^|_)ONTS?(_|$)", upper):
                return True
            continue
        if term == "OLT":
            if re.search(r"(^|_)OLTS?(_|$)", upper):
                return True
            continue
        if term == "PON":
            if re.search(r"(^|_)PON(_|$)", upper):
                return True
            continue
        if term == "MAC":
            if re.search(r"(^|_)MAC(_|ID|$)|ROUTERMAC", upper):
                return True
            continue
        if term in upper:
            return True
    return False


def signal_predicate_sql() -> str:
    predicates = [
        "UPPER(column_name) LIKE '%DBM%'",
        "UPPER(column_name) LIKE '%OPTIC%'",
        "UPPER(column_name) LIKE '%OPTICAL%'",
        "UPPER(column_name) LIKE '%SIGNAL%'",
        "UPPER(column_name) LIKE '%POWER%'",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)RX(_|POWER|$)|RXPOWER')",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)TX(_|POWER|$)|TXPOWER')",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)ONTS?(_|$)')",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)OLTS?(_|$)')",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)PON(_|$)')",
        "UPPER(column_name) LIKE '%ROUTER%'",
        "UPPER(column_name) LIKE '%NETBOX%'",
        "REGEXP_LIKE(UPPER(column_name), '(^|_)MAC(_|ID|$)|ROUTERMAC')",
        "UPPER(column_name) LIKE '%SERIAL%'",
        "UPPER(column_name) LIKE '%DEVICE%'",
        "UPPER(column_name) LIKE '%FIBER%'",
        "UPPER(column_name) LIKE '%FIBRE%'",
        "UPPER(column_name) LIKE '%SPLITTER%'",
        "UPPER(table_name) LIKE '%OPTICAL%'",
        "UPPER(table_name) LIKE '%SIGNAL%'",
        "UPPER(table_name) LIKE '%ROUTER%'",
        "UPPER(table_name) LIKE '%NETBOX%'",
        "REGEXP_LIKE(UPPER(table_name), '(^|_)ONTS?(_|$)')",
        "REGEXP_LIKE(UPPER(table_name), '(^|_)OLTS?(_|$)')",
        "REGEXP_LIKE(UPPER(table_name), '(^|_)PON(_|$)')",
    ]
    return " OR ".join(predicates)


def score_table(table_name: str, columns: list[dict[str, str]]) -> int:
    score = 0
    if has_any(table_name, VALUE_TERMS):
        score += 12
    if has_any(table_name, ("ROUTER", "NETBOX", "DEVICE")):
        score += 4
    for col in columns:
        name = col["COLUMN_NAME"]
        if has_any(name, VALUE_TERMS):
            score += 8
        if has_any(name, CONTEXT_TERMS):
            score += 2
    return score


def choose_sample_columns(columns: list[dict[str, str]]) -> list[str]:
    picked: list[str] = []
    for terms in (VALUE_TERMS, CONTEXT_TERMS):
        for col in columns:
            name = col["COLUMN_NAME"]
            if name not in picked and has_any(name, terms):
                picked.append(name)
            if len(picked) >= 18:
                return picked
    for col in columns:
        name = col["COLUMN_NAME"]
        if name not in picked:
            picked.append(name)
        if len(picked) >= 18:
            break
    return picked


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(BOOKING_TRUTH_ROOT))
    from data_lib.data_fetch.wiom_data import WiomData

    print("initializing WiomData", flush=True)
    db = WiomData("snowflake")
    db._connection_params["login_timeout"] = 20
    db._connection_params["network_timeout"] = 45
    db._connection_params["ocsp_response_cache_filename"] = str(DATA_DIR / "snowflake_ocsp_cache.json")

    result: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "files": {},
        "errors": {},
    }

    term_sql = signal_predicate_sql()
    columns_sql = f"""
        SELECT
          table_schema,
          table_name,
          column_name,
          data_type,
          ordinal_position
        FROM prod_db.information_schema.columns
        WHERE table_schema IN ('PUBLIC', 'DBT', 'DYNAMODB', 'DS_TABLES')
          AND ({term_sql})
        ORDER BY table_schema, table_name, ordinal_position
        LIMIT 2500
    """

    try:
        print("running dbm_signal_column_inventory", flush=True)
        cols_df = db.query(columns_sql)
        cols_path = DATA_DIR / "dbm_signal_column_inventory.csv"
        cols_df.to_csv(cols_path, index=False)
        result["files"]["dbm_signal_column_inventory"] = str(cols_path.relative_to(ROOT))
        print(f"wrote {cols_path} rows={len(cols_df)}", flush=True)
    except Exception as exc:
        result["ok"] = False
        result["errors"]["dbm_signal_column_inventory"] = str(exc)
        (DATA_DIR / "dbm_signal_summary.json").write_text(json.dumps(result, indent=2, default=str))
        print(json.dumps(result, indent=2, default=str), flush=True)
        return

    cols = cols_df.to_dict("records")
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in cols:
        grouped[(str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))].append(
            {
                "COLUMN_NAME": str(row["COLUMN_NAME"]),
                "DATA_TYPE": str(row["DATA_TYPE"]),
                "ORDINAL_POSITION": str(row["ORDINAL_POSITION"]),
            }
        )

    table_rows: list[dict[str, object]] = []
    for (schema, table), table_cols in grouped.items():
        value_cols = [c["COLUMN_NAME"] for c in table_cols if has_any(c["COLUMN_NAME"], VALUE_TERMS)]
        context_cols = [c["COLUMN_NAME"] for c in table_cols if has_any(c["COLUMN_NAME"], CONTEXT_TERMS)]
        table_rows.append(
            {
                "TABLE_SCHEMA": schema,
                "TABLE_NAME": table,
                "SIGNAL_SCORE": score_table(table, table_cols),
                "MATCHED_COLUMN_COUNT": len(table_cols),
                "VALUE_COLUMNS": ", ".join(value_cols[:20]),
                "CONTEXT_COLUMNS": ", ".join(context_cols[:20]),
            }
        )
    table_rows.sort(key=lambda r: (-int(r["SIGNAL_SCORE"]), str(r["TABLE_SCHEMA"]), str(r["TABLE_NAME"])))
    table_path = DATA_DIR / "dbm_signal_table_inventory.csv"
    write_csv(table_path, table_rows)
    result["files"]["dbm_signal_table_inventory"] = str(table_path.relative_to(ROOT))
    print(f"wrote {table_path} rows={len(table_rows)}", flush=True)

    metadata_sql = """
        SELECT table_schema, table_name, row_count, bytes, created, last_altered
        FROM prod_db.information_schema.tables
        WHERE table_schema IN ('PUBLIC', 'DBT', 'DYNAMODB', 'DS_TABLES')
    """
    print("running dbm_signal_table_metadata", flush=True)
    metadata_df = db.query(metadata_sql)
    metadata_lookup = {
        (str(r["TABLE_SCHEMA"]), str(r["TABLE_NAME"])): r for r in metadata_df.to_dict("records")
    }

    enriched_rows: list[dict[str, object]] = []
    for row in table_rows:
        meta = metadata_lookup.get((str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"])), {})
        enriched_rows.append(
            {
                **row,
                "ROW_COUNT": meta.get("ROW_COUNT", ""),
                "LAST_ALTERED": meta.get("LAST_ALTERED", ""),
            }
        )
    enriched_path = DATA_DIR / "dbm_signal_table_inventory.csv"
    write_csv(enriched_path, enriched_rows)

    selected = [
        r
        for r in enriched_rows
        if int(r["SIGNAL_SCORE"]) >= 12
        and (
            has_any(str(r["TABLE_NAME"]), ("ROUTER", "NETBOX", "SIGNAL", "DEVICE"))
            or has_any(str(r["VALUE_COLUMNS"]), VALUE_TERMS)
        )
    ][:8]

    sample_manifest: list[dict[str, object]] = []
    for row in selected:
        schema = str(row["TABLE_SCHEMA"])
        table = str(row["TABLE_NAME"])
        table_cols = grouped[(schema, table)]
        sample_cols = choose_sample_columns(table_cols)
        if not sample_cols:
            continue
        predicates = [
            f"{quote_ident(c)} IS NOT NULL"
            for c in sample_cols
            if has_any(c, VALUE_TERMS)
        ][:8]
        where = "WHERE " + " OR ".join(predicates) if predicates else ""
        order_cols = [c for c in sample_cols if has_any(c, ("ADDED", "CREATED", "UPDATED", "TIME", "DATE"))]
        order_by = f"ORDER BY {quote_ident(order_cols[0])} DESC" if order_cols else ""
        sql = f"""
            SELECT {", ".join(quote_ident(c) for c in sample_cols)}
            FROM {full_name(schema, table)}
            {where}
            {order_by}
            LIMIT 25
        """
        try:
            print(f"running sample {schema}.{table}", flush=True)
            sample_df = db.query(sql)
            out = DATA_DIR / f"dbm_sample_{safe_name(schema)}_{safe_name(table)}.csv"
            sample_df.to_csv(out, index=False)
            sample_manifest.append(
                {
                    "TABLE_SCHEMA": schema,
                    "TABLE_NAME": table,
                    "ROWS": len(sample_df),
                    "COLUMNS": ", ".join(sample_cols),
                    "CSV": str(out.relative_to(ROOT)),
                }
            )
            print(f"wrote {out} rows={len(sample_df)}", flush=True)
        except Exception as exc:
            result["errors"][f"sample_{schema}_{table}"] = str(exc)
            print(f"failed sample {schema}.{table}: {exc}", flush=True)

    sample_manifest_path = DATA_DIR / "dbm_signal_sample_manifest.csv"
    write_csv(sample_manifest_path, sample_manifest)
    result["files"]["dbm_signal_sample_manifest"] = str(sample_manifest_path.relative_to(ROOT))

    curated_queries = {
        "optical_signal_latest_health": """
            WITH latest AS (
              SELECT MAX(date_ist) AS date_ist
              FROM prod_db.dbt.fct_optical_signal
            )
            SELECT
              f.date_ist,
              f.optical_health,
              COUNT(*) AS device_rows,
              COUNT(DISTINCT f.nas_id) AS nas_count,
              ROUND(AVG(f.avg_optical_dbm), 2) AS avg_dbm,
              ROUND(MIN(f.avg_optical_dbm), 2) AS min_dbm,
              ROUND(MAX(f.avg_optical_dbm), 2) AS max_dbm,
              SUM(f.optical_readings) AS optical_readings
            FROM prod_db.dbt.fct_optical_signal f
            JOIN latest l ON f.date_ist = l.date_ist
            GROUP BY 1, 2
            ORDER BY device_rows DESC
        """,
        "optical_signal_daily_trend": """
            WITH latest AS (
              SELECT MAX(date_ist) AS date_ist
              FROM prod_db.dbt.fct_optical_signal
            )
            SELECT
              f.date_ist,
              COUNT(*) AS device_rows,
              COUNT(DISTINCT f.nas_id) AS nas_count,
              ROUND(AVG(f.avg_optical_dbm), 2) AS avg_dbm,
              ROUND(MIN(f.avg_optical_dbm), 2) AS min_dbm,
              ROUND(MAX(f.avg_optical_dbm), 2) AS max_dbm,
              SUM(f.optical_readings) AS optical_readings,
              SUM(IFF(f.avg_optical_dbm < -25 OR f.avg_optical_dbm > -8, 1, 0)) AS board_range_oor_devices
            FROM prod_db.dbt.fct_optical_signal f
            JOIN latest l ON f.date_ist >= DATEADD(day, -14, l.date_ist)
            GROUP BY 1
            ORDER BY 1 DESC
        """,
        "optical_oor_pct_trend": """
            SELECT
              date_ist,
              oor_device_count,
              total_devices,
              ROUND(oor_pct, 2) AS oor_pct
            FROM prod_db.dbt.fct_optical_oor_pct
            ORDER BY date_ist DESC
            LIMIT 45
        """,
        "optical_device_worst_sample": """
            WITH latest AS (
              SELECT MAX(date_ist) AS date_ist
              FROM prod_db.dbt.fct_optical_signal
            )
            SELECT
              f.nas_id,
              f.date_ist,
              ROUND(f.avg_optical_dbm, 2) AS avg_optical_dbm,
              f.optical_readings,
              f.optical_health,
              IFF(f.avg_optical_dbm < -25 OR f.avg_optical_dbm > -8, 'outside board -8 to -25 dBm', 'inside board range') AS board_range_status
            FROM prod_db.dbt.fct_optical_signal f
            JOIN latest l ON f.date_ist = l.date_ist
            WHERE f.avg_optical_dbm IS NOT NULL
            ORDER BY
              IFF(f.avg_optical_dbm < -25 OR f.avg_optical_dbm > -8, 0, 1),
              f.avg_optical_dbm ASC
            LIMIT 40
        """,
        "hourly_device_ping_optical_daily": """
            WITH latest AS (
              SELECT MAX(TO_DATE(date_ist)) AS date_ist
              FROM prod_db.dbt.hourly_device_ping_influx
            )
            SELECT
              TO_DATE(h.date_ist) AS date_ist,
              COUNT(*) AS hourly_rows,
              COUNT(DISTINCT h.device_id) AS device_count,
              COUNT(DISTINCT h.nas_id) AS nas_count,
              ROUND(AVG(h.optical_avg), 2) AS avg_optical_avg_dbm,
              ROUND(MIN(h.optical_min), 2) AS min_optical_dbm,
              ROUND(MAX(h.optical_max), 2) AS max_optical_dbm,
              SUM(h.total_pings_received) AS pings_received,
              SUM(h.total_pings_missed) AS pings_missed
            FROM prod_db.dbt.hourly_device_ping_influx h
            JOIN latest l ON TO_DATE(h.date_ist) >= DATEADD(day, -7, l.date_ist)
            GROUP BY 1
            ORDER BY 1 DESC
        """,
        "gx_router_rx_power_daily": """
            WITH parsed AS (
              SELECT
                TO_DATE(TRY_TO_TIMESTAMP_NTZ("_TIME")) AS date_ist,
                device_id,
                nas_id,
                rx_power_avg,
                rx_power_min,
                rx_power_max
              FROM prod_db.public.gx_router_hourly_data
              WHERE TRY_TO_TIMESTAMP_NTZ("_TIME") IS NOT NULL
            ),
            latest AS (
              SELECT MAX(date_ist) AS date_ist FROM parsed
            )
            SELECT
              p.date_ist,
              COUNT(*) AS hourly_rows,
              COUNT(DISTINCT p.device_id) AS device_count,
              COUNT(DISTINCT p.nas_id) AS nas_count,
              ROUND(AVG(p.rx_power_avg), 2) AS avg_rx_power_dbm,
              ROUND(MIN(p.rx_power_min), 2) AS min_rx_power_dbm,
              ROUND(MAX(p.rx_power_max), 2) AS max_rx_power_dbm
            FROM parsed p
            JOIN latest l ON p.date_ist >= DATEADD(day, -7, l.date_ist)
            GROUP BY 1
            ORDER BY 1 DESC
        """,
        "router_details_power_profile": """
            WITH cleaned AS (
              SELECT
                "rx_power",
                "tx_power",
                IFF("rx_power" BETWEEN -60 AND 10, "rx_power", NULL) AS rx_power_clean,
                IFF("tx_power" BETWEEN -10 AND 40, "tx_power", NULL) AS tx_power_clean,
                "is_customer_active",
                "is_plan_active"
              FROM prod_db.public.router_details
              WHERE "deleted" = 0 OR "deleted" IS NULL
            )
            SELECT
              COUNT(*) AS router_rows,
              COUNT_IF("rx_power" IS NOT NULL) AS rx_power_rows,
              COUNT_IF("tx_power" IS NOT NULL) AS tx_power_rows,
              COUNT(rx_power_clean) AS valid_rx_power_rows,
              COUNT(tx_power_clean) AS valid_tx_power_rows,
              COUNT_IF("is_customer_active" = 1) AS active_customer_rows,
              COUNT_IF("is_plan_active" = 1) AS active_plan_rows,
              COUNT_IF(rx_power_clean < -25 OR rx_power_clean > -8) AS board_range_oor_rx_rows,
              ROUND(AVG(rx_power_clean), 2) AS avg_rx_power_dbm,
              ROUND(MIN(rx_power_clean), 2) AS min_rx_power_dbm,
              ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY rx_power_clean), 2) AS p10_rx_power_dbm,
              ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY rx_power_clean), 2) AS median_rx_power_dbm,
              ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY rx_power_clean), 2) AS p90_rx_power_dbm,
              ROUND(MAX(rx_power_clean), 2) AS max_rx_power_dbm,
              ROUND(AVG(tx_power_clean), 2) AS avg_tx_power_dbm,
              ROUND(MIN(tx_power_clean), 2) AS min_tx_power_dbm,
              ROUND(MAX(tx_power_clean), 2) AS max_tx_power_dbm
            FROM cleaned
        """,
        "router_details_power_sample": """
            SELECT
              "nas_id" AS nas_id,
              ROUND("rx_power", 2) AS rx_power_dbm,
              ROUND("tx_power", 2) AS tx_power_dbm,
              "ping_in_24hrs" AS ping_in_24hrs,
              "is_customer_active" AS is_customer_active,
              "is_plan_active" AS is_plan_active,
              "added_time" AS added_time,
              "modified_time" AS modified_time,
              IFF("rx_power" < -25 OR "rx_power" > -8, 'outside board -8 to -25 dBm', 'inside board range') AS board_range_status
            FROM prod_db.public.router_details
            WHERE ("deleted" = 0 OR "deleted" IS NULL)
              AND "rx_power" BETWEEN -60 AND 10
            ORDER BY
              IFF("is_customer_active" = 1 AND "is_plan_active" = 1, 0, 1),
              IFF("rx_power" < -25 OR "rx_power" > -8, 0, 1),
              "rx_power" ASC
            LIMIT 40
        """,
        "taskvanilla_opticalpower_events": """
            WITH base AS (
              SELECT
                event_name,
                optical_power_source,
                added_time,
                TRY_TO_DOUBLE(REGEXP_SUBSTR(opticalpower, '-?[0-9]+(\\\\.[0-9]+)?')) AS optical_dbm
              FROM prod_db.dbt.taskvanilla_audit
              WHERE added_time >= '2025-06-01'
                AND (opticalpower IS NOT NULL OR optical_power_source IS NOT NULL)
            )
            SELECT
              event_name,
              COALESCE(optical_power_source, 'unknown') AS optical_power_source,
              COUNT(*) AS event_rows,
              COUNT(optical_dbm) AS numeric_optical_power_rows,
              ROUND(AVG(optical_dbm), 2) AS avg_optical_dbm,
              ROUND(MIN(optical_dbm), 2) AS min_optical_dbm,
              ROUND(MAX(optical_dbm), 2) AS max_optical_dbm,
              MAX(added_time) AS latest_added_time
            FROM base
            GROUP BY 1, 2
            ORDER BY event_rows DESC
            LIMIT 40
        """,
        "partner_ont_rollup": """
            SELECT
              COUNT(*) AS partner_rows,
              SUM(total_devices) AS total_devices,
              SUM(total_onts) AS total_onts,
              SUM(active_onts) AS active_onts,
              SUM(churned_onts) AS churned_onts,
              SUM(partner_office_onts) AS partner_office_onts,
              SUM(repair_onts) AS repair_onts,
              SUM(partner_recovered_onts) AS partner_recovered_onts,
              SUM(total_routers) AS total_routers,
              SUM(active_routers) AS active_routers,
              SUM(active_customer_with_optical_power) AS active_customers_with_optical_power
            FROM prod_db.dbt.partner_janam_kundli
        """,
        "partner_ont_top": """
            SELECT
              total_devices,
              total_onts,
              active_onts,
              churned_onts,
              partner_office_onts,
              repair_onts,
              partner_recovered_onts,
              total_routers,
              active_routers,
              active_customer_with_optical_power
            FROM prod_db.dbt.partner_janam_kundli
            ORDER BY active_onts DESC NULLS LAST
            LIMIT 30
        """,
        "booking_ont_distribution": """
            SELECT
              ont,
              COUNT(*) AS booking_rows,
              COUNT(DISTINCT device_id) AS distinct_device_ids
            FROM prod_db.dynamodb.booking
            GROUP BY 1
            ORDER BY booking_rows DESC
            LIMIT 25
        """,
        "incentive_optical_power_profile": """
            SELECT
              COUNT(*) AS rows_with_optical_power,
              ROUND(AVG(optical_power), 2) AS avg_optical_power,
              ROUND(MIN(optical_power), 2) AS min_optical_power,
              ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY optical_power), 2) AS p10_optical_power,
              ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY optical_power), 2) AS median_optical_power,
              ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY optical_power), 2) AS p90_optical_power,
              ROUND(MAX(optical_power), 2) AS max_optical_power
            FROM prod_db.dynamodb.in_p_incentive_processing_schedule
            WHERE optical_power IS NOT NULL
        """,
        "relevant_network_table_counts": """
            SELECT
              table_schema,
              table_name,
              row_count,
              last_altered
            FROM prod_db.information_schema.tables
            WHERE (table_schema, table_name) IN (
              ('DBT', 'FCT_OPTICAL_SIGNAL'),
              ('DBT', 'AVG_OPTICAL_SIGNAL'),
              ('DBT', 'FCT_OPTICAL_OOR_PCT'),
              ('DBT', 'HOURLY_DEVICE_PING_INFLUX'),
              ('PUBLIC', 'GX_ROUTER_HOURLY_DATA'),
              ('PUBLIC', 'ROUTER_DETAILS'),
              ('PUBLIC', 'ROUTER_DETAILS_AUDIT'),
              ('DBT', 'TASKVANILLA_AUDIT'),
              ('DBT', 'PARTNER_JANAM_KUNDLI'),
              ('DYNAMODB', 'BOOKING'),
              ('DYNAMODB', 'IN_P_INCENTIVE_PROCESSING_SCHEDULE')
            )
            ORDER BY COALESCE(row_count, 0) DESC
        """,
        "olt_column_inventory": """
            SELECT
              table_schema,
              table_name,
              column_name,
              data_type,
              ordinal_position
            FROM prod_db.information_schema.columns
            WHERE table_schema IN ('PUBLIC', 'DBT', 'DYNAMODB', 'DS_TABLES')
              AND (
                REGEXP_LIKE(UPPER(column_name), '(^|_)OLTS?(_|$)')
                OR REGEXP_LIKE(UPPER(table_name), '(^|_)OLTS?(_|$)')
              )
            ORDER BY table_schema, table_name, ordinal_position
            LIMIT 100
        """,
    }

    for name, sql in curated_queries.items():
        try:
            print(f"running {name}", flush=True)
            df = db.query(sql)
            out = DATA_DIR / f"dbm_{name}.csv"
            df.to_csv(out, index=False)
            result["files"][name] = str(out.relative_to(ROOT))
            print(f"wrote {out} rows={len(df)}", flush=True)
        except Exception as exc:
            result["errors"][name] = str(exc)
            print(f"failed {name}: {exc}", flush=True)

    result["ok"] = not bool(result["errors"])
    (DATA_DIR / "dbm_signal_summary.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
