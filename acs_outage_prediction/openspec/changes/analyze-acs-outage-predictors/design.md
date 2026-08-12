## Context

See `proposal.md` for motivation and `specs/acs-outage-feasibility-analysis/spec.md` for the behavior contract. A prior full-table check observed 816,923 rows for 660 devices from 2026-05-31 through 2026-08-10; the live source had reached 826,578 rows by a later 2026-08-11 check. Mutable whole-table row totals are provenance, not a reproducibility gate; frozen-window counts and query IDs are the decision evidence. ACS `device_id` and `serial_number` do not directly match the formal outage `DEVICE_ID`; an exact two-inventory bridge is required.

The corrected geographic pilot authorized only a coverage/freshness/within-device-variance audit. Its decision rule was revised after a provisional result, so neither that pilot nor this overlapping ACS period can establish a confirmed predictor. The status of this change is `FEASIBILITY_PILOT_ONLY` until a locked analysis is repeated on an untouched future window. The ACS result does not decide the separate question of whether Snowflake optical telemetry predicts outages.

## Reconciled evidence as of 2026-08-11

### Canonical mapping audit

The only accepted route is:

`ACS serial_number`
→ `PROD_DB.MASTER_DB_READ_DBO.T_DEVICE.PON_SERIAL`
→ master `DEVICE_ID`
→ `PROD_DB.PUBLIC.T_DEVICE.DEVICE_ID`
→ `COALESCE(PUBLIC.T_DEVICE.LONG_NAS_ID, PUBLIC.T_DEVICE.NASID)`
→ strict active `PROD_DB.DBT.ACTIVE_BASE` `CUSTOMER_V2.NASID`
→ formal outage `INCIDENT_IMPACTED_DEVICE.DEVICE_ID`.

Case, surrounding whitespace, and non-alphanumeric serial separators may be normalized; no suffix, edit-distance, substring, or other fuzzy match is admissible. The aggregate audit found:

| Gate stage | Devices |
|---|---:|
| ACS population | 660 |
| Unique, non-colliding ACS serial | 644 |
| Exact master `PON_SERIAL` match | 251 |
| One-to-one public-device/NAS bridge | 249 |
| Strict active `CUSTOMER_V2` cohort | 81 |
| Formal outage observation in the overlap | 77 |

Two ambiguous mappings were excluded. The canonical in-memory mapping invariant was 81 ACS keys = 81 warehouse devices = 81 NAS IDs; direct identifiers were not exported. Thus the canonical mapped cohort is only 12.27% of the ACS population and is not fleet-representative.

Snowflake query IDs for the aggregate evidence are:

- cross-inventory audit: `01c6512b-0002-7659-0009-01fa2671b17e`
- canonical mapping: `01c6512c-0002-7674-0009-01fa2671abbe`
- mapped outage/hour audit: `01c6512c-0002-7749-0009-01fa2671c18a`

### Chronological feasibility audit

Hourly anchors span 2026-06-02 00:00 UTC through 2026-08-10 10:00 UTC and use fixed 60%/20%/20% chronological partitions. A provisional aggregate outcome-only audit, before the final feature/risk-set construction, found:

| Split | Devices | Eligible hours | 6 h positive / control | 24 h positive / control |
|---|---:|---:|---:|---:|
| Train | 42 | 32,012 | 1,341 / 30,671 | 3,653 / 28,359 |
| Validation | 59 | 9,693 | 1,649 / 8,044 | 4,294 / 5,399 |
| Test | 61 | 14,772 | 1,851 / 12,921 | 5,397 / 9,375 |

Both outcome classes exist, but 24-hour prevalence changes from 11.4% in train to 44.3% in validation and 36.5% in test, while observable devices change from 42 to 59 to 61. This established severe drift but is not the final model estimand: the corrected complete-service, split-boundary, strict-prior-telemetry, and active-outage risk-set counts below supersede these provisional row labels.

### ACS source-readiness audit

The source-only audit read only whitelisted leaves for three half-open, indexed windows; it did not join or inspect outcomes:

- early: `[2026-06-02 00:00, 2026-06-05 00:00)` UTC
- middle: `[2026-07-05 00:00, 2026-07-08 00:00)` UTC
- late: `[2026-08-07 00:00, 2026-08-10 00:00)` UTC

The audit read 82,867 snapshots: 40,079 early, 30,223 middle, and 12,565 late. Its aggregate results were:

