## Purpose

Define a privacy-safe pilot that measures customer outage impact by geographic cell and hour using only the audited `CUSTOMER_V2` population.

## ADDED Requirements

### Requirement: CUSTOMER_V2-only customer cohort
The pilot SHALL use only `PROD_DB.DBT.ACTIVE_BASE` rows whose `SOURCE` is exactly `CUSTOMER_V2`. It SHALL collapse duplicate rows for an account only when its NASID, latitude, longitude, and state agree, exclude conflicting accounts, and retain only active accounts with valid India coordinates.

#### Scenario: Mixed customer sources are available
- **WHEN** the customer table contains `CUSTOMER_V2` and other source values
- **THEN** the pilot includes only `CUSTOMER_V2` rows and reports the number excluded by every cohort rule

#### Scenario: An account has conflicting customer rows
- **WHEN** an account has more than one distinct NASID, coordinate pair, or state
- **THEN** the pilot excludes that account and records the conflict count in its audit output

### Requirement: Time-valid customer denominators
The pilot SHALL distinguish the active, valid-coordinate network footprint from the primary customer-impact denominator. For a cell-hour, the primary denominator SHALL include a customer only when its location started no later than that hour and its plan had not expired at that hour.

#### Scenario: A current customer was not yet present
- **WHEN** a customer's `LOCATION_START_TIME` is later than the evaluated cell-hour
- **THEN** the pilot excludes that customer from the cell-hour denominator

#### Scenario: A customer's plan had expired
- **WHEN** a customer's `PLAN_EXPIRY_TIME` is earlier than the evaluated cell-hour
- **THEN** the pilot excludes that customer from the primary cell-hour denominator while preserving the separately labelled network-footprint count

### Requirement: Audited one-to-one outage mapping
The pilot SHALL use non-deleted formal incident and impacted-device records and map each outage `DEVICE_ID` through `T_DEVICE` to a `CUSTOMER_V2` NASID. It SHALL accept only mappings that are one-to-one in both directions within the analysis population, report mapping coverage and ambiguity counts, and SHALL NOT use direct numeric casting or fuzzy identity matching as a fallback.

#### Scenario: The mapping gate passes
- **WHEN** at least 90% of distinct outage devices in the bounded window map through an unambiguous bridge to an eligible `CUSTOMER_V2` NASID
- **THEN** the pilot records the numerator, denominator, coverage percentage, and exclusion counts before constructing cell-hour results

#### Scenario: The mapping gate fails
- **WHEN** mapping coverage is below 90% or accepted mappings are not one-to-one in both directions
- **THEN** the pilot stops before cell-hour construction, writes the audit result, and exits unsuccessfully

### Requirement: Density-normalized cell-hour construction
The pilot SHALL assign eligible customers to fixed 0.01-degree pilot cells and represent each outage across every hourly window it overlaps. Each cell-hour SHALL count distinct eligible and affected customers, so repeated incident rows cannot count the same customer more than once in that cell-hour.

#### Scenario: An outage spans multiple hours
- **WHEN** an outage interval overlaps more than one hourly window
- **THEN** the affected customer is counted once in every overlapping cell-hour and no more than once per cell-hour

#### Scenario: Density differs between cells
- **WHEN** two cells contain different numbers of eligible customers
- **THEN** each cell-hour outage rate is calculated as distinct affected customers divided by distinct eligible customers rather than compared by raw outage counts alone

### Requirement: Aggregate spatial metrics
For each reportable cell-hour, the pilot SHALL provide eligible-customer count, affected-customer count, affected-customer rate, distinct incident count, outage-duration summaries, and an aggregate measure across the eight adjacent grid cells. It SHALL label the 0.01-degree grid as an approximate pilot geography rather than an exact one-kilometre boundary.

#### Scenario: A reportable cell-hour is produced
- **WHEN** a cell-hour has at least five eligible customers
- **THEN** the output contains all required cell, outage, duration, and neighbouring-cell aggregate measures

#### Scenario: A cell-hour has a small denominator
- **WHEN** a cell-hour has fewer than five eligible customers
- **THEN** the pilot suppresses that cell-hour from detailed output and reports only the aggregate number of suppressed rows

### Requirement: Privacy-safe artifacts
The pilot SHALL produce `audit.json`, `cell_hour_outages.csv`, and `report.md` using aggregate cell-hour data only. These artifacts SHALL NOT contain customer names, mobile numbers, account identifiers, raw NASIDs, device identifiers, IP addresses, SSIDs, full ACS payloads, or exact customer coordinates.

#### Scenario: Pilot artifacts are inspected
- **WHEN** the pilot completes or stops at a gate
- **THEN** an automated check confirms that no prohibited identifier fields or exact customer-coordinate fields exist in any artifact

### Requirement: Chronological and descriptive interpretation
Any stability assessment SHALL use earlier observations to assess later observations rather than random row splitting. The report SHALL describe results as outage localization and customer-impact measurement, state the current-snapshot and shared-source limitations, and SHALL NOT claim root cause, causality, independent outage validation, or predictive performance.

#### Scenario: Geographic stability is assessed
- **WHEN** the pilot compares spatial outage patterns over time
- **THEN** it uses non-overlapping chronological windows and reports the dates and customer eligibility rules for each window

#### Scenario: The pilot report is completed
- **WHEN** aggregate results are available
- **THEN** the report explicitly states whether the geographic pilot is useful enough to justify a later ACS and prediction phase without presenting that later phase as completed
