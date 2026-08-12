## Why

The canonical v14 study starts labels immediately after each hourly anchor. A router warning must leave time for action, and prior work commonly separates telemetry from the failure window. We therefore need one source-free sensitivity that asks whether the already-frozen ACS bundle predicts outages only after a 30-minute action gap.

## What Changes

- Read only the immutable, privacy-safe v14 model frame and verify its frozen hashes.
- Exclude anchors whose next formal outage onset is in `(0, 30 minutes]`.
- Evaluate fixed `(30 minutes, 6 hours]` and `(30 minutes, 24 hours]` labels without moving the existing chronological splits.
- Keep the canonical eight-column ACS model manifest, preprocessing, validation-only threshold selection, metrics, and time-only comparison.
- Treat regularized logistic regression at 6 hours as the sole primary comparison.
- Run one fixed, untuned random forest only as a nonlinear sensitivity.
- Write aggregate results only; do not write row-level probabilities or identifiers.
- Keep every result `EXPLORATORY_POST_HOC_ONLY`. This change cannot alter v14 or satisfy untouched-future confirmation task 6.3.

## Non-goals

- Discovering more ACS parameters, tuning a model grid, selecting a winning horizon or model on test results, production alerting, causal attribution, or claiming confirmation.

## Impact

- Adds one local source-free command and a separately versioned derived output directory.
- Performs no database or network access and no source-system writes.
