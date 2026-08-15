# Outage trigger, severity, and attribution

Source board: [Outage Cause Attribution Tree](https://www.figma.com/board/amY5LW3x5vGuzu2gO6TPSq/Outage-Cause-Attribution-Tree?node-id=0-1&p=fseverity)

## Goal

Use the smallest reliable outage fact to assign deterministic cause buckets while minimizing false promises and retaining at least 80% of known outages.

Severity, cause, and confidence remain separate:

- **Severity**: affected customers and duration.
- **Cause bucket**: the first matching failure-domain rule.
- **Confidence**: evidence completeness, not an uncalibrated probability.

## Verified source

`OUTAGE_MEMBER_V3` is sufficient as the raw outage-membership source:

```sql
SELECT
  outage_id,
  device_id,
  csp_id,
  member_first_fail_at_ist
FROM PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.OUTAGE_MEMBER_V3
WHERE NOT COALESCE(_fivetran_deleted, FALSE);
```

The 2026-08-12 schema audit found 1,027,868 active rows. Every active row has an outage ID, device ID, CSP ID, and member failure time; every `(outage_id, device_id)` pair is unique; and all members join to one of 39,177 active `OUTAGE_V3` outages with no CSP mismatch. `OUTAGE_ID` belongs to exactly one CSP.

Derive the shared source-outage start without another table:

```text
outage_start_ist = MIN(member_first_fail_at_ist) per outage_id
```

`OUTAGE_V3.OPENED_AT_IST` is detector creation time, not outage start: it trails `FIRST_FAIL_AT_IST` by 10–25 minutes (median 20). `TICK_LEDGER_V3` is detector-health metadata and is not needed for v1 attribution.

## Required evidence

The latest active `PROD_DB.DYNAMODB_READ.CUSTOMER_V_2` row supplies the customer, device ID, and plan expiry. The cohort keeps a device only when a valid successful ping exists in the rolling 15-day snapshot and `PLAN_EXPIRY_TIME > last_successful_ping_ist + 12 hours`. `GOOGLE_ADDRESS_ID → PROD_DB.PUBLIC.T_ADDRESS.ID` supplies latitude and longitude, while `DEVICE_ID → PROD_DB.PUBLIC.T_DEVICE.DEVICE_ID` supplies the CSP ID. No `ACTIVE_BASE` rows are used, and outage membership cannot restore an ineligible device.

## CSV-only implementation

`sql/outage_devices.sql` is one SELECT joining address, device, and outage data. It exports `data/input/outage_devices.csv`, the only file read by `attribute.py`. Mobile is retained for reconciliation but never used by the rules, and the identifier-bearing CSV is ignored by Git.

This is deliberately a latest-state eligible baseline, not a historical reconstruction. Add dated fleet snapshots only if historical denominators become a validated requirement.

## Minimal build plan

1. Export eligible Customer V2 devices, T_ADDRESS coordinates, CSP IDs, and eligible outage membership using the single SELECT.
2. Count distinct active devices per CSP-H3 and affected devices per comparison event.
3. Use affected share alone to assign each valid CSP-H3 as `DOWN` or `UP`; there is no device-count threshold.
4. Match temporally overlapping source outages so CSPs and zones can be compared.
5. Apply the attribution tree in order; the first matching rule wins.
6. Emit the minimal result and retain supporting evidence for audit.

## Attribution tree

1. **Enough evidence?**
   - No → `NOISE`.
2. **Multiple CSPs down in the same zone?**
   - Yes: **Are those CSPs also down in their other zones?**
     - No → `AREA-SHARED`: likely power or shared physical dependency.
     - Yes → `REGIONAL`: wide-area upstream or regional event.
3. Otherwise, **is the affected CSP down across all/almost all of its zones?**
   - Yes → `ISP / OLT`: CSP-wide upstream or OLT failure.
4. Otherwise, **are neighboring CSPs in this zone up?**
   - Yes → `LOCAL CSP FAULT`: fibre cut, local switch, node power issue, etc.
   - No comparison → `UNKNOWN`: single-zone CSP or single-CSP zone.

Any ambiguous pattern falls into `UNKNOWN`.

## Output

Published result:

```text
outage_id, bucket, confidence
```

Internal audit evidence:

```text
outage_id, attribution_event_id, csp_id, h3_id, outage_start_ist,
affected_devices, eligible_devices, affected_share,
affected_h3_count, eligible_h3_count, compared_csp_count, rule_matched,
bucket, confidence
```

`attribution_event_id` is derived only to group overlapping source outages across CSPs. Preserve the V3 `outage_id`; do not replace it.

## Confidence

Use evidence grades until outcome-calibrated probabilities exist:

- **High**: all required comparisons exist and the matched rule is clearly beyond its thresholds.
- **Medium**: required comparisons exist but evidence is close to a threshold or based on a small denominator.
- **Low evidence**: route to `NOISE` or `UNKNOWN`, not a confident physical-cause bucket.

## Validation

Tune only three v1 parameters:

- minimum affected share;
- temporal overlap window;
- fraction defining “almost all zones.”

Choose the simplest threshold set that captures at least 80% of known outages while minimizing false incident promises. Report mapping coverage, bucket coverage, `UNKNOWN` rate, precision/recall, and detection delay by CSP and H3 density.

## Non-goals for v1

- `TICK_LEDGER_V3` ingestion;
- recovery or duration modelling;
- probability models;
- claiming a proven power cut or fibre cut without independent evidence;
- replacing the existing strict one-hour/70%-of-H3 retrospective label.

The 15-minute candidate and 30-minute confirmed-silence rule remains the proposed operational trigger. This V3 bucketing work supplies historical outage labels for validating it; the two outputs must remain separately named.
