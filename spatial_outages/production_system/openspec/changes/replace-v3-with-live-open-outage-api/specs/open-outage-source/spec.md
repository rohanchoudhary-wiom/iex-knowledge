## Purpose

Defines the authoritative live source, validation, freshness, and failure behavior for the atlas's operational open-outage view.

## ADDED Requirements

### Requirement: Live OPEN outage source
The production system SHALL build its outage view only from `GET https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN`. It SHALL NOT use Snowflake `OUTAGE_V3` or `OUTAGE_MEMBER_V3` rows to add, retain, remove, or populate outages in that view.

#### Scenario: Open outage is published
- **WHEN** a valid API response contains an outage
- **THEN** the next atomic atlas snapshot contains that outage and its API-supplied device membership and duration

#### Scenario: Outage is no longer open
- **WHEN** an outage is absent from the next valid `status=OPEN` response
- **THEN** the next atomic atlas snapshot excludes that outage

#### Scenario: Historical endpoints exist
- **WHEN** the atlas performs its scheduled production refresh
- **THEN** it SHALL NOT call `status=ALL` or `status=CLOSED`

### Requirement: Open response validation and atomicity
The system SHALL validate the response count, `as_of` timestamp, unique outage IDs, unique non-empty device membership, and non-negative duration before replacing the current view. A failed refresh SHALL NOT publish a partial or empty replacement.

#### Scenario: Valid refresh
- **WHEN** every response field passes validation and required device status calls succeed
- **THEN** the system atomically replaces the previous open-outage view and exposes the API `as_of` value

#### Scenario: Invalid or unavailable refresh
- **WHEN** the OPEN endpoint is unavailable or any required response field fails validation
- **THEN** startup fails or the running service retains its previous snapshot and reports the refresh error

### Requirement: Customer inventory is outage-table independent
The Customer V2 inventory snapshot SHALL contain customer/device identity, CSP ownership, address, eligibility, ping-history features, and coordinates without joining `OUTAGE_V3` or `OUTAGE_MEMBER_V3`.

#### Scenario: Snowflake outage replicas lag
- **WHEN** Customer V2 inventory is refreshed while replicated outage tables are stale or unavailable
- **THEN** the inventory refresh succeeds without reading either outage table

### Requirement: Failure-time provenance without V3
When the OPEN API does not provide member failure timestamps, attribution SHALL use the member's last successful live ping plus five minutes as supporting failure time and SHALL identify that provenance as a proxy. The proxy SHALL NOT change finalized API membership or independently detect outages.

#### Scenario: Member failure timestamp is absent
- **WHEN** an open outage member has a valid last successful ping but no API failure timestamp
- **THEN** the system records last successful ping plus five minutes as supporting timing evidence with proxy provenance

#### Scenario: Member live ping is unusable
- **WHEN** an open outage member has no valid live ping from which to form the proxy
- **THEN** the member cannot enter a timed spatial component and the system does not recover a timestamp from Snowflake outage tables
