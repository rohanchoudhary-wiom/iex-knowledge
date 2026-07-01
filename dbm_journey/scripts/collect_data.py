from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IEX_ROOT = ROOT.parent
PROJS_ROOT = IEX_ROOT.parent
CSP_OS_ROOT = PROJS_ROOT / "csp-os-yaml"
BOOKING_TRUTH_ROOT = PROJS_ROOT / "booking_truth"
DATA_DIR = ROOT / "data"


def read_text(path: Path) -> str:
    return path.read_text(errors="replace")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extension_counts() -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for p in CSP_OS_ROOT.rglob("*"):
        if not p.is_file():
            continue
        suffix = p.suffix.lower().lstrip(".") or "[no_ext]"
        counts[suffix] += 1
    return [{"extension": k, "count": v} for k, v in counts.most_common()]


def extract_block(text: str, top_key: str) -> str:
    match = re.search(rf"^{re.escape(top_key)}:\s*$", text, flags=re.M)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^[a-zA-Z_][\w-]*:\s*", text[start:], flags=re.M)
    end = start + next_match.start() if next_match else len(text)
    return text[start:end]


def service_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    yaml_dir = CSP_OS_ROOT / "yaml-prd"
    for p in sorted(yaml_dir.glob("*.yaml")):
        if p.name.startswith("diff-") or "changeset" in p.name or "tests" in p.name:
            continue
        text = read_text(p)
        service = re.search(r"^service_id:\s*['\"]?([^'\"\n#]+)", text, flags=re.M)
        deps_block = extract_block(text, "dependencies")
        events_block = extract_block(text, "events")
        flows_block = extract_block(text, "flows")
        data_model_block = extract_block(text, "data_model")
        entities = []
        for m in re.finditer(r"^\s{2,}-\s+(?:name|entity|table):\s*['\"]?([^'\"\n#]+)", data_model_block, flags=re.M):
            entities.append(m.group(1).strip())
        for m in re.finditer(r"^\s{4,}(?:name|entity|table):\s*['\"]?([^'\"\n#]+)", data_model_block, flags=re.M):
            value = m.group(1).strip()
            if value not in entities:
                entities.append(value)
        event_tokens = sorted(set(re.findall(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){1,}\b", events_block or text)))
        endpoints = sorted(set(re.findall(r"/(?:api|internal|public|events|quality|connection|csp|v1)[A-Za-z0-9_/{}/?.=&:-]*", text)))
        rows.append(
            {
                "file": p.name,
                "service_id": service.group(1).strip() if service else "",
                "dependency_count": len(re.findall(r"^\s*-\s+service:", deps_block, flags=re.M)),
                "event_count": len(event_tokens),
                "endpoint_count": len(endpoints),
                "flow_count": len(re.findall(r"^\s{2,}[a-zA-Z0-9_-]+:", flows_block, flags=re.M)),
                "entity_count": len(entities),
                "entities": ", ".join(entities[:14]),
            }
        )
    return rows


def seed_table_inventory() -> list[dict[str, Any]]:
    roots = [
        CSP_OS_ROOT / "docs" / "qa-seed-data",
        CSP_OS_ROOT / "docs" / "migrations",
        CSP_OS_ROOT / "migration_scripts" / "wallet",
    ]
    counts: Counter[tuple[str, str]] = Counter()
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.sql"):
            rel = str(p.relative_to(CSP_OS_ROOT))
            text = read_text(p)
            for table in re.findall(r"INSERT\s+INTO\s+([a-zA-Z0-9_.]+)", text, flags=re.I):
                counts[(rel, table)] += 1
    return [
        {"source_file": f, "table_name": t, "insert_blocks": c}
        for (f, t), c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    ]


def postman_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in list((CSP_OS_ROOT / "app-specs").glob("*.postman_collection.json")) + list((CSP_OS_ROOT / "postman").glob("**/*.postman_collection.json")):
        try:
            payload = json.loads(read_text(p))
        except Exception as exc:
            rows.append({"file": str(p.relative_to(CSP_OS_ROOT)), "requests": 0, "error": str(exc)})
            continue

        def count_items(items: list[dict[str, Any]]) -> int:
            total = 0
            for item in items:
                if "request" in item:
                    total += 1
                total += count_items(item.get("item", []))
            return total

        rows.append({"file": str(p.relative_to(CSP_OS_ROOT)), "requests": count_items(payload.get("item", [])), "error": ""})
    return sorted(rows, key=lambda r: r["file"])


def run_snowflake_queries() -> dict[str, Any]:
    sys.path.insert(0, str(BOOKING_TRUTH_ROOT))
    from data_lib.data_fetch.wiom_data import WiomData

    db = WiomData("snowflake")
    queries = {
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
        "taskvanilla_event_counts": """
            SELECT
              event_name,
              COUNT(*) AS rows,
              COUNT(DISTINCT mobile) AS distinct_mobiles,
              COUNT(DISTINCT COALESCE(NULLIF(TRIM(account_id), ''), NULLIF(TRIM(partner_id), ''))) AS distinct_partners,
              MAX(added_time) AS latest_added_time
            FROM prod_db.dbt.taskvanilla_audit
            WHERE added_time >= '2025-06-01'
              AND event_name IN ('INTERESTED', 'LATE_INTERESTED', 'DECLINED')
            GROUP BY 1
            ORDER BY rows DESC
        """,
        "core_source_counts": """
            SELECT 'taskvanilla_audit_since_2025_06_01' AS metric, COUNT(*) AS value FROM prod_db.dbt.taskvanilla_audit WHERE added_time >= '2025-06-01'
            UNION ALL
            SELECT 'taskvanilla_booking_events_since_2025_06_01', COUNT(*) FROM prod_db.dbt.taskvanilla_audit WHERE added_time >= '2025-06-01' AND event_name IN ('INTERESTED', 'LATE_INTERESTED', 'DECLINED')
            UNION ALL
            SELECT 'public_t_account_rows', COUNT(*) FROM prod_db.public.t_account
            UNION ALL
            SELECT 'public_t_address_with_lat_lng', COUNT(*) FROM prod_db.public.t_address WHERE lat IS NOT NULL AND lng IS NOT NULL
            UNION ALL
            SELECT 'home_router_active_plan_accounts', COUNT(DISTINCT account_id) FROM prod_db.dynamodb.home_router_plan_info WHERE charges IS NOT NULL AND charges > 19 AND otp = 'DONE'
        """,
        "partner_account_health": """
            SELECT
              CASE
                WHEN logical_group ILIKE '%lead_allocation_blocked%' OR logical_group ILIKE '%delisted%' THEN 'Inactive'
                ELSE 'Active'
              END AS active_state,
              COUNT(*) AS accounts
            FROM prod_db.public.t_account
            WHERE id IS NOT NULL
            GROUP BY 1
            ORDER BY accounts DESC
        """,
    }
    out: dict[str, Any] = {"ok": True, "queries": {}, "errors": {}}
    for name, sql in queries.items():
        try:
            df = db.query(sql)
            csv_path = DATA_DIR / f"snowflake_{name}.csv"
            df.to_csv(csv_path, index=False)
            out["queries"][name] = {
                "rows": int(len(df)),
                "columns": list(map(str, df.columns)),
                "csv": str(csv_path.relative_to(ROOT)),
            }
        except Exception as exc:
            out["errors"][name] = str(exc)
    return out


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    services = service_inventory()
    seed_tables = seed_table_inventory()
    postman = postman_inventory()
    extensions = extension_counts()

    write_csv(
        DATA_DIR / "service_inventory.csv",
        services,
        ["file", "service_id", "dependency_count", "event_count", "endpoint_count", "flow_count", "entity_count", "entities"],
    )
    write_csv(DATA_DIR / "seed_table_inventory.csv", seed_tables, ["source_file", "table_name", "insert_blocks"])
    write_csv(DATA_DIR / "postman_inventory.csv", postman, ["file", "requests", "error"])
    write_csv(DATA_DIR / "extension_counts.csv", extensions, ["extension", "count"])

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_repos": {
            "csp_os_yaml": str(CSP_OS_ROOT),
            "booking_truth": str(BOOKING_TRUTH_ROOT),
            "output": str(ROOT),
        },
        "local": {
            "service_prd_files": len(services),
            "services_with_ids": sum(1 for r in services if r["service_id"]),
            "total_dependencies_declared": sum(int(r["dependency_count"]) for r in services),
            "total_event_tokens": sum(int(r["event_count"]) for r in services),
            "total_endpoint_tokens": sum(int(r["endpoint_count"]) for r in services),
            "seed_table_entries": len(seed_tables),
            "postman_collections": len(postman),
            "postman_requests": sum(int(r["requests"]) for r in postman),
            "top_extensions": extensions[:12],
        },
        "top_services_by_events": sorted(services, key=lambda r: int(r["event_count"]), reverse=True)[:12],
        "top_seed_tables": seed_tables[:16],
    }

    try:
        summary["snowflake"] = run_snowflake_queries()
    except Exception as exc:
        summary["snowflake"] = {"ok": False, "error": str(exc)}

    write_json(DATA_DIR / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str)[:6000])


if __name__ == "__main__":
    main()
