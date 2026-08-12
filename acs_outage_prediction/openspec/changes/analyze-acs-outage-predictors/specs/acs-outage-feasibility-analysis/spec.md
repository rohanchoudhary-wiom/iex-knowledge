## Purpose

Define a reproducible, privacy-safe sequence of source-readiness, exact-mapping, mapped-training, dataframe, and chronological-evaluation gates for determining whether pre-outage ACS telemetry contains useful outage-prediction signal.

## ADDED Requirements

### Requirement: Source-readiness gate precedes outcome analysis
The analysis SHALL inspect ACS availability, schema, time coverage, freshness, and within-device variation before joining outage outcomes. For the current pilot it SHALL use the half-open UTC windows `[2026-06-02, 2026-06-05)`, `[2026-07-05, 2026-07-08)`, and `[2026-08-07, 2026-08-10)`, and SHALL record the aggregate 82,867-row audit result. It MUST NOT weaken the source gate or search additional payload paths after seeing outcomes.

The executable pilot SHALL validate the frozen source-audit artifact rather than hard-code a pass. It SHALL pin and verify the artifact SHA-256 plus its schema/configuration, status and decision, exact eligible flat/P1 lists, windows, sampled-row total, timezone, thresholds, boot-age limit, and privacy result.

#### Scenario: A candidate fails source readiness
- **WHEN** an ACS candidate lacks usable freshness, coverage, or within-device variation in the bounded source audit
- **THEN** it is retained as a null data-quality result and is not correlated, placed in the modelling frame, or replaced through open-ended parameter discovery

#### Scenario: A candidate passes source readiness
- **WHEN** a whitelisted candidate has usable source coverage and variation under its declared semantics
- **THEN** it may advance only to the mapped-training eligibility gate and is not yet called an outage predictor

### Requirement: Flat and JSON access is exactly whitelisted
The analysis SHALL read only flat `uptime_s`, `cpu_pct`, `memory_free_kb`, `memory_total_kb`, `temperature_c`, `optical_rx_dbm`, `optical_tx_dbm`, `inform_time`, and `param_count`, plus the following P1 JSON paths:

1. `InternetGatewayDevice.DeviceInfo.MemoryStatus.Free`
2. `InternetGatewayDevice.DeviceInfo.ProcessStatus.CPUUsage`
3. `InternetGatewayDevice.DeviceInfo.TemperatureStatus.TemperatureSensor.N.Value`
4. `InternetGatewayDevice.DeviceInfo.UpTime`
5. `InternetGatewayDevice.GX_OntOpticalParam.RXPower`
6. `InternetGatewayDevice.GX_OntOpticalParam.TXPower`
7. `InternetGatewayDevice.GX_OntOpticalParam.TransceiverTemperature`
8. `InternetGatewayDevice.GX_OntOpticalParam.BiasCurrent`
9. `InternetGatewayDevice.GX_OntOpticalParam.SupplyVoltage`
10. `InternetGatewayDevice.LANDevice.N.WLANConfiguration.N.TotalAssociations`
11. `InternetGatewayDevice.LANDevice.N.WLANConfiguration.N.AssociatedDevice.N.SignalStrength`
12. `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.ConnectionStatus`
13. `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.LastConnectionError`
14. `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.Uptime`
15. root `$._lastInform`
16. root `$._lastBoot`

The analysis SHALL use the frozen reducers: memory free minimum, CPU maximum, sensor temperature maximum, device uptime minimum, optical RX minimum, optical TX maximum, transceiver temperature maximum, bias current maximum, supply voltage minimum, WiFi associations sum, associated-device signal median, PPP category sets/collapses, PPP uptime minimum, last-inform age, and boot age/boot transitions. It MUST NOT read a complete JSON payload into an output artifact.

#### Scenario: A wildcard path has multiple instances
- **WHEN** a whitelisted path contains `N`
- **THEN** all present numeric object keys are reduced within the snapshot using only the reducer declared for that path

#### Scenario: An unlisted path appears useful
- **WHEN** a parameter is not in the frozen sixteen-path whitelist
- **THEN** it is not read or tested in this change, regardless of apparent correlation or domain plausibility

