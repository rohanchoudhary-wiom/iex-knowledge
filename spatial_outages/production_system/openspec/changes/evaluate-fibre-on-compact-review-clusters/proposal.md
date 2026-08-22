## Why

The shared support gate currently prevents Rule 4 from examining compact outages with fewer than 10 located DOWN members, even though valid fibre evidence is defined as a same-house pair or same-gali group. This suppresses the strongest fibre candidates, including tightly concentrated, concurrent failures surrounded by healthy controls.

## What Changes

- Keep the existing supported-polygon gate for provider and premise-power Rules 2A–3B.
- Allow Rule 4 to evaluate a spatially compact review component after Rules 2A–3B are ineligible.
- Preserve exact same-house/same-gali evidence as Rule 4A.
- Add Rule 4B at `0.60` confidence when exact address grouping fails and every locality, concurrency, and healthy-control gate passes. Compact review components keep the 3–9 DOWN and 300 m limits; supported components may contain 3 or more DOWN devices with R90 at most 500 m.
- Keep unmatched compact components as `UNKNOWN`; compactness alone never produces `FIBRE_CUT`.
- Expose the evaluated fibre evidence in the atlas instead of reporting that Rule 4 was skipped.

## Capabilities

### New Capabilities

- `compact-fibre-attribution`: Evaluate exact address and locality-plus-counterfactual fibre evidence for compact sub-outages that fall below the provider/power support-count gate.

### Modified Capabilities

None.

## Impact

- Attribution funnel and group roll-up in `attribution/engine.py`.
- Fibre-rule wording in `RULES.md` and rule-funnel explanation in `static/index.html`.
- Regression coverage in `test_system.py`; no new dependency or API contract.
