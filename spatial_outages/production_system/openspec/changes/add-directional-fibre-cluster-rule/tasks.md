## 1. Directional Geometry Regression

- [x] 1.1 Add a 4395-shaped all-CSP DOWN-device regression and verify it fails Rule 4 before implementation.
- [x] 1.2 Add negative regressions for fewer than 5 devices, a radial/diffuse component, and insufficient healthy controls; verify each remains `UNKNOWN`.

## 2. Rule 4C Implementation

- [x] 2.1 Add the dependency-free principal-direction profile and verify horizontal, vertical, and diagonal fixtures produce equivalent directionality metrics.
- [x] 2.2 Re-cluster all-CSP DOWN candidates at the specified tighter reach, apply every Rule 4C geometry/control gate after Rule 4B, and verify only the selected component is returned with member/comparison provenance.

## 3. Contract and Atlas

- [x] 3.1 Add Rule 4C to `RULES.md` with its exact thresholds, order, confidence, affected-device semantics, and diagnostic timing requirement; verify the contract matches the implementation constants.
- [x] 3.2 Add Rule 4C to the human-readable atlas checklist with actual directionality, length, width, and control evidence; verify no internal decision code is displayed.

## 4. Validation and Live Refresh

- [x] 4.1 Run the full Python suite, frontend JavaScript syntax check, diff check, and strict OpenSpec validation; verify all pass.
- [x] 4.2 Restart localhost and verify live outage 4395 returns `FIBRE_CUT` through Rule 4C with only its matched directional component affected.