### Requirement: Per-value freshness and special boot semantics
For an ordinary nested leaf, the analysis SHALL require its own `_timestamp` to be parseable, no later than the enclosing `inform_time`, and no more than 24 hours old. A fresh `$._lastInform` MUST NOT make another leaf fresh. `$._lastInform` MAY be used only for call-home age. `$._lastBoot` SHALL instead require a parseable, non-future timestamp and plausible boot age no greater than ten years, and SHALL be represented as boot age and changes in the underlying boot timestamp.

#### Scenario: Inform is current but a leaf timestamp is stale
- **WHEN** `$._lastInform` is within 24 hours but an ordinary leaf's own timestamp is older than 24 hours
- **THEN** the ordinary leaf is missing for feature construction

#### Scenario: Last boot is stable between informs
- **WHEN** `$._lastBoot` remains unchanged across otherwise eligible snapshots
- **THEN** the analysis treats that as one continuing boot, not as repeated fresh variation or repeated reboot events

#### Scenario: Last boot changes
- **WHEN** the valid underlying `$._lastBoot` timestamp changes for a device
- **THEN** the analysis may derive a boot-transition indicator strictly after that source value was observed

### Requirement: Non-ACS optical telemetry is scoped separately
The analysis SHALL qualify every statement that optical power is absent as applying only to MySQL `acs_raw_dump`. It SHALL record that separate optical telemetry exists in `PROD_DB.DBT.HOURLY_DEVICE_PING_INFLUX`, its `PROD_DB.PUBLIC` mirror, `PROD_DB.DBT.STG_IX_PING_INFLUX`, `PROD_DB.DBT.FCT_OPTICAL_SIGNAL`, `PROD_DB.DBT.AVG_OPTICAL_SIGNAL`, and `PROD_DB.PUBLIC.ROUTER_DETAILS_AUDIT`. It SHALL distinguish device-hour optical summaries from NAS-day aggregates, change-history rows, CSP-day Quality rollups, and installation-only snapshots. None of these sources MAY be added post hoc to the canonical v14 ACS frame or used to change its decision.

A future optical-outage prediction change SHALL use `PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX` as the primary point-in-time source unless a later audit proves a stronger availability contract. Every feature row SHALL satisfy both normalized hour end `<= prediction_anchor` and `INSERTED_AT <= prediction_anchor`; the known same-day-midnight `HOUR_END_IST` defect MAY be normalized only to `HOUR_START_IST + 1 hour` after matching that exact defect. The analysis SHALL join formal outcomes directly by normalized exact `DEVICE_ID`, exclude conflicting duplicate device-hours, construct expected active-device hours so absent rows remain observable as missingness, and quarantine unresolved sentinels, implausible values, synthetic families, and `OPTICAL_IN_RANGE_PINGS`. Before modelling, it SHALL independently verify schema, grain, time coverage, cadence, value units, availability-time semantics, missingness, device-family exclusions, identifier uniqueness, and exact join coverage. It MUST use newly frozen chronological boundaries and MUST NOT treat an already-inspected v14 split, the 2026-08-12 matched screen, or the 15-feature development model as confirmation.

The future change SHALL verify the producer contract for the formal router-outage labels before using ping or optical fields. Until label independence is proven, ping-loss, ping-streak, and row-absence features SHALL be reported only as a labelled availability comparator and MUST NOT support an independent outage-predictor claim. If the label producer uses optical power, evaluation against that label MUST stop until an independent outcome source is selected.

#### Scenario: ACS optical fields are absent but Snowflake optical data exists
- **WHEN** the `acs_raw_dump` optical candidates have zero usable coverage while a separate Snowflake optical table has device-level measurements
- **THEN** the report retains the ACS null, records the external source as untested, and does not describe outage prediction overall as a no-go

#### Scenario: An aggregate optical table is proposed as a device predictor
- **WHEN** the candidate source contains only CSP-day numerator/denominator rollups or a single installation snapshot
- **THEN** it is excluded from device-hour prediction and may be used only for aggregate monitoring or source reconciliation

#### Scenario: A future device-level optical pilot is opened
- **WHEN** `STG_IX_PING_INFLUX` or `HOURLY_DEVICE_PING_INFLUX` is proposed for outage prediction
- **THEN** the change uses the public hourly table for point-in-time replay, first audits a direct exact `DEVICE_ID` join to the impacted-device source, and otherwise uses only an audited one-to-one device/NAS bridge, with no fuzzy fallback

