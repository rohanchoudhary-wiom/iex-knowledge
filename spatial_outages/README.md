# Spatial outage triage

## What we are solving

Take Detection's frozen outage, build one polygon, and publish one conservative attribution.

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

[sql/outage_devices.sql](sql/outage_devices.sql) is the only query. Its first gate defines the eligible comparison population; outage membership cannot add an ineligible device back.

```text
latest active DYNAMODB_READ.CUSTOMER_V_2 row per device
  → require PLAN_EXPIRY_TIME
  → find the last valid successful ping in the rolling 15-day snapshot
  → keep only PLAN_EXPIRY_TIME > last successful ping + 12 hours
  → LEFT JOIN T_ADDRESS for location and T_DEVICE for CSP ID
  → LEFT JOIN eligible OUTAGE_MEMBER_V3 and OUTAGE_V3 records
  → data/input/outage_devices.csv
```

The 12-hour comparison is strict. A device with no valid successful ping in the 15-day snapshot is excluded. `HOME_ROUTER_PLAN_INFO` is not used because `CUSTOMER_V_2.PLAN_EXPIRY_TIME` is populated for the active rows used by this export. No `ACTIVE_BASE` rows are used.

The same eligible cohort supplies the CSP/H3 denominator, outage members, polygon peers, and map dots. The export also carries the validated hourly-ping bitmap so the selected polygon can distinguish outage members, devices with a successful ping in that outage hour, and devices without positive ping proof.

## CSV implementation

The attribution workflow reads and writes CSV only. The SQL file is a SELECT-only export; the Python script does not connect to Snowflake.

```text
spatial_outages/
├── attribute.py
├── build_map_data.py
├── requirements.txt
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
│       └── outage_attributions.csv
├── sql/
│   └── outage_devices.sql
├── public/
│   └── severity.html
├── map_site/
│   ├── public/
│   │   ├── index.html
│   │   └── map.html
│   └── worker.js
└── Outage Cause Attribution Tree.jam
```

The SQL exports registry data, eligible frozen membership, cohort state, and Detection's trigger time. Python uses member coordinates to build one convex hull and PyProj to calculate geodesic area. The export is ignored by Git because it contains direct identifiers.

Run:

```bash
python spatial_outages/attribute.py
python spatial_outages/attribute.py --self-check
python spatial_outages/build_map_data.py
```

## Private outage map

The map filters polygons by date, time, lookback window, cause, and CSP name. Device data is not fetched or rendered until a polygon is clicked. That click shows every located eligible device inside the polygon: red for outage members, green for a validated successful ping in the outage hour, and grey when no positive ping is proven. Clicking a dot shows its status, customer name, and address.

`build_map_data.py` writes `outages.geojson` and the compressed `devices.json.gz` payload into `map_site/public/`. Both generated files are ignored by the main repository because the device payload contains customer information; production access remains owner-only.

The snapshot refreshed on 15 August 2026 contained 76,503 eligible devices, 6,163 classified outages, 6,152 polygons, and 76,440 located device points.

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

## Attribution

The published root-cause taxonomy is `CSP_SIDE`, `ACCESS_FIBRE`, `PREMISE_POWER`, or `UNKNOWN`. Active Customer V2 connections inside each polygon provide the first CSP counterfactual: the outage is `CSP_SIDE` when peer CSPs are present and unaffected. H3 rules cover the same comparison when the polygon test is inconclusive; ambiguous multi-CSP and no-peer patterns remain `UNKNOWN`.

`ACCESS_FIBRE` and `PREMISE_POWER` remain deliberately unused until recovery, contact, sentinel, or confirmed-cause evidence validates them.

## Outputs

`csp_h3_states.csv`, `attribution_events.csv`, and `outage_evidence.csv` retain internal calculations. Concurrent outages may share an internal attribution event, but this never changes their source IDs.

Published outage result:

```text
one outage_id
one geometry + polygon_area_km2
frozen and located member counts
one root_cause + spatial_extent + confidence
one idempotent revision lifecycle
```

The CSV upsert is keyed only by `outage_id`: unchanged reruns preserve the revision; changed results increment it. Healthy-device count, cause distribution, finality, event pattern, and restoration class remain blank or `UNKNOWN` until their requested sources exist.

## Thresholds to validate

Only tune:

1. minimum affected share within CSP × H3;
2. time window for matching outages;
3. percentage of CSP zones meaning “almost all.”

Select the threshold set with the lowest false-promise rate while retaining at least 80% outage recall.

Rules: [rules.md](rules.md) · Detailed design: [SEVERITY_MODEL.md](SEVERITY_MODEL.md) · Visual: [public/severity.html](public/severity.html)
