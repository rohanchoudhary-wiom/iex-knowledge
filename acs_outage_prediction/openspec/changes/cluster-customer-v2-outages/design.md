## Context

See `proposal.md` for motivation. A read-only seven-day audit found 68,062 distinct outage devices and mapped 63,431 of them to eligible `CUSTOMER_V2` NASIDs through a deterministic, one-to-one `T_DEVICE` bridge: 93.2% coverage. The active, valid-coordinate `CUSTOMER_V2` footprint contains 147,792 accounts after excluding invalid India coordinates, but the primary denominator must also be valid at each historical hour. Current customer rows cannot establish presence before `LOCATION_START_TIME`.

The pilot crosses customer, device-inventory, incident, and impacted-device data. It must keep all joins and detailed rows inside Snowflake, expose only aggregates locally, and remain a descriptive feasibility study rather than a prediction system.

## Goals / Non-Goals

**Goals:**

- Produce one auditable customer cohort and device-mapping gate before spatial analysis.
- Produce interpretable, density-normalized cell-hour measures with bounded runtime and output.
- Preserve enough audit metadata to reproduce the window, exclusions, and denominators.
- Make the smallest viable implementation: one sequential pilot command and one runnable integration check.

**Non-Goals:**

- Building a generic geospatial platform, reusable feature store, dashboard, or production alerting service.
- Choosing ACS predictors or training a model in this change.
- Inferring historical customer locations earlier than the available location-start timestamp.

## Decisions

### Push detailed joins and aggregation into read-only SQL

One SQL pipeline will use common table expressions for the customer cohort, unique inventory bridge, outage intervals, hourly expansion, and cell aggregation. Only audit counts and reportable aggregates will leave Snowflake. This reduces privacy exposure and local memory use.

Alternative considered: export joined customer-device rows and process them locally. Rejected because identifiers and precise locations would leave the warehouse without improving the pilot.

### Build a strict CUSTOMER_V2 cohort before joining outages

The cohort will filter `SOURCE = 'CUSTOMER_V2'`, validate active status and India coordinates, and group by account. An account is retained only if NASID, coordinate pair, and `ACTIVE_STATE` are internally consistent. The network-footprint count uses that clean active cohort; each primary cell-hour denominator additionally enforces `LOCATION_START_TIME <= hour` and `PLAN_EXPIRY_TIME >= hour`. NASIDs shared by more than one otherwise-clean account are excluded before outage mapping.

Alternative considered: combine customer sources to increase historical coverage. Rejected because it changes the requested population and introduces cross-source reconciliation.

### Use only the audited one-to-one inventory bridge

Outage `DEVICE_ID` will join to `T_DEVICE.DEVICE_ID`, whose `COALESCE(LONG_NAS_ID, NASID)` value joins to the clean customer NASID. Both `DEVICE_ID -> NASID` and `NASID -> DEVICE_ID` must be unique in the bounded population. The command will record coverage and stop below the 90% gate rather than add fuzzy or direct-cast fallbacks.

Alternative considered: cast outage `DEVICE_ID` directly to a customer NASID. Rejected because the audit matched only 893 devices by that path.

### Use fixed 0.01-degree cells for the feasibility pilot

The pilot will derive stable integer cell keys with `FLOOR(latitude / 0.01)` and `FLOOR(longitude / 0.01)`. This gives explicit half-open boundaries and is approximately kilometre-scale for this exploratory use, but the report will not call it an exact equal-area grid. The sensitivity grid shifts both axes by 0.005 degrees. Detailed cell-hour rows with fewer than five eligible customers will be suppressed, and suppressed cells will not contribute to exported neighbour details.

Alternative considered: introduce H3 or a spatial clustering dependency. Deferred because a fixed grid answers the first feasibility question with less code and no new dependency. A production phase can replace it if boundary or area distortion changes decisions.

### Freeze time, interval, and feasibility semantics before the live run

Use the latest seven complete Asia/Kolkata calendar days and treat every analysis window and outage interval as half-open `[start, end)`. The documented warehouse convention treats `FIRST_FAIL_TIMESTAMP` as a UTC-valued `TIMESTAMP_NTZ`; convert it explicitly to Asia/Kolkata before hourly bucketing and record an internal failure-to-creation clock-order sanity check. This is supporting evidence, not independent timezone proof. Incidents with null or negative duration are excluded and counted; a zero-minute incident receives a one-minute interval so its onset hour is represented. The fixed 168-hour spine bounds long incidents without altering their recorded duration summary.

