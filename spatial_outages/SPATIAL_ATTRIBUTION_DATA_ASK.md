# Labs outage triage: data ask

## 1. Contract

Detection decides what is down and freezes the affected devices. Labs explains the likely cause and confidence. Communication decides what to tell each audience.

```text
one outage_id -> one polygon -> one attribution
```

- `outage_id` and its frozen device list are source truth after the shared input-cohort gate below.
- The one-outage-to-one-polygon mapping is a locked requirement, not an implementation choice.
- Labs never changes membership, splits an outage, merges outage IDs or creates another published ID.
- Labs may compare overlapping or concurrent outages internally, but overlap is evidence only.
- Overlap never creates or merges outage IDs, creates another polygon, or creates another output row.
- Attribution is always published against the original `outage_id`.
- The result may be revised while the outage is open and finalised after restoration.

Detection v0 assumption: an affected device was silent for 15 minutes, had a SUCCESS in the previous 30 minutes, and joined other devices that went silent inside the same 30-minute onset window. A changed Detection rule requires a versioned interface update.

Current input-cohort gate: keep only the latest active `CUSTOMER_V_2` row whose `PLAN_EXPIRY_TIME` is strictly more than 12 hours after that device's last valid successful ping in the rolling 15-day snapshot. A device with no valid successful ping is excluded. Apply this once before membership, denominators, polygon construction, and map rendering.

## 2. Triage output taxonomy

### Root cause: exactly one current value

| Value | Meaning |
|---|---|
| `CSP_SIDE` | Upstream ISP or OLT-shaped failure; merged for v1 |
| `ACCESS_FIBRE` | Local access-fibre failure |
| `PREMISE_POWER` | Power or shared electrical failure |
| `UNKNOWN` | Evidence cannot support a reliable cause |

`CSP_SIDE` has a nullable `sub_cause` reserved for a future ISP/OLT split.

### Separate dimensions

| Field | Values |
|---|---|
| `spatial_extent` | LOCAL, SUB_REGIONAL, REGIONAL |
| `event_pattern` | UNPLANNED, SCHEDULED, RECURRING, UNKNOWN |

Regional and sub-regional describe scale, not root cause.

## 3. Labs flow

| Step | Action |
|---:|---|
| 1 | Load one detected outage and its frozen devices |
| 2 | Build exactly one polygon and calculate its area in km² |
| 3 | Enrich from portfolio-wide ping, location, CSP and contact history |
| 4 | Compare nearby concurrent outages internally without changing their IDs |
| 5 | Calculate cause evidence, confidence and restoration class |
| 6 | Upsert one versioned result for each `outage_id` |

## 4. Required inputs

### Detection GET: keep narrow

| Field | Meaning |
|---|---|
| `outage_id` | Source and output key |
| `csp_id` | CSP owning the outage |
| `device_list` | Frozen affected devices |
| `outage_trigger_time` | Detection time |

Detection sends only the bounded affected set. Labs derives all comparison data independently.

### Portfolio-wide data available to Labs

| Data | Required fields/use |
|---|---|
| Ping history | `device_id`, server timestamp, reachable/unreachable state, last valid successful ping |
| Device registry | `device_id`, active state, plan expiry, event-time latitude/longitude and CSP mapping |
| Concurrent outages | All open `outage_id`, `csp_id`, device lists and trigger times |
| Recovery state | Device reachability after onset and outage restoration time |
| ISD tickets/calls | Timestamp, device/customer link and reason text when available |
| CSP confirmation | Neutral confirmed-cause response linked to `outage_id` |

Historical coordinates and mappings must be used for backfills. The current export uses latest-state Customer V2 data and is therefore a current snapshot, not a historically reconstructed baseline. Missing, stale or merely non-member devices are not treated as healthy.

## 5. Polygon and device counts

- All usable member locations create exactly one polygon for the `outage_id`.
- Distant members never create sub-outages or extra published polygons.
- `polygon_area_km2` is the geodesic area.
- `unhealthy_device_count` equals the distinct frozen members that pass the shared input-cohort gate.
- `located_unhealthy_device_count` records members usable for geometry.
- `healthy_device_count` counts non-members inside the polygon that were live in Detection's same 30-minute pre-onset window and remained reachable at the comparison time.
- Healthy and unhealthy counts use distinct devices.
- If one reliable polygon cannot be built, return a geometry-quality failure; do not split the outage.

## 6. Internal evidence

These signals support triage but never alter `outage_id` or the final output grain.

