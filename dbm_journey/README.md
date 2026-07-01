# DBM Journey

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