The geographic pilot returns `GO_FOR_ACS` only when all frozen conditions hold: mapping coverage is at least 90%; reportable cells retain at least 80% of eligible customer-hours on both grids; both chronological halves contain at least 100 affected customer-hours and at least 20 cells that remain reportable for all 84 hours of both halves; the Spearman correlation between early- and late-half cell outage rates is at least 0.50 on both grids; cells selected in the early half's top decile have at least 1.5 times the overall outage rate when evaluated in the late half on both grids; and shifting the grid changes each half's overall affected-customer rate by at most 10% and the late out-of-sample affected-customer concentration by at most 25%. Any failed or non-computable condition returns `NO_GO_FOR_ACS`.

The decision metrics use two onset arms. Both retain only incidents whose converted failure onset falls inside the seven-day window, assign each incident to exactly one 84-hour half by onset, and clip its exposure at that half's boundary. The second arm also excludes durations over 10,080 minutes. Every decision condition must pass in both arms and both grids. Carry-in/all-overlap incidents remain in the descriptive CSV but cannot establish temporal transfer.

Corrective revision, 2026-08-11: review of the first provisional run found that 11,290 intervals exceeded the entire analysis window and that many carry-ins populated both halves. That provisional `GO_FOR_ACS` was invalidated before handoff. The corrected rerun is explicitly a post-hoc technical sensitivity because its definitions were revised after inspecting the provisional output. A corrected pass authorizes only the next ACS coverage/freshness/within-device-variance audit; prediction or predictor-effect claims require an untouched later time window.

These are feasibility thresholds, not claims of statistical significance. They are frozen before the live output is inspected and may only be changed in a new documented pilot.

### Measure outage exposure over overlapping hours

An incident interval begins at `FIRST_FAIL_TIMESTAMP` and ends after `DURATION_MINUTES`. It contributes an affected customer to each overlapping hourly bucket. The aggregation counts a NASID only once per cell-hour even if duplicate or overlapping incident records exist. Outputs include eligible and affected customer counts, their ratio, distinct incidents, duration summaries, and the same aggregates over the eight adjacent cells.

Alternative considered: count only the incident-start hour. Rejected because it understates long outages and makes duration comparisons misleading.

### Keep validation chronological and claims descriptive

The default pilot window is the latest complete seven-day period available from non-deleted formal incidents. Pattern stability compares non-overlapping earlier and later periods with no incident contributing to both. Cells are selected by early-period rate and evaluated without reselection in the later period. The incident source creates the outage measure, so the result can assess localization and temporal consistency but cannot independently validate outage truth or predictive accuracy. Same-half top-decile lift, carry-in impact, long-duration impact, and geometric hotspot-overlap are diagnostics only.

Alternative considered: random train/test splitting. Rejected because it leaks temporal structure and implies a prediction exercise that is outside this change.

### Write three aggregate artifacts

The command will write `audit.json`, `cell_hour_outages.csv`, and `report.md` under a run-specific local output directory. The audit records the analysis window, source counts, cohort exclusions, mapping coverage, suppressed-cell count, and gate status. A schema-level privacy check will reject prohibited identifier or exact-coordinate columns before writing. A failed source or mapping gate writes only the privacy-safe `audit.json` required to explain the stop; it does not create empty spatial artifacts.

Alternative considered: a notebook-first workflow. Rejected because a small command is easier to rerun and verify; a notebook can consume the aggregate CSV later if needed.

## Risks / Trade-offs

- [Current active snapshot creates survivorship bias for older hours] → Keep the window short, enforce location and plan timestamps, and state that the denominator is not a complete historical subscriber ledger.
- [A 0.01-degree grid has unequal physical width and boundary sensitivity] → Label it approximate and run one shifted-grid sensitivity comparison before recommending a production geography.
- [Shared incident data cannot validate its own outage correctness] → Limit conclusions to coverage, localization, duration, and temporal consistency.
- [Overlapping incidents may inflate incident counts] → Deduplicate affected NASIDs per cell-hour and present incident count separately from affected-customer count.
- [Suppression removes sparse-area detail] → Report aggregate suppression counts and do not weaken the threshold in exported artifacts.
- [Warehouse expansion to hours can be expensive] → Bound the default run to seven days, filter before expansion, and aggregate in Snowflake.

## Migration Plan

1. Add the pilot command and its integration check without changing the existing ACS analysis change.
2. Run the cohort and mapping gates against a bounded seven-day window.
3. If the gates pass, create aggregate cell-hour artifacts and the descriptive report; otherwise retain only the aggregate audit failure.
4. Remove the local run directory to roll back; the workflow performs no database writes or schema migrations.
