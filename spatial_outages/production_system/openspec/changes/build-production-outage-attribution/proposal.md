## Why

TFF needs one POST API that attributes a detected outage without assuming its members are still down or that every other Customer V2 device is up. Current state must come from the Detection-owned `get_device_status` API, with a device considered down only after 10 minutes without a successful ping.

## What Changes

- Expose `POST /outage_attribution` with `outage_id`, `devices`, and `ongoing_time`.
- Pull the same inputs for the operational map from Detection's real open-outage GET endpoint.
- Load the eligible comparison population from Customer V2 device data, including CSP and coordinates.
- Call `get_device_status(device_ids)` and derive `DOWN`, `UP`, or `UNKNOWN` from each returned last-successful-ping time.
- Filter CSP-wide ISP/OLT-shaped outages before adaptive local clustering.
- Attribute supported local groups as fibre cut, premise power, or unknown from actual provider status inside R90.
- Return one conservative parent attribution and categorical confidence.
- Render the evaluated outages and actual device states on a local map.

## Capabilities

### New Capabilities

- `outage-attribution`: POST evaluation, Customer V2 enrichment, last-ping state, ISP/OLT filtering, adaptive local attribution, and map evidence.

### Modified Capabilities

None.

## Impact

- New dependency-free Python service under `spatial_outages/production_system`.
- Reads a fresh Customer V2 CSV snapshot, calls the configured Detection `get_device_status` endpoint, and fails closed when either dependency is stale or unavailable.
- Does not modify TFF outage membership or existing spatial-outage code.
- Keeps current results in memory for the first development version.