| Source measure | Early | Middle | Late |
|---|---:|---:|---:|
| ordinary JSON leaf fresh coverage | 0.53050 | 0.00589 | 0.00350 |
| WiFi signal fresh coverage | 0.20472 | 0.00122 | 0.00199 |
| PPP field fresh coverage | 0.45558 | 0.00589 | 0.00350 |
| `$._lastInform` fresh coverage | 0.55767 | 0.71515 | 0.57159 |
| `$._lastBoot` usable coverage | 0.99963 | 0.99977 | 0.99960 |
| devices with a `$._lastBoot` transition, share | 0.96010 | 0.96094 | 0.72174 |

`$._lastInform` had zero IQR and zero varying devices despite its coverage. `$._lastBoot` was usable for 590 devices. Populated flat `uptime_s`, CPU, memory, temperature, and `param_count` fields each had zero devices with within-device variation in the late window; flat optical RX/TX coverage was zero. Thus every ordinary nested value had less than 1% fresh coverage in the middle and late windows, and only root `$._lastBoot` passed the source-only screen.

That pass establishes source availability, not predictor eligibility. A last-boot timestamp is deliberately stable between boots and must be represented as boot age and boot transitions, not rejected for failing a 24-hour leaf-freshness rule or treated as a continuously refreshed measurement. Mapped-cohort and training-period eligibility remained pending at that source-audit stage and were resolved by the final run below.

### Separate non-ACS optical telemetry inventory

The zero optical coverage above applies only to the flat and JSON fields in MySQL `acs_raw_dump`. The Quality OS source contract independently identifies five-minute optical pings feeding `HOURLY_DEVICE_PING_INFLUX`, and the Snowflake inventory confirms these separate sources:

| Source | Grain / useful fields | Role in a future outage study |
|---|---|---|
| `PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX` | device-hour; direct `DEVICE_ID`; hour start/end; optical minimum/average/maximum; ping counts; `UPDATED_AT` and `INSERTED_AT` | Primary point-in-time optical source, subject to owner confirmation of timestamp semantics |
| `PROD_DB.DBT.HOURLY_DEVICE_PING_INFLUX` | current device-hour materialization with the same core optical and ping fields | Source reconciliation only for historical replay because its `INSERTED_AT` reflects rematerialization |
| `PROD_DB.DBT.STG_IX_PING_INFLUX` | projection of the DBT hourly table with `DEVICE_FAMILY`; omits hour end, missed-ping total, and availability timestamps | Cohort/family audit only; not an independent feature source |
| `PROD_DB.DBT.FCT_OPTICAL_SIGNAL` and `AVG_OPTICAL_SIGNAL` | NAS-day derivatives; `AVG_OPTICAL_DBM`, `OPTICAL_READINGS`, `OPTICAL_HEALTH` | Reconciliation only; too coarse and not independent for a six-hour claim |
| `PROD_DB.PUBLIC.ROUTER_DETAILS_AUDIT` | change history by device/NAS; `rx_power`, `tx_power`, `change_time` | Secondary historical source after cadence and sentinel auditing |
| Quality Postgres `telemetry_rollup_records` | CSP-day `optical_numerator`/`optical_denominator` | Aggregate monitoring only; cannot label or rank individual devices |
| TAS `install_execution_candidates.optical_power_dbm` | one mutable installation-candidate value plus source | Installation evidence only; not continuous pre-outage telemetry |

The local Snowflake inventory snapshot recorded roughly 165.5 million DBT hourly-ping rows, 9.0 million `FCT_OPTICAL_SIGNAL` rows, 7.9 million `AVG_OPTICAL_SIGNAL` rows, and 35.9 million `ROUTER_DETAILS_AUDIT` rows as of 2026-06-27. These mutable counts demonstrate material availability but are not frozen coverage or reproducibility gates.

No source in this section was used by v14. The read-only 2026-08-12 audit found 12,987,351 public/DBT hourly rows across 86,279 devices in the seven complete days ending 2026-08-11. Of 280,677 in-window formal incident-device onsets, 99.62% had at least one public hourly row in the prior 24 hours that was already inserted at onset; a two-hour action gap retained 99.33%. The direct join is therefore feasible.

The signal-quality audit is less favorable. Over the same week, 41,793 of 86,279 devices (48.44%) had constant `OPTICAL_AVG`; 9,416 were constant at `-8`; the median device exposed only two distinct values; completely missed hours do not appear as rows; 13 device-hour keys had conflicting NAS values; and a small set of physically implausible optical values remains unresolved. `OPTICAL_IN_RANGE_PINGS` also exceeded received-ping counts on inspected rows and is excluded until its contract is explained. A point-in-time matched screen of roughly 40,000 devices found only about `-0.03 dB` case-control separation in six-hour optical mean at two-to-three and five-to-six hour leads, identical `-21 dBm` medians, and only 0.21--0.28 percentage-point higher out-of-range share. These are exploratory source-audit results, not held-out model performance, and support only `FEASIBILITY_PILOT_ONLY`.

