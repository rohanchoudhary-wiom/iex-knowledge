from __future__ import annotations

import csv
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
DATA = ROOT / "data"


def rows(name: str) -> list[dict[str, str]]:
    path = DATA / name
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def esc(value: object) -> str:
    return html.escape(str(value))


def fmt_int(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{int(float(str(value))):,}"
    except Exception:
        return str(value)


def fmt_num(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(str(value))
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def table_html(data: list[dict[str, str]], columns: list[str], limit: int = 12) -> str:
    if not data:
        return '<p class="muted-copy">No rows returned.</p>'
    head = "".join(f"<th>{esc(c.replace('_', ' ').title())}</th>" for c in columns)
    body = []
    for row in data[:limit]:
        body.append("<tr>" + "".join(f"<td>{esc(row.get(c, ''))}</td>" for c in columns) + "</tr>")
    return f"<div class=\"data-table\"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def write_csv(path: Path, data: list[dict[str, str]]) -> None:
    columns: list[str] = []
    for row in data:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data)


def one_row(name: str) -> dict[str, str]:
    data = rows(name)
    return data[0] if data else {}


def sum_col(data: list[dict[str, str]], key: str) -> int:
    total = 0
    for row in data:
        try:
            total += int(float(row.get(key) or 0))
        except Exception:
            pass
    return total


def build_metric(label: str, value: object, caption: str) -> str:
    return f"""
      <div class="metric">
        <span class="pill">{esc(label)}</span>
        <strong>{esc(value)}</strong>
        <span>{esc(caption)}</span>
      </div>
    """


def table_count_lookup() -> dict[tuple[str, str], dict[str, str]]:
    lookup: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows("dbm_relevant_network_table_counts.csv"):
        lookup[(row.get("TABLE_SCHEMA", ""), row.get("TABLE_NAME", ""))] = row
    return lookup


def table_count(schema: str, table: str, key: str) -> str:
    row = table_count_lookup().get((schema, table), {})
    return row.get(key, "")


def build_snowflake_tables_section() -> str:
    lookup = table_count_lookup()

    def meta(schema: str, table: str, role: str, columns: str, outputs: str) -> dict[str, str]:
        info = lookup.get((schema, table), {})
        return {
            "SNOWFLAKE_TABLE": f"PROD_DB.{schema}.{table}",
            "ROLE_IN_DBM_REPORT": role,
            "COLUMNS_USED_OR_CHECKED": columns,
            "ROW_COUNT": fmt_int(info.get("ROW_COUNT")),
            "LAST_ALTERED": info.get("LAST_ALTERED", ""),
            "OUTPUT_FILES": outputs,
        }

    table_rows = [
        meta(
            "DBT",
            "HOURLY_DEVICE_PING_INFLUX",
            "Largest optical telemetry table. Used for daily optical min/avg/max and ping health.",
            "PARTNER_ID, DEVICE_ID, NAS_ID, DATE_IST, HOUR_START_IST, OPTICAL_MIN, OPTICAL_AVG, OPTICAL_MAX, TOTAL_PINGS_RECEIVED, TOTAL_PINGS_MISSED",
            "dbm_hourly_device_ping_optical_daily.csv",
        ),
        meta(
            "PUBLIC",
            "ROUTER_DETAILS_AUDIT",
            "Historical router power/change table. Included so audit depth is visible.",
            "device_id, nas_id, customer_id, partner_id, tx_power, rx_power, is_customer_active, is_plan_active, change_time",
            "dbm_relevant_network_table_counts.csv, dbm_signal_table_inventory.csv",
        ),
        meta(
            "DBT",
            "TASKVANILLA_AUDIT",
            "Installation/task event stream with captured optical power source and value.",
            "EVENT_NAME, ADDED_TIME, DEVICE_ID, DEVICEID, CONNECTIONTYPE, OPTICALPOWER, OPTICAL_POWER_SOURCE, ROUTER_MAC",
            "dbm_taskvanilla_opticalpower_events.csv",
        ),
        meta(
            "DBT",
            "FCT_OPTICAL_SIGNAL",
            "Main daily NAS optical dBm fact used for Good/Poor health and board-range OOR counts.",
            "NAS_ID, DATE_IST, AVG_OPTICAL_DBM, OPTICAL_READINGS, OPTICAL_HEALTH",
            "dbm_optical_signal_latest_health.csv, dbm_optical_signal_daily_trend.csv, dbm_optical_device_worst_sample.csv",
        ),
        meta(
            "DBT",
            "AVG_OPTICAL_SIGNAL",
            "Older/alternate daily optical dBm aggregate discovered in column scan.",
            "NAS_ID, DATE_IST, AVG_OPTICAL_DBM, OPTICAL_READINGS, OPTICAL_HEALTH",
            "dbm_signal_table_inventory.csv, dbm_sample_dbt_avg_optical_signal.csv",
        ),
        meta(
            "DYNAMODB",
            "BOOKING",
            "Booking source with ONT field distribution.",
            "ONT, DEVICE_ID",
            "dbm_booking_ont_distribution.csv",
        ),
        meta(
            "PUBLIC",
            "ROUTER_DETAILS",
            "Current router table with direct Rx/Tx power readings.",
            "device_id, nas_id, customer_id, partner_id, location, ping_in_24hrs, tx_power, rx_power, is_customer_active, is_plan_active, deleted, added_time, modified_time",
            "dbm_router_details_power_profile.csv, dbm_router_details_power_sample.csv",
        ),
        meta(
            "DYNAMODB",
            "IN_P_INCENTIVE_PROCESSING_SCHEDULE",
            "Install/incentive table with optical power measurement profile.",
            "DEVICE_ID, OPTICAL_POWER",
            "dbm_incentive_optical_power_profile.csv",
        ),
        meta(
            "DBT",
            "PARTNER_JANAM_KUNDLI",
            "Partner-level ONT/router rollup.",
            "ACTIVE_CUSTOMER_WITH_OPTICAL_POWER, TOTAL_DEVICES, TOTAL_ONTS, ACTIVE_ONTS, CHURNED_ONTS, PARTNER_OFFICE_ONTS, REPAIR_ONTS, TOTAL_ROUTERS, ACTIVE_ROUTERS",
            "dbm_partner_ont_rollup.csv, dbm_partner_ont_top.csv",
        ),
        meta(
            "DBT",
            "FCT_OPTICAL_OOR_PCT",
            "Daily out-of-range percent fact.",
            "DATE_IST, OOR_DEVICE_COUNT, TOTAL_DEVICES, OOR_PCT",
            "dbm_optical_oor_pct_trend.csv",
        ),
        meta(
            "PUBLIC",
            "GX_ROUTER_HOURLY_DATA",
            "Small router hourly Rx-power source discovered during scan.",
            "_TIME, DEVICE_ID, NAS_ID, PARTNER_ID, RX_POWER_AVG, RX_POWER_MAX, RX_POWER_MIN",
            "dbm_gx_router_rx_power_daily.csv",
        ),
    ]
    write_csv(DATA / "dbm_snowflake_tables_used.csv", table_rows)

    discovery_rows = [
        {
            "DISCOVERY_QUERY": "PROD_DB.INFORMATION_SCHEMA.COLUMNS",
            "WHAT_IT_DID": "Searched PUBLIC, DBT, DYNAMODB, DS_TABLES for DBM, ONT, OLT, PON, OPTICAL, SIGNAL, RX/TX, POWER, ROUTER, NETBOX, MAC, DEVICE, SPLITTER fields.",
            "OUTPUT_FILE": "dbm_signal_column_inventory.csv",
            "ROWS_RETURNED": fmt_int(len(rows("dbm_signal_column_inventory.csv"))),
        },
        {
            "DISCOVERY_QUERY": "PROD_DB.INFORMATION_SCHEMA.TABLES",
            "WHAT_IT_DID": "Pulled row counts and last-altered timestamps for the relevant network tables.",
            "OUTPUT_FILE": "dbm_relevant_network_table_counts.csv",
            "ROWS_RETURNED": fmt_int(len(rows("dbm_relevant_network_table_counts.csv"))),
        },
        {
            "DISCOVERY_QUERY": "OLT-specific INFORMATION_SCHEMA.COLUMNS scan",
            "WHAT_IT_DID": "Regex searched for OLT table or column names. It returned zero rows in the queried schemas.",
            "OUTPUT_FILE": "dbm_olt_column_inventory.csv",
            "ROWS_RETURNED": fmt_int(len(rows("dbm_olt_column_inventory.csv"))),
        },
    ]

    return f"""
    <section class="snowflake-tables-section">
      <h2>Actual Snowflake Tables Queried</h2>
      <p class="live-data-copy">
        These are the exact Snowflake tables behind the DBM/ONT/dBm report. Fully qualified names are shown so the table choice can be judged before trusting the visuals.
      </p>
      <div class="panel">
        {table_html(table_rows, ["SNOWFLAKE_TABLE", "ROLE_IN_DBM_REPORT", "COLUMNS_USED_OR_CHECKED", "ROW_COUNT", "LAST_ALTERED", "OUTPUT_FILES"], 20)}
      </div>
      <div class="panel live-grid">
        <h3>Discovery Queries</h3>
        {table_html(discovery_rows, ["DISCOVERY_QUERY", "WHAT_IT_DID", "OUTPUT_FILE", "ROWS_RETURNED"], 5)}
      </div>
      <p class="live-data-foot">
        The dedicated CSV is <code>dbm_journey/data/dbm_snowflake_tables_used.csv</code>.
      </p>
    </section>
    """


def build_final_inference_section() -> str:
    latest_health = rows("dbm_optical_signal_latest_health.csv")
    daily_trend = rows("dbm_optical_signal_daily_trend.csv")
    router_profile = one_row("dbm_router_details_power_profile.csv")
    latest_date = daily_trend[0].get("DATE_IST", "-") if daily_trend else "-"
    latest_avg = daily_trend[0].get("AVG_DBM", "-") if daily_trend else "-"
    latest_oor = daily_trend[0].get("BOARD_RANGE_OOR_DEVICES", "-") if daily_trend else "-"
    latest_devices = daily_trend[0].get("DEVICE_ROWS", "-") if daily_trend else "-"

    good = next((r for r in latest_health if r.get("OPTICAL_HEALTH") == "Good"), {})
    poor = next((r for r in latest_health if r.get("OPTICAL_HEALTH") == "Poor"), {})

    return f"""
    <section class="final-inference-section">
      <h2>Final Inference: What dBm Customers Are Getting</h2>
      <div class="inference-grid">
        <div class="inference-card primary">
          <span class="pill">Customer-side Rx</span>
          <strong>{esc(fmt_num(latest_avg))} dBm</strong>
          <p>Average latest optical receive power across {esc(fmt_int(latest_devices))} NAS/device rows on {esc(latest_date)} from <code>PROD_DB.DBT.FCT_OPTICAL_SIGNAL</code>.</p>
        </div>
        <div class="inference-card">
          <span class="pill">Healthy bucket</span>
          <strong>{esc(fmt_num(good.get("AVG_DBM")))} dBm</strong>
          <p>{esc(fmt_int(good.get("DEVICE_ROWS")))} rows marked Good. This is inside the whiteboard target window of about -8 to -25 dBm.</p>
        </div>
        <div class="inference-card danger">
          <span class="pill">Poor bucket</span>
          <strong>{esc(fmt_num(poor.get("AVG_DBM")))} dBm</strong>
          <p>{esc(fmt_int(poor.get("DEVICE_ROWS")))} rows marked Poor. These customers are weaker than the board's -25 dBm floor.</p>
        </div>
        <div class="inference-card">
          <span class="pill">Router Rx median</span>
          <strong>{esc(fmt_num(router_profile.get("MEDIAN_RX_POWER_DBM")))} dBm</strong>
          <p>Median valid current router receive power from <code>PROD_DB.PUBLIC.ROUTER_DETAILS</code>; {esc(fmt_int(router_profile.get("BOARD_RANGE_OOR_RX_ROWS")))} valid Rx rows are outside the board range.</p>
        </div>
      </div>
      <div class="final-callout">
        <b>Answer:</b> customers are generally getting around <strong>{esc(fmt_num(latest_avg))} dBm</strong> at the customer-side optical receiver. Healthy customers are around <strong>{esc(fmt_num(good.get("AVG_DBM")))} dBm</strong>. Poor/out-of-range customers are around <strong>{esc(fmt_num(poor.get("AVG_DBM")))} dBm</strong>, which is the group to investigate for weak fiber signal, bad split path, connector/patch loss, or field issues.
      </div>
      <p class="live-data-foot">
        For one exact customer, use <code>NAS_ID</code>, <code>device_id</code>, or <code>customer_id</code> against <code>FCT_OPTICAL_SIGNAL</code> / <code>ROUTER_DETAILS</code>. The current report answers the fleet-level customer dBm.
      </p>
    </section>
    """


def build_live_data_section() -> str:
    latest_health = rows("dbm_optical_signal_latest_health.csv")
    daily_trend = rows("dbm_optical_signal_daily_trend.csv")
    oor_trend = rows("dbm_optical_oor_pct_trend.csv")
    hourly = rows("dbm_hourly_device_ping_optical_daily.csv")
    router_profile = one_row("dbm_router_details_power_profile.csv")
    router_sample = rows("dbm_router_details_power_sample.csv")
    task_events = rows("dbm_taskvanilla_opticalpower_events.csv")
    partner_rollup = one_row("dbm_partner_ont_rollup.csv")
    partner_top = rows("dbm_partner_ont_top.csv")
    booking_ont = rows("dbm_booking_ont_distribution.csv")
    incentive_profile = one_row("dbm_incentive_optical_power_profile.csv")
    table_counts = rows("dbm_relevant_network_table_counts.csv")
    table_inventory = rows("dbm_signal_table_inventory.csv")
    olt_inventory = rows("dbm_olt_column_inventory.csv")
    summary_path = DATA / "dbm_signal_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    latest_date = latest_health[0].get("DATE_IST", "-") if latest_health else "-"
    total_signal_rows = sum_col(latest_health, "DEVICE_ROWS")
    poor_rows = sum_col([r for r in latest_health if r.get("OPTICAL_HEALTH") == "Poor"], "DEVICE_ROWS")
    good_rows = sum_col([r for r in latest_health if r.get("OPTICAL_HEALTH") == "Good"], "DEVICE_ROWS")
    latest_daily = daily_trend[0] if daily_trend else {}
    latest_oor = oor_trend[0] if oor_trend else {}

    metric_cards = "\n".join(
        [
            build_metric(
                "Latest optical dBm",
                fmt_int(total_signal_rows),
                f"NAS/device rows on {latest_date}; Good {fmt_int(good_rows)}, Poor {fmt_int(poor_rows)}.",
            ),
            build_metric(
                "Board OOR count",
                fmt_int(latest_daily.get("BOARD_RANGE_OOR_DEVICES")),
                "Devices outside the whiteboard range of about -8 to -25 dBm.",
            ),
            build_metric(
                "ONT rollup",
                fmt_int(partner_rollup.get("TOTAL_ONTS")),
                f"Active ONTs {fmt_int(partner_rollup.get('ACTIVE_ONTS'))}; partner rows {fmt_int(partner_rollup.get('PARTNER_ROWS'))}.",
            ),
            build_metric(
                "Router Rx sample",
                fmt_int(router_profile.get("VALID_RX_POWER_ROWS")),
                f"Median Rx {fmt_num(router_profile.get('MEDIAN_RX_POWER_DBM'))} dBm; OOR rows {fmt_int(router_profile.get('BOARD_RANGE_OOR_RX_ROWS'))}.",
            ),
        ]
    )

    olt_note = (
        "Exact OLT table or column search returned 0 rows. Snowflake has ONT counts and optical/Rx power measurements, "
        "but not a clear OLT inventory table in the queried schemas."
        if not olt_inventory
        else f"Found {len(olt_inventory)} OLT-like columns."
    )

    generated = summary.get("generated_at", "unknown")

    return f"""
    <section class="live-data-section">
      <h2>Live Snowflake Data Behind The DBM / ONT Story</h2>
      <p class="live-data-copy">
        Queried from Snowflake through the <code>booking_truth</code> connector. This section maps the whiteboard theory to the real tables: ONT counts, optical dBm readings, router Rx/Tx power, TaskVanilla optical-power capture, and OOR trend.
      </p>

      <div class="key-points live-metrics">
        {metric_cards}
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>Latest Optical Health By dBm</h3>
          {table_html(latest_health, ["DATE_IST", "OPTICAL_HEALTH", "DEVICE_ROWS", "NAS_COUNT", "AVG_DBM", "MIN_DBM", "MAX_DBM", "OPTICAL_READINGS"], 8)}
        </div>
        <div class="panel">
          <h3>OOR Percent Trend</h3>
          {table_html(oor_trend, ["DATE_IST", "OOR_DEVICE_COUNT", "TOTAL_DEVICES", "OOR_PCT"], 10)}
        </div>
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>Daily Optical Signal Trend</h3>
          {table_html(daily_trend, ["DATE_IST", "DEVICE_ROWS", "AVG_DBM", "MIN_DBM", "MAX_DBM", "BOARD_RANGE_OOR_DEVICES"], 10)}
        </div>
        <div class="panel">
          <h3>Hourly Device Ping Optical Data</h3>
          {table_html(hourly, ["DATE_IST", "HOURLY_ROWS", "DEVICE_COUNT", "AVG_OPTICAL_AVG_DBM", "MIN_OPTICAL_DBM", "MAX_OPTICAL_DBM", "PINGS_RECEIVED", "PINGS_MISSED"], 8)}
        </div>
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>Router Details Rx / Tx Power Profile</h3>
          {table_html([router_profile] if router_profile else [], ["ROUTER_ROWS", "VALID_RX_POWER_ROWS", "BOARD_RANGE_OOR_RX_ROWS", "AVG_RX_POWER_DBM", "MIN_RX_POWER_DBM", "P10_RX_POWER_DBM", "MEDIAN_RX_POWER_DBM", "P90_RX_POWER_DBM", "MAX_RX_POWER_DBM", "AVG_TX_POWER_DBM"], 1)}
        </div>
        <div class="panel">
          <h3>Router Rx Sample, Worst Active First</h3>
          {table_html(router_sample, ["NAS_ID", "RX_POWER_DBM", "TX_POWER_DBM", "IS_CUSTOMER_ACTIVE", "IS_PLAN_ACTIVE", "MODIFIED_TIME", "BOARD_RANGE_STATUS"], 12)}
        </div>
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>TaskVanilla Optical Power Events</h3>
          {table_html(task_events, ["EVENT_NAME", "OPTICAL_POWER_SOURCE", "EVENT_ROWS", "NUMERIC_OPTICAL_POWER_ROWS", "AVG_OPTICAL_DBM", "MIN_OPTICAL_DBM", "MAX_OPTICAL_DBM", "LATEST_ADDED_TIME"], 10)}
        </div>
        <div class="panel">
          <h3>ONT Counts From Partner Janam Kundli</h3>
          {table_html([partner_rollup] if partner_rollup else [], ["PARTNER_ROWS", "TOTAL_DEVICES", "TOTAL_ONTS", "ACTIVE_ONTS", "CHURNED_ONTS", "PARTNER_OFFICE_ONTS", "REPAIR_ONTS", "TOTAL_ROUTERS", "ACTIVE_ROUTERS", "ACTIVE_CUSTOMERS_WITH_OPTICAL_POWER"], 1)}
        </div>
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>Top Partner ONT Rows</h3>
          {table_html(partner_top, ["TOTAL_DEVICES", "TOTAL_ONTS", "ACTIVE_ONTS", "CHURNED_ONTS", "PARTNER_OFFICE_ONTS", "TOTAL_ROUTERS", "ACTIVE_CUSTOMERS_WITH_OPTICAL_POWER"], 10)}
        </div>
        <div class="panel">
          <h3>Booking ONT Field Distribution</h3>
          {table_html(booking_ont, ["ONT", "BOOKING_ROWS", "DISTINCT_DEVICE_IDS"], 10)}
        </div>
      </div>

      <div class="grid two live-grid">
        <div class="panel">
          <h3>Install/Incentive Optical Power Profile</h3>
          {table_html([incentive_profile] if incentive_profile else [], ["ROWS_WITH_OPTICAL_POWER", "AVG_OPTICAL_POWER", "MIN_OPTICAL_POWER", "P10_OPTICAL_POWER", "MEDIAN_OPTICAL_POWER", "P90_OPTICAL_POWER", "MAX_OPTICAL_POWER"], 1)}
        </div>
        <div class="panel">
          <h3>Where The Data Lives</h3>
          {table_html(table_counts, ["TABLE_SCHEMA", "TABLE_NAME", "ROW_COUNT", "LAST_ALTERED"], 12)}
        </div>
      </div>

      <div class="visual-card live-map">
        <div class="visual-title">
          <h3>Whiteboard Term To Snowflake Reality</h3>
          <p>OLT is the upstream launch concept from the board. The queried warehouse exposes the customer-side measurements more clearly: ONTs, NAS IDs, optical dBm, Rx/Tx power, and OOR status.</p>
        </div>
        <div class="device-map">
          <div class="device"><div class="device-symbol">OLT</div><b>Not found as a table/column</b><p>{esc(olt_note)}</p></div>
          <div class="device"><div class="device-symbol">ONT</div><b>Partner rollup</b><p><code>DBT.PARTNER_JANAM_KUNDLI</code> has total, active, churned, partner-office, repair ONT counts.</p></div>
          <div class="device"><div class="device-symbol">dBm</div><b>Optical signal</b><p><code>DBT.FCT_OPTICAL_SIGNAL</code> has <code>AVG_OPTICAL_DBM</code>, readings, and health.</p></div>
          <div class="device"><div class="device-symbol">Rx</div><b>Router details</b><p><code>PUBLIC.ROUTER_DETAILS</code> has direct <code>rx_power</code> and <code>tx_power</code> readings.</p></div>
        </div>
      </div>

      <div class="panel live-grid">
        <h3>Matched Signal Tables / Columns</h3>
        {table_html(table_inventory, ["TABLE_SCHEMA", "TABLE_NAME", "ROW_COUNT", "VALUE_COLUMNS", "CONTEXT_COLUMNS", "LAST_ALTERED"], 15)}
      </div>

      <p class="live-data-foot">
        Query refresh: {esc(generated)}. Cached files are under <code>dbm_journey/data</code>. No Snowflake credentials are embedded in this HTML.
      </p>
    </section>
    """


def main() -> None:
    base_path = PROJECT_ROOT / "index.html"
    base = base_path.read_text()
    extra_style = """
  <style>
    .snowflake-tables-section,
    .final-inference-section,
    .live-data-section { margin-top: 42px; }
    .live-data-copy,
    .live-data-foot,
    .muted-copy { color: var(--muted); }
    .live-data-copy { max-width: 980px; margin-bottom: 16px; }
    .live-metrics { margin-top: 16px; }
    .live-grid { margin-top: 16px; }
    .data-table { overflow-x: auto; }
    .data-table table { min-width: 760px; }
    .data-table th,
    .data-table td { white-space: nowrap; }
    .live-map { margin-top: 18px; }
    .live-data-foot { margin-top: 16px; font-size: 0.92rem; }
    .inference-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
    .inference-card { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .inference-card.primary { border-color: #9cc3ff; background: #f4f8ff; }
    .inference-card.danger { border-color: #ffb2aa; background: #fff7f6; }
    .inference-card strong { display: block; margin: 8px 0; font-size: 1.8rem; color: #1155b0; }
    .inference-card.danger strong { color: #b42318; }
    .inference-card p { color: var(--muted); }
    .final-callout { margin-top: 14px; border: 1px solid #bfe4cf; background: #edf9f2; border-radius: 8px; padding: 16px; }
    @media (max-width: 900px) {
      .data-table table { min-width: 640px; }
      .data-table th,
      .data-table td { white-space: normal; }
      .inference-grid { grid-template-columns: 1fr; }
    }
  </style>
"""
    live_section = build_live_data_section()
    snowflake_tables_section = build_snowflake_tables_section()
    final_inference_section = build_final_inference_section()
    html_doc = base.replace("</head>", extra_style + "</head>")
    html_doc = html_doc.replace("</header>", "</header>\n" + final_inference_section + "\n" + snowflake_tables_section, 1)
    marker = "    <section>\n      <h2>Operational Takeaways</h2>"
    if marker in html_doc:
        html_doc = html_doc.replace(marker, live_section + "\n" + marker)
    else:
        html_doc = html_doc.replace("</main>", live_section + "\n  </main>")
    html_doc = html_doc.replace(
        "<title>GPON / FTTH Optical Link Budget Notes</title>",
        "<title>DBM Journey - GPON / FTTH Optical Data</title>",
    )
    (ROOT / "index.html").write_text(html_doc)

    readme = f"""# DBM Journey

Standalone GPON/FTTH DBM report built in `/Users/Rohanchoudhary/Desktop/projs/iex_study/dbm_journey`.

## Sources

- Whiteboard study: `/Users/Rohanchoudhary/Desktop/projs/iex_study/index.html`
- Whiteboard photos: `whiteboard_1.jpg`, `whiteboard_2.jpg`
- Snowflake connector: `/Users/Rohanchoudhary/Desktop/projs/booking_truth/data_lib/data_fetch/wiom_data.py`
- Live data cache: `dbm_journey/data/*.csv`

## Main Snowflake outputs

- `dbm_optical_signal_latest_health.csv`
- `dbm_optical_signal_daily_trend.csv`
- `dbm_optical_oor_pct_trend.csv`
- `dbm_router_details_power_profile.csv`
- `dbm_router_details_power_sample.csv`
- `dbm_taskvanilla_opticalpower_events.csv`
- `dbm_partner_ont_rollup.csv`
- `dbm_booking_ont_distribution.csv`
- `dbm_signal_table_inventory.csv`
- `dbm_snowflake_tables_used.csv`

## Snowflake tables queried

- `PROD_DB.DBT.HOURLY_DEVICE_PING_INFLUX`
- `PROD_DB.PUBLIC.ROUTER_DETAILS_AUDIT`
- `PROD_DB.DBT.TASKVANILLA_AUDIT`
- `PROD_DB.DBT.FCT_OPTICAL_SIGNAL`
- `PROD_DB.DBT.AVG_OPTICAL_SIGNAL`
- `PROD_DB.DYNAMODB.BOOKING`
- `PROD_DB.PUBLIC.ROUTER_DETAILS`
- `PROD_DB.DYNAMODB.IN_P_INCENTIVE_PROCESSING_SCHEDULE`
- `PROD_DB.DBT.PARTNER_JANAM_KUNDLI`
- `PROD_DB.DBT.FCT_OPTICAL_OOR_PCT`
- `PROD_DB.PUBLIC.GX_ROUTER_HOURLY_DATA`

## Refresh

Run from `/Users/Rohanchoudhary/Desktop/projs/iex_study`:

```bash
/Users/Rohanchoudhary/Desktop/projs/booking_truth/venv/bin/python -u dbm_journey/scripts/dbm_signal_probe.py
python3 dbm_journey/scripts/build_report.py
```

The report embeds aggregate/sample CSV outputs only. It does not embed Snowflake secrets.
"""
    (ROOT / "README.md").write_text(readme)


if __name__ == "__main__":
    main()
