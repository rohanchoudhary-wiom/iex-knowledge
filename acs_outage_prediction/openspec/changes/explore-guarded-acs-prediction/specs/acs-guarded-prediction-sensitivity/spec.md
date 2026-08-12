## Purpose

Define a reproducible, privacy-safe post-hoc sensitivity for prediction after a 30-minute action gap using only the immutable canonical v14 frame.

## ADDED Requirements

### Requirement: Parent input and scope are immutable
The analysis SHALL verify the frozen SHA-256 hashes of the v14 model frame, v14 audit, and imported canonical helper implementation before reading outcomes. It SHALL perform no database or network access and SHALL NOT modify canonical artifacts.

#### Scenario: A parent hash differs
- **WHEN** any frozen parent hash does not match
- **THEN** the command stops before fitting or scoring a model

### Requirement: The guard-period risk set is explicit
The analysis SHALL preserve v14 split assignments, exclude rows whose first formal onset satisfies `0 < lead <= 30` minutes, and define positives only as `30 < lead <= 360` or `30 < lead <= 1440` minutes for the respective endpoints. Guard-period failures MUST NOT be relabelled as controls.

#### Scenario: An outage begins during the action gap
- **WHEN** the first future formal onset is no more than 30 minutes after an anchor
- **THEN** that anchor is excluded from both guarded endpoints and counted in guard attrition

#### Scenario: No onset is recorded within 24 hours
- **WHEN** parent lead time is missing
- **THEN** the row is a control for both guarded endpoints

### Requirement: Inputs and model comparisons are frozen
The ACS model SHALL use exactly the four numeric v14 feature columns and their four explicit missingness columns. The primary comparison SHALL be regularized logistic ACS+time versus logistic time-only for `(30m,6h]`. Logistic `(30m,24h]` and one fixed, untuned random forest SHALL be secondary/sensitivity analyses and SHALL NOT be selected as winners from test performance.

#### Scenario: A candidate input is future-derived
- **WHEN** a column is an outcome, lead, duration, split, partner, or device field
- **THEN** it is prohibited from every model input

### Requirement: Evaluation remains chronological and device-aware
Preprocessing and fitting SHALL use retained training rows, thresholds SHALL use retained validation rows, and test SHALL be scored once. The analysis SHALL compare each ACS model with a same-family time-only model on identical rows and report equal-device average-precision and Brier differences with paired device-bootstrap intervals alongside pooled metrics and alert burden. Each held-out split SHALL contain at least ten positive devices and ten control devices for each endpoint, and test SHALL contain at least twenty mixed-class devices for equal-device average precision. The device-aware gate SHALL pass only when all three conditions hold: pooled AP-delta device-bootstrap 95% lower bound greater than zero, equal-device AP-delta device-bootstrap 95% lower bound greater than zero, and equal-device Brier-delta device-bootstrap 95% upper bound less than zero.

#### Scenario: Pooled and equal-device results disagree
- **WHEN** the pooled PR-AUC improves but the equal-device interval includes zero or is negative
- **THEN** the result is reported as non-stable across devices rather than a useful predictor

### Requirement: Interpretation remains exploratory
Every output SHALL state `EXPLORATORY_POST_HOC_ONLY` and retain null and contradictory results. The audit and human-readable report SHALL state that confirmation requires the unchanged statistical protocol on genuinely untouched post-2026-08-10 data. The current command SHALL remain pinned to v14; a separate prospective confirmation change/runner SHALL register the new input hash and chronological boundaries before outcome inspection.

#### Scenario: A current-window model appears strong
- **WHEN** any current-window model or horizon has favorable test metrics
- **THEN** it remains a hypothesis-generating sensitivity and does not satisfy canonical task 6.3