#### Scenario: Optical source feasibility passes but the exploratory separation is negligible
- **WHEN** point-in-time telemetry coverage is high but matched pre-incident optical levels, trends, and out-of-range shares have trivial separation
- **THEN** the status remains `FEASIBILITY_PILOT_ONLY`, modelling is limited to a predeclared simple baseline, and no optical predictor claim is made without incremental held-out, device-aware, calibrated benefit over time-only and availability-only comparators

#### Scenario: A combined development model has only pooled optical lift
- **WHEN** combined-minus-non-optical PR-AUC has a device-bootstrap interval crossing zero or the equal-device improvement is not broadly distributed
- **THEN** the result is labelled `CURRENT_DEVELOPMENT_POOLED_OPTICAL_LIFT_ONLY`, non-optical performance is qualified by detector-lineage risk, and no predictor claim is made

### Requirement: Deterministic canonical identifier mapping
The analysis SHALL use only the exact route `ACS serial_number → MASTER_DB_READ_DBO.T_DEVICE.PON_SERIAL → master DEVICE_ID → PUBLIC.T_DEVICE.DEVICE_ID → COALESCE(public LONG_NAS_ID, public NASID) → strict active CUSTOMER_V2 NASID → formal outage DEVICE_ID`. Every accepted leg SHALL be non-null and one-to-one for the selected period. The analysis MUST report stage coverage and ambiguity counts, MUST NOT use fuzzy matching, and MUST NOT export direct identifiers.

#### Scenario: Canonical mapping is reproduced
- **WHEN** the aggregate audit reproduces the frozen mapping
- **THEN** it reports 660 ACS devices, 644 unique non-colliding serials, 251 exact PON matches, 249 one-to-one public/NAS bridges, 81 strict active `CUSTOMER_V2` devices, 77 with a service-valid formal outage onset in the overlap, two excluded ambiguities, and an 81=81=81 final join invariant

#### Scenario: Canonical mapping changes
- **WHEN** the counts, uniqueness, or source contracts no longer reproduce
- **THEN** the analysis stops and reports the discrepancy instead of silently selecting another bridge

#### Scenario: Analysis-window telemetry omits an otherwise valid identity
- **WHEN** a device has a valid non-colliding serial before the frozen analysis end but no telemetry inside the modelling window
- **THEN** it remains in exact mapping reproduction and is reported later as telemetry/model-frame attrition rather than being removed from the identity denominator

### Requirement: UTC-valued outage timestamp assumption is explicit
The analysis SHALL establish a UTC MySQL session and treat ACS `inform_time` as UTC. It SHALL treat public outage `FIRST_FAIL_TIMESTAMP` as a UTC-valued `TIMESTAMP_NTZ`, localizing the latter to UTC without converting it from Asia/Kolkata. It SHALL treat `ACTIVE_BASE.LOCATION_START_TIME` and `PLAN_EXPIRY_TIME` as Asia/Kolkata-local NTZ values and explicitly convert UTC anchors and incident onsets to Asia/Kolkata before checking the service interval. It SHALL record these as pilot assumptions rather than independently proven timezone facts. It SHALL use `FIRST_FAIL_TIMESTAMP` for onset and MUST NOT substitute impacted-device `CREATED_AT`.

#### Scenario: Timestamp contract is not upheld
- **WHEN** a boundary check or later source contract contradicts the UTC-valued `FIRST_FAIL_TIMESTAMP` assumption
- **THEN** label construction stops before ACS values are joined to outcomes

#### Scenario: A UTC event is checked against local service bounds
- **WHEN** an anchor or formal incident onset is compared with `LOCATION_START_TIME` or `PLAN_EXPIRY_TIME`
- **THEN** the UTC timestamp is first converted to Asia/Kolkata, and out-of-service anchors/incidents are excluded

### Requirement: Mapped-training eligibility is frozen without held-out peeking
Only source-ready candidates MAY enter a mapped-cohort training audit. Feature eligibility SHALL use training-period predictor availability, freshness, and within-device variation, not apparent outcome association. Numeric candidates SHALL require at least 25% fresh non-null eligible training anchors, at least 20 independent training devices with at least three fresh observations each, non-zero training IQR, and non-zero within-device range in at least 10% of those devices. Categorical candidates SHALL require at least two levels, a non-dominant level of at least 1%, at least 10 training devices, and at least five within-device transitions.

