## 1. Regression Coverage

- [x] 1.1 Add a focused engine test where a sub-10 compact same-gali group with a healthy mixed-cluster control becomes `FIBRE_CUT`, and verify it fails before implementation.
- [x] 1.2 Add focused negative checks for missing address grouping, missing healthy control, and spatially invalid review components; verify each remains `UNKNOWN` and Rules 2A–3B stay unavailable.

## 2. Attribution and Presentation

- [x] 2.1 Let components whose only review reason is `MIN_DOWN_MEMBERS` run the existing fibre evidence path without running provider/power rules; verify the focused tests pass.
- [x] 2.2 Include matched compact fibre groups in cause roll-up and affected-device output while retaining unmatched groups as review evidence; verify mixed and unmatched roll-up tests pass.
- [x] 2.3 Update `RULES.md` and the atlas funnel text to distinguish the provider/power support gate from compact Rule 4 evaluation; verify the UI no longer says Rule 4 was skipped for eligible compact groups.

## 3. Validation and Live Refresh

- [x] 3.1 Run the full test suite, frontend syntax check, diff check, and strict OpenSpec validation; verify all checks pass.
- [x] 3.2 Restart localhost and verify a compact live component exposes Rule 4 address/control evidence without weakening the LIVE OPEN outage view.

## 4. Locality Counterfactual Fallback

- [x] 4.1 Add a focused 4315-shaped regression where exact HOUSE/GALI grouping fails but all Rule 4B locality, radius, concurrency, and control thresholds pass; add one failing-threshold case and verify the new positive test fails before implementation.
- [x] 4.2 Implement normalized locality grouping, the known non-outage control denominator, and ordered Rule 4A→4B evaluation; verify Rule 4A remains preferred and focused Rule 4B tests pass.
- [x] 4.3 Update `RULES.md` and the atlas evidence story for Rule 4A versus Rule 4B; verify the rendered evidence contains the selected path and every Rule 4B metric.
- [x] 4.4 Run the full checks, strict OpenSpec validation, restart localhost, and verify outage 4315 becomes `FIBRE_CUT` at `0.60` without changing the LIVE OPEN membership.

## 5. Supported Locality Fallback

- [x] 5.1 Add a focused 4395-shaped regression where a supported 10-device component with R90 between 300 m and 500 m passes the existing concurrency, locality, and control gates; retain a failing check above 500 m.
- [x] 5.2 Extend Rule 4B to supported components with at least 3 DOWN devices and R90 at most 500 m while retaining the 3–9 DOWN and 300 m limits for compact review components; update `RULES.md`.
- [x] 5.3 Run the full checks and strict OpenSpec validation, restart localhost, and verify live outage 4395 becomes `FIBRE_CUT` at `0.60` when its evidence still matches.
