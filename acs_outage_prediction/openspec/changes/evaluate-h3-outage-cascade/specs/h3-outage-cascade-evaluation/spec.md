## Purpose

Define a reproducible, privacy-safe retrospective test of whether a 10% early ping-miss episode in an H3-9 cell predicts a 70% same-cell strict telemetry-outage cascade within 60 minutes, including incremental optical contribution.

## ADDED Requirements

### Requirement: Exact eligible population and trigger episode

The analysis SHALL freeze the time-valid device denominator at every anchor, require at least 20 devices with at least 12 hours of service tenure, define early state as three latest missed five-minute opportunities without an existing twelve-slot outage, require at least `ceil(10% × N)` early devices and fewer than `ceil(10% × N)` strict-outage devices, and retain only the first trigger after 60 minutes without another raw trigger. Explicit zero-ping hours with null endpoint timestamps SHALL be valid; the absent-row interpretation SHALL be disclosed.

#### Scenario: A cell is already broadly in strict outage

- **WHEN** at least 10% of its frozen devices are already in a strict twelve-slot outage
- **THEN** the anchor is not an early cascade trigger

### Requirement: Frozen simultaneous same-cell target

The label SHALL be positive only when at least `ceil(70% × N)` of the frozen same-cell denominator are simultaneously in strict outage at one common five-minute checkpoint in `(anchor, anchor + 60 minutes]`. Every future checkpoint and contributing bitmap SHALL pass quality checks; incomplete horizons SHALL be censored, not negative.

#### Scenario: Different devices fail at different times

- **WHEN** 70% of devices fail cumulatively but never simultaneously meet the strict state
- **THEN** the target remains negative

### Requirement: Frozen leakage-bounded feature models

The analysis SHALL fit one 10-feature non-optical model and one 15-feature combined model that adds exactly five optical summaries from the six completed event-time hours and the preceding six-hour comparator. The optical median-shift feature SHALL be described as an unpaired cell-distribution shift, not a within-device change. It SHALL use training-only imputation, scaling, and coefficients. It MUST NOT use the incomplete anchor hour, pre-service telemetry, future target state, identities, or exact coordinates as features.

#### Scenario: Optical data extends beyond the anchor

- **WHEN** an optical value belongs to the anchor's incomplete hour or a future hour
- **THEN** it is excluded from every predictor

### Requirement: Chronological validation and hard gates

The analysis SHALL use chronological train, validation, and development-test partitions with one-hour boundary purges. Threshold selection SHALL occur only on validation. Support SHALL require 20 training positives per combined-model coefficient, at least 100 test positives, and at least 20 test days. Test-period performance SHALL require positive PR-AUC and Brier improvement confidence bounds, at least 90% recall, and at least 25% false-alert reduction relative to alerting on every trigger.

Incremental optical contribution SHALL be labelled not assessable whenever the support gate fails. Reusing dates exposed by an earlier model iteration SHALL be disclosed as development sensitivity rather than untouched confirmation.

#### Scenario: Any hard gate fails

- **WHEN** support, performance, or production-latency requirements are not all satisfied
- **THEN** the result is `CASCADE_MODEL_NOT_SUPPORTED` and MUST NOT be deployed or described as a production outage predictor

### Requirement: Aggregate-only reproducible outputs

The run SHALL export only an aggregate report, audit, and standardized coefficient table with source query IDs and artifact hashes. It MUST NOT export device, NAS, customer, account, exact-coordinate, or trigger-level H3/time records.

#### Scenario: A prohibited field reaches an artifact

- **WHEN** an output schema contains an identity, exact location, or trigger-level record
- **THEN** the run fails before detailed artifacts are written

### Requirement: Retrospective-only interpretation

The report SHALL call the outcome a ping-defined telemetry-outage cascade and SHALL disclose that final hourly values arrive too late for a live 15-minute trigger. It SHALL NOT claim all-cascade coverage, causal prediction, or confirmed customer-service failure.

#### Scenario: Hourly data misses the warning deadline

- **WHEN** final source values arrive after the proposed 15-minute warning window
- **THEN** production latency fails regardless of retrospective model ranking
