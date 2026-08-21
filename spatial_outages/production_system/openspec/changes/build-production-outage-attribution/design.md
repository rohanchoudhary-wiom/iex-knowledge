## Context

TFF calls the service with a frozen outage ID, member IDs, and duration. The operational map reads those same fields from Detection's open-outage GET endpoint. Detection separately exposes `get_device_status(device_ids) -> last successful ping time`; its deployed URL remains required configuration. Customer V2 supplies the comparison population, CSP, and coordinates.

## Goals / Non-Goals

**Goals:**

- Small runnable service, deterministic rules, and a rendered evidence map.
- Never derive device state from outage membership.
- Reuse only the Python standard library.

**Non-Goals:**

- Durable storage, production authentication, calibrated probabilities, or independent cause confirmation.
- Replacing TFF detection or editing its membership.

## Decisions

### One dependency-free service

`app.py` uses `ThreadingHTTPServer` for the attribution endpoint, status client, health endpoint, map data, and static map. `attribution.py` contains CSV loading and deterministic decision logic. This avoids a framework and keeps the core directly testable.

### Real-only production refresh

Production startup rejects a Customer V2 snapshot older than 24 hours, validates the real open-outage response, and completes one attribution refresh before serving. A background refresh atomically replaces open results every 60 seconds. A later dependency failure marks the retained snapshot stale; it never injects demo outages or assumes missing status is healthy. Explicit `--demo` is test-only.

### State is time-based

For every status call:

```text
age = evaluation_time - last_successful_ping
age >= 10 minutes -> DOWN
age < 10 minutes  -> UP
missing/invalid/future -> UNKNOWN
```

The engine calls status for the full target-CSP Customer V2 denominator before ISP/OLT, then for every Customer V2 device inside supported local boundaries. Membership is never a state signal.

### Minimal adaptive clustering

For each currently DOWN, located posted device, calculate its third-neighbour distance, clipped to 100–1,000 metres. Connect two devices when their distance is within either endpoint's local reach; connected components are candidate groups and components below 10 devices remain review/noise. This is the smallest deterministic density-adaptive graph needed for the current pilot; a vetted VDBSCAN package replaces it only if replay proves the approximation inadequate.

The centre is median latitude/longitude. R70–R100 use nearest-rank geodesic distances. Supported groups require at least 10 devices, R90 ≤1,000 m, and R90−R80 ≤500 m.

### Corrective methodology sequence

The member footprint is not a radius profile. Below the CSP-wide gate, current DOWN members are first partitioned into anchored 30-minute failure windows and then split with variable-density spatial reach. Every resulting component is retained as either SUPPORTED or REVIEW evidence. R70/R80/R90/R100 are calculated only from one component's failures around its median centre. R90 is the provider/customer comparison boundary; R100 is tail evidence and never widens that comparison.

A component with `R90 - R80 > 500 m` is re-clustered once with a tighter reach. Small components and unresolved tails remain visible but do not veto an otherwise supported group. Parent attribution uses only supported groups and remains UNKNOWN when their causes disagree or when every supported group lacks the required provider comparison.

Last-successful-ping plus five minutes is the explicit V3-compatible failure-time proxy until the open-outage feed supplies `member_first_fail_at_ist`. The result records that provenance. Numeric values requested by the operating rules are policy scores, not calibrated cause probabilities; spatial support, cause likelihood, and independent confirmation remain separate evidence states.

### Ordered cause rules

1. Complete target CSP status and DOWN share ≥75% for CSPs with at least 50 active connections, otherwise ≥80% → `ISP_OLT_CSP_SIDE`.
2. Otherwise cluster posted devices that are actually DOWN.
3. Inside supported R90, query actual status for all Customer V2 devices.
4. Two CSPs each ≥70% DOWN → `PREMISE_POWER`.
5. Target CSP ≥70% DOWN and every qualified peer <20% DOWN → `FIBRE_CUT`.
6. Anything incomplete or mixed → `UNKNOWN`.

The minimum qualified provider population is configurable and defaults to five devices. MEDIUM means a rule is supported by ping/location evidence; LOW means unknown. HIGH is reserved.

### Conservative parent roll-up

Return a local cause only when every supported retained group agrees. Otherwise return UNKNOWN. Keep detailed evidence in memory for `/map-data`; the POST response stays limited to the requested three fields.

## Risks / Trade-offs

- **[In-memory results disappear on restart]** → Accept for development; add the production store when its owner is selected.
- **[Adaptive graph is an approximation]** → Version its thresholds and replace only after replay shows false merges/splits.
- **[Customer V2 is latest-state]** → Use an event-time inventory when it becomes available.
- **[Status API contract is not finalized]** → Keep its URL configurable and validate responses strictly.
