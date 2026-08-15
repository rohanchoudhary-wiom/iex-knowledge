# Spatial outage attribution rules

This file is the implementation contract for the CSV attribution engine. Rules run in the order shown below, and the first match wins.

The engine identifies the most likely **failure domain**. It does not prove a physical root cause such as a power cut, fibre cut, OLT fault, or backend defect.

## Input

The single input is `data/input/outage_devices.csv`, exported by [`sql/outage_devices.sql`](sql/outage_devices.sql).

```text
latest active CUSTOMER_V_2 row per device with PLAN_EXPIRY_TIME
  -> last valid successful ping in the rolling 15-day snapshot
  -> retain only PLAN_EXPIRY_TIME > last successful ping + 12 hours
  -> T_ADDRESS through GOOGLE_ADDRESS_ID for latitude and longitude
  -> T_DEVICE through DEVICE_ID for CSP_ID
  -> eligible OUTAGE_MEMBER_V3 rows through CUSTOMER_V_2.DEVICE_ID
```

### Cohort gate

Eligibility is applied before outage membership, denominator construction, polygon comparison, or map rendering:

```text
eligible = latest Customer V2 row is ACTIVE
       and PLAN_EXPIRY_TIME is present
       and a valid successful ping exists in the 15-day snapshot
       and PLAN_EXPIRY_TIME > last_successful_ping_ist + 12 hours
```

The comparison is strict: equality at 12 hours is excluded. An outage membership never bypasses this gate. `plan_expiry_ist` and `last_successful_ping_ist` remain in the CSV as audit fields but are not re-evaluated by the Python engine.

Required engine columns:

| Column | Meaning |
|---|---|
| `device_id` | Stable device identifier |
| `csp_id` | CSP serving the device |
| `h3_id` | Fixed H3 cell containing the device/router |
| `outage_id` | Source outage identifier; blank when the device has no outage |
| `member_first_fail_at_ist` | First failure time for this device in the outage |

`mobile`, `latitude`, and `longitude` may remain in the CSV for reconciliation. They are not used by the current rules.

### Input validity rules

- `device_id` and `csp_id` are mandatory on every row. H3 is required for spatial comparison; devices without a usable location remain in the outage member count but not spatial denominators or polygon geometry.
- One device must resolve to exactly one `CSP_ID + H3_ID` pair.
- A router/device always belongs to the same H3. Conflicting H3 assignments stop the run.
- An outage must belong to exactly one CSP.
- If `outage_id` is present, failure time must also be present.
- Duplicate membership for the same device and H3 is counted once.
- At least one active device row and one outage row are required.
- Invalid input stops the run; it is not converted into an `UNKNOWN` bucket.

## Terms and calculations

| Term | Exact meaning |
|---|---|
| Eligible devices | Distinct active devices in one `CSP_ID + H3_ID` cell |
| Affected devices | Distinct eligible devices belonging to an outage in the current attribution event |
| Affected share | `affected_devices / eligible_devices` |
| DOWN cell | Affected share is at least `0.70` |
| UP cell | Affected share is below `0.70` |
| Affected H3 | An H3 where the CSP's cell is DOWN |
| Eligible H3 | Any H3 containing an active device for the CSP |
| CSP-wide affected share | `affected_h3_count / eligible_h3_count` |
| CSP down share | Concurrent affected eligible devices divided by all eligible Customer V2 devices for the CSP |
| Polygon peer CSP | Another CSP with an eligible Customer V2 device inside the outage polygon |
| Neighboring CSP | Another active CSP with devices in the same target H3 |
| Target H3 | The outage's DOWN H3 with the most DOWN CSPs; affected share breaks a tie. Where none are DOWN, the same selection is made from its member H3s |
| Attribution event | Outages whose start times fall inside the same anchored time window |

There is **no minimum device-count rule**. Cell size does not block classification:

| Affected | Eligible | Share | Cell state |
|---:|---:|---:|---|
| 1 | 1 | 100% | DOWN |
| 2 | 3 | 66.7% | UP |
| 3 | 3 | 100% | DOWN |
| 7 | 10 | 70% | DOWN |

A small cell can therefore be DOWN. Its final outage bucket can still be `UNKNOWN` when the comparisons required to attribute a failure domain do not exist.

