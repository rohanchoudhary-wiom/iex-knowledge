## Context

The parent v14 frame contains 54,772 privacy-safe hourly device anchors with strictly prior ACS features, frozen train/validation/test assignments, and the first formal outage onset within 24 hours. All current splits have already been inspected, so this analysis is a post-hoc sensitivity rather than a new holdout test.

The measured inputs are `boot_age_hours`, `reboot_count_6h`, `reboot_count_24h`, and `inform_staleness_minutes`. The model also retains their four explicit `_missing` columns. The first three measured inputs are transformations of the sole source-ready `$._lastBoot`; staleness is timing metadata. They are not four independently discovered ACS parameters.

## Frozen protocol

1. Verify SHA-256 for the compressed parent frame, parent audit, and imported canonical helper implementation.
2. Preserve every v14 split assignment. Exclude, rather than relabel, any row with first onset `0 < lead <= 30` minutes.
3. Set the 6-hour outcome to `30 < lead <= 360` and the 24-hour outcome to `30 < lead <= 1440`. A missing lead is a control; for 6 hours, an earliest onset after 360 minutes is also a control.
4. Use the exact eight-column v14 ACS manifest. Outcome, lead time, duration, split, partner, and device fields are never model inputs.
5. Retrain preprocessing and coefficients on the retained training rows. Select operating thresholds only on retained validation rows. Score test once.
6. Primary: regularized logistic ACS+time versus logistic time-only for `(30m,6h]`.
7. Secondary: logistic `(30m,24h]`. Sensitivity only: one fixed random forest at both horizons, with 300 trees, maximum depth 8, minimum leaf size 100, square-root feature sampling, no class weighting, and no tuning.
8. Primary device-aware summary: equal-device mean per-device average-precision difference, ACS+time minus time-only, with paired device-bootstrap 95% interval and fraction improved. Also report equal-device Brier difference. Pooled PR-AUC and its device-cluster interval, Brier, calibration, warning time, and validation-threshold alert burden are secondary. The device-aware gate passes only when the pooled AP-delta 95% lower bound is greater than zero, the equal-device AP-delta 95% lower bound is greater than zero, and the equal-device Brier-delta 95% upper bound is less than zero.
9. Report descriptive pooled and within-device correlations for exact input columns without p-values; repeated hourly anchors make naive row-level p-values invalid.
10. Do not choose a model or claim a predictor from current test results. The statistical protocol must later be run unchanged on genuinely untouched post-2026-08-10 data for confirmation. Because this command deliberately pins v14, that confirmation requires a separate prospectively specified confirmation change/runner that registers the new input hash and chronological boundaries before outcome inspection.

## Validity limits

- Excluding guard-period failures makes the estimand conditional on being evaluable after the action gap.
- Hourly anchors overlap and can repeat an outage; device resampling is conditional on the fitted model and does not fully capture cross-device shared incidents.
- Devices recur across splits, so this tests temporal stability, not unseen-device generalization.
- Formal incident onset may lag real degradation; the guard reduces but cannot eliminate onset-proxy risk.
- Large outcome-prevalence drift across splits remains a calibration risk.
