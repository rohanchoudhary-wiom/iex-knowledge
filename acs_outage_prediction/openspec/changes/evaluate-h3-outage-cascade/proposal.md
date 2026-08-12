## Why

The full-fleet H3 map established enough device density for a bounded cascade test. Evaluate whether a first same-cell episode with at least 10% of devices in an early ping-miss state predicts at least 70% of the same frozen device set entering a strict 60-minute telemetry outage within the next hour, and test whether optical summaries add chronological development-period value.

## What Changes

- Build one retrospective trigger episode per H3 resolution-9 cell with at least 20 time-valid devices.
- Fit an L2 logistic model with 10 ping, spatial, density, and time features plus 5 optical features.
- Compare it with alerting on every trigger, training climatology, and the same model without optical features.
- Use chronological train, validation, and test partitions with one-hour boundary purges and fixed support and performance gates.
- Export only aggregate evidence and standardized coefficients; keep device, location, H3/time trigger rows, and identity mappings inside Snowflake or process memory.
- Record `CASCADE_MODEL_NOT_SUPPORTED` whenever support, chronological test-period performance, or production-latency gates fail.

## Capabilities

### New Capabilities

- `h3-outage-cascade-evaluation`: Privacy-safe retrospective evaluation of a 10%-to-70% same-cell ping-outage cascade and incremental optical contribution.

### Modified Capabilities

None.

## Non-goals

- Production alerting or deployment.
- Coverage claims for cascades that never cross the 10% trigger.
- Confirmed customer-service-outage, causal, or root-cause claims.
- Treating adjacent-cell state as part of the target rather than as predictors.

## Impact

- Reads the hourly ping/optical source and audited device-to-customer mapping.
- Uses session-scoped temporary Snowflake tables only; it creates no persistent warehouse state.
- Supersedes the first draft cascade bundle after its trigger and zero-ping validation mismatches were corrected.
