## 1. Contract and frame

- [x] 1.1 Freeze the H3-9 denominator, 10% early-miss trigger, 70% same-cell simultaneous target, and 60-minute horizon.
- [x] 1.2 Reuse the audited mapping and hourly bitmap quality contract, including valid zero-ping hours and explicit absent-row semantics.
- [x] 1.3 Keep neighboring cells as features only and purge chronological split boundaries.

## 2. Models and gates

- [x] 2.1 Fit the 10-feature non-optical and 15-feature combined L2 logistic models with training-only preprocessing.
- [x] 2.2 Freeze the validation threshold, alert-all baseline, climatology baseline, and day-bootstrap comparisons.
- [x] 2.3 Apply the positive-event, test-day, chronological performance, optical-increment, and source-latency gates without post-hoc relaxation.

## 3. Outputs and verification

- [x] 3.1 Complete the corrected retrospective run and record the measured no-go result.
- [x] 3.2 Run deterministic self-check and Python compilation.
- [x] 3.3 Verify aggregate-only schemas, artifact hashes, and absence of prohibited identifiers.
- [ ] 3.4 Validate this change with the OpenSpec CLI when the executable is available.