A future optical pilot must use the public hourly table, require both normalized hour end `<= prediction_anchor` and `INSERTED_AT <= prediction_anchor`, build an expected active-device-hour spine, exclude conflicting duplicate keys and unresolved synthetic/sentinel families, and use direct `DEVICE_ID` joins. It must first establish whether the formal router-outage detector is generated from the same ping or optical feed. Ping-loss fields are only a labelled availability comparator until independence is proven; if optical participates in label generation, an independent outage truth source is required. All data through 2026-08-11 is development/audit data, and confirmation requires a new chronological holdout.

### Actual 15-feature optical development model

`optical_outage_model.py` executed one development-only regularized logistic run over six weeks ending 2026-08-11. It used three-hour anchors, a stable 5% device sample, a four-week/one-week/one-week chronological split, and the formal onset target `(2h, 6h]`; anchors inside an outage or with an onset in the two-hour action gap were excluded. The 15 frozen inputs were five time, five availability/ping/outage-history, and five optical features. The point-in-time frame contained 886,929 rows from 3,372 devices; the test split contained 148,309 rows, 8,705 positives, and 3,086 devices.

| Test model | Features | PR-AUC | Brier | ECE | Alert rate | Precision | Recall |
|---|---:|---:|---:|---:|---:|---:|---:|
| time only | 5 | 0.0607 | 0.0557 | 0.0193 | 5.43% | 6.22% | 5.76% |
| time + non-optical | 10 | 0.2143 | 0.0510 | 0.0065 | 3.57% | 34.27% | 20.84% |
| time + optical | 10 | 0.0624 | 0.0557 | 0.0194 | 4.66% | 6.38% | 5.07% |
| combined | 15 | 0.2147 | 0.0510 | 0.0065 | 3.60% | 34.43% | 21.09% |

The primary incremental comparison is combined minus non-optical. Its test PR-AUC delta was only `+0.00042`, with a device-bootstrap 95% interval from `-0.00007` to `+0.00098`; Brier improved by `-0.000011`; and equal-device AP improved by `+0.00073` even though only 23.4% of mixed-class devices improved and the median device delta was zero. Optical plus time also failed the device-aware check: pooled PR-AUC improved by `+0.00165`, but equal-device AP changed by `-0.00780` (95% interval `-0.01068` to `-0.00538`). The decision is therefore `CURRENT_DEVELOPMENT_POOLED_OPTICAL_LIFT_ONLY`, not an incremental optical predictor. The much larger non-optical result is detector-replication sensitivity until ping-label independence is proven.

The reproducible aggregate bundle is `outputs/optical_outage_model_2026-08-12_v1/`. Its Snowflake query ID is `01c65313-0002-7674-0009-01fa267ef7fa`; source writes were not attempted. These already-inspected dates cannot satisfy future confirmation.

### Final corrected feasibility run

The canonical completed artifact is `outputs/acs_outage_feasibility_2026-08-11_v14/`. Earlier `acs_correlation_pilot` and feasibility v1-v13 artifacts are superseded diagnostics or recorded gate/runtime/reporting failures; none is canonical or supports a predictor claim. In particular, v8/v10 reproduced the same primary onset result but retained query-time-mutable duration values, v12 deliberately stopped when an over-conservative status interpretation broke the frozen overlap gate, and v13 reproduced the final science but incompletely named/reported the two duration-unavailability reasons. The v14 run reproduced the pinned source audit, all 660→644→251→251→249→81 identity stages, and the original 77-device service-valid-onset overlap estimand. Its Snowflake query IDs are `01c651e4-0002-765e-0009-01fa2676f7a2` (mapping) and `01c651e9-0002-7674-0009-01fa2676e996` (formal incidents).

The v14 bundle is bound to analysis-script SHA-256 `7d85f3cbab6574a1433a909c1de2e91651af12487cf1c84e1eaf64ca1d33c4ef`; its model-frame, feature-result, and report SHA-256 values are `e71132a682a8ba5aa9699c83cb1db364f36531997570621443b28c8958f370de`, `b8afd3a422634323a610ffabfcde1e0ee8841a8065083f49e49e338a43bfc705`, and `7a5636177ac9769aeeb52366354818f5016373239fcbc0fa06e0e67a6cd9aa56`.

