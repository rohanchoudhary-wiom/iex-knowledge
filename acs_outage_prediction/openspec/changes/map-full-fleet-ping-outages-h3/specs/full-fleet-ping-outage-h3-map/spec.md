## Purpose

Define a privacy-safe full-population map of strict 60-minute ping-defined telemetry outages at H3 resolution 9.

## ADDED Requirements

### Requirement: Full eligible ping population

The analysis SHALL read `PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX` for the previous complete Asia/Kolkata calendar month plus boundary observations and SHALL NOT apply device hash sampling. It SHALL normalize the known exact hour-end defect, exclude synthetic device families, quarantine conflicting or malformed device-hours, and report every attrition stage.

#### Scenario: A sampling predicate is present

- **WHEN** a device hash modulus or another sampling predicate is applied
- **THEN** the full-population gate fails before H3 aggregation

#### Scenario: An observed hour is conflicting

- **WHEN** a candidate silence interval crosses a conflicting or malformed device-hour
- **THEN** the interval is quarantined instead of being interpreted as no ping

### Requirement: Strict twelve-slot telemetry outage

The analysis SHALL set the first missed opportunity to five minutes after a known successful ping. An outage qualifies only after 60 consecutive minutes without a success; for a recovered gap this requires at least 65 minutes between adjacent successes. The event ends at the first later success, and absent hourly rows inside one gap SHALL NOT create duplicate events.

#### Scenario: Successes are 60 minutes apart

- **WHEN** the next success occurs 60 minutes after the previous success
- **THEN** the interval does not qualify because only 55 minutes elapsed after the first missed opportunity

#### Scenario: Successes are 65 minutes apart

- **WHEN** the next success occurs 65 minutes after the previous success
- **THEN** one event starts five minutes after the previous success with a recovered duration of 60 minutes

#### Scenario: Recovery is not observed

- **WHEN** the threshold has elapsed but no later success exists by the fixed observation boundary
- **THEN** retain one right-censored event without a completed duration

### Requirement: Exact time-valid geographic mapping

The analysis SHALL accept only an exact one-to-one `DEVICE_ID -> PUBLIC.T_DEVICE -> CUSTOMER_V2 NASID` bridge with a clean active customer and valid India coordinates. An event SHALL count only when its start lies within the customer's location and plan interval. H3 assignment SHALL use `H3_LATLNG_TO_CELL_STRING(latitude, longitude, 9)` inside Snowflake.

#### Scenario: Mapping or coordinates are ambiguous

- **WHEN** identity mapping is not one-to-one or coordinates conflict or fall outside India bounds
- **THEN** exclude and count the device before H3 aggregation

#### Scenario: Mapping coverage is inadequate

- **WHEN** fewer than 90% of full-population July ping devices map exactly
- **THEN** write aggregate audit evidence only and do not characterize a fleet H3 map

### Requirement: Density-normalized H3 aggregates

Every reportable cell SHALL contain eligible devices, affected devices, affected-device share, event count, recovered-duration summaries, and right-censored-event count. The audit SHALL separately report cell occupancy and mapped-device shares in cells containing at least three and at least five devices.

#### Scenario: A cell has fewer than five eligible devices

- **WHEN** detailed H3 output would describe a sparse cell
- **THEN** suppress its H3 ID and detailed measures while retaining global suppression counts

### Requirement: Privacy-safe reproducible artifacts

The run SHALL write aggregate-only `audit.json`, `h3_cell_summary.csv`, and `report.md` with its month, observation boundary, exact rule, mapping attrition, suppression, query ID, and artifact hashes. It MUST NOT export exact coordinates or direct or pseudonymous device, NAS, account, or customer identifiers.

#### Scenario: A prohibited field reaches output

- **WHEN** an output schema contains a prohibited identity or coordinate field
- **THEN** the run fails before writing detailed artifacts

### Requirement: Telemetry-outage interpretation

The report SHALL call these events ping-defined telemetry outages. It SHALL NOT present them as confirmed customer-service outages, formal incidents, root causes, spatial clusters, or predictive performance.
