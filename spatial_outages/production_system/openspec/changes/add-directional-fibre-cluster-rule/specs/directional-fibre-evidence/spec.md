## Purpose

Identify access-fibre outages from narrow street-like groups of current DOWN devices when address extraction is incomplete, without filtering the group by CSP or finalized outage membership.

## ADDED Requirements

### Requirement: Directional fibre evidence is an ordered final-stage rule
The system SHALL evaluate directional fibre evidence as Rule 4C only after Rules 1–3B, Rule 4A house/gali evidence, and Rule 4B locality evidence do not match. Rule 4C SHALL evaluate every current DOWN device inside the comparison polygon regardless of CSP or finalized outage membership.

#### Scenario: Earlier rule already matched
- **WHEN** any rule from 1 through 4B matches
- **THEN** Rule 4C SHALL be skipped and SHALL NOT change the attribution or affected-device set

#### Scenario: Address rules miss a directional failure
- **WHEN** Rules 1 through 4B do not match and current DOWN polygon devices form a qualifying directional component
- **THEN** Rule 4C SHALL attribute that component to `FIBRE_CUT`

### Requirement: Directional components satisfy guarded geometric thresholds
A Rule 4C component SHALL contain at least 5 current DOWN devices, have principal-axis length from 50 m through 500 m, have principal-to-perpendicular spread ratio of at least 3.0, and have 90th-percentile perpendicular width at most 50 m. At least 70% of 5 or more known-state devices outside the component SHALL be UP. Failure timing concentration SHALL be returned as diagnostic evidence and SHALL NOT veto the geometric match.

#### Scenario: Horizontal, vertical, or diagonal component qualifies
- **WHEN** a spatially connected DOWN component satisfies every count, length, directionality, width, and healthy-control threshold regardless of map orientation
- **THEN** the system SHALL match Rule 4C at confidence `0.60`

#### Scenario: Radial or diffuse component is rejected
- **WHEN** a DOWN component is too short, too long, too wide, insufficiently directional, or has insufficient healthy controls
- **THEN** Rule 4C SHALL NOT match that component

#### Scenario: Pair does not become a directional outage
- **WHEN** fewer than 5 current DOWN devices form a narrow line
- **THEN** Rule 4C SHALL NOT attribute them to `FIBRE_CUT`

### Requirement: Directional attribution affects only the matched component
When Rule 4C matches, `affected_device_ids` SHALL contain exactly the matched current DOWN component. The evidence SHALL expose affected member and comparison-only IDs, candidate component count, principal-axis length, perpendicular width, directionality ratio, known controls, UP controls, UP-control share, and timing concentration.

#### Scenario: Cross-CSP directional component
- **WHEN** a qualifying component contains current DOWN devices from multiple CSPs or devices outside finalized outage membership
- **THEN** all matched devices SHALL appear in `affected_device_ids` and their membership provenance SHALL be exposed separately

#### Scenario: Other DOWN devices remain unaffected
- **WHEN** current DOWN polygon devices exist outside the selected directional component
- **THEN** those devices SHALL remain candidates in evidence but SHALL NOT appear in `affected_device_ids`