All 81 mapped devices had bounded telemetry: 114,679 rows, 99.939% valid `$._lastBoot`, and 6,922 observed boot-timestamp changes. Of 135,027 candidate device-hours, 120,549 had a complete 24-hour in-service horizon; 66,455 also had a strictly prior inform within 24 hours. The maximum-horizon split purge removed 2,369 rows, active-outage exclusion removed 9,314, and the final frame contains 54,772 device-hours from 71 devices. The frozen mapped-training gate retained boot age, 6-hour and 24-hour true reboot counts, and inform staleness; the 1-hour reboot count failed its non-zero-IQR gate.

Temporal outcome drift remains severe:

| Split | Rows | Devices | 6 h prevalence | 24 h prevalence |
|---|---:|---:|---:|---:|
| Train | 31,438 | 42 | 0.0400 | 0.1077 |
| Validation | 8,562 | 58 | 0.1713 | 0.4504 |
| Test | 14,772 | 61 | 0.1258 | 0.3654 |

The combined ACS-plus-time model improves pooled test-row ranking over the frozen time-only model, conditionally resampling test devices: PR-AUC delta 0.0361, 95% CI `[0.0059, 0.0660]` at 6 hours and 0.1016, 95% CI `[0.0426, 0.1533]` at 24 hours. This does not establish a useful parameter:

- 6-hour mean equal-device AP delta is -0.0311, 95% CI `[-0.0628, -0.0045]`; 21/57 devices improve.
- 24-hour mean equal-device AP delta is -0.0013, 95% CI `[-0.0294, 0.0258]`; 21/48 devices improve.
- Calibration fails at both horizons: ACS mean probability/test prevalence is 0.2594/0.1258 at 6 hours and 0.6466/0.3654 at 24 hours; ten-bin ECE is 0.1527 and 0.2818.
- Zero of eight individual feature-by-horizon tests survive Benjamini-Hochberg adjustment; minimum device-level q is 0.835. No parameter has multi-horizon, time-stable, partner-stable evidence.
- The secondary duration diagnostic is conditional on closure verification: 83 future-positive device-hours across seven devices have missing duration, 56 test devices remain in the complete-case diagnostic, and the minimum duration q is 0.389. It is not an all-incident duration estimate.

The supported decision is therefore `CURRENT_WINDOW_EXPLORATORY_ROW_RANKING_SIGNAL_ONLY` under `FEASIBILITY_PILOT_ONLY`: the combined feature set has a pooled row-ordering association, but no individual ACS parameter with useful, calibrated, device-consistent predictive evidence was found.

### Separate exploratory event-aligned window diagnostic

The separately versioned `acs_window_analysis.py` command reads only the immutable v14 model frame and audit; it makes no database query and does not modify the canonical run. Its completed bundle is `outputs/acs_window_analysis_2026-08-11_v2/`, with decision `CURRENT_WINDOW_POST_HOC_EVENT_ALIGNED_DIAGNOSTIC_ONLY`. The draft v1 bundle reproduced the same primary null result but is superseded because it gave onset clusters invalid independent-unit inference, exported an absolute local path in its audit, and imported unpinned helper code. V2 makes equal-onset aggregation descriptive only, uses the device as the inferential unit, excludes every onset shared by multiple devices for its second device-level sensitivity, sanitizes provenance, and is self-contained. This derived diagnostic is case-only and cannot satisfy task 6.3 or change v14's `CURRENT_WINDOW_EXPLORATORY_ROW_RANKING_SIGNAL_ONLY` decision.

The protocol was frozen before its feature effects were inspected. A pre-run statistical audit replaced an earlier median sketch with the mean-based estimand below before any effect result was read. It reconstructs onset as `prediction_time_utc + time_to_next_outage_minutes`, normalizes the float reconstruction to source-second precision, and keys a device event by `(device_key, onset)`. Descriptive windows are `(0,1]`, `(1,3]`, `(3,6]`, `(6,12]`, and `(12,24]` hours before onset, requiring respectively at least 1, 1, 2, 3, and 6 observed hourly rows per event. The primary same-event contrast is the hourly-row mean in the near window `(0,6]` minus the hourly-row mean in the far window `(6,24]`; exactly six hours belongs only to near. Each feature/event must supply at least three nonmissing near rows and nine nonmissing far rows. Event differences are averaged within device, then devices receive equal weight within each chronological split.

The six fixed transformations of the four selected v14 numeric features are:

| Diagnostic signal | Frozen definition |
|---|---|
| `recent_reboot_1h` | `I(boot_age_hours <= 1)`, preserving missingness; this is a reported-recent-boot indicator, not the ineligible v14 1-hour transition count |
| `reboot_count_0_6h` | `reboot_count_6h` |
| `reboot_count_6_24h` | `reboot_count_24h - reboot_count_6h`, after asserting the difference cannot be negative |
| `reboot_rate_acceleration` | `reboot_count_6h / 6 - (reboot_count_24h - reboot_count_6h) / 18` reboots/hour |
| `reboot_recency_hours` | `-boot_age_hours`, preserving missingness |
| `inform_staleness_minutes` | the existing strictly-prior staleness value |

