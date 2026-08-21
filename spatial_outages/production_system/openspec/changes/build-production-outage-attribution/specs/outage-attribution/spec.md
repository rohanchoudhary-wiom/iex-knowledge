## Purpose

Defines the API and decision behavior for attributing one TFF outage using Customer V2 inventory and Detection-owned last-ping evidence.

## ADDED Requirements

### Requirement: Attribution POST API
The system SHALL expose `POST /outage_attribution` accepting `outage_id`, a non-empty unique list of `devices`, an optional `recovered_devices` list, and non-negative `ongoing_time` seconds. It SHALL return `outage_id`, `attribution`, and `confidence`.

#### Scenario: Valid request
- **WHEN** TFF posts a valid outage
- **THEN** the system evaluates it and returns the same outage ID with one attribution and confidence

#### Scenario: Invalid request
- **WHEN** the outage ID is absent, devices are empty or duplicated, or ongoing time is negative
- **THEN** the system returns HTTP 400 identifying the invalid input

### Requirement: Customer V2 comparison population
The system SHALL use deduplicated Customer V2 devices with CSP ownership and valid coordinates as the ISP/OLT and local comparison population.

#### Scenario: Customer V2 device is not an outage member
- **WHEN** a Customer V2 device is outside the posted member list
- **THEN** its state is still obtained from `get_device_status` rather than assumed UP

#### Scenario: Posted device is absent from Customer V2
- **WHEN** a posted outage member cannot be resolved in Customer V2
- **THEN** the system returns `UNKNOWN` with LOW confidence rather than guessing its provider or location

### Requirement: Last-ping device state
The system SHALL call `get_device_status` with device IDs and use its last successful server-time ping for each device. At evaluation time a device SHALL be DOWN when no successful ping occurred for at least 10 minutes, UP when the last successful ping is less than 10 minutes old, and UNKNOWN when the ping is missing, invalid, or in the future.

#### Scenario: Outage member has a recent ping
- **WHEN** a posted outage member successfully pinged less than 10 minutes ago
- **THEN** the system treats it as UP despite its outage membership

#### Scenario: Comparison device has an old ping
- **WHEN** a non-member Customer V2 device has not successfully pinged for at least 10 minutes
- **THEN** the system treats it as DOWN despite not being an outage member

#### Scenario: Ping evidence is missing
- **WHEN** `get_device_status` has no valid last ping for a requested device
- **THEN** the system treats the device as UNKNOWN and never as UP

### Requirement: ISP/OLT pre-filter
The system SHALL first calculate the affected CSP's DOWN share across its complete active Customer V2 population. For CSPs with at least 50 active connections, an inclusive 75% DOWN share SHALL return `ISP_OLT_CSP_SIDE`; for smaller CSPs, the inclusive threshold SHALL remain 80%.

#### Scenario: CSP-wide outage
- **WHEN** the affected CSP crosses its size-based 75% or 80% gate
- **THEN** the system returns `ISP_OLT_CSP_SIDE` with policy score `0.8` below 80% and `0.9` at 80% or above

#### Scenario: Incomplete CSP status
- **WHEN** any device required by the CSP denominator has UNKNOWN status
- **THEN** that device remains in the denominator and counts as neither UP nor DOWN

### Requirement: Adaptive local attribution
Below the ISP/OLT gate, the system SHALL cluster currently DOWN posted devices using density-adaptive distance, retain small components as noise, calculate R70/R80/R90/R100 from the median centre, and use R90 as the local comparison boundary. A local group SHALL be supported only with at least 10 located DOWN devices, R90 at most 1 kilometre, and `R90 - R80` at most 500 metres.

The system SHALL partition failures by an anchored 30-minute window before spatial clustering, retain sub-threshold components as REVIEW evidence, re-cluster a component once when `R90 - R80` exceeds 500 metres, and keep R100 tails outside the R90 comparison population. Noise or review components SHALL NOT invalidate a separate supported component.

#### Scenario: Multi-provider local failure
- **WHEN** at least two qualified CSPs inside a supported R90 boundary are each at least 70% DOWN
- **THEN** the group supports `PREMISE_POWER`

#### Scenario: One provider fails while peers remain up
- **WHEN** the target CSP is at least 70% DOWN and every qualified peer CSP is below 20% concurrent DOWN and at least 80% currently UP
- **THEN** the group supports `FIBRE_CUT`

#### Scenario: Local evidence is weak or mixed
- **WHEN** the group fails a spatial gate, has no qualified peer, or matches neither provider pattern
- **THEN** the group remains REVIEW/UNKNOWN evidence and the parent may apply the explicit LOW-confidence fallback

### Requirement: Low-confidence fallback
After strict attribution fails, the system SHALL still return a LOW-confidence verdict when current located DOWN evidence exists. Two or more qualified CSPs with at least two concurrent DOWN devices each, or a multi-CSP outage, SHALL produce `PREMISE_POWER`; the remaining single-CSP pattern SHALL produce `FIBRE_CUT`. Missing dependencies and zero current located DOWN evidence SHALL remain `UNKNOWN`.

### Requirement: Parent result and confidence
The system SHALL return `ISP_OLT_CSP_SIDE`, `CSP_SPECIFIC_LOCAL`, `FIBRE_CUT`, `PREMISE_POWER`, or `UNKNOWN`. The CSP-wide and local-CSP rules SHALL use operating-policy scores `0.9` and `0.8`; fibre and premise rules SHALL use MEDIUM evidence confidence; unresolved results SHALL use LOW. Independent confirmation SHALL remain separate and MISSING until supplied.

Map evidence SHALL distinguish policy score, spatial evidence grade, cause likelihood, and confirmation status. A numeric policy score SHALL NOT be described as a calibrated physical-cause probability.

#### Scenario: Local groups disagree
- **WHEN** supported local groups produce different causes
- **THEN** the parent result is `UNKNOWN` with LOW confidence

### Requirement: Outage evidence map
The system SHALL render current evaluations with R70/R80/R90/R100 and one component boundary per local group, plus device colors based on `get_device_status`: red for DOWN, green for UP, and grey for UNKNOWN. It SHALL list review/noise, R100 tail, and missing-status device IDs.

#### Scenario: Map is rendered
- **WHEN** an outage has been evaluated
- **THEN** the map shows its attribution, policy score or evidence confidence, radius evidence, timing provenance, stability, provider eligibility and shares, confirmation state, and actual compared device states

### Requirement: Real open-outage ingestion
The production service SHALL validate and ingest Detection's open-outage GET response using one source `as_of` timestamp. Each refresh SHALL atomically replace the current open result set.

#### Scenario: Open outage closes between refreshes
- **WHEN** an outage is absent from the next valid open-outage response
- **THEN** it is absent from the next published map snapshot

#### Scenario: Outage feed is invalid
- **WHEN** count, timestamp, outage ID, devices, or duration fails validation
- **THEN** production startup fails or an already-running snapshot is marked stale without partial replacement

### Requirement: No synthetic production fallback
Production SHALL require a Customer V2 snapshot no older than 24 hours and a configured real `get_device_status` endpoint. It SHALL NOT seed demo outages, use historical CSV ping time as current state, or infer state from membership.

#### Scenario: Required real dependency is unavailable
- **WHEN** Customer V2 is stale or the outage/status API cannot supply valid data
- **THEN** the service fails closed and reports dependency failure instead of rendering synthetic results
