## Why

The ping-outage development model used a 5% sample of 3,372 devices, which is too sparse for approximately 200 m spatial analysis. Before choosing cross-device clustering thresholds or fitting another model, map the full eligible ping population and its strict 60-minute telemetry-silence events to H3 resolution 9.

## What Changes

- Read the full eligible population from `PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX` without device hash sampling.
- Define one telemetry-outage event after twelve consecutive missed five-minute opportunities, ending at the first later successful ping or remaining right-censored.
- Reuse the clean, time-valid `CUSTOMER_V2` cohort and exact one-to-one `PUBLIC.T_DEVICE` bridge.
- Assign H3 resolution-9 cells in Snowflake and export density-normalized aggregate coverage and outage measures only.
- Suppress H3 detail below five eligible devices and keep exact coordinates and identity mappings inside Snowflake.
- Defer cross-device incident clustering, prediction, alert thresholds, and customer-service-outage claims.

## Capabilities

### New Capabilities

- `full-fleet-ping-outage-h3-map`: Full-population, privacy-safe H3 resolution-9 mapping of strict ping-defined telemetry outages.

### Modified Capabilities

None.

## Impact

- Reads `PUBLIC.HOURLY_DEVICE_PING_INFLUX`, `PUBLIC.T_DEVICE`, and `DBT.ACTIVE_BASE` without warehouse writes.
- Adds one aggregate runner and one audit/report bundle.
- Does not change the completed formal-incident spatial pilot or the 15-feature development model.
