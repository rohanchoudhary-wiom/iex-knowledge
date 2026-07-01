from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PROJS_ROOT = ROOT.parent.parent
BOOKING_TRUTH_ROOT = PROJS_ROOT / "booking_truth"
DEFAULT_ACTIVE_BASE = BOOKING_TRUTH_ROOT / "active_base_reference" / "active_base_customer_base.csv"
DEFAULT_OUTPUT_DIR = BOOKING_TRUTH_ROOT / "step_outputs" / "dbm_tenure"


def import_wiom_data():
    sys.path.insert(0, str(BOOKING_TRUTH_ROOT))
    from data_lib.data_fetch.wiom_data import WiomData

    return WiomData


def clean_id(series: pd.Series) -> pd.Series:
    return (
        series.astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    )


def status_from_dbm(value: object) -> str:
    if pd.isna(value):
        return "missing"
    dbm = float(value)
    if dbm < -25:
        return "weak_signal"
    if dbm > -8:
        return "too_strong_or_invalid"
    return "good"


def tenure_bucket(days: object) -> str:
    if pd.isna(days):
        return "missing"
    days_i = int(days)
    if days_i < 0:
        return "negative"
    if days_i <= 30:
        return "000-030"
    if days_i <= 60:
        return "031-060"
    if days_i <= 90:
        return "061-090"
    if days_i <= 180:
        return "091-180"
    if days_i <= 360:
        return "181-360"
    if days_i <= 720:
        return "361-720"
    return "720_plus"


def expiry_bucket(days: object) -> str:
    if pd.isna(days):
        return "missing"
    days_i = int(days)
    if days_i >= 0:
        return "active_future_expiry"
    overdue = abs(days_i)
    if overdue <= 30:
        return "expired_000_030"
    if overdue <= 90:
        return "expired_031_090"
    if overdue <= 180:
        return "expired_091_180"
    if overdue <= 360:
        return "expired_181_360"
    return "expired_360_plus"


