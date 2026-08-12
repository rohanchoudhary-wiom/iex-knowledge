## Context

The completed spatial pilot maps formal incidents to fixed 0.01-degree cells. This change has a different target: strict ping-defined telemetry silence over the unsampled July population. The two outcomes remain separate.

## Decisions

### Reuse quality gates and remove sampling

Reuse normalized device IDs, the exact known `HOUR_END_IST` repair, synthetic-family exclusion, conflicting device-hour quarantine, the clean `CUSTOMER_V2` cohort, and the one-to-one inventory bridge. Remove the 5% hash predicate. A conflicting or malformed observed hour breaks inference and cannot become evidence of silence.

### Derive the strict event from adjacent successful pings

Set the first missed opportunity to five minutes after the previous successful ping. A recovered event qualifies when the next success is at least 65 minutes after the previous success, leaving at least 60 minutes from first miss to recovery. Missing hourly rows inside a gap form one event. Events without a later success by the fixed observation boundary remain right-censored and have no completed duration.

### Use native H3 resolution 9

After validating India coordinate bounds, assign `H3_LATLNG_TO_CELL_STRING(latitude, longitude, 9)` inside Snowflake. Resolution 9 averages approximately 201 m per edge and 0.105 km2 in area; it is not an exact 200 m diameter. No H3 package or local coordinate export is needed.

### Keep the output aggregate and descriptive

Export only cells with at least five service-eligible mapped devices. Report mapping attrition, occupancy, affected-device share, recovered duration, censoring, and suppressed coverage. Do not merge cells or time windows into incidents in this change.

## Measured Result

The completed July 2026 run processed 51,689,408 source rows and retained 84,789 devices. The exact bridge mapped 81,772 devices (96.44%) across 7,704 H3 cells; 80,663 overlapped a valid July service interval. It found 608,940 mapped, service-valid July-started telemetry outages across 74,732 devices, including 2,476 right-censored events. The 3,167 exported cells cover 89.94% of the eligible mapped population; 4,489 sparse cells remain suppressed.

## Risks / Trade-offs

- Current customer locations can create historical survivorship bias; location-start and plan-expiry bounds are enforced and disclosed.
- Long gaps may reflect inactivity, maintenance, or collection failure; results are named telemetry outages rather than confirmed service outages.
- H3 boundaries can split one physical incident; neighbor merging is deferred until clustering thresholds are frozen.
- Sparse cells are mapped internally but omitted from detailed artifacts.

## Migration Plan

1. Run the read-only full-population mapper for the previous complete IST month.
2. Publish aggregate mapping, density, outage, and suppression evidence.
3. Open a separate change for spatial-temporal clustering after reviewing cell occupancy.
4. Roll back by deleting local aggregate artifacts; no warehouse state is changed.
