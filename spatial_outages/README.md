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

[sql/outage_devices.sql](sql/outage_devices.sql) is the only query:

```text
active DYNAMODB_READ.CUSTOMER_V_2
  → JOIN T_ADDRESS by GOOGLE_ADDRESS_ID for latitude and longitude
  → JOIN T_DEVICE by DEVICE_ID for CSP ID
  → keep device ID, CSP ID, mobile, latitude, longitude, and fixed H3
  → LEFT JOIN OUTAGE_MEMBER_V3 directly from CUSTOMER_V_2.DEVICE_ID
  → data/input/outage_devices.csv
```

No `ACTIVE_BASE` rows are used. The left join retains active Customer V2 devices without an outage, so the same CSV provides both the active CSP-H3 denominator and affected outage members.

## CSV implementation

The attribution workflow reads and writes CSV only. The SQL file is a SELECT-only export; the Python script does not connect to Snowflake.

```text
spatial_outages/
├── attribute.py
├── attribution/
│   ├── csv_io.py
│   ├── engine.py
│   ├── self_check.py
│   ├── domain/
│   │   ├── thresholds.py
│   │   ├── outage.py
│   │   ├── cell_state.py
│   │   ├── event.py
│   │   ├── rule_context.py
│   │   └── decision.py
│   └── rules/
│       ├── noise.py
│       ├── area_shared.py
│       ├── regional.py
│       ├── isp_olt.py
│       ├── local_csp_fault.py
│       ├── unknown.py
│       └── ordered.py
├── data/
│   ├── input/
│   │   └── outage_devices.csv
│   └── output/
│       ├── csp_h3_states.csv
│       ├── attribution_events.csv
│       ├── outage_evidence.csv
│       └── outage_buckets.csv
├── sql/
│   └── outage_devices.sql
├── public/
│   └── severity.html
└── Outage Cause Attribution Tree.jam
```

The SQL exports `device_id, csp_id, mobile, latitude, longitude, h3_id, outage_id, member_first_fail_at_ist`. CSP ID comes from `T_DEVICE`; the unrelated `OUTAGE_MEMBER_V3.CSP_ID` is not used. The Python runner uses only the IDs, H3, and outage fields; mobile and coordinates remain reconciliation evidence. The export is ignored by Git because it contains direct identifiers.

Run:

```bash
python spatial_outages/attribute.py
python spatial_outages/attribute.py --self-check
```

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
CSP × H3    → is this CSP down in this zone?
H3          → are multiple CSPs down in this zone?
CSP         → is this CSP down across its network?
outage      → what bucket and confidence should we publish?
```

## Bucketing

Apply the rules in this order. First match wins. Any ambiguous pattern is `UNKNOWN`.

```text
Enough evidence?
├─ NO  → NOISE
└─ YES
   └─ Multiple CSPs down in the same zone?
      ├─ YES
      │  └─ Are those CSPs also down in their other zones?
      │     ├─ NO  → AREA-SHARED
      │     │         Likely power or shared physical dependency
      │     └─ YES → REGIONAL
      │               Wide-area upstream or regional event
      └─ NO
         └─ Is the affected CSP down across all/almost all zones?
            ├─ YES → ISP / OLT
            │         CSP-wide upstream / OLT failure
            └─ NO
               └─ Are neighboring CSPs in this zone up?
                  ├─ YES           → LOCAL CSP FAULT
                  │                   Fibre cut, local switch, node power issue
                  └─ NO COMPARISON → UNKNOWN
                                      Single-zone CSP or single-CSP zone
```

The tree locates the likely failure domain. `backend bug`, `power cut`, `fibre cut`, `OLT fault`, and `ISP down` require supporting operational evidence before being stated as a root cause.

## Outputs

`csp_h3_states.csv`, `attribution_events.csv`, and `outage_evidence.csv` retain the calculations behind each decision.

Published outage result:

```text
outage_id
bucket
confidence
```

Confidence is `HIGH` or `MEDIUM` based on comparison coverage and distance from the selected thresholds. Insufficient or incomparable evidence becomes `NOISE` or `UNKNOWN`, not a confident cause.

## Thresholds to validate

Only tune:

1. minimum affected share within CSP × H3;
2. time window for matching outages;
3. percentage of CSP zones meaning “almost all.”

Select the threshold set with the lowest false-promise rate while retaining at least 80% outage recall.

Rules: [rules.md](rules.md) · Detailed design: [SEVERITY_MODEL.md](SEVERITY_MODEL.md) · Visual: [public/severity.html](public/severity.html)
