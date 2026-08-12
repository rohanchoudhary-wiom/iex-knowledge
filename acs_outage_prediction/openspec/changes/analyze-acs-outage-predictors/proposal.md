## Why

The ACS store contains pre-outage router telemetry and Snowflake contains formal outage incidents, but their device identifiers do not directly match. A gated feasibility analysis is needed before any ACS parameter can be claimed as an outage predictor.

The completed aggregate audits narrow that question substantially. An exact inventory bridge reaches only 81 of 660 ACS devices (12.27%), and the chronological cohort and outcome prevalence drift sharply. In three bounded ACS source windows, ordinary whitelisted JSON values are less than 1% fresh in the middle and late windows, populated flat dynamic fields have no late-window within-device variation, and flat optical fields are absent from `acs_raw_dump`. Only the root `_lastBoot` value passed the source-only screen; at that stage it still required reboot-event semantics and a mapped-training check. The current work is therefore a feasibility pilot, not a validated predictor study.

Separate Snowflake telemetry does contain optical power. The live source audit identifies `PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX` as the primary point-in-time candidate because it exposes direct device identifiers, hourly boundaries, optical minimum/average/maximum, ping fields, and `INSERTED_AT`. `PROD_DB.DBT.STG_IX_PING_INFLUX` is a projection of the DBT hourly materialization rather than an independent source and omits the availability timestamp; `FCT_OPTICAL_SIGNAL` and `AVG_OPTICAL_SIGNAL` are NAS-day derivatives; and `PUBLIC.ROUTER_DETAILS_AUDIT` is irregular change history. These sources were not inputs to the canonical ACS v14 run. Its null result is therefore ACS-specific, not a fleet-wide no-go for optical-based outage prediction. A bounded matched screen nevertheless found only negligible pre-incident optical separation, so the external source is eligible for a prospectively locked feasibility pilot, not a predictor claim.

## What Changes

- Audit candidate ACS-to-warehouse identifier mappings and measure uniqueness, coverage, and time overlap without exporting direct identifiers.
- Freeze the canonical exact bridge and the nine flat fields and sixteen P1 JSON paths that may be inspected; fuzzy matching and open-ended JSON discovery remain prohibited.
- Run a source-readiness screen before joining outcomes, then a mapped-training availability/variation gate. Stop before dataframe construction when no feature survives.
- If a feature survives, create a device-hour dataframe containing only ACS information strictly available before each prediction timestamp, with 6-hour and 24-hour outage labels and future outage duration.
- Freeze feature eligibility and preprocessing on training data before exposing validation or test outcomes. Rank surviving ACS parameters using coverage, effect size, uncertainty, stability, and a chronological predictive baseline rather than raw correlation alone.
- Produce aggregate, privacy-safe data-quality, leakage, and analysis reports.
- Record the non-ACS optical source inventory without adding those sources post hoc to the completed canonical ACS analysis.
- Run one separately versioned, database-source-free event-aligned window diagnostic from the immutable final frame; keep it case-only, post-hoc, and incapable of changing the canonical feasibility decision.
- Require confirmation on a later untouched time window before any predictor claim. The current overlap can support only `FEASIBILITY_PILOT_ONLY`.
- Non-goals: causal attribution, production alerting, automated remediation, mobile-number features, post-outage diagnosis, full extraction of sensitive ACS payloads, searching additional ACS parameters because the whitelist failed, and retrofitting Snowflake optical telemetry into the already-inspected v14 splits.

## Capabilities

### New Capabilities

- `acs-outage-feasibility-analysis`: Gated identifier audit, leakage-safe analytical dataframe construction, and exploratory evaluation of pre-outage ACS parameters.

### Modified Capabilities

None.

## Impact

- Reads MySQL `acs_raw_dump`, Snowflake outage incident/impacted-device tables, and the minimum fields from `MASTER_DB_READ_DBO.T_DEVICE`, `PUBLIC.T_DEVICE`, and strict `CUSTOMER_V2` active-base rows needed for the canonical bridge.
- Documents separate Snowflake optical sources as candidates for a future prospectively locked change; the current command does not query or model them.
- Adds a small local analysis command, derived aggregate outputs, and one runnable integration check inside this project.
- Requires a MySQL driver and the already-configured Snowflake access path at execution time; no source database writes are permitted.