## Tunable values

| Setting | Default | Boundary |
|---|---:|---|
| Minimum affected share in a CSP-H3 cell | 70% | Inclusive: exactly 70% is DOWN |
| Outage overlap window | 30 minutes | Inclusive: exactly 30 minutes joins the event |
| "Almost all" H3s for one CSP | 80% | Inclusive: exactly 80% qualifies |

These are the only rule thresholds. Production values must be selected through validation against known outages and false promises.

## Event construction

An outage starts at the earliest `member_first_fail_at_ist` among its devices. Outages are sorted by start time.

The 30-minute grouping is anchored to the first outage in the event; it is not a rolling or chained window.

```text
for each outage in start-time order:
    if there is no current event:
        start a new event
    else if outage.start - first_outage_in_current_event.start <= 30 minutes:
        add outage to the current event
    else:
        start a new event
```

Example: outages at 10:00 and 10:25 share an event. An outage at 10:50 starts a new event even though it is only 25 minutes after 10:25.

## Cell-state calculation

The engine calculates states for:

- every H3 used by an affected CSP; and
- every active CSP present in an affected H3.

```text
affected_devices = distinct failed devices for CSP_ID + H3_ID in this event
eligible_devices = distinct active devices for CSP_ID + H3_ID

if affected_devices > eligible_devices:
    stop: the outage evidence exceeds the active baseline

affected_share = affected_devices / eligible_devices

if affected_share >= 0.70:
    state = DOWN
else:
    state = UP
```

Current implementation assumption: when an active comparison cell has no matching outage member inside the event, its affected count is zero and it is treated as UP.

## Ordered bucket rules

| Priority | Rule ID | If | Bucket |
|---:|---|---|---|
| 0 | `R0_CSP_DOWN_SHARE` | At least 80% of the CSP's eligible Customer V2 devices are concurrently down | `CSP_SIDE` |
| 0 | `R0_POLYGON_PEER_UP` | Another active CSP is inside the polygon and no peer-CSP device there is concurrently affected | `CSP_SIDE` |
| 1 | `R1_NO_DOWN_CSP_H3` | The affected CSP has no DOWN H3 | `UNKNOWN` |
| 2 | `R2_MULTI_CSP_ONE_H3` | At least two CSPs are DOWN in the target H3, all have another H3 available for comparison, and they are not all DOWN elsewhere | `UNKNOWN` |
| 3 | `R3_MULTI_CSP_MULTI_H3` | At least two CSPs are DOWN in the target H3, all have another H3 available for comparison, and every shared CSP is also DOWN in at least one other H3 | `UNKNOWN` |
| 4 | `R4_CSP_WIDE` | Fewer than two CSPs are DOWN in the target H3; the affected CSP has at least two eligible H3s; at least 80% of its H3s are DOWN; and a neighboring CSP exists | `CSP_SIDE` |
| 5 | `R5_NEIGHBOR_CSP_UP` | Fewer than two CSPs are DOWN in the target H3; the affected CSP has at least two eligible H3s; below 80% of its H3s are DOWN; and a neighboring CSP is UP | `CSP_SIDE` |
| 6 | `R6_*` | None of the rules above can make a supported comparison | `UNKNOWN` |

### Rule 1 — NOISE

```text
if affected CSP has zero DOWN H3s:
    bucket = UNKNOWN
```

Example: CSP Alpha has 2 affected devices out of 10 in H3 A. Its share is 20%, so the cell is UP and the outage remains `UNKNOWN` unless the polygon peer rule matched first.

### Rule 2 — AREA-SHARED

```text
if DOWN CSPs in target H3 >= 2
and every shared CSP has at least 2 eligible H3s
and at least one shared CSP is not DOWN in another H3:
    bucket = UNKNOWN
```

Example:

| H3 | CSP Alpha | CSP Beta |
|---|---|---|
| H3 A | DOWN | DOWN |
| H3 B | UP | UP |

The failure is shared inside H3 A rather than consistently spread across both CSP networks.

### Rule 3 — REGIONAL

```text
if DOWN CSPs in target H3 >= 2
and every shared CSP has at least 2 eligible H3s
and every shared CSP is DOWN in at least one other H3:
    bucket = UNKNOWN
```

