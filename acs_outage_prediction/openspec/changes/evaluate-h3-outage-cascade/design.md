## Context

The H3 map found a large mapped fleet, but cascade events are temporally dependent and rare. Device count alone is not enough evidence for a model. The target remains ping-defined telemetry silence, not confirmed service failure.

## Decisions

### Freeze the same-cell trigger and target

For every 15-minute anchor, freeze the time-valid device set in each H3-9 cell, require at least 20 devices, and require every included device to have at least 12 hours of service tenure so its complete optical history is in-service. A device is early when its latest three five-minute opportunities are missed but its latest twelve are not all missed. A strict outage is twelve consecutive missed opportunities. A raw trigger requires `early >= ceil(10% × N)`, current strict outages `< ceil(10% × N)`, and no quarantined contributing hour. Keep the first trigger only when no raw trigger occurred during the preceding 60 minutes.

The positive target requires at least `ceil(70% × N)` of the frozen same-cell denominator to be simultaneously in strict outage at one common five-minute checkpoint in `(anchor, anchor + 60 minutes]`. Adjacent H3 cells are features only. In a time-valid roster, an absent hourly source row is interpreted as twelve missed ping opportunities; this makes the outcome a telemetry-outage outcome.

### Cap the combined model at fifteen features

The non-optical comparator contains early and current outage shares, their exact 15-minute deltas, three neighboring-cell shares, log device count, and hour sine/cosine. The combined model adds five summaries from the six completed event-time hours: optical median, pooled cell-device-hour median shift from the preceding six hours, out-of-range share, median within-hour spread, and valid-hour share. The shift is a cell-distribution feature, not a paired within-device change. Optical summaries never use the anchor's incomplete hour.

Use training-only median imputation and scaling with L2 logistic regression. Select each operating threshold once on validation at at least 90% recall, then evaluate the chronological development test period once. Because v1 already exposed those dates, this comparison is not confirmation.

### Require support, performance, and operational availability

Support requires at least 20 training positives per combined-model coefficient, at least 100 test positives, and at least 20 test days. Performance requires the day-bootstrap PR-AUC improvement over training climatology to have a 95% lower bound above zero, Brier improvement to have an upper bound below zero, test recall of at least 90%, and at least 25% fewer false alerts than alerting on every trigger. Incremental optical contribution is separately compared with the 10-feature non-optical model.

Production requires source values to be available inside the proposed 15-minute warning window. Final hourly bitmaps arrive about 61 minutes after hour-end, so the current source fails this gate independently of model scores.

## Measured Result

The corrected run in `outputs/h3_cascade_model_2026-08-12_v2/` contains 14,056 first-trigger episodes across 1,177 eligible H3-9 cells and 54,426 mapped devices. Training contains 80 positive cascades versus 300 required; the chronological development test contains 46 positives over nine days versus 100 positives and 20 days required.

On the 3,857 test episodes, alerting on every trigger had 1.19% precision and 3,811 false alerts. The combined model had PR-AUC 0.2379, 9.81% precision, 89.13% recall, and 377 false alerts. Its PR-AUC improvement over climatology had a positive day-bootstrap interval, but its Brier improvement interval crossed zero and recall missed the 90% gate. The non-optical comparator had PR-AUC 0.2292. Adding optical features changed PR-AUC by +0.0087 with a 95% interval from -0.0194 to +0.0522 and worsened Brier by 0.000393, so incremental optical value is not demonstrated and is not formally assessable with failed support.

The source-latency gate also fails. The decision is `CASCADE_MODEL_NOT_SUPPORTED`. Because v1 exposed the same dates before the corrected optical feature set was frozen, v2 is a development sensitivity rather than untouched confirmation.

## Risks / Trade-offs

- Final event-time rows were not available at their simulated anchors; this is retrospective feasibility only.
- Current mapping with service-time bounds is not a fully historical point-in-time roster.
- Missing hourly rows can reflect telemetry collection failure rather than customer connectivity loss.
- Day bootstrap preserves within-day dependence but does not merge neighboring-cell events into independent incidents.
- The conditional frame cannot measure all-cascade trigger coverage.

## Migration Plan

1. Keep the current result as a no-go feasibility artifact.
2. Acquire a raw per-ping stream with arrival timestamps and a point-in-time service roster.
3. Collect enough untouched calendar time to satisfy the locked support gates.
4. Open a separate production-validation change; do not lower the gates after inspecting this run.