The primary inference is a two-sided one-sample test of the equal-device mean paired with a 2,000-draw device-bootstrap percentile CI for that mean. Benjamini-Hochberg adjustment covers all six transformations separately in train, validation, and test. The same device-level mean test, bootstrap CI, and six-test adjustment are repeated after excluding every onset shared by multiple devices; Wilcoxon signed-rank results with fixed zero/tie handling are retained only as rank-location sensitivities. Equal-onset averages are descriptive because the same devices recur across onset clusters. A temporally stable event-aligned candidate must have one training direction and, in every split for both the primary device analysis and singleton-onset device sensitivity, adjusted mean-test `q < 0.05` and a 95% bootstrap mean CI excluding zero in that direction.

The run aligned 12,640 future-positive rows from 68 devices to 949 reconstructed device events and 891 onset clusters; 48 onset clusters were shared by two to four devices. There were 646 events with at least one row in both broad periods and 446 events across 67 devices met the frozen 3/9 support rule for every transformation. No transformation passed the frozen stability gate. Several validation-only patterns were strong, but training did not support them. Test inform staleness increased by an equal-device mean 52.74 minutes near onset (95% bootstrap CI `[25.49, 85.39]`, BH `q = 0.0098`) while it decreased in train and validation, a sign reversal incompatible with a stable escalation signal. No result changes the v14 conclusion.

The diagnostic is bound to parent audit SHA-256 `7092265955d15b4ad7bdbf575aa8819972dc5746eace0f5c3f9bcf88a8bc0ed0`, parent compressed frame SHA-256 `e71132a682a8ba5aa9699c83cb1db364f36531997570621443b28c8958f370de`, parent decompressed frame SHA-256 `e84d5e4096ced722f0710606c78b84d0dc94688dca69a8a9f62f9b2a23e4e2ae`, and script SHA-256 `2c5f7f8c1d25bb41190a5cf733b3fc19173ec3abb2bdd39abb8b9bbd58a1369f`. V2 result, trajectory, report, and audit SHA-256 values are `518f641f78a41ebb02cb7808923bf09a300707343245c5b45ebae98ccce526d7`, `d03b4c36de49c71ecaa865f8d70a2d1a408e2b77292b0a4602b7f8d64633046e`, `831f5be5c6c1a4afcf8385d0b9265067796838da2ae9c2ada2d55c57c4378ae5`, and `b5b62e13d0420040f502b39e9c9dc6141ae955d2258d4377763582068fcd15eb`. Because the frame omits the formal incident identifier, reconstructed onset is only an incident-cluster proxy. More importantly, this case-only near/far comparison has no matched non-outage control and cannot establish specificity, prediction, causality, fleet generalization, or confirmation. Reused devices across splits measure temporal stability, not independent replication.

## Goals / Non-Goals

**Goals:**

- Close identifier, timestamp, feature-readiness, and chronological-feasibility questions before materializing row-level data.
- Keep the pilot bounded, read-only, leakage-safe, and reproducible from aggregate evidence.
- Freeze a small parameter whitelist and stop when it supplies no usable variation.
- If a feature survives the mapped-training gate, compare it with a declared no-ACS baseline on chronological held-out data.
- Export only pseudonymous rows and aggregate evidence.

**Non-Goals:**

- A production ETL job, feature store, alerting service, dashboard, or general ACS parser.
- Fuzzy entity resolution, causal claims, or optimization over a model grid.
- Searching more JSON parameters after seeing an unhelpful result.
- Treating partner, device, mobile, IP, SSID, incident, or raw inventory identifiers as predictors.

## Decisions

### 1. Use three sequential gates

The workflow is:

1. source readiness on unlabeled, bounded ACS windows;
2. exact mapping plus mapped-training feature eligibility;
3. conditional device-hour frame and chronological model comparison.

Each phase records an aggregate result and later phases stop when the prior mandatory gate fails. The source-readiness gate cannot be weakened after outcomes are inspected. A path that fails freshness or variation remains a documented null result rather than a prompt to search the payload.

The executable analysis validates the completed source-audit artifact itself before connecting to either outcome source: its SHA-256 is pinned, and status, schema/configuration, exact eligible-feature lists, three frozen windows, 82,867 sampled rows, timezone, thresholds, and privacy result must reproduce. A hard-coded source-pass flag is not sufficient.

