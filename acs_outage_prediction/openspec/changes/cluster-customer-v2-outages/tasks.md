## 1. Pilot Command and Cohort Gate

- [x] 1.1 Add one read-only pilot command with a bounded seven-day default window and a run-specific aggregate output directory.
- [x] 1.2 Implement the `CUSTOMER_V2`-only cohort, consistent-account collapse, active and India-coordinate filters, and per-rule audit counts.
- [x] 1.3 Implement separate network-footprint and time-valid customer-impact denominators using location-start and plan-expiry timestamps.
- [x] 1.4 Add a deterministic fixture check for source exclusion, conflicting duplicates, and time-valid denominator behavior.

## 2. Outage Mapping Gate

- [x] 2.1 Implement non-deleted incident and impacted-device selection for the bounded window.
- [x] 2.2 Implement the bidirectionally unique `DEVICE_ID` to `T_DEVICE` to `CUSTOMER_V2` NASID bridge without cast or fuzzy fallbacks.
- [x] 2.3 Record mapped, unmapped, and ambiguous counts and stop before spatial work when coverage is below 90%.
- [x] 2.4 Extend the fixture check to cover passing and failing mapping gates.

## 3. Cell-Hour Analysis

- [x] 3.1 Build outage intervals from failure time and duration, expand overlapping hours, and deduplicate each affected NASID within a cell-hour.
- [x] 3.2 Aggregate eligible customers, affected customers, outage rate, distinct incidents, and duration summaries into fixed 0.01-degree cell-hours.
- [x] 3.3 Add eight-neighbour aggregate measures and suppress detailed cell-hours with fewer than five eligible customers.
- [x] 3.4 Run one shifted-grid comparison and record whether boundary sensitivity materially changes the geographic conclusions.

## 4. Aggregate Outputs and Verification

- [x] 4.1 Write `audit.json` and `cell_hour_outages.csv` with the window, exclusions, gate results, suppression count, and required aggregate measures.
- [x] 4.2 Add a schema-level privacy check that fails before writing any prohibited identifier or exact-coordinate field.
- [x] 4.3 Generate `report.md` with chronological stability results, known limitations, and an explicit go/no-go decision for a later ACS phase.
- [x] 4.4 Run the integration check and one bounded live pilot, confirm all outputs are aggregate-only, and record the exact reproduction command.
