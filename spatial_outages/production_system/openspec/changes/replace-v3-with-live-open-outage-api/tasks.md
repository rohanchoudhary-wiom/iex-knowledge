## 1. Source Contract Regression Coverage

- [x] 1.1 Add focused tests proving the production outage URL is `status=OPEN`, valid refreshes atomically replace membership, and invalid refreshes retain the previous snapshot; verify the targeted tests fail before implementation and pass afterward.
- [x] 1.2 Add focused checks proving production SQL/runtime files do not reference `OUTAGE_V3` or `OUTAGE_MEMBER_V3` and timing provenance is `LAST_PING_PROXY`; verify the checks pass against the completed implementation.

## 2. Remove the Snowflake Outage Dependency

- [x] 2.1 Remove outage V3 CTEs, joins, and output fields from the Customer V2 export query; verify the query returns a non-empty deduplicated inventory without either outage table in its Snowflake query history.
- [x] 2.2 Remove V3 failure-time storage/parsing from the inventory model and make live last ping plus five minutes the only supporting timing path; verify model and attribution tests pass.

## 3. Enforce the Live OPEN View

- [x] 3.1 Keep the service production feed fixed to Detection's `status=OPEN` endpoint and preserve strict response validation, atomic replacement, and fail-closed health behavior; verify `status=ALL` and `status=CLOSED` are absent from scheduled runtime requests.
- [x] 3.2 Update operating documentation to identify Detection OPEN as the sole outage source and Snowflake as Customer V2 inventory only; verify documentation contains no production V3 dependency claim.

## 4. Refresh and Live Verification

- [x] 4.1 Refresh the Customer V2 snapshot with the V3-free query, run the full test suite and strict OpenSpec validation, and verify the exported CSV contains no outage V3 timing columns.
- [x] 4.2 Restart localhost and verify health is LIVE, the atlas count matches the current OPEN API response, a closed outage disappears on refresh, and no historical ALL/CLOSED response is loaded.