### 2. Freeze the exact identifier bridge

Use only the canonical route recorded above. Each leg must be non-null and one-to-one. Rebuild ACS serial identities from all source rows before the frozen analysis end, while keeping telemetry extraction bounded to the declared lookback and analysis window. The run stops unless every frozen stage count reproduces exactly: 660 source devices, 644 unique non-colliding serials, 251 exact and one-to-one master matches, 249 public/NAS bridges, 81 strict customer mappings, and 77 mapped devices with a service-valid formal outage onset overlapping the base analysis interval. Direct identifiers may exist in process memory only long enough to validate and execute the join; output rows use contiguous run-local sequential device keys. The 81-device mapping, exclusions, coverage, and three query IDs are part of the frozen audit record.

### 3. Treat outage NTZ values as UTC-valued, with the assumption explicit

MySQL `inform_time` is UTC based on the database session check. The executable sets its MySQL session to `+00:00` before opening the read-only transaction. For this pilot, public incident `FIRST_FAIL_TIMESTAMP` is treated as a UTC-valued `TIMESTAMP_NTZ` and is localized to UTC without a 5.5-hour conversion. `ACTIVE_BASE.LOCATION_START_TIME` and `PLAN_EXPIRY_TIME` are Asia/Kolkata-local NTZ values, so UTC prediction anchors and UTC incident onsets are explicitly converted to Asia/Kolkata before service-interval comparisons. This is an explicit source-contract assumption supported by the corrected outage audit, not an independently proven timezone fact; the run must record it and perform boundary checks.

Use `FIRST_FAIL_TIMESTAMP` for onset and `DURATION_MINUTES` for duration. `INCIDENT_IMPACTED_DEVICE.CREATED_AT` is record creation time and must not be used as failure onset. If the UTC-valued NTZ convention cannot be upheld for a later source version, stop before label construction.

### 4. Freeze the flat and P1 source whitelist

The only allowed flat columns are:

| Flat field | Allowed role |
|---|---|
| `uptime_s` | candidate uptime/reboot summary |
| `cpu_pct` | candidate CPU summary |
| `memory_free_kb` | candidate memory summary |
| `memory_total_kb` | denominator for memory fraction; not a size proxy outcome |
| `temperature_c` | candidate temperature summary |
| `optical_rx_dbm` | candidate receive-power summary |
| `optical_tx_dbm` | candidate transmit-power summary |
| `inform_time` | ordering, strict-prior selection, and staleness only |
| `param_count` | candidate snapshot-completeness summary |

The only allowed `params_json` reads are the following sixteen paths. `N` means all present numeric object keys. For ordinary leaf objects, read only `_value` and `_timestamp` and apply the declared within-snapshot reducer.

| # | Whitelisted path | Within-snapshot reducer / semantics |
|---:|---|---|
| 1 | `InternetGatewayDevice.DeviceInfo.MemoryStatus.Free` | minimum numeric value |
| 2 | `InternetGatewayDevice.DeviceInfo.ProcessStatus.CPUUsage` | maximum numeric value |
| 3 | `InternetGatewayDevice.DeviceInfo.TemperatureStatus.TemperatureSensor.N.Value` | maximum numeric value across sensors |
| 4 | `InternetGatewayDevice.DeviceInfo.UpTime` | minimum numeric value |
| 5 | `InternetGatewayDevice.GX_OntOpticalParam.RXPower` | minimum numeric value |
| 6 | `InternetGatewayDevice.GX_OntOpticalParam.TXPower` | maximum numeric value |
| 7 | `InternetGatewayDevice.GX_OntOpticalParam.TransceiverTemperature` | maximum numeric value |
| 8 | `InternetGatewayDevice.GX_OntOpticalParam.BiasCurrent` | maximum numeric value |
| 9 | `InternetGatewayDevice.GX_OntOpticalParam.SupplyVoltage` | minimum numeric value |
| 10 | `InternetGatewayDevice.LANDevice.N.WLANConfiguration.N.TotalAssociations` | sum numeric values across WLAN instances |
| 11 | `InternetGatewayDevice.LANDevice.N.WLANConfiguration.N.AssociatedDevice.N.SignalStrength` | median numeric value across associated devices |
| 12 | `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.ConnectionStatus` | audit distinct categories; if eligible, collapse to all `Connected` versus any non-connected |
| 13 | `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.LastConnectionError` | audit distinct categories; if eligible, collapse to all `ERROR_NONE` versus any error |
| 14 | `InternetGatewayDevice.WANDevice.N.WANConnectionDevice.N.WANPPPConnection.N.Uptime` | minimum numeric value |
| 15 | `_meta._lastInform` (actual JSON root `$._lastInform`) | timestamp used only for call-home age/freshness |
| 16 | `_meta._lastBoot` (actual JSON root `$._lastBoot`) | timestamp converted to boot age and boot-transition indicators |