#### Scenario: No candidate passes mapped-training eligibility
- **WHEN** every source-ready candidate fails a frozen mapped-training criterion
- **THEN** the analysis stops with `NO_GO_FOR_MODEL_FRAME` and does not calculate feature-outcome correlations or fit a model

#### Scenario: A candidate passes mapped-training eligibility
- **WHEN** at least one candidate passes all applicable training-only criteria and both outcome classes remain feasible
- **THEN** the eligible feature manifest, reducers, split boundaries, missingness handling, model, metrics, and threshold rule are frozen before validation or test outcomes are inspected

#### Scenario: Held-out coverage differs
- **WHEN** validation or test coverage differs after the feature manifest is frozen
- **THEN** the difference is reported as drift and does not cause a feature to be rescued, removed, or redefined

#### Scenario: Mandatory exclusions change training eligibility
- **WHEN** a prelabel-selected feature fails the same frozen gate after split-boundary and active-outage exclusions
- **THEN** evaluation stops; post-exclusion data MUST NOT add, rescue, or redefine a feature

### Requirement: Device-hour observation frame is conditional and unique
If and only if the mapped-training gate passes, the analysis SHALL create at most one row per mapped pseudonymous device and hourly prediction timestamp. Each row SHALL require both the anchor and its full 24-hour label horizon to be inside the Asia/Kolkata-local service interval, use the latest ACS snapshot strictly before the anchor, require an inform within the previous 24 hours, retain explicit missingness and staleness, and contain only meaningful 1-hour, 6-hour, and 24-hour summaries for frozen eligible features.

#### Scenario: Multiple ACS informs precede an hour
- **WHEN** a device has multiple eligible ACS snapshots before a prediction timestamp
- **THEN** strictly prior snapshots are summarized without duplicating the device-hour row

#### Scenario: ACS telemetry is stale or absent
- **WHEN** no eligible fresh value exists in a lookback window
- **THEN** the feature remains missing and its missingness/staleness indicators are populated without outcome-informed imputation

### Requirement: Leakage-safe outcomes and controls
The analysis SHALL fetch valid non-deleted formal incidents over the bounded carry-in and label-tail interval before onset/service filtering. It SHALL calculate `outage_next_6h`, `outage_next_24h`, `time_to_next_outage_minutes`, and `next_outage_duration_minutes` only from incident onsets inside the local service interval. Every feature event time MUST be strictly earlier than the prediction timestamp. Rows already inside any fetched active outage MUST be excluded, including when the outage onset predates service entry, and eligible non-outage device-hours from the same mapped population SHALL be retained as controls. Raw, service-overlap, and service-valid-onset incident attrition SHALL be reported separately.

The fixed outcome observation boundary SHALL equal the last scheduled hourly anchor plus the maximum 24-hour label horizon; the source query MUST run no earlier than that boundary. The formal risk interval SHALL remain the half-open interval from onset through `max(DURATION_MINUTES, 1 minute)`, capped at the boundary; mutable or stale closure status MUST NOT redefine that frozen interval. An incident duration SHALL be closure-verified only when `status = CLOSED`, `is_closed = TRUE`, `closed_at` lies from onset through the boundary, and the reported recovery is no later than the boundary. A reported recovery after the boundary is administratively right-censored; a recovery no later than the boundary without qualifying closure evidence is closure-unverified missingness, not proof that the incident survived to the boundary. In both cases the onset label and time-to-onset remain valid, the reported interval still governs active-outage exclusion, and `next_outage_duration_minutes` remains missing. Duration association SHALL use only future-positive rows with a closure-verified duration (or a separately predeclared censoring method), and MUST NOT analyze a capped lower bound as an exact duration or generalize complete-case correlation to all incidents.

For every earlier chronological split, the analysis SHALL purge an anchor when its inclusive maximum label horizon reaches or crosses the next split start. With the frozen 24-hour maximum horizon, a retained earlier-split anchor MUST satisfy `anchor + 24 hours < next split start`.

#### Scenario: Outage begins after prediction
- **WHEN** a mapped device's incident starts within a label horizon after the prediction timestamp
- **THEN** the corresponding future-outage label is positive and duration remains an outcome only

#### Scenario: Duration is censored or closure-unverified
- **WHEN** an incident onset is observed inside the label horizon but qualifying closure and recovery are not both established by the fixed boundary
- **THEN** the future-onset label remains positive, the full duration is missing, and the row is excluded from ordinary complete-case duration correlation

