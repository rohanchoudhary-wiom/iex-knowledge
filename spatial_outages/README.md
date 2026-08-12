# Spatial outage severity

## What we are solving

Detect an outage early, measure how large it is, and assign the most likely failure domain.

Success means:

- fewer false outage promises;
- at least 80% of known outages captured.

## Trigger

| Device silence | State |
|---|---|
| Under 15 minutes | Healthy |
| 15–29 minutes | Candidate |
| 30 minutes or more | Confirmed silent |

A confirmed silent device is evidence. It becomes a shared outage only when other devices fail at the same time and form a CSP/H3 pattern.

## Source

[sql/01_outage_members.sql](sql/01_outage_members.sql) selects the only four outage fields needed from the V3 tables:

```text
outage_id
device_id
csp_id
member_first_fail_at_ist
```

Derive:

```text
outage_start_ist = MIN(member_first_fail_at_ist) by outage_id
```

Then attach each device's H3 at the time of failure and the eligible-device count for that CSP/H3.

## Raw severity

Do not start with one severity score. Keep the observable impact at each level:

| Level | Measure |
|---|---|
| Device | silence minutes |
| H3 | affected devices and CSPs |
| CSP × H3 | affected devices / eligible devices |
| CSP | affected H3s / eligible H3s |
| Outage | affected devices, H3s, CSPs, and duration |

These rollups answer different questions:

```text
device      → is this device down?
CSP × H3    → is this operator down in this zone?
H3          → are multiple operators down in this zone?
CSP         → is this operator down across its network?
outage      → what bucket and confidence should we publish?
```

## Bucketing

Apply the rules in this order. First match wins. Any ambiguous pattern is `UNKNOWN`.

```text
Enough evidence?
├─ NO  → NOISE
└─ YES
   └─ Multiple operators down in the same zone?
      ├─ YES
      │  └─ Are those operators also down in their other zones?
      │     ├─ NO  → AREA-SHARED
      │     │         Likely power or shared physical dependency
      │     └─ YES → REGIONAL
      │               Wide-area upstream or regional event
      └─ NO
         └─ Is the affected operator down across all/almost all zones?
            ├─ YES → ISP / OLT
            │         Operator-wide upstream / OLT failure
            └─ NO
               └─ Are neighboring operators in this zone up?
                  ├─ YES           → LOCAL OPERATOR FAULT
                  │                   Fibre cut, local switch, node power issue
                  └─ NO COMPARISON → UNKNOWN
                                      Single-zone operator or single-operator zone
```

The tree locates the likely failure domain. `backend bug`, `power cut`, `fibre cut`, `OLT fault`, and `ISP down` require supporting operational evidence before being stated as a root cause.

## Outputs

Device-level evidence:

```text
device_id
outage_id
csp_id
h3_id
outage_start_ist
silence_minutes
device_bucket
```

Published outage result:

```text
outage_id
bucket
confidence
```

Confidence is `HIGH` or `MEDIUM` based on comparison coverage and distance from the selected thresholds. Insufficient or incomparable evidence becomes `NOISE` or `UNKNOWN`, not a confident cause.

## Thresholds to validate

Only tune:

1. minimum affected devices;
2. minimum affected share within CSP × H3;
3. time window for matching outages;
4. percentage of CSP zones meaning “almost all.”

Select the threshold set with the lowest false-promise rate while retaining at least 80% outage recall.

## Next step

Join `data/outage_members_v3.csv` to historical device H3 and active CSP-H3 denominators. The first deliverable is one row per source outage:

```text
outage_id, bucket, confidence
```

Detailed design: [SEVERITY_MODEL.md](SEVERITY_MODEL.md) · Visual: [public/severity.html](public/severity.html)