Example:

| H3 | CSP Alpha | CSP Beta |
|---|---|---|
| H3 A | DOWN | DOWN |
| H3 B | DOWN | DOWN |

Both CSPs failing across multiple H3s supports a regional failure domain.

### Rule 4 — ISP / OLT

```text
if affected CSP has at least one DOWN H3
and fewer than 2 CSPs are DOWN in the target H3
and affected CSP has at least 2 eligible H3s
and affected CSP's DOWN H3 share >= 80%
and another CSP is active in the target H3:
    bucket = CSP_SIDE
```

Example:

| Coverage | CSP Alpha | CSP Beta |
|---|---|---|
| DOWN H3s / eligible H3s | 9 / 10 | 0 / 10 |
| Target H3 | DOWN | UP |

CSP Alpha is down across almost all of its footprint while CSP Beta supplies a local comparison.

### Rule 5 — LOCAL CSP FAULT

```text
if affected CSP has at least one DOWN H3
and fewer than 2 CSPs are DOWN in the target H3
and affected CSP has at least 2 eligible H3s
and affected CSP's DOWN H3 share < 80%
and at least one neighboring CSP is UP:
    bucket = CSP_SIDE
```

Example:

| H3 | CSP Alpha | CSP Beta |
|---|---|---|
| H3 A | DOWN | UP |
| H3 B | UP | UP |

The evidence points to CSP Alpha in one local area, not the whole area and not most of Alpha's network.

### Rule 6 — UNKNOWN

`UNKNOWN` is always the final fallback. It receives `MEDIUM` confidence.

```text
if multiple CSPs are DOWN here or affected CSP has fewer than 2 eligible H3s:
    rule = R6_NO_CROSS_H3_COMPARISON
else if no neighboring CSP exists in the target H3:
    rule = R6_NO_NEIGHBOR_CSP
else:
    rule = R6_AMBIGUOUS_PATTERN

bucket = UNKNOWN
confidence = MEDIUM
```

Examples include a 1/1 DOWN cell for a CSP that exists in only one H3, or a CSP-wide pattern in an H3 where no other CSP exists for comparison.

## Confidence

| Decision | HIGH | MEDIUM |
|---|---|---|
| Cause bucket | Every DOWN H3 used for the affected CSP is strictly above 70% | At least one DOWN H3 is exactly 70% |
| UNKNOWN | Never | Always |

Exactly 70% is enough for a DOWN state, but it stays `MEDIUM` confidence because it sits directly on the decision boundary.

## Output evidence

The engine writes four CSVs:

| File | Purpose |
|---|---|
| `csp_h3_states.csv` | Device counts, affected share, and UP/DOWN result for each evaluated CSP-H3 cell |
| `attribution_events.csv` | Time-grouped outage event boundaries and counts |
| `outage_evidence.csv` | Target H3, H3 coverage, CSP comparison, matched rule, bucket, and confidence for each outage |
| `outage_buckets.csv` | Minimal publishable result: `outage_id`, `bucket`, and `confidence` |

Source `outage_id` values are preserved. `attribution_event_id` exists only to record which outages were evaluated together.

## Guardrails requiring validation

- The denominator is the current active fleet in the input CSV, not a historical as-of baseline.
- Missing outage evidence in an event is currently interpreted as zero affected devices for that comparison cell.
- H3 assignment is fixed per device in one run; conflicting mappings are rejected.
- The rule labels indicate likely failure domains, not confirmed physical root causes.
- The 70%, 30-minute, and 80% values are trial defaults until back-testing sets production values.

## Code ownership

| Logic | Source |
|---|---|
| CSV validation and output schemas | [`attribution/csv_io.py`](attribution/csv_io.py) |
| Event grouping, cell state, target H3, and confidence | [`attribution/engine.py`](attribution/engine.py) |
| Tunable defaults | [`attribution/domain/thresholds.py`](attribution/domain/thresholds.py) |
| Rule order | [`attribution/rules/ordered.py`](attribution/rules/ordered.py) |
| Bucket rules | [`attribution/rules/`](attribution/rules/) |
| Executable examples covering every bucket | [`attribution/self_check.py`](attribution/self_check.py) |