#### Scenario: Closure status and reported interval disagree
- **WHEN** an incident remains marked active after its reported formal interval has ended
- **THEN** the reported onset-plus-duration interval still determines active-outage exclusion, while the inconsistent closure state prevents its duration from entering complete-case correlation

#### Scenario: Outage carries into service
- **WHEN** a valid formal incident begins before service entry but remains active at an otherwise eligible in-service anchor
- **THEN** the anchor is excluded as already inside an active outage even though that onset is not a future-label candidate

#### Scenario: Measurement occurs at or after the anchor or target onset
- **WHEN** an ACS value, recovery field, incident field, or derived value is not available strictly before prediction or leaks the target onset
- **THEN** it is excluded from predictors and the leakage audit fails if it reaches the frame

#### Scenario: A future label window touches a later split
- **WHEN** a training or validation anchor plus 24 hours is equal to or later than the next partition start
- **THEN** that anchor is purged before labels are exposed, even if its realized label is negative

### Requirement: Chronological evaluation remains a feasibility pilot
The analysis SHALL use the frozen 60%/20%/20% chronological partitions. Learned eligibility, preprocessing, and coefficients SHALL use training only; any operating threshold SHALL use validation only; test SHALL be evaluated once. The linear elapsed-time term SHALL be capped at the last training anchor before validation/test scoring. A regularized logistic regression using frozen ACS features SHALL be compared on identical rows with both a constant training-prevalence prediction and a frozen time-only logistic model. It SHALL report PR-AUC, precision, recall, specificity, alert rate, Brier score, calibration, warning time, and conditional fitted-model uncertainty from resampling test devices for incremental held-out performance at 6-hour and 24-hour horizons. It SHALL also report a within-device ranking diagnostic that macro-averages the per-device test difference `AP(ACS + time) - AP(time only)` among devices containing both classes, with a device bootstrap and at least 20 eligible devices. Duration SHALL be analyzed separately among future-positive rows.

#### Scenario: A chronological partition has one outcome class
- **WHEN** a required partition contains only outage or only non-outage observations
- **THEN** predictive evaluation stops and reports insufficient chronological validation

#### Scenario: Held-out independent-device support is inadequate
- **WHEN** validation or test has fewer than 20 feature-observed devices, fewer than 10 devices contributing positive rows, or fewer than 10 devices contributing control rows for a horizon
- **THEN** the horizon is reported as inadequate for a predictive claim and its boundary is not changed post hoc

#### Scenario: ACS features appear to improve held-out prediction
- **WHEN** frozen ACS features improve a predeclared held-out metric over both no-ACS comparators with conditional device-resampled uncertainty that supports the direction, Brier uncertainty supports improvement, absolute calibration gap is at most 0.05, ten-bin ECE is at most 0.10, and the minimum-supported within-device diagnostic supports improvement
- **THEN** the report states the effect, both device-level diagnostics, calibration, coverage, drift, mapping selection, and `FEASIBILITY_PILOT_ONLY`, and does not call the feature confirmed

#### Scenario: Only pooled row ranking improves
- **WHEN** pooled held-out PR-AUC improves but calibration or within-device ranking does not
- **THEN** the result may be labelled only `CURRENT_WINDOW_EXPLORATORY_ROW_RANKING_SIGNAL_ONLY`, not a useful or operational predictor

#### Scenario: ACS features do not improve prediction
- **WHEN** held-out performance does not improve or effects are unstable across time or partners
- **THEN** the report concludes that useful predictive ACS signal was not demonstrated and retains the null result

### Requirement: Derived event-aligned analysis remains separate and exploratory
A derived event-aligned diagnostic MAY read the immutable canonical model frame without querying any source database. It SHALL pin the parent frame and audit hashes, preserve the parent chronological splits, and leave the canonical artifacts and decision unchanged. It SHALL be labelled `CURRENT_WINDOW_POST_HOC_EVENT_ALIGNED_DIAGNOSTIC_ONLY` under `FEASIBILITY_PILOT_ONLY` and MUST NOT be represented as independent confirmation.

