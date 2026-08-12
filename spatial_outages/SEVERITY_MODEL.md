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

## Required external evidence

The four-column source is enough from the three V3 tables, but the attribution tree also needs:

1. a historical `device_id → h3_id` mapping valid at `member_first_fail_at_ist`;
2. eligible active-device counts per `csp_id, h3_id` at that time;
3. the active-zone count per CSP and the operators available for comparison in each zone.

Without these denominators, affected counts cannot establish that an operator or zone was down.

## Minimal build plan

1. Extract the four-column member fact and filter Fivetran deletions.
2. Attach the device's historical H3; reject ambiguous mappings and report coverage.
3. Build eligible-device denominators for every CSP-H3 at the event time.
4. Aggregate to `outage_id, csp_id, h3_id` with affected count, eligible count, affected share, and first-failure time.
5. Mark each CSP-H3 as `DOWN`, `UP`, or `UNKNOWN` using validated count/share thresholds.
6. Match temporally overlapping source outages so different operators and zones can be compared.
7. Apply the attribution tree in order; the first matching rule wins.
8. Emit the minimal result and retain supporting evidence for audit.

## Attribution tree

1. **Enough evidence?**
   - No → `NOISE`.
2. **Multiple operators down in the same zone?**
   - Yes: **Are those operators also down in their other zones?**
     - No → `AREA-SHARED`: likely power or shared physical dependency.
     - Yes → `REGIONAL`: wide-area upstream or regional event.
3. Otherwise, **is the affected operator down across all/almost all of its zones?**
   - Yes → `ISP / OLT`: operator-wide upstream or OLT failure.
4. Otherwise, **are neighboring operators in this zone up?**
   - Yes → `LOCAL OPERATOR FAULT`: fibre cut, local switch, node power issue, etc.
   - No comparison → `UNKNOWN`: single-zone operator or single-operator zone.

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
affected_h3_count, compared_csp_count, rule_matched,
bucket, confidence
```

`attribution_event_id` is derived only to group overlapping source outages across CSPs. Preserve the V3 `outage_id`; do not replace it.

## Confidence

Use evidence grades until outcome-calibrated probabilities exist:

- **High**: all required comparisons exist and the matched rule is clearly beyond its thresholds.
- **Medium**: required comparisons exist but evidence is close to a threshold or based on a small denominator.
- **Low evidence**: route to `NOISE` or `UNKNOWN`, not a confident physical-cause bucket.

## Validation

Tune only four v1 parameters:

- minimum affected devices;
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