| Signal | Question |
|---|---|
| Spatial saturation | What share of previously live devices inside the polygon went down? |
| Cross-CSP correlation | Did other CSPs in the same place fail or survive in the same window? |
| Concurrent-outage correlation | Do separate outage IDs describe one likely physical occurrence? |
| Co-failure history | Have the same devices repeatedly failed together? |
| Onset spread | How closely did device failures begin? |
| Restoration shape | Did devices recover together or gradually? |
| Recurrence and clock time | Does the same group fail repeatedly in the same time band? |
| Customer contact rate | How unusual is contact volume per affected device versus the locality baseline? |
| Sentinel state | Did a battery-backed reference device survive? Available only where deployed |

Cross-CSP correlation is evidence, not proof. It is trusted only where CSPs have independent fibre and upstream paths.

## 7. Revision behaviour

- Triage runs at detection and refreshes while the outage remains open.
- Each refresh increments `revision` and sets `revised_at`.
- Duration, contacts and restoration shape may change the cause or confidence.
- The current published row remains keyed only by `outage_id`.
- The verdict is finalised on restoration.
- Updates must be idempotent.

## 8. Final POST/output

Exactly one current row per `outage_id`; `outage_id` is the only key.

| Field | Meaning |
|---|---|
| `outage_id` | Source and output key |
| `geometry`, `geometry_version` | Single outage polygon |
| `polygon_area_km2` | Polygon size in square kilometres |
| `unhealthy_device_count` | Complete frozen affected set |
| `located_unhealthy_device_count` | Affected devices used for geometry |
| `healthy_device_count` | Positively healthy comparison devices inside the polygon |
| `root_cause` | CSP_SIDE, ACCESS_FIBRE, PREMISE_POWER or UNKNOWN |
| `sub_cause` | Nullable future ISP/OLT detail |
| `spatial_extent` | LOCAL, SUB_REGIONAL or REGIONAL |
| `event_pattern` | UNPLANNED, SCHEDULED, RECURRING or UNKNOWN |
| `confidence` | Calibrated value when labels exist; coarse band before that |
| `cause_distribution` | Support assigned to each root-cause value |
| `evidence[]` | Signals that fired and their values |
| `expected_restoration_class` | Downstream restoration-duration class |
| `revision`, `revised_at`, `is_final` | Verdict lifecycle |
| `engine_version`, `snapshot_at` | Reproducibility |

Evidence stays inside the outage row or internal audit store; it does not create another published key.

Communication applies audience-specific confidence thresholds. A low-confidence hypothesis may be sent to the CSP for confirmation, while customers receive a cause-specific message only above the stricter customer threshold. Labs supplies confidence and restoration class; it does not decide or send either message.

## 9. Labels and validation

- The feedback loop ships alongside v1, not as a later phase.
- Backfill historical pings, outages and ISD contacts before waiting for new data.
- Use CSP confirmation as the strongest practical cause label.
- Ask neutrally; never ask the CSP to confirm the model's guess.
- Do not connect confirmation to penalties, SLA scoring or incentives.
- Ticket volume validates visibility, not cause, unless reason text supplies a cause.
- Normalise contact rate against each locality's own baseline.
- Keep `UNKNOWN` as a valid result instead of forcing a guess.
- Calibrate confidence on held-out labelled outages.
- Beat the majority-class or “always power” baseline; otherwise stop cause classification.
- Validate sentinels in a small pilot before deployment.

## 10. Open decisions and limits

| Gap | Decision needed |
|---|---|
| Fibre/power separation | Validate that the available signals separate the pair reliably |
| Spatial extent | Lock LOCAL, SUB_REGIONAL and REGIONAL boundaries |
| Polygon distortion | Set location-coverage and maximum-gap quality rules |
| Restoration class | Define the duration bands consumed by Communication |
| CSP shutdown | Decide whether wilful CSP shutdown needs its own root cause |
| ISP/OLT split | Revisit only when trustworthy `olt_id` or validated grouping exists and operations needs the distinction |

Ping reachability alone cannot prove physical root cause. If the classifier does not beat the baseline, retain the data plumbing and collapse the unsupported cause distinction.

## 11. Minimum handoff

Required before implementation:

- Narrow Detection GET with stable `outage_id` and frozen membership.
- Portfolio-wide historical ping access.
- Event-time coordinates and CSP mappings.
- Concurrent-outage visibility.
- Historical ISD contacts and CSP-confirmation path.
- Agreed output taxonomy, restoration classes and revision cadence.

Stop classification for an outage when keys, membership, timestamps or location coverage fail validation. Do not guess missing facts.
