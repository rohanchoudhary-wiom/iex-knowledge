## Purpose

Allow small, spatially coherent access-network failures to receive fibre-cut attribution from exact house/gali evidence or a guarded locality-plus-counterfactual fallback without weakening the provider and premise-power support gate.

## ADDED Requirements

### Requirement: Compact review components reach Rule 4
The system SHALL evaluate Rule 4 for a component that fails spatial support only because it contains fewer than 10 current located DOWN outage members. Rules 2A through 3B SHALL remain unavailable to that component.

#### Scenario: Small compact component
- **WHEN** a component has fewer than 10 DOWN members, R90 at most 1 km, and R90 minus R80 at most 500 m
- **THEN** the system skips Rules 2A through 3B and evaluates Rule 4 using its comparison polygon

#### Scenario: Spatially invalid review component
- **WHEN** a component exceeds the R90 or R90-minus-R80 spatial limit
- **THEN** the system does not evaluate Rule 4 and retains the component as review evidence

### Requirement: Fibre attribution requires address and control evidence
The system SHALL evaluate two ordered fibre paths. Rule 4A SHALL match when the mixed local clustering produces either at least two DOWN outage devices sharing a normalized house with local context or at least three DOWN outage devices sharing a normalized gali with local context, and the matched mixed cluster contains at least one healthy UP control device. If Rule 4A fails, Rule 4B SHALL match at confidence `0.60` only when at least 80% of failures fall within the strongest 10-minute window, at least three DOWN devices share one normalized NLP locality, and at least 70% of five or more nearby non-outage controls with known state are UP. A compact review component SHALL additionally contain 3–9 DOWN devices and have R90 at most 300 m. A supported component SHALL contain at least 3 DOWN devices and have R90 at most 500 m.

#### Scenario: Same-gali failure with healthy control
- **WHEN** at least three DOWN outage devices in a compact review component share valid gali evidence and their mixed cluster contains a healthy UP device
- **THEN** Rule 4A attributes only the matched devices to `FIBRE_CUT` using address-model confidence

#### Scenario: Compact-review locality and counterfactual fallback
- **WHEN** Rule 4A does not match and a compact review component with 3–9 DOWN devices satisfies the 300 m radius, 80% concurrency, shared-locality, and 70%-UP control requirements
- **THEN** Rule 4B attributes the locality-matched DOWN devices to `FIBRE_CUT` at confidence `0.60`

#### Scenario: Supported locality and counterfactual fallback
- **WHEN** Rules 2A–3B and Rule 4A do not match and a supported component with at least 3 DOWN devices satisfies the 500 m radius, 80% concurrency, shared-locality, and 70%-UP control requirements
- **THEN** Rule 4B attributes the locality-matched DOWN devices to `FIBRE_CUT` at confidence `0.60`

#### Scenario: Weak locality fallback evidence
- **WHEN** Rule 4A does not match and any Rule 4B threshold is missing
- **THEN** the system returns `UNKNOWN` for that component

#### Scenario: Address match without healthy control
- **WHEN** DOWN devices share valid house or gali evidence but their mixed cluster has no healthy UP device
- **THEN** the system does not attribute `FIBRE_CUT`

### Requirement: Compact fibre results participate in attribution roll-up
A compact review component with a valid Rule 4A or Rule 4B match SHALL be attribution-bearing and SHALL participate in the existing sub-outage cause roll-up. An unmatched compact review component SHALL remain non-attribution review evidence.

#### Scenario: Only compact fibre component matches
- **WHEN** an outage has no supported component and one compact review component matches Rule 4
- **THEN** the outage result is `FIBRE_CUT` with the matched affected device IDs

### Requirement: Atlas distinguishes skipped and evaluated fibre rules
The atlas SHALL state whether Rule 4 was evaluated for each review component, identify whether Rule 4A or Rule 4B matched, and expose the address/locality, spatial, concurrency, and healthy-control evidence used.

#### Scenario: Compact review component shown in atlas
- **WHEN** a compact review component is eligible for Rule 4
- **THEN** the rule funnel reports the Rule 4 result rather than claiming Rules 2A through 4 were all skipped
