## 1. Full-population source and mapping audit

- [x] 1.1 Freeze July 2026 and the August 11 recovery-observation boundary; remove the 5% hash filter.
- [x] 1.2 Reuse hourly normalization, conflicting-key quarantine, synthetic-family exclusion, and clean time-valid `CUSTOMER_V2` rules.
- [x] 1.3 Audit the exact inventory bridge and enforce at least 90% full-population mapping coverage.

## 2. Strict ping-outage events

- [x] 2.1 Derive adjacent-success gaps across absent hourly rows and apply the exact twelve-missed-slot rule.
- [x] 2.2 Keep recovered and right-censored events distinct and require event starts inside the customer service interval.
- [x] 2.3 Add one deterministic check for the exact 65-minute boundary.

## 3. H3 resolution-9 mapping

- [x] 3.1 Assign mapped devices with Snowflake-native H3 after validating coordinates.
- [x] 3.2 Aggregate eligible devices, affected devices, events, affected share, recovered duration, and censoring by cell.
- [x] 3.3 Report H3 occupancy and mapped-device shares in cells with at least three and five devices.

## 4. Outputs and verification

- [x] 4.1 Write aggregate-only `audit.json`, `h3_cell_summary.csv`, and `report.md` with the query ID and artifact hashes.
- [x] 4.2 Fail before export on prohibited identifiers, exact coordinates, invalid H3 cells, or sparse cell details.
- [x] 4.3 Run the full July population and record mapping and telemetry-outage evidence without clustering or prediction claims.
