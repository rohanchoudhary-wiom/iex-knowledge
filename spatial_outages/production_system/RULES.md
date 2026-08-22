# Outage Attribution Rules

Rules form an ordered funnel: `1 → 2A → 2B → 3A → 3B → 4A → 4B → 4C → 5`. A later rule evaluates only outages not matched earlier. `UNKNOWN` devices stay in denominators and count as neither UP nor DOWN. Confidence is numeric from `0.00` to `1.00`.

## Evidence construction

| Step | Rule |
|---:|---|
| 1 | Resolve every posted and comparison device through Customer V2 and `POST https://router-outage-detection.i2e1.in/GetDeviceLivePing` in JSON batches of at most 200 device IDs. Interpret `latestPing` as IST: DOWN at 10 minutes or more, UP below 10 minutes, otherwise UNKNOWN. API failure or truncation fails the refresh closed. |
| 2 | Consume finalized OPEN outage membership only from `GET https://router-outage-detection.i2e1.in/get_outage_attribution?status=OPEN`. Snowflake outage V3 tables do not define the operational view. Attribution does not detect or split outages using temporal gaps; last successful ping plus 5 minutes is supporting proxy timing only. |
| 3 | Spatially cluster current DOWN outage members with valid locations. Keep every component: at least 10 devices can be SUPPORTED for Rules 2A–3B; smaller components remain REVIEW evidence but can reach Rule 4 when their only review reason is the member-count gate. |
| 4 | For each component, calculate its centre and R70/R80/R90/R100 profile. Re-cluster once when R90−R80 exceeds 500 m. The R90 core defines the comparison polygon; R100 is tail evidence only. |
| 5 | A component is SUPPORTED when it contains at least 10 DOWN members, R90 is at most 1 km, and R90−R80 is at most 500 m. Review/noise components do not veto a separate supported component. |
| 6 | For fibre-cut evaluation, combine every current DOWN and UP device inside the comparison polygon and cluster them as one local population before evaluating house/gali evidence. DOWN candidates are included regardless of CSP or finalized outage membership. If address/locality rules miss, re-cluster all DOWN candidates at tighter reach for directional Rule 4C evidence. Run this check for supported components after Rules 2A–3B fail and for compact review components whose only review reason is fewer than 10 DOWN members. |

A CSP is qualified inside a comparison polygon when it has at least 5 devices. UNKNOWN devices remain in its denominator.

## House/gali evidence

For each mixed DOWN/UP cluster, flatten the Customer V2 address fields of every current DOWN candidate and run `shiprocket-ai/open-modernbert-indian-address-ner` in batches. Retain `house_details`, `road`, `locality`, and `sub_locality` entities at model confidence of at least `0.50`.

| Scope | Valid grouping |
|---|---|
| `HOUSE` | At least 2 addressable DOWN devices share the same normalized house with road or locality context |
| `GALI` | HOUSE does not match, and at least 3 addressable DOWN devices share the same normalized road with locality context |
| `AREA` / `LOCALITY` | Rule 4B support only; it cannot produce `FIBRE_CUT` without the compactness and healthy-control gates |
| `UNKNOWN` | No valid same-house or same-gali grouping exists |

A valid HOUSE or GALI group is the Rule 4A match. Rule 4B runs only when 4A fails and locality evidence is reinforced by every spatial and counterfactual gate below. Rule 4C runs only when both address paths fail and tests a narrow one-direction DOWN component. The strongest 10-minute failure share remains visible as timing evidence but does not veto Rule 4B or 4C.

## 1. CSP-wide outage

| Field | Rule |
|---|---|
| Boundary | Complete Customer V2 population of the target CSP |
| Match | Target CSP is at least 70% DOWN |
| Attribution | `ISP_OLT_CSP_SIDE` |
| Confidence | `0.80` |

Below the 70% gate, continue to Rule 2A without emitting a CSP-wide verdict.

## 2A. Local CSP-specific outage with peers

| Field | Rule |
|---|---|
| Boundary | One supported R90 comparison polygon |
| Match | Target CSP is at least 80% locally DOWN, at least one qualified peer exists, and every qualified peer is at most 20% DOWN |
| Attribution | `CSP_SPECIFIC_LOCAL` |
| Confidence | `0.80` |

## 2B. Local CSP-specific outage without peers

