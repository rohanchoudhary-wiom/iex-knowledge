## Why

Rule 4 currently considers only finalized outage members as DOWN fibre candidates, even when the live comparison polygon contains a coherent same-house or same-gali group of DOWN devices from other CSPs. This hides the strongest access-fibre evidence at the final stage of the funnel.

## What Changes

- After Rules 1–3B fail, treat every currently DOWN comparison-polygon device as a fibre candidate regardless of CSP or finalized outage membership.
- Cluster those DOWN candidates with current UP comparison devices before applying the existing ordered Rule 4A house/gali and Rule 4B locality/control checks.
- Keep CSP identity out of the fibre decision; CSP remains evidence only for earlier funnel rules.
- Keep short-window concurrency as diagnostic evidence for Rule 4B, not a veto when all-CSP DOWN devices form a compact shared-locality group with healthy controls.
- **BREAKING**: `affected_device_ids` for `FIBRE_CUT` may include comparison devices that were not posted as finalized members of the source outage.
- Expose member versus comparison provenance for affected fibre devices and verify the live 4408-shaped cross-CSP pattern.

## Capabilities

### New Capabilities

- `cross-csp-fibre-evidence`: Build final-stage fibre evidence from all current DOWN and UP devices in the comparison polygon without CSP or outage-membership filtering.

### Modified Capabilities

None.

## Impact

- Fibre population, affected-device output, and evidence details in `attribution/engine.py`.
- Fibre contract and output semantics in `RULES.md` and atlas evidence text.
- Regression coverage in `test_system.py`; no new dependency or upstream API.
