# Outage Attribution Rules

Rules run in order. `UNKNOWN` status stays in the denominator and counts as neither UP nor DOWN. Numeric values below are operating-policy scores, not calibrated probabilities.

## Evidence construction

| Step | Rule |
|---:|---|
| 1 | Resolve every posted and comparison device through Customer V2 and batch last-ping status. A device is DOWN at ≥10 minutes, UP below 10 minutes, otherwise UNKNOWN. |
| 2 | After the CSP-wide gate, keep only currently DOWN, located posted members. Partition them into anchored 30-minute failure windows, using `member_first_fail_at_ist` when present and `last successful ping + 5 minutes` only as an explicit proxy. |
| 3 | Run variable-density spatial clustering inside each time window. Keep every component: at least 10 devices can be SUPPORTED; smaller components remain REVIEW evidence. |
| 4 | For each component, use the median centre and calculate R70/R80/R90/R100. Re-cluster once when R90−R80 exceeds 500 m. R90 is the comparison boundary; R100 is tail evidence only. |
| 5 | A component is spatially supported when it has at least 10 DOWN members, R90 ≤1 km, and R90−R80 ≤500 m. Review/noise components do not veto a separate supported component. |

## 1. CSP-wide outage

| Field | Rule |
|---|---|
| Boundary | Complete Customer V2 population of the outage CSP |
| Match | At least 75% DOWN when the CSP has 50 or more active connections; at least 80% DOWN when it has fewer than 50 |
| Attribution | `ISP_OLT_CSP_SIDE` |
| Policy score | `0.8` from 75% to below 80%; `0.9` at 80% or above |

Below the applicable size-based gate, the CSP signal is retained and local rules continue.

| CSP DOWN share | Policy score |
|---|---:|
| 60% to below 65% | `0.5` |
| 65% to below 70% | `0.6` |
| 70% to below 75% | `0.75` |
| 75% to below 80% | `0.8` |

## 2. Local CSP-specific outage

| Field | Rule |
|---|---|
| Boundary | One supported sub-outage's R90 circle; R100 tail is excluded |
| Population | All Customer V2 devices inside R90 |
| Minimums | More than 20 total devices and more than 10 other-CSP devices |
| Match | Target CSP is at least 80% concurrent DOWN and other CSPs combined are at least 80% UP |
| Attribution | `CSP_SPECIFIC_LOCAL` |
| Policy score | `0.8` |

## 3. Premise power

| Field | Rule |
|---|---|
| Boundary | One supported sub-outage's R90 circle |
| Provider eligibility | At least 5 devices for that CSP inside R90; UNKNOWN devices remain in its denominator |
| Match | At least two qualified CSPs are each at least 70% concurrent DOWN |
| Attribution | `PREMISE_POWER` |
| Evidence confidence | `MEDIUM`; independent confirmation remains `MISSING` |

## 4. Fibre cut

| Field | Rule |
|---|---|
| Boundary | One supported sub-outage's R90 circle |
| Provider eligibility | At least 5 devices for that CSP inside R90; UNKNOWN devices remain in its denominator |
| Match | Target CSP is at least 70% concurrent DOWN and every qualified peer CSP is below 20% concurrent DOWN and at least 80% currently UP |
| Attribution | `FIBRE_CUT` |
| Evidence confidence | `MEDIUM`; independent confirmation remains `MISSING` |

## Parent roll-up and fallback

| Condition | Result |
|---|---|
| Every supported sub-outage agrees on one non-UNKNOWN cause | Return that cause |
| Supported sub-outages disagree | `UNKNOWN / LOCAL_GROUPS_DISAGREE` |
| Supported groups lack a qualified peer comparison | `UNKNOWN / NO_QUALIFIED_PEER` |
| Only review groups remain | `UNKNOWN / SUB_OUTAGE_REVIEW_ONLY` |
| No current DOWN located members remain | `UNKNOWN / NO_CURRENT_DOWN_MEMBERS` |

## Low-confidence verdict

After strict rules fail, an outage with current located DOWN evidence still receives a verdict:

| Residual signal | Verdict |
|---|---|
| At least two qualified CSPs each have at least two concurrent DOWN devices, or the outage contains multiple CSPs | `PREMISE_POWER / LOW` |
| Otherwise the outage has a single-CSP failure pattern | `FIBRE_CUT / LOW` |

Missing Customer V2 coverage, unavailable status evidence, or zero current located DOWN devices remain `UNKNOWN`.

The atlas must show R70/R80/R90/R100, timing source and concentration, stability, provider eligibility, tail IDs, review/noise IDs, recovered member IDs, spatial evidence, cause likelihood, and confirmation separately.
