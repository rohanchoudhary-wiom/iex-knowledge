## Why

Raw outage counts overstate impact in dense areas and provide no customer denominator. `CUSTOMER_V2` has a sufficiently clean, geocoded active-customer base, and a seven-day audit found a deterministic one-to-one path covering 63,431 outage devices, so a bounded spatial-impact pilot is now feasible.

## What Changes

- Use only `PROD_DB.DBT.ACTIVE_BASE` rows where `SOURCE = 'CUSTOMER_V2'`; do not use `T_STORE` or `T_WG_CUSTOMER` customers.
- Collapse consistent duplicates by account and exclude accounts with conflicting NASIDs, coordinates, or state.
- Use active customers with valid India coordinates as the network-footprint denominator and require an unexpired plan at each hour for the primary customer-impact denominator.
- Map outage `DEVICE_ID` through a one-to-one `T_DEVICE` bridge to `CUSTOMER_V2.NASID`; stop rather than use ambiguous mappings.
- Aggregate eligible and affected customers into approximately 1 km geographic cells and hourly windows.
- Report affected customers, eligible customers, outage rate, duration, density, and neighbouring-cell concentration without exposing customer identifiers or exact coordinates.
- Keep ACS parameters and predictive modelling deferred until this geographic pilot demonstrates useful, stable outage localization.
- Non-goals: production alerting, root-cause attribution, causal claims, fuzzy identity matching, individual-customer maps, and using names or mobile numbers.

## Capabilities

### New Capabilities

- `customer-outage-spatial-pilot`: A privacy-safe, `CUSTOMER_V2`-only customer denominator, audited outage mapping, and density-normalized cell-hour outage analysis.

### Modified Capabilities

None.

## Impact

- Reads `DBT.ACTIVE_BASE`, `PUBLIC.T_DEVICE`, and the formal incident/impacted-device tables without writing to Snowflake.
- Adds a bounded local pilot command, aggregate audit output, cell-hour results, and a concise report.
- Preserves the separate `analyze-acs-outage-predictors` change; no ACS implementation is included here.