def load_active_base(path: Path) -> pd.DataFrame:
    dtype = {
        "mobile": "string",
        "account_id": "string",
        "long_customer_account_id": "string",
        "long_lco_id": "string",
        "nasid": "string",
        "lco_name": "string",
        "zone": "string",
        "wg_status": "string",
        "active_state": "string",
    }
    df = pd.read_csv(path, dtype=dtype)
    df = df.rename(
        columns={
            "long_lco_id": "partner_id",
            "nasid": "nas_id",
        }
    )
    for col in ("mobile", "account_id", "long_customer_account_id", "partner_id", "nas_id"):
        if col in df.columns:
            df[col] = clean_id(df[col])
    for col in ("location_start_time", "plan_expiry_time", "partner_added_time", "ingested_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ("latitude", "longitude"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    valid_mobile = df["mobile"].str.fullmatch(r"[6-9][0-9]{9}").fillna(False)
    df = df.loc[
        valid_mobile
        & df["partner_id"].notna()
        & df["nas_id"].notna()
        & df["location_start_time"].notna()
    ].copy()
    return df


def fetch_latest_optical_signal(db) -> pd.DataFrame:
    sql = """
        WITH latest AS (
          SELECT MAX(date_ist) AS date_ist
          FROM prod_db.dbt.fct_optical_signal
        )
        SELECT
          CAST(f.nas_id AS VARCHAR) AS nas_id,
          f.date_ist AS optical_date,
          f.avg_optical_dbm,
          f.optical_readings,
          f.optical_health
        FROM prod_db.dbt.fct_optical_signal f
        JOIN latest l ON f.date_ist = l.date_ist
        WHERE f.nas_id IS NOT NULL
    """
    df = db.query(sql)
    df.columns = [str(c).lower() for c in df.columns]
    df["nas_id"] = clean_id(df["nas_id"])
    df["optical_date"] = pd.to_datetime(df["optical_date"], errors="coerce")
    df["avg_optical_dbm"] = pd.to_numeric(df["avg_optical_dbm"], errors="coerce")
    df["optical_readings"] = pd.to_numeric(df["optical_readings"], errors="coerce")
    return df


def fetch_router_power(db) -> pd.DataFrame:
    sql = """
        SELECT
          CAST("nas_id" AS VARCHAR) AS nas_id,
          "rx_power" AS router_rx_power_dbm,
          "tx_power" AS router_tx_power_dbm,
          "modified_time" AS router_power_modified_time
        FROM prod_db.public.router_details
        WHERE ("deleted" = 0 OR "deleted" IS NULL)
          AND "nas_id" IS NOT NULL
          AND "rx_power" BETWEEN -60 AND 10
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY CAST("nas_id" AS VARCHAR)
          ORDER BY "modified_time" DESC NULLS LAST
        ) = 1
    """
    df = db.query(sql)
    df.columns = [str(c).lower() for c in df.columns]
    df["nas_id"] = clean_id(df["nas_id"])
    df["router_rx_power_dbm"] = pd.to_numeric(df["router_rx_power_dbm"], errors="coerce")
    df["router_tx_power_dbm"] = pd.to_numeric(df["router_tx_power_dbm"], errors="coerce")
    df["router_power_modified_time"] = pd.to_datetime(
        df["router_power_modified_time"], errors="coerce"
    )
    return df


def build_customer_dataset(active: pd.DataFrame, optical: pd.DataFrame, router: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "source",
        "account_id",
        "long_customer_account_id",
        "mobile",
        "partner_id",
        "nas_id",
        "lco_name",
        "zone",
        "wg_status",
        "active_state",
        "plan_expiry_time",
        "location_start_time",
        "latitude",
        "longitude",
    ]
    data = active.loc[:, [c for c in cols if c in active.columns]].copy()
    data = data.merge(optical, on="nas_id", how="left")
    data = data.merge(router, on="nas_id", how="left")

    data["customer_received_dbm"] = data["avg_optical_dbm"].combine_first(
        data["router_rx_power_dbm"]
    )
    data["dbm_source"] = "missing"
    data.loc[data["avg_optical_dbm"].notna(), "dbm_source"] = "fct_optical_signal"
    data.loc[
        data["avg_optical_dbm"].isna() & data["router_rx_power_dbm"].notna(),
        "dbm_source",
    ] = "router_details"

    data["dbm_measurement_time"] = data["optical_date"].combine_first(
        data["router_power_modified_time"]
    )
    analysis_as_of_time = data["optical_date"].dropna().max()
    if pd.isna(analysis_as_of_time):
        analysis_as_of_time = data["router_power_modified_time"].dropna().max()
    data["analysis_as_of_time"] = analysis_as_of_time
    data["tenure_days"] = (
        data["analysis_as_of_time"] - data["location_start_time"]
    ).dt.days
    data["plan_expiry_days_from_as_of"] = (
        data["plan_expiry_time"] - data["analysis_as_of_time"]
    ).dt.days
    data["plan_expiry_days_from_measurement"] = data["plan_expiry_days_from_as_of"]
    data["tenure_bucket"] = data["tenure_days"].map(tenure_bucket)
    data["expiry_bucket"] = data["plan_expiry_days_from_as_of"].map(expiry_bucket)
    data["dbm_status"] = data["customer_received_dbm"].map(status_from_dbm)
    data["is_dbm_oor"] = data["dbm_status"].isin(["weak_signal", "too_strong_or_invalid"])
    data["dbm_gap_to_floor"] = data["customer_received_dbm"] + 25
    return data


def add_percentages(summary: pd.DataFrame) -> pd.DataFrame:
    summary = summary.copy()
    rows = pd.to_numeric(summary["rows"], errors="coerce").astype(float).replace(0, float("nan"))
    rows_with_dbm = (
        pd.to_numeric(summary["rows_with_dbm"], errors="coerce")
        .astype(float)
        .replace(0, float("nan"))
    )
    summary["dbm_coverage_pct"] = (
        pd.to_numeric(summary["rows_with_dbm"], errors="coerce").astype(float) / rows * 100
    ).round(2)
    summary["weak_signal_pct"] = (
        pd.to_numeric(summary["weak_signal_rows"], errors="coerce").astype(float)
        / rows_with_dbm
        * 100
    ).round(2)
    summary["oor_pct"] = (
        pd.to_numeric(summary["oor_rows"], errors="coerce").astype(float)
        / rows_with_dbm
        * 100
    ).round(2)
    return summary


def summarize_by(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    with_dbm = df.loc[df["customer_received_dbm"].notna()].copy()
    grouped = df.groupby(group_cols, dropna=False)
    rows = grouped.size().rename("rows")
    rows_with_dbm = grouped["customer_received_dbm"].count().rename("rows_with_dbm")
    weak = grouped["dbm_status"].apply(lambda s: int((s == "weak_signal").sum())).rename(
        "weak_signal_rows"
    )
    oor = grouped["is_dbm_oor"].sum().astype(int).rename("oor_rows")

    stats = with_dbm.groupby(group_cols, dropna=False)["customer_received_dbm"].agg(
        avg_dbm="mean",
        median_dbm="median",
        p10_dbm=lambda s: s.quantile(0.10),
        p25_dbm=lambda s: s.quantile(0.25),
        p75_dbm=lambda s: s.quantile(0.75),
        p90_dbm=lambda s: s.quantile(0.90),
    )
    out = pd.concat([rows, rows_with_dbm, weak, oor, stats], axis=1).reset_index()
    numeric = ["avg_dbm", "median_dbm", "p10_dbm", "p25_dbm", "p75_dbm", "p90_dbm"]
    for col in numeric:
        if col in out.columns:
            out[col] = out[col].round(2)
    return add_percentages(out)


def pearson_corr(left: pd.Series, right: pd.Series):
    paired = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(paired) < 2 or paired["left"].nunique() < 2 or paired["right"].nunique() < 2:
        return pd.NA
    return paired["left"].corr(paired["right"], method="pearson")


def spearman_corr(left: pd.Series, right: pd.Series):
    ranked = pd.DataFrame({"left": left, "right": right}).dropna()
    return pearson_corr(ranked["left"].rank(), ranked["right"].rank())


def correlation_summary(df: pd.DataFrame) -> pd.DataFrame:
    valid = df.loc[
        df["customer_received_dbm"].notna() & df["tenure_days"].notna()
    ].copy()
    rows = []
    for label, frame in [
        ("all_sources", valid),
        ("fct_optical_signal_only", valid.loc[valid["dbm_source"].eq("fct_optical_signal")]),
        ("router_details_only", valid.loc[valid["dbm_source"].eq("router_details")]),
    ]:
        if len(frame) >= 2:
            pearson = pearson_corr(frame["tenure_days"], frame["customer_received_dbm"])
            spearman = spearman_corr(frame["tenure_days"], frame["customer_received_dbm"])
        else:
            pearson = pd.NA
            spearman = pd.NA
        rows.append(
            {
                "population": label,
                "rows": len(frame),
                "avg_tenure_days": round(frame["tenure_days"].mean(), 2) if len(frame) else pd.NA,
                "avg_dbm": round(frame["customer_received_dbm"].mean(), 2) if len(frame) else pd.NA,
                "median_dbm": round(frame["customer_received_dbm"].median(), 2) if len(frame) else pd.NA,
                "pearson_tenure_vs_dbm": round(float(pearson), 4) if pd.notna(pearson) else pd.NA,
                "spearman_tenure_vs_dbm": round(float(spearman), 4) if pd.notna(spearman) else pd.NA,
            }
        )
    return pd.DataFrame(rows)


def partner_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = summarize_by(df, ["partner_id", "lco_name", "zone"])
    valid = df.loc[
        df["customer_received_dbm"].notna() & df["tenure_days"].notna()
    ].copy()
    corrs = []
    for keys, frame in valid.groupby(["partner_id", "lco_name", "zone"], dropna=False):
        if len(frame) < 30:
            corr = pd.NA
        else:
            corr = spearman_corr(frame["tenure_days"], frame["customer_received_dbm"])
        corrs.append(
            {
                "partner_id": keys[0],
                "lco_name": keys[1],
                "zone": keys[2],
                "spearman_tenure_vs_dbm": round(float(corr), 4) if pd.notna(corr) else pd.NA,
            }
        )
    corr_df = pd.DataFrame(corrs)
    out = out.merge(corr_df, on=["partner_id", "lco_name", "zone"], how="left")
    return out.sort_values(
        ["weak_signal_pct", "rows_with_dbm"], ascending=[False, False], na_position="last"
    )


def write_findings(output_dir: Path, df: pd.DataFrame, corr: pd.DataFrame, bucket: pd.DataFrame) -> None:
    total = len(df)
    nas = int(df["nas_id"].notna().sum())
    dbm_rows = int(df["customer_received_dbm"].notna().sum())
    weak = int((df["dbm_status"] == "weak_signal").sum())
    good = int((df["dbm_status"] == "good").sum())
    coverage = round(dbm_rows / total * 100, 2) if total else 0
    weak_pct = round(weak / dbm_rows * 100, 2) if dbm_rows else 0
    all_corr = corr.loc[corr["population"].eq("all_sources")].iloc[0].to_dict()

    bucket_lines = []
    for _, row in bucket.iterrows():
        bucket_lines.append(
            f"| {row['tenure_bucket']} | {int(row['rows_with_dbm'])} | "
            f"{row['median_dbm']} | {row['avg_dbm']} | {row['weak_signal_pct']}% |"
        )

    text = f"""# Active Base DBM Tenure Findings

Generated at: {datetime.now(timezone.utc).isoformat()}

## Population

- Active-base rows: {total:,}
- Rows with NAS ID: {nas:,}
- Rows with customer received dBm: {dbm_rows:,}
- DBM coverage: {coverage}%
- Good rows: {good:,}
- Weak signal rows below -25 dBm: {weak:,} ({weak_pct}% of rows with DBM)

## Correlation

- Pearson tenure_days vs customer_received_dbm: {all_corr.get('pearson_tenure_vs_dbm')}
- Spearman tenure_days vs customer_received_dbm: {all_corr.get('spearman_tenure_vs_dbm')}

Interpretation rule: a negative correlation means older-tenure customers tend to have more negative/weaker dBm. A near-zero correlation means tenure alone is not explaining DBM.

## Tenure Buckets

| Tenure bucket | Rows with DBM | Median dBm | Avg dBm | Weak signal % |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(bucket_lines)}

## Recommended Read

If the overall correlation is weak but some partners/zones have high weak-signal percentage, the problem is likely partner/area/network design rather than customer age itself.
"""
    (output_dir / "active_base_dbm_tenure_findings.md").write_text(text)


def main() -> None:
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    active = load_active_base(DEFAULT_ACTIVE_BASE)
    WiomData = import_wiom_data()
    db = WiomData("snowflake")
    db._connection_params["login_timeout"] = 20
    db._connection_params["network_timeout"] = 60
    db._connection_params["ocsp_response_cache_filename"] = str(output_dir / "snowflake_ocsp_cache.json")

    optical = fetch_latest_optical_signal(db)
    router = fetch_router_power(db)
    customer = build_customer_dataset(active, optical, router)

    row_cols = [
        "mobile",
        "account_id",
        "long_customer_account_id",
        "partner_id",
        "nas_id",
        "lco_name",
        "zone",
        "wg_status",
        "active_state",
        "location_start_time",
        "plan_expiry_time",
        "analysis_as_of_time",
        "tenure_days",
        "tenure_bucket",
        "plan_expiry_days_from_as_of",
        "plan_expiry_days_from_measurement",
        "expiry_bucket",
        "customer_received_dbm",
        "dbm_status",
        "is_dbm_oor",
        "dbm_gap_to_floor",
        "dbm_source",
        "optical_date",
        "avg_optical_dbm",
        "optical_health",
        "optical_readings",
        "router_rx_power_dbm",
        "router_tx_power_dbm",
        "router_power_modified_time",
        "latitude",
        "longitude",
    ]
    customer.loc[:, row_cols].to_csv(
        output_dir / "active_base_dbm_tenure_customer.csv", index=False
    )

    corr = correlation_summary(customer)
    bucket = summarize_by(customer, ["tenure_bucket"]).sort_values("tenure_bucket")
    expiry = summarize_by(customer, ["expiry_bucket"]).sort_values("expiry_bucket")
    partner = partner_summary(customer)
    zone = summarize_by(customer, ["zone"]).sort_values(
        ["weak_signal_pct", "rows_with_dbm"], ascending=[False, False], na_position="last"
    )

    corr.to_csv(output_dir / "active_base_dbm_tenure_correlation_summary.csv", index=False)
    bucket.to_csv(output_dir / "active_base_dbm_tenure_bucket_summary.csv", index=False)
    expiry.to_csv(output_dir / "active_base_dbm_expiry_bucket_summary.csv", index=False)
    partner.to_csv(output_dir / "active_base_dbm_tenure_partner_summary.csv", index=False)
    zone.to_csv(output_dir / "active_base_dbm_tenure_zone_summary.csv", index=False)

    weak_sample = customer.loc[customer["dbm_status"].eq("weak_signal"), row_cols].sort_values(
        "customer_received_dbm"
    )
    good_sample = customer.loc[customer["dbm_status"].eq("good"), row_cols].sort_values(
        "customer_received_dbm"
    )
    weak_sample.head(100).to_csv(output_dir / "active_base_dbm_weak_sample.csv", index=False)
    good_sample.head(100).to_csv(output_dir / "active_base_dbm_good_sample.csv", index=False)
    write_findings(output_dir, customer, corr, bucket)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_base_input": str(DEFAULT_ACTIVE_BASE),
        "output_dir": str(output_dir),
        "active_base_rows": int(len(active)),
        "latest_optical_rows": int(len(optical)),
        "router_power_rows": int(len(router)),
        "analysis_as_of_time": (
            customer["analysis_as_of_time"].dropna().iloc[0].isoformat()
            if customer["analysis_as_of_time"].notna().any()
            else None
        ),
        "customer_rows": int(len(customer)),
        "rows_with_nas_id": int(customer["nas_id"].notna().sum()),
        "rows_with_dbm": int(customer["customer_received_dbm"].notna().sum()),
        "outputs": [
            "active_base_dbm_tenure_customer.csv",
            "active_base_dbm_tenure_correlation_summary.csv",
            "active_base_dbm_tenure_bucket_summary.csv",
            "active_base_dbm_expiry_bucket_summary.csv",
            "active_base_dbm_tenure_partner_summary.csv",
            "active_base_dbm_tenure_zone_summary.csv",
            "active_base_dbm_weak_sample.csv",
            "active_base_dbm_good_sample.csv",
            "active_base_dbm_tenure_findings.md",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
