# Internet Outage Attribution — Decision Brief

**Prepared by:** Rohan Choudhary

**Audience:** Named Internet Experience reviewers

## Executive summary

When an outage is reported, a list of DOWN devices alone cannot explain the cause. The attribution system first establishes **when** devices failed, then identifies **where** failures concentrate, and finally compares CSP behaviour inside the same local boundary.

This produces one reviewable outcome—`ISP_OLT_CSP_SIDE`, `CSP_SPECIFIC_LOCAL`, `PREMISE_POWER`, `FIBRE_CUT`, or `UNKNOWN`—with a numeric confidence score and the evidence behind it. Missing or conflicting evidence remains visible instead of being forced into a cause.

> **Decision flow:** Live device state → failure-time window → spatial cluster → comparison polygon → cross-CSP pattern → attribution and confidence

## How evidence becomes a decision

| Stage | Question | System behaviour |
|---|---|---|
| 1. Establish state | Is each device currently reachable? | Latest successful ping below 10 minutes is **UP**; 10 minutes or more is **DOWN**; invalid, missing, future-dated, or unavailable evidence is **UNKNOWN**. |
| 2. Align time | Did the failures occur together? | Current DOWN devices are aligned into common failure-time windows. |
| 3. Find place | Are failures spatially concentrated? | State-of-the-art spatial clustering identifies local failure groups within each time window. |
| 4. Define comparison | Which nearby devices provide a fair control group? | A robust cluster core becomes the comparison polygon; all CSP devices inside it are evaluated. |
| 5. Attribute | Is the pattern broad, provider-specific, shared across providers, or concentrated within a house or gali? | Ordered rules evaluate CSP behaviour and local grouping evidence to select one cause. |
| 6. Score | How strongly does the evidence support that cause? | A `0.00–1.00` confidence score is returned separately from the attribution. |

UNKNOWN devices stay in denominators and count as neither UP nor DOWN. A CSP is eligible for local comparison when at least 5 of its devices are inside the polygon.

## Clustering and polygon strategy

The system combines temporal alignment with state-of-the-art spatial clustering to identify coherent local failure zones. Cluster quality, concentration, and stability checks separate supported outage areas from sparse or ambiguous evidence.

Each supported cluster produces a robust core polygon that defines the local comparison population. Devices from every CSP inside that boundary are evaluated together, while peripheral observations remain contextual evidence.

Clustering establishes **where a fair comparison should be made**; it does not determine the outage cause. Attribution is decided only after CSP behaviour and local grouping evidence are evaluated inside the resulting polygon.

For fibre-cut assessment, DOWN outage devices are combined with nearby healthy UP devices inside the comparison polygon and clustered as one local population. House and gali grouping is evaluated only after this mixed-device clustering step.

## Ordered attribution logic

Rules form a funnel from highest to lowest priority. Each rule evaluates only outages not matched by an earlier rule, and a later rule cannot replace an earlier result.

| Priority | Exact rule | Attribution |
|---:|---|---|
| **1** | Target CSP is **at least 70% DOWN across its complete population** | `ISP_OLT_CSP_SIDE` |
| **2A** | Else, locally the target CSP is **at least 80% DOWN**, at least one qualified peer exists, and **all qualified peers are at most 20% DOWN** | `CSP_SPECIFIC_LOCAL` |
| **2B** | Else, **no qualified peer exists** and the target CSP is **at least 90% DOWN locally** | `CSP_SPECIFIC_LOCAL` with `0.60` confidence |
| **3A** | Else, **at least two qualified CSPs exist** and **every qualified CSP is at least 70% DOWN** | `PREMISE_POWER` |
| **3B** | Else, **no qualified peer exists** and the target CSP is **at least 70% DOWN locally** | `PREMISE_POWER` with `0.60` confidence; because Rule 2B runs first, this covers 70% to below 90% DOWN |
| **4** | Else, cluster DOWN outage devices together with nearby healthy UP devices inside the comparison polygon; then, if affected device pairs or groups share the **same house or gali**, attribute those affected pairs or groups | `FIBRE_CUT` |
| **5** | If no valid same-house or same-gali grouping exists | `UNKNOWN` |

> **Fibre-cut evidence flow:** DOWN outage devices + healthy UP devices → mixed-device clustering → house/gali grouping → affected pair/group attribution

## Output schema

| Field | Type | Description |
|---|---|---|
| `outage_id` | `STRING` | Unique outage identifier |
| `attribution` | `STRING` | `ISP_OLT_CSP_SIDE`, `CSP_SPECIFIC_LOCAL`, `PREMISE_POWER`, `FIBRE_CUT`, or `UNKNOWN` |
| `confidence` | `DECIMAL(3,2)` | Confidence score from `0.00` to `1.00` |
| `affected_device_ids` | `ARRAY<STRING>` | Devices covered by the attribution; contains only the affected pair/group for fibre cuts |
| `evaluated_at` | `TIMESTAMP` | Time at which the attribution was evaluated |
| `device_pings` | `ARRAY<OBJECT>` | Upstream `GetDeviceLivePing` snapshot for every evaluated device. Each item contains `{device_id: STRING, status: UP\|DOWN\|UNKNOWN, latest_ping_at: TIMESTAMP\|NULL, csp: STRING\|NULL, in_polygon: BOOLEAN}` |