| Field | Rule |
|---|---|
| Boundary | One supported R90 comparison polygon |
| Match | No qualified peer exists and the target CSP is at least 90% locally DOWN |
| Attribution | `CSP_SPECIFIC_LOCAL` |
| Confidence | `0.60` because only one CSP is observable |

## 3A. Multi-CSP premise power

| Field | Rule |
|---|---|
| Boundary | One supported R90 comparison polygon |
| Match | At least two qualified CSPs exist and every qualified CSP is at least 70% concurrently DOWN |
| Attribution | `PREMISE_POWER` |
| Confidence | Lowest DOWN share across all qualified CSPs, capped at `0.90` |

## 3B. Monopoly premise power

| Field | Rule |
|---|---|
| Boundary | One supported R90 comparison polygon |
| Match | No qualified peer exists, the target CSP is at least 70% locally DOWN, and Rule 2B did not match; this is effectively 70% to below 90% DOWN |
| Attribution | `PREMISE_POWER` |
| Confidence | `0.60` because only one CSP is observable |

## 4. Fibre cut

| Field | Rule |
|---|---|
| Boundary | One supported comparison polygon after Rules 1–3B fail, or one compact review polygon whose only review reason is fewer than 10 DOWN members; compact review polygons skip Rules 2A–3B |
| Population | Every current DOWN and UP comparison-polygon device clustered together; CSP and outage membership do not filter Rule 4 candidates |
| Rule 4A match | At least 2 DOWN devices share one normalized HOUSE with local context, or at least 3 share one normalized GALI; the matched mixed cluster contains at least one healthy UP control |
| Rule 4B match | If 4A fails: at least 3 locality-matched DOWN candidates have R90 at most 500 m, and at least 70% of 5 or more known non-affected controls are UP; strongest 10-minute failure share is diagnostic only |
| Rule 4C match | If 4A and 4B fail: tighter variable-density clustering finds at least 5 DOWN candidates with principal length from 50 m through 500 m, directionality ratio at least 3.0, and perpendicular P90 width at most 50 m, while at least 70% of 5 or more known non-component controls are UP; orientation and CSP do not filter the group, and strongest 10-minute failure share is diagnostic only |
| Attribution | `FIBRE_CUT` for only the affected pair or group |
| Confidence | Rule 4A uses average retained NLP confidence capped at `0.90`; Rules 4B and 4C use `0.60` |

## 5. Unknown

If Rules 1–4C fail, return `UNKNOWN` with confidence `0.00`.

## Group roll-up

| Condition | Result |
|---|---|
| Every attribution-bearing sub-outage agrees on one non-UNKNOWN attribution | Return that attribution |
| Attribution-bearing sub-outages disagree | `UNKNOWN / LOCAL_GROUPS_DISAGREE` |
| No qualified peer exists | Evaluate Rule 2B and then Rule 3B |
| Only unmatched review groups remain | `UNKNOWN / SUB_OUTAGE_REVIEW_ONLY` |
| No current located DOWN members remain | `UNKNOWN / NO_CURRENT_DOWN_MEMBERS` |

There is no low-confidence fallback that forces a residual pattern into `PREMISE_POWER` or `FIBRE_CUT`.

## Output schema

| Field | Type | Description |
|---|---|---|
| `outage_id` | `STRING` | Unique outage identifier |
| `attribution` | `STRING` | `ISP_OLT_CSP_SIDE`, `CSP_SPECIFIC_LOCAL`, `PREMISE_POWER`, `FIBRE_CUT`, or `UNKNOWN` |
| `confidence` | `DECIMAL(3,2)` | Confidence score from `0.00` to `1.00` |
| `affected_device_ids` | `ARRAY<STRING>` | Devices covered by the attribution; a fibre pair/group can include comparison devices that were not finalized outage members |
| `evaluated_at` | `TIMESTAMP` | Attribution evaluation time |
| `device_pings` | `ARRAY<OBJECT>` | Upstream `GetDeviceLivePing` snapshot for every evaluated device. Each item contains `{device_id: STRING, status: UP\|DOWN\|UNKNOWN, latest_ping_at: TIMESTAMP\|NULL, csp: STRING\|NULL, in_polygon: BOOLEAN}` |

The atlas must expose provider eligibility, polygon membership, all-CSP DOWN/UP fibre candidates, affected member/comparison provenance, healthy device IDs, house/gali evidence, directional component count/length/width/ratio/control evidence, numeric confidence, evaluation time, and the upstream device-ping snapshot.