For v2, the diagnostic SHALL reconstruct onset as `prediction_time_utc + time_to_next_outage_minutes`, normalize to nearest-second source precision, and key events by pseudonymous device plus reconstructed onset. It SHALL assert strictly prior telemetry, `prediction_time_utc < onset`, split isolation, and `reboot_count_24h >= reboot_count_6h`, and SHALL NOT use duration or closure fields. Its descriptive lead windows SHALL be `(0,1]`, `(1,3]`, `(3,6]`, `(6,12]`, and `(12,24]` hours, requiring respectively at least 1, 1, 2, 3, and 6 observed hourly rows per event. Its primary contrast SHALL be the hourly-row feature mean in `(0,6]` minus the mean in `(6,24]`, requiring at least three nonmissing near rows and nine nonmissing far rows per feature/event. It SHALL average event contrasts within device and then give devices equal weight within split.

The v2 family SHALL contain exactly `recent_reboot_1h = I(boot_age_hours <= 1)` with missingness preserved, `reboot_count_0_6h = reboot_count_6h`, `reboot_count_6_24h = reboot_count_24h - reboot_count_6h`, `reboot_rate_acceleration = reboot_count_6h/6 - (reboot_count_24h-reboot_count_6h)/18`, `reboot_recency_hours = -boot_age_hours` with missingness preserved, and `inform_staleness_minutes`. The mean effect SHALL use a two-sided mean test and device-bootstrap mean CI; Wilcoxon SHALL remain only a rank-location sensitivity. Multiplicity SHALL be adjusted across all six transformations separately in each chronological split for the primary device analysis and the device analysis after excluding every onset shared by multiple devices. Reconstructed-onset-cluster aggregation SHALL be descriptive only because devices recur across onset clusters and MUST NOT enter the candidate gate.

#### Scenario: An event-aligned pattern appears in the current window
- **WHEN** a near/far transformation appears associated with proximity to current-window outage cases
- **THEN** it remains hypothesis-generating, cannot change the canonical v14 decision, and cannot be called predictive, specific, causal, fleet-generalizable, or confirmed without matched non-outage controls and an untouched future window

#### Scenario: A derived diagnostic bundle is written
- **WHEN** the source-free v2 command completes
- **THEN** its aggregate bundle pins parent hashes, script and protocol, seed and bootstrap method, all six tested results and nulls, row/event/device attrition, shared-onset sensitivities, privacy assertions, empty command-local query IDs, software versions, and output hashes

### Requirement: Untouched future confirmation
No parameter SHALL be called a confirmed outage predictor from the current overlap. Confirmation SHALL require the unchanged mapping, whitelist, reducers, freshness rules, preprocessing, model, threshold, and metrics to be applied to an untouched period after 2026-08-10.

#### Scenario: Current-window analysis completes
- **WHEN** all current source, mapping, dataframe, and evaluation gates pass
- **THEN** the strongest permitted status remains `FEASIBILITY_PILOT_ONLY`

### Requirement: Identifiers and sensitive data are prohibited
Mobile numbers, IP addresses, SSIDs, raw device identifiers, incident identifiers, passwords, usernames, and complete ACS parameter payloads MUST NOT appear in the modelling dataframe, feature results, or reports. Partner and direct identifiers MAY be used only in memory for joining, grouping, deduplication, splitting, and aggregate stability analysis. Exported device keys SHALL be contiguous run-local pseudonyms, and every partner group with fewer than five observed devices SHALL be collapsed into `OTHER`.

#### Scenario: Sensitive fields exist upstream
- **WHEN** an input source contains sensitive or identifying fields
- **THEN** outputs retain only run-local sequential device keys and approved coarse groups, and a schema audit confirms prohibited fields are absent before writing

### Requirement: Reproducible aggregate outputs are gate-aware
Every run SHALL produce a machine-readable aggregate audit and concise Markdown report with source windows, source counts and artifact hash, mapping coverage, ambiguity counts, query IDs and SQL hashes, timezone assumption, whitelist and reducer version, gate thresholds, split boundaries, telemetry/incident/exclusion counts, software versions, output counts and artifact hashes, and status. `model_frame.csv.gz` and `feature_results.csv` SHALL be written only when their upstream gates authorize them. Mandatory-gate failure SHALL return a non-zero exit status.

#### Scenario: A mandatory gate fails
- **WHEN** source readiness, mapping reproduction, mapped-training eligibility, leakage, privacy, or chronological class feasibility fails
- **THEN** the aggregate audit and report identify the failed gate, and no unauthorized downstream artifact is written