Passwords, usernames, mobile numbers, IPs, SSIDs, raw IDs, write/control parameters, and complete JSON payloads are prohibited. The whitelist is not expandable based on correlations observed in this pilot.

### 5. Apply freshness and train-only eligibility before feature construction

An ordinary nested leaf is fresh only when its own `_timestamp` is parseable, is not after the ACS `inform_time`, and is no more than 24 hours old at that inform. A current `$._lastInform` does not make other leaves current. `$._lastInform` itself must be no more than 24 hours old at the inform.

`$._lastBoot` is different: require a parseable timestamp not after the inform and a plausible boot age of at most ten years, then derive seconds since boot and whether the boot timestamp changed. Do not apply the ordinary 24-hour freshness cutoff to it. A monotonic increase in derived boot age is not independent measurement variation; only changes in the underlying boot timestamp indicate a new boot.

The mapped-training gate is frozen before validation or test outcomes are read. Feature eligibility uses training-period availability, freshness, and within-device variation only; it does not use apparent association with the outcome. After mandatory boundary and active-outage exclusions, the same gate is rerun only as a verification that every prelabel-selected feature remains eligible; it cannot add, rescue, or redefine a feature. The outcome is consulted only to confirm class-count feasibility and later fit the declared training model. A numeric candidate requires:

- fresh non-null coverage of at least 25% of eligible training anchors;
- at least 20 independent training devices with fresh values and at least three observations each;
- non-zero training IQR; and
- a non-zero within-device range in at least 10% of those devices.

A categorical candidate requires at least two levels, a non-dominant level of at least 1%, at least 10 training devices, and at least five within-device transitions. After features are frozen, coverage by outcome class and in validation/test is reported as a drift diagnostic, never used to rescue, remove, or redefine a feature.

### 6. Build a device-hour frame only after the gate passes

Create hourly anchors for mapped devices only when an ACS inform exists in the previous 24 hours and both the anchor and its complete 24-hour label horizon lie inside the service interval. Select the latest strictly prior snapshot. For the current frozen source result, derive boot age and 1-hour/6-hour/24-hour counts of actual changes in the underlying `$._lastBoot` timestamp, plus missingness and inform staleness. `inform_time` is not converted into call-count predictors. Do not mechanically generate every reducer for every field.

Fetch valid, non-deleted formal incidents over the bounded carry-in and label-tail interval before applying onset/service filtering. Use all fetched intervals to exclude anchors already inside an outage, including an incident that began before service entry but remains active afterward. Use only incident onsets inside the Asia/Kolkata-local service interval to build `outage_next_6h`, `outage_next_24h`, `time_to_next_outage_minutes`, and `next_outage_duration_minutes`. Report raw, service-overlap, and service-valid-onset attrition separately. Because the future interval includes its endpoint, purge every training or validation anchor for which `anchor + 24 hours >= next split start`; this single maximum-horizon purge protects both labels from cross-partition incidents. Keep one row per pseudonymous device and hour. A seven-day maximum-duration arm may be reported only as a declared sensitivity; it cannot replace the primary onset labels after results are seen.

Freeze the outcome observation boundary at the last scheduled anchor plus 24 hours (`2026-08-11 10:00 UTC` for the current window), and do not query before it elapses. The formal risk interval remains onset plus `max(DURATION_MINUTES, 1 minute)`, capped at the boundary; status does not extend or shorten it because the live audit found 15 January incidents still marked active after their reported intervals ended. Regard duration as closure-verified only when `STATUS = CLOSED`, `IS_CLOSED = TRUE`, `CLOSED_AT` lies between onset and the boundary, and the reported recovery is no later than the boundary. A reported recovery after the boundary is administratively right-censored; a recovery within the boundary without qualifying closure is instead closure-unverified missingness. Both retain onset labels and time-to-onset, set full duration missing, and stay out of the complete-case duration correlation. That correlation is descriptive only for the closure-verified subset, and future source backfills can change it unless the Snowflake snapshot or a privacy-safe outcome extract is frozen. This amendment corrects a reproducibility issue observed when six open incidents increased by exactly 31 minutes between two otherwise identical runs; it does not tune or alter the primary onset model. The privacy-safe stale-status diagnostic is Snowflake query `01c651d6-0002-7674-0009-01fa267692a6`; the `CLOSED_AT` clock-order diagnostic is `01c651d9-0002-765e-0009-01fa2676874a` (765,442 closed rows, zero closure times before onset, but still only an assumption about UTC-valued NTZ semantics).

