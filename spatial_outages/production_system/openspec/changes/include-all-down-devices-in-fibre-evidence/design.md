## Context

See `proposal.md`. The existing comparison polygon already resolves live states for every device, but the fibre path constructs its DOWN population from the source outage component only. Non-member DOWN devices are displayed and counted as controls rather than being eligible for the final Rule 4 address grouping.

## Goals / Non-Goals

**Goals:**

- Reuse the current comparison polygon, live-state cache, mixed clustering, address model, and ordered Rule 4 paths.
- Make CSP and outage membership irrelevant to final-stage fibre candidate selection.
- Preserve provenance when fibre attribution expands beyond posted members.

**Non-Goals:**

- Do not change finalized outage membership or Rules 1–3B.
- Do not classify every red map device as fibre; address or guarded locality evidence remains required.
- Do not add another model, clustering package, or upstream call.

## Decisions

1. Inside the existing fibre evidence function, derive DOWN candidates from all comparison devices whose live state is DOWN and healthy candidates from all devices whose state is UP. This is the smallest root change because every Rule 4 caller routes through that function.
2. Run the existing mixed-device clustering over all known-state comparison devices. For each mixed cluster, apply HOUSE/GALI grouping to every DOWN candidate and require an UP device in that cluster.
3. Compute Rule 4B count, R90, and strongest-10-minute share from the expanded DOWN candidate group rather than the source outage component. Require at least three DOWN candidates and R90 at most 500 m; retain the existing locality and control thresholds. Report concurrency as diagnostic evidence instead of using it as a veto.
4. Keep unmatched DOWN devices out of the affected result. Add member and comparison-only affected ID lists to fibre evidence while retaining the complete `affected_device_ids` contract.
5. Preserve the source component boundary and provider evidence. Expanding Rule 4 evidence must not mutate Detection's outage membership or retroactively alter Rules 1–3B.

Alternative rejected: merging comparison DOWN devices into the source outage before the funnel would let attribution rewrite Detection membership and contaminate CSP/power rules. Expanding only the final Rule 4 evidence population preserves the ordered funnel.

## Risks / Trade-offs

- [Long-stale background DOWN devices create false groups] → Require Rule 4A address coherence plus a healthy mixed-cluster control, or every Rule 4B radius, locality, and healthy-control gate; expose concurrency for audit.
- [Affected IDs exceed source membership] → Expose member versus comparison-only provenance explicitly.
- [Dense UP devices bridge spatial components] → Keep address grouping mandatory for Rule 4A and validate the expanded Rule 4B group independently.

## Migration Plan

Update tests, the fibre population, rule documentation, and atlas evidence together. Restart localhost and verify a 4408-shaped live case. Roll back the focused Rule 4 population change if non-member stale devices are attributed without address or guarded locality evidence.
