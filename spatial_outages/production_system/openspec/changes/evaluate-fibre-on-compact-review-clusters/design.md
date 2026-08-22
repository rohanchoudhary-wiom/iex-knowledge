## Context

See `proposal.md`. Today one `supported` boolean controls both provider/power evaluation and fibre evaluation. It is false below 10 DOWN members, so the existing mixed-device and address checks are never called for small components.

## Goals / Non-Goals

**Goals:**

- Separate provider/power eligibility from compact fibre eligibility.
- Reuse the existing polygon, mixed clustering, address grouping, confidence, and roll-up paths.
- Add a lower-confidence locality-plus-counterfactual fallback when exact HOUSE/GALI extraction fails.
- Make the atlas show whether Rule 4 ran and what it found.

**Non-Goals:**

- Do not lower the 10-DOWN threshold for Rules 2A–3B.
- Do not infer fibre from compactness, timing, locality, or healthy controls alone; Rule 4B requires all signals together.
- Do not add a model, API, or temporal outage-detection rule.

## Decisions

1. A component is compact-fibre-eligible when its only review reason is `MIN_DOWN_MEMBERS`. This reuses the existing R90 and R90−R80 limits and excludes spatially diffuse review components.
2. Supported components keep the current ordered path: Rules 2A–3B, then Rule 4. Compact review components skip Rules 2A–3B and call the existing Rule 4 evidence path directly.
3. Rule 4A remains the preferred path and keeps the current exact HOUSE/GALI grouping and model-derived confidence.
4. Rule 4B runs only after 4A fails. Compact review components require 3–9 DOWN members and R90 at most 300 m; supported components require at least 3 DOWN members and R90 at most 500 m. Both paths require strongest-10-minute share at least 0.80, a normalized NLP locality shared by at least three DOWN members, and at least five known non-outage controls with UP share at least 0.70. Rule 4B returns fixed confidence `0.60` and only the locality-matched DOWN device IDs.
5. The control denominator includes nearby non-outage comparison devices with known UP or DOWN state; outage members and UNKNOWN controls are excluded.
6. A compact Rule 4A/4B match becomes an attribution-bearing group for roll-up while retaining its review-grade provider/power status. An unmatched compact component remains ordinary review evidence.
7. Regression tests inject deterministic address entities; live model loading is unnecessary for the unit check.

Alternatives rejected: lowering the global minimum to nine would weaken power/provider attribution; treating every compact cluster as fibre would remove the required locality and counterfactual evidence; treating generic area equality alone as a gali would overstate address precision.

## Risks / Trade-offs

- [Address NER misses an exact gali] → Allow Rule 4B only when NLP still supplies shared locality and every spatial, concurrency, and control guard passes.
- [Small groups become false fibre positives] → Require either the existing Rule 4A evidence or every Rule 4B guard together.
- [Proxy ping timing is imperfect] → Use it only as one Rule 4B guard, never as standalone cause evidence.
- [Initial 300/500 m, 80%, and 70% thresholds are uncalibrated] → Keep them explicit and backtest before changing them.
- [A compact fibre group and supported group disagree] → Use the existing disagreement roll-up instead of hiding either cause.

## Migration Plan

Update the rule contract, engine, atlas explanation, and tests together; restart localhost and inspect a compact live candidate. Roll back the focused engine/UI/rule changes if compact review groups produce unsupported attributions.
