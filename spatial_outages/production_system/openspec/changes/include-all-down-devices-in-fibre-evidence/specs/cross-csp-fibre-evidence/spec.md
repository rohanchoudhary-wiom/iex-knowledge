## Purpose

Use all live devices inside a comparison polygon to identify final-stage access-fibre failures without filtering the evidence by CSP or finalized outage membership.

## ADDED Requirements

### Requirement: Fibre evidence uses every known polygon device
After Rules 1–3B fail, the system SHALL build Rule 4 evidence from every comparison-polygon device with a known live state. Every current DOWN device SHALL be an affected candidate and every current UP device SHALL be a healthy candidate regardless of CSP or finalized outage membership. UNKNOWN devices SHALL remain visible but SHALL NOT contribute to either population.

#### Scenario: Cross-CSP same-gali failure
- **WHEN** current DOWN devices from multiple CSPs or membership states form a valid same-house or same-gali group inside one mixed DOWN/UP cluster with at least one healthy UP device
- **THEN** Rule 4A attributes the matched DOWN group to `FIBRE_CUT` without filtering it by CSP or outage membership

#### Scenario: CSP affects only earlier rules
- **WHEN** Rules 1–3B do not match and Rule 4 evaluates a comparison polygon
- **THEN** Rule 4 uses spatial, address, timing, and healthy-control evidence without applying a CSP equality requirement

### Requirement: Expanded DOWN groups use the ordered fibre rules
The system SHALL apply Rule 4A before Rule 4B to the expanded all-CSP DOWN candidate population. Rule 4A SHALL retain the existing same-house or same-gali requirements. If Rule 4A fails, Rule 4B SHALL evaluate the expanded candidate group using its own radius, shared locality, and healthy-control evidence. The strongest-10-minute failure share SHALL remain visible as diagnostic evidence but SHALL NOT veto Rule 4B.

#### Scenario: Expanded locality fallback
- **WHEN** at least three current DOWN candidates form a group with R90 at most 500 m, at least three share one normalized NLP locality, and at least 70% of five or more known non-affected controls are UP
- **THEN** Rule 4B attributes the locality-matched DOWN devices to `FIBRE_CUT` at confidence `0.60`
- **AND** the strongest-10-minute failure share is returned as diagnostic evidence without controlling the decision

#### Scenario: Unrelated stale DOWN devices
- **WHEN** current DOWN comparison devices do not join a valid Rule 4A address group and do not collectively satisfy every Rule 4B gate
- **THEN** those devices SHALL NOT be included in `affected_device_ids`

### Requirement: Fibre output exposes membership provenance
For a fibre match, the system SHALL return every matched DOWN device in `affected_device_ids`, including matched comparison devices that were not finalized outage members. Fibre evidence SHALL separately identify matched member and non-member device IDs.

#### Scenario: Non-member device contributes to fibre attribution
- **WHEN** a valid fibre group contains both finalized outage members and other current DOWN comparison devices
- **THEN** `affected_device_ids` contains the complete matched group and the evidence distinguishes member IDs from comparison-only IDs
