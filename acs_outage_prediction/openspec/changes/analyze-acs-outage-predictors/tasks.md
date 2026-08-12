## 1. Reconciled aggregate evidence

- [x] 1.1 Audit and freeze the canonical exact ACS serial → master device → public device/NAS → strict active `CUSTOMER_V2` → formal outage route; record its 81/660 coverage, ambiguity exclusions, one-to-one invariant, and Snowflake query IDs.
- [x] 1.2 Audit chronological mapping/outcome feasibility for fixed 60%/20%/20% hourly partitions; confirm both classes and record the large device/prevalence drift.
- [x] 1.3 Run the unlabeled source-only audit over the fixed early/middle/late three-day windows using exactly nine flat fields and sixteen P1 paths; record freshness, within-device variation, and null optical results without identifiers or full JSON.
- [x] 1.4 Freeze `$._lastBoot` as the only source-ready P1 candidate and document its special boot-age/boot-transition semantics. Record all other source-audit failures as null results.
- [x] 1.5 Reconcile the ACS optical null with the separate Snowflake optical inventory; record the device-hour, NAS-day, change-history, CSP-day aggregate, and installation-snapshot sources without adding them to v14.

## 2. Minimal reproducible entry point

- [x] 2.1 Add one local read-only analysis command with explicit date-range/output arguments, the frozen whitelist/reducers/gates, and derived-output paths excluded from Git.
- [x] 2.2 Reproduce the completed aggregate source and mapping audits in `audit.json`, including source windows, query IDs, timezone assumption, counts, exclusions, thresholds, and `FEASIBILITY_PILOT_ONLY` status but no identifiers.
- [x] 2.3 Add one runnable in-memory self-check covering one-to-one mapping rejection, ordinary-leaf freshness, `$._lastBoot` transition semantics, strictly prior feature selection, and future-outage labelling.

## 3. Mandatory mapped-training feature gate

- [x] 3.1 Query only the canonical mapped cohort and only source-ready candidates for the fixed training period; keep direct IDs in memory and never write raw JSON.
- [x] 3.2 Apply the frozen training-only coverage and within-device-variation criteria without inspecting validation/test outcomes or selecting features by apparent outcome association.
- [x] 3.3 Report mapped-training counts and eligibility for boot age and true boot-timestamp transitions. If no candidate passes, stop with a supported `NO_GO_FOR_MODEL_FRAME` conclusion.
- [x] 3.4 Freeze the eligible feature manifest, reducers, missingness handling, split boundaries, model, metrics, and operating-threshold rule before validation or test outcomes are exposed.

## 4. Conditional device-hour frame

- [x] 4.1 If and only if task 3.3 passes, build unique mapped device-hour anchors with the latest strictly prior ACS snapshot, meaningful 1-hour/6-hour/24-hour summaries, staleness, missingness, and representative controls.
- [x] 4.2 Generate 6-hour and 24-hour future-onset labels and censored/closure-unverified future duration from valid formal incidents using the UTC-valued `FIRST_FAIL_TIMESTAMP` NTZ assumption; require closure by the fixed label-tail boundary for complete duration and exclude rows already in the reported formal outage interval.
- [x] 4.3 Run schema, uniqueness, timestamp-order, active-outage, prohibited-field, and no-raw-JSON checks before writing `model_frame.csv.gz`.

## 5. Conditional chronological evaluation

- [x] 5.1 Fit preprocessing and regularized logistic regression on training only, set any operating threshold on validation, and evaluate test once against constant-prevalence and frozen time-only no-ACS baselines on identical rows.
- [x] 5.2 Report PR-AUC, precision, recall, Brier score, warning time, feature coverage, robust descriptives, device-clustered uncertainty, Benjamini-Hochberg adjustment, and partner/time stability for both horizons.
- [x] 5.3 Analyze outage duration only among closure-verified future-positive rows and report the complete-case, query-snapshot-conditional diagnostic separately from onset prediction.
- [x] 5.4 Write conditional `feature_results.csv` and `report.md`, retaining null results, mapping selection, temporal drift, and an explicit supported/not-demonstrated conclusion under `FEASIBILITY_PILOT_ONLY`.
- [x] 5.5 Run the separately versioned, database-source-free event-aligned diagnostic from the immutable v14 frame using the frozen bins, six transformations, support rules, aggregation order, and multiplicity families; retain every tested result and null.

## 6. Validation and confirmation

- [x] 6.1 Run the self-check, privacy/leakage audit, exact aggregate reproduction, and record commands, versions, query IDs, and output counts.
- [ ] 6.2 Run strict OpenSpec CLI validation when the `openspec` executable is available; manual structure/content validation and `git diff --check` pass in this workspace.
- [ ] 6.3 Apply the locked mapping, whitelist, reducers, freshness rules, preprocessing, model, threshold, and metrics to a genuinely untouched post-development window before making any confirmed predictor claim.
- [x] 6.4 Validate v2 parent-input hashes, onset reconstruction, strict anchor-before-onset ordering, feature/event/device attrition, singleton-onset sensitivity, privacy, command template, seed, software versions, and output hashes. This validation does not satisfy task 6.3.
- [ ] 6.5 Open a separate prospectively locked optical-outage feasibility change using `PUBLIC.HOURLY_DEVICE_PING_INFLUX`; freeze the label-producer contract, two-hour action gap, expected-hour spine, value/family exclusions, direct exact `DEVICE_ID` join, `HOUR_END_IST`/`INSERTED_AT` cutoffs, minimal optical features, comparators, and untouched post-2026-08-11 holdout before modelling.
- [x] 6.6 Run and audit one actual development-only 15-feature logistic comparison over already-inspected dates. Record `CURRENT_DEVELOPMENT_POOLED_OPTICAL_LIFT_ONLY`; this post-audit run does not complete 6.5 or untouched confirmation 6.3.
