# Spatial outage attribution — rolling 15-day report

Generated on **13 August 2026** using the current active-device baseline and outage-member failures from the preceding 15 days.

## Executive summary

| Measure | Result |
|---|---:|
| Classified outages | 6,217 |
| Active devices in baseline | 169,059 |
| Active CSPs | 1,075 |
| Active H3 cells | 10,403 |
| Active CSP-H3 cells | 18,602 |
| Distinct affected devices | 53,038 |
| Outage-device memberships | 144,503 |
| Attribution events | 478 |
| Observed first failure | 29 July 2026, 15:30 IST |
| Observed last failure | 13 August 2026, 14:47 IST |

The source contained **6,226 outages**. Nine were excluded because their devices mapped to two current CSP IDs, leaving **6,217 unambiguous outages** for attribution.

## Bucket distribution

| Bucket | Outages | Share | High confidence | Medium confidence |
|---|---:|---:|---:|---:|
| `NOISE` | 4,066 | 65.40% | 4,066 | 0 |
| `LOCAL CSP FAULT` | 1,173 | 18.87% | 1,146 | 27 |
| `UNKNOWN` | 921 | 14.81% | 0 | 921 |
| `REGIONAL` | 31 | 0.50% | 27 | 4 |
| `AREA-SHARED` | 25 | 0.40% | 23 | 2 |
| `ISP / OLT` | 1 | 0.02% | 0 | 1 |
| **Total** | **6,217** | **100%** | **5,262** | **955** |

Overall confidence:

- **HIGH:** 5,262 outages (84.64%)
- **MEDIUM:** 955 outages (15.36%)

## Interpretation

- **4,066 outages (65.40%) were NOISE:** none of their CSP-H3 cells reached the 70% affected-share threshold.
- **2,151 outages produced at least one DOWN CSP-H3 cell.**
- **1,230 outages received a failure-domain bucket:** `LOCAL CSP FAULT`, `AREA-SHARED`, `REGIONAL`, or `ISP / OLT`.
- **921 outages remained UNKNOWN.** Every one matched `R6_NO_NEIGHBOR_CSP`: no second active CSP existed in the target H3 for comparison.
- The dominant attributed failure domain was **LOCAL CSP FAULT**, representing 1,173 outages.
- Shared and wide-area patterns were uncommon: 25 `AREA-SHARED`, 31 `REGIONAL`, and one `ISP / OLT` outage.

These buckets indicate the most likely failure domain. They do not prove a physical root cause such as a power cut, fibre cut, OLT defect, or backend failure.

## Rule matches

| Rule | Outages | Share |
|---|---:|---:|
| `R1_NO_DOWN_CSP_H3` | 4,066 | 65.40% |
| `R5_NEIGHBOR_CSP_UP` | 1,173 | 18.87% |
| `R6_NO_NEIGHBOR_CSP` | 921 | 14.81% |
| `R3_MULTI_CSP_MULTI_H3` | 31 | 0.50% |
| `R2_MULTI_CSP_ONE_H3` | 25 | 0.40% |
| `R4_CSP_WIDE` | 1 | 0.02% |

No outage matched `R6_NO_CROSS_H3_COMPARISON` or `R6_AMBIGUOUS_PATTERN` in this run.

## Daily outage counts

The first and last dates are partial days because this was a rolling 15-day query.

| Date | Outages |
|---|---:|
| 29 July 2026 | 133 |
| 30 July 2026 | 430 |
| 31 July 2026 | 346 |
| 1 August 2026 | 339 |
| 2 August 2026 | 393 |
| 3 August 2026 | 337 |
| 4 August 2026 | 445 |
| 5 August 2026 | 383 |
| 6 August 2026 | 470 |
| 7 August 2026 | 641 |
| 8 August 2026 | 613 |
| 9 August 2026 | 413 |
| 10 August 2026 | 420 |
| 11 August 2026 | 292 |
| 12 August 2026 | 344 |
| 13 August 2026 | 218 |

Peak days were **7 August with 641 outages** and **8 August with 613 outages**.

## Attribution-event profile

| Measure | Result |
|---|---:|
| Attribution events | 478 |
| Events containing multiple outages | 476 |
| Median outages per event | 11 |
| Maximum outages in one event | 90 |
| Evaluated CSP-H3 state rows | 215,961 |
| DOWN state rows | 3,772 |
| UP state rows | 212,189 |

Events use an inclusive 30-minute window anchored to the first outage in each event.

## Baseline H3 density

There is no minimum-device rule. A CSP-H3 cell is DOWN solely when its affected-device share is at least 70%.

| Eligible devices in CSP-H3 | Cells | Share of cells |
|---:|---:|---:|
| 1 | 6,124 | 32.92% |
| 2 | 2,509 | 13.49% |
| 3 | 1,555 | 8.36% |
| 4 | 1,076 | 5.78% |
| 5 or more | 7,338 | 39.45% |

Small H3 cells are therefore intentionally retained. For example, a 1/1 affected cell is DOWN, although the outage may still become `UNKNOWN` when comparison evidence is absent.

## Ten CSPs with the most outages

| CSP ID | Outages | Share |
|---|---:|---:|
| `a0c0b0` | 118 | 1.90% |
| `a0b6y3` | 89 | 1.43% |
| `a0b7i3` | 74 | 1.19% |
| `a0c0h2` | 74 | 1.19% |
| `a0b7g1` | 70 | 1.13% |
| `a0b9n5` | 68 | 1.09% |
| `a0b9k1` | 67 | 1.08% |
| `a0b9v1` | 65 | 1.05% |
| `a0b8w7` | 65 | 1.05% |
| `a0b7h8` | 63 | 1.01% |

## Data and execution notes

- The query retains all active `CUSTOMER_V_2` devices as the denominator.
- Only `OUTAGE_MEMBER_V3` memberships within the rolling 15-day window are attached.
- `CSP_ID` comes exclusively from `T_DEVICE`. `OUTAGE_MEMBER_V3.CSP_ID` was not used because it belongs to a different identifier domain and had zero matches with `T_DEVICE.CSP_ID` in the audit.
- Duplicate active customer records are resolved to the latest record per device.
- Outages mapping to more than one current CSP are excluded, while their devices remain in the active baseline.
- The baseline is current-active, not historically reconstructed at each outage time.
- Missing outage membership in an evaluated comparison cell is treated as zero affected devices and therefore UP.
- Trial thresholds were 70% affected devices, a 30-minute overlap window, and 80% of CSP H3s for “almost all.”

Snowflake export query ID: `01c65c0f-0002-7845-0009-01fa26b6a5f2`.

## Files

- Query: [`sql/outage_devices.sql`](sql/outage_devices.sql)
- Rule contract: [`rules.md`](rules.md)
- Bucket output: [`data/output/outage_buckets.csv`](data/output/outage_buckets.csv)
- Detailed evidence: [`data/output/outage_evidence.csv`](data/output/outage_evidence.csv)
- CSP-H3 states: [`data/output/csp_h3_states.csv`](data/output/csp_h3_states.csv)
- Attribution events: [`data/output/attribution_events.csv`](data/output/attribution_events.csv)

The engine self-check passed, the 6,217 input outages reconciled one-to-one with evidence and bucket rows, and the output CSV totals matched this report.
