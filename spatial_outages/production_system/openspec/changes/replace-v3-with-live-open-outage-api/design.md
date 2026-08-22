## Context

The service already refreshes the operational map from Detection's `status=OPEN` endpoint, but the Customer V2 export still joins `OUTAGE_MEMBER_V3` and `OUTAGE_V3`, and the runtime inventory prefers those replicated failure timestamps. This leaves a hidden Snowflake outage dependency in an otherwise live path. See `proposal.md` and `specs/open-outage-source/spec.md` for the required behavior.

## Goals / Non-Goals

**Goals:**

- Make live OPEN API membership the only authority for the operational outage view.
- Make the Snowflake export a Customer V2 inventory query with no outage-table joins.
- Preserve atomic refresh, fail-closed behavior, live device status, attribution rules, and the existing output contract.
- Make the remaining timing proxy explicit and testable.

**Non-Goals:**

- Poll or render all 42,000+ historical outages.
- Add pagination, filtering, or fields to Detection's API.
- Store historical outage responses in this service.
- Reconstruct per-member failure timestamps that the OPEN API does not supply.

## Decisions

### Use `status=OPEN` directly

The configured production URL will include `status=OPEN`, and response validation will remain at the service boundary. Calling `status=ALL` and filtering locally was rejected because the endpoint is large, unpaginated, and does not include per-row status.

### Remove V3 data from inventory rather than masking it

The SQL export will delete the V3 CTEs, joins, and output columns. The runtime inventory will delete its outage-failure-time map and parser. Keeping unused columns was rejected because it would preserve the stale dependency and invite accidental reuse.

### Retain one documented timing proxy

For current DOWN members, the engine will derive supporting failure time from the live last successful ping plus five minutes. This already exists as the fallback and becomes the only path until Detection adds member failure timestamps to the OPEN API.

### Preserve atomic refresh and prior snapshot on failure

The service will continue evaluating a complete candidate result set before swapping it into memory. API or status failures leave the prior snapshot visible with an error state; there is no Snowflake or historical fallback.

## Risks / Trade-offs

- [OPEN API unavailable] → Fail closed and retain the previous atomic snapshot with an explicit health error.
- [Proxy failure time is less precise than V3] → Label provenance explicitly and use it only as supporting timing evidence, never as membership detection.
- [Historical outages are unavailable in the atlas] → Keep `ALL`/`CLOSED` for separate, explicit backfill jobs outside the live service.
- [Customer export shape changes] → Refresh the CSV after deployment and retain optional-column compatibility only long enough to read an existing pre-migration snapshot during rollback.

## Migration Plan

1. Add regression checks for the OPEN-only URL, response replacement, no V3 SQL/runtime references, and proxy provenance.
2. Remove V3 CTEs and columns from the Customer V2 query and simplify the runtime inventory/timing path.
3. Refresh the Customer V2 snapshot, run the full suite, and start the service against the live OPEN and ping APIs.
4. Verify health `source=LIVE`, API `as_of`, current open count, removal of a closed outage, and absence of production V3 queries.
5. Roll back by restoring the prior code and compatible pre-migration CSV; the public API contract does not change.
