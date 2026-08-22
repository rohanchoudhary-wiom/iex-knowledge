## Context

See `proposal.md`. Rule 4 already has the complete all-CSP comparison population and an existing deterministic variable-density clustering helper. It lacks a shape measurement, so an address-poor line of DOWN devices reaches Rule 5 even when the map strongly suggests a shared street-level fibre path.

## Goals / Non-Goals

**Goals:**

- Add one deterministic, orientation-independent shape signal to the existing Rule 4 path.
- Select only the coherent directional component rather than every DOWN device in the comparison polygon.
- Keep evidence inspectable from the API and atlas.

**Non-Goals:**

- Do not change Detection membership, comparison polygons, or Rules 1–4B.
- Do not infer the physical fibre route or claim independent cause confirmation.
- Do not add a clustering or linear-algebra dependency.

## Decisions

1. Reuse the current variable-density clustering helper on DOWN candidates only with `reach_scale=0.5` and `max_reach=250`. The tighter second pass isolates a street-scale component without changing the source outage or mixed-device clusters. Live 4395 produces a 10-device component under this pass.
2. Measure each component in local metres using an equirectangular projection around its mean coordinate. Compute the 2×2 covariance eigenvalues and principal direction with `math`; no numerical dependency is needed.
3. Describe shape with principal-axis projection length, the 90th percentile absolute perpendicular distance, and `sqrt(largest_eigenvalue / smallest_eigenvalue)`. This detects horizontal, vertical, and diagonal lines with one orientation-independent calculation.
4. Require at least 5 DOWN devices, length 50–500 m, directionality at least 3.0, perpendicular P90 width at most 50 m, and at least 70% of 5 or more known-state non-component controls UP. These gates make a short pair, compact point blob, diffuse area, or broadly unhealthy polygon insufficient.
5. Evaluate Rule 4C only when 4A and 4B miss. Choose the qualifying component with the most DOWN devices, breaking ties by higher directionality and narrower width. Return only that component as affected at confidence `0.60`; keep timing diagnostic.
6. Add `RULE_4C_DIRECTIONAL_CLUSTER` to the public decision evidence and render it as “Directional gali fibre cluster” with the actual geometric and control values.

Alternative considered: rotate the map or check only latitude/longitude bands. Rejected because the result would depend on map orientation and miss diagonal streets.

Alternative considered: add a full clustering package. Rejected because the existing variable-density pass already isolates the live 4395 core; only the missing shape profile is required.

## Risks / Trade-offs

- [Dense random failures accidentally look linear] → Require five devices, bounded length/width, directionality, and healthy surrounding controls.
- [Coordinate error distorts a narrow street] → Use a 50 m P90 width rather than maximum distance so one noisy coordinate does not veto the component.
- [Pilot thresholds are not historically calibrated] → Keep all metrics visible and backtest before changing the locked thresholds.
- [Tight clustering splits one long route] → Select the strongest component now; revisit reach only if labelled replay shows systematic fragmentation.

## Migration Plan

Add focused positive and negative tests, implement the shared shape helper and Rule 4C branch, update the rule contract and checklist, run the complete validation suite, restart localhost, and verify live outage 4395 becomes `FIBRE_CUT`. Roll back the isolated Rule 4C branch if unrelated shapes begin matching.
