## Why

Snowflake's replicated outage V3 tables can lag production by roughly two hours, so they must not define the operational open-outage view. Detection already exposes a timestamped live API with an explicit `status=OPEN` filter and should be the single source of outage membership for the atlas.

## What Changes

- Make `GET https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN` the sole production source for the open-outage view.
- Validate the API count, `as_of`, outage IDs, device membership, and duration before atomically replacing the current view.
- Remove `OUTAGE_V3` and `OUTAGE_MEMBER_V3` from the Customer V2 inventory query and runtime model.
- Keep Snowflake only for deduplicated Customer V2 inventory, CSP ownership, addresses, and coordinates.
- Use live device-ping evidence for current state and the documented last-ping proxy for supporting failure time when the open-outage API does not provide member failure timestamps.
- **BREAKING**: remove `OUTAGE_MEMBER_V3` timing provenance from production attribution; runtime outage membership and visibility no longer depend on replicated V3 data.
- Keep `status=ALL` and `status=CLOSED` outside the live atlas path; they may be used only for explicit historical backfills.

## Capabilities

### New Capabilities

- `open-outage-source`: Authoritative, validated, atomic ingestion of Detection's live OPEN outage API without Snowflake outage-table dependencies.

### Modified Capabilities

None.

## Impact

- Affects the Customer V2 SQL export, inventory model, attribution timing provenance, live service configuration, tests, and operating documentation.
- Removes production reads of `PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.OUTAGE_V3` and `OUTAGE_MEMBER_V3`.
- Preserves the existing public attribution response and dashboard behavior while making open-outage removal follow the next valid API refresh.