### 7. Freeze evaluation before held-out outcomes are exposed

Use the fixed 60%/20%/20% chronological boundaries. Fit eligibility decisions, preprocessing, missing-value handling, coefficients, and any operating threshold on training data only. Use validation once for the predeclared operating threshold, then evaluate test once. Compare a regularized logistic regression using the frozen ACS features against two no-ACS comparators on identical rows: a constant training-prevalence prediction and one frozen time-only logistic model using calendar/time-trend terms. Cap the linear elapsed-time term at the final training anchor before scoring validation/test so chronological extrapolation cannot saturate probabilities. Report PR-AUC, precision, recall, specificity, alert rate, Brier score, calibration, and warning time for 6-hour and 24-hour horizons separately. Alongside a conditional fitted-model bootstrap that resamples test devices, macro-average `AP(ACS + time) - AP(time only)` across test devices that contain both classes and bootstrap those device-level deltas; at least 20 mixed-class devices are required. A useful-signal conclusion requires improvement over both comparators with conditional uncertainty, absolute calibration gap at most 0.05, ten-bin ECE at most 0.10, and positive within-device ranking support. Pooled row ranking alone is labelled exploratory row-ranking signal only and is not called unseen-device generalization.

Every split must contain both outcome classes. In addition, each held-out split must contain at least 20 feature-observed devices and at least 10 devices contributing positive rows and 10 contributing control rows for the relevant horizon. Failure is reported as inadequate independent-device support rather than repaired by changing boundaries.

For each surviving feature, report coverage, robust group summaries, an effect size with device-clustered uncertainty, Benjamini-Hochberg adjusted exploratory p-values, and time/partner stability. Partner agreement is not calculated unless at least two coarse groups each contribute five observed devices. Duration analysis is restricted to future outage-positive rows and reported separately. All conclusions are predictive associations, never causal effects.

No current-window result may be labeled a confirmed predictor. Confirmation requires the unchanged whitelist, reducers, freshness rules, mapping rules, preprocessing, model, threshold, and metrics to be applied to data collected after 2026-08-10 that was untouched during development.

### 8. Keep outputs aggregate and conditional

Always write an aggregate `audit.json` and concise `report.md`. Write `model_frame.csv.gz` and `feature_results.csv` only after their respective gates pass. The model frame contains contiguous sequential device keys and coarse partner groups only; any group with fewer than five observed devices is collapsed into `OTHER`. A schema/privacy check must reject direct identifiers, sensitive ACS fields, and raw JSON before any file is written. The audit records sanitized source-table names, source/SQL/output hashes, row and device attrition, incident and telemetry counts, boundary/active-outage exclusions, query IDs, and software versions.

Every report records the status `FEASIBILITY_PILOT_ONLY`, source windows, split boundaries, timezone assumption, whitelist version, gate thresholds, query IDs, software versions, output counts, null results, and cohort/drift limitations.

## Risks / Trade-offs

- [Selected mapping] Only 81/660 ACS devices reach strict `CUSTOMER_V2`; conclusions cannot be generalized to the fleet.
- [Severe temporal drift] Device availability and 24-hour prevalence change sharply; report split-level calibration and do not pool the held-out periods into a reassuring average.
- [Telemetry staleness] Ordinary nested paths fail source freshness and flat fields lack late variation; do not correlate stale constants.
- [`_lastBoot` semantics] Stability between reboots is expected. Use boot age and actual boot-timestamp transitions, not raw timestamp magnitude or artificial within-inform change.
- [Timezone assumption] `FIRST_FAIL_TIMESTAMP` is treated as UTC-valued NTZ but not independently proven; retain boundary checks and stop if the source contract changes.
- [Mutable duration] Open-incident `DURATION_MINUTES` changes with query time; pin the label-tail boundary, require closure by that boundary for complete duration, and analyze censored duration only with an explicitly declared censoring method.
- [Repeated device-hours] Split chronologically and calculate uncertainty by resampling devices, not rows.
- [Post-hoc development period] The geographic rule and ACS gates were refined on the present overlap; an untouched future window is required for confirmation.
- [Large ACS JSON source] Query only indexed bounded windows, the mapped cohort when authorized, and whitelisted leaves; never scan or export the full payload.

## Migration Plan

This is a new local, read-only analysis. Apply creates derived outputs only and does not alter source databases. Rollback consists of removing the analysis files and derived-output directory; OpenSpec artifacts remain as the decision record.
