from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJS_ROOT = ROOT.parent.parent
BOOKING_TRUTH_ROOT = PROJS_ROOT / "booking_truth"
DATA_DIR = ROOT / "data"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(BOOKING_TRUTH_ROOT))
    from data_lib.data_fetch.wiom_data import WiomData

    print("initializing WiomData", flush=True)
    db = WiomData("snowflake")
    # Keep this report builder responsive if Snowflake auth/network is slow.
    db._connection_params["login_timeout"] = 20
    db._connection_params["network_timeout"] = 30
    db._connection_params["ocsp_response_cache_filename"] = str(DATA_DIR / "snowflake_ocsp_cache.json")

    queries = {
        "connection_smoke": "SELECT CURRENT_TIMESTAMP() AS ts, CURRENT_DATABASE() AS database_name, CURRENT_SCHEMA() AS schema_name",
        "core_source_counts": """
            SELECT 'taskvanilla_audit_since_2025_06_01' AS metric, COUNT(*) AS value
            FROM prod_db.dbt.taskvanilla_audit
            WHERE added_time >= '2025-06-01'
            UNION ALL
            SELECT 'taskvanilla_booking_events_since_2025_06_01', COUNT(*)
            FROM prod_db.dbt.taskvanilla_audit
            WHERE added_time >= '2025-06-01'
              AND event_name IN ('INTERESTED', 'LATE_INTERESTED', 'DECLINED')
            UNION ALL
            SELECT 'public_t_account_rows', COUNT(*) FROM prod_db.public.t_account
            UNION ALL
            SELECT 'public_t_address_with_lat_lng', COUNT(*)
            FROM prod_db.public.t_address
            WHERE lat IS NOT NULL AND lng IS NOT NULL
            UNION ALL
            SELECT 'home_router_active_plan_accounts', COUNT(DISTINCT account_id)
            FROM prod_db.dynamodb.home_router_plan_info
            WHERE charges IS NOT NULL AND charges > 19 AND otp = 'DONE'
        """,
        "taskvanilla_event_counts": """
            SELECT
              event_name,
              COUNT(*) AS event_rows,
              COUNT(DISTINCT mobile) AS distinct_mobiles,
              COUNT(DISTINCT COALESCE(NULLIF(TRIM(account_id), ''), NULLIF(TRIM(partner_id), ''))) AS distinct_partners,
              MAX(added_time) AS latest_added_time
            FROM prod_db.dbt.taskvanilla_audit
            WHERE added_time >= '2025-06-01'
              AND event_name IN ('INTERESTED', 'LATE_INTERESTED', 'DECLINED')
            GROUP BY 1
            ORDER BY event_rows DESC
        """,
        "source_table_metadata": """
            SELECT table_schema, table_name, row_count, bytes, created, last_altered
            FROM prod_db.information_schema.tables
            WHERE table_schema IN ('PUBLIC', 'DBT', 'DYNAMODB', 'DS_TABLES')
              AND (
                LOWER(table_name) LIKE '%taskvanilla%'
                OR LOWER(table_name) LIKE '%account%'
                OR LOWER(table_name) LIKE '%partner%'
                OR LOWER(table_name) LIKE '%connection%'
                OR LOWER(table_name) LIKE '%customer%'
                OR LOWER(table_name) LIKE '%address%'
                OR LOWER(table_name) LIKE '%netbox%'
                OR LOWER(table_name) LIKE '%plan%'
                OR LOWER(table_name) LIKE '%router%'
              )
            ORDER BY COALESCE(row_count, 0) DESC, table_schema, table_name
            LIMIT 120
        """,
    }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "queries": {},
        "errors": {},
    }
    for name, sql in queries.items():
        try:
            print(f"running {name}", flush=True)
            df = db.query(sql)
            out = DATA_DIR / f"snowflake_{name}.csv"
            df.to_csv(out, index=False)
            result["queries"][name] = {
                "rows": int(len(df)),
                "columns": list(map(str, df.columns)),
                "csv": str(out.relative_to(ROOT)),
            }
            print(f"wrote {out} rows={len(df)}", flush=True)
        except Exception as exc:
            result["ok"] = False
            result["errors"][name] = str(exc)
            print(f"failed {name}: {exc}", flush=True)

    (DATA_DIR / "snowflake_summary.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
