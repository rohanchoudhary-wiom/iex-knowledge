## 1. Core Inputs

- [x] 1.1 Implement Customer V2 CSV loading with deduplication and coordinate/CSP validation.
- [x] 1.2 Implement the configured bulk `get_device_status` client and strict last-ping parsing.

## 2. Attribution Engine

- [x] 2.1 Implement the 10-minute UP/DOWN/UNKNOWN rule and complete-denominator ISP/OLT gate.
- [x] 2.2 Implement minimal density-adaptive clustering, radius evidence, provider comparison, and conservative parent roll-up.
- [x] 2.3 Add one focused test suite proving member/non-member state comes only from last ping and covering all four attributions.

## 3. Service and Map

- [x] 3.1 Implement POST validation, the exact three-field response, health, and map-data endpoints.
- [x] 3.2 Build the static outage map with actual DOWN/UP/UNKNOWN device colors and decision evidence.
- [x] 3.3 Add a synthetic Customer V2/status demo that exercises fibre, premise-power, ISP/OLT, and unknown results through the APIs.

## 4. Verification

- [x] 4.1 Run unit/self-checks and strict OpenSpec validation.
- [x] 4.2 Start demo mode and render/verify the outage map in the browser.

## 5. Real Production Inputs

- [x] 5.1 Validate and ingest Detection's real open-outage GET response.
- [x] 5.2 Reject stale Customer V2 snapshots and require the real `get_device_status` URL.
- [x] 5.3 Atomically refresh live results and expose dependency freshness through map data and health.
- [x] 5.4 Remove automatic demo seeding from the atlas; keep synthetic data behind explicit `--demo` only.

## 6. Methodology Reconciliation

- [x] 6.1 Split the monolithic attribution module into a small package with stable public imports.
- [x] 6.2 Apply the 30-minute timing window before variable-density spatial clustering.
- [x] 6.3 Calculate R70/R80/R90/R100 per sub-outage, re-cluster radius jumps, and retain R100 tails and small groups as review evidence.
- [x] 6.4 Compare providers only inside each sub-outage's R90 boundary and roll up supported groups without letting noise veto them.
- [x] 6.5 Separate policy score, spatial evidence grade, cause likelihood, and independent confirmation in map evidence.
- [x] 6.6 Render R70/R80/R90 boundaries, tail/noise reasons, timing, stability, and provider eligibility in the atlas.
- [x] 6.7 Add focused regression coverage and verify the live localhost snapshot.
