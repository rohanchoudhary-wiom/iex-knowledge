## 1. Regression Coverage

- [x] 1.1 Add a focused 4408-shaped test where same-gali current DOWN devices span CSPs and outage-membership states, and verify Rule 4A returns the complete affected group with member/comparison provenance; verify it fails before implementation.
- [x] 1.2 Add negative coverage proving unrelated DOWN comparison devices remain unaffected when neither Rule 4A nor every expanded Rule 4B gate matches.

## 2. Fibre Population and Evidence

- [x] 2.1 Expand the shared Rule 4 evidence path to all known-state comparison devices, compute Rule 4B metrics from expanded DOWN candidates, and expose affected member/comparison IDs; verify focused tests pass without changing Rules 1–3B.
- [x] 2.2 Update `RULES.md` and the atlas evidence story to state that fibre candidate selection ignores CSP and source membership; verify rendered evidence distinguishes candidate and affected provenance.
- [x] 2.3 Add a non-concurrent all-CSP locality regression and remove the strongest-10-minute veto while retaining the metric as diagnostic evidence.
- [x] 2.4 Update the rule document and atlas wording to identify temporal concentration as diagnostic evidence for Rule 4B.

## 3. Validation and Live Refresh

- [x] 3.1 Run the full test suite, frontend syntax check, diff check, and strict OpenSpec validation; verify every check passes.
- [x] 3.2 Restart localhost and verify outage 4408 evaluates the cross-CSP DOWN population through Rule 4 and becomes `FIBRE_CUT` when the live same-gali evidence matches.
