## Why

The final fibre stage currently misses outages whose all-CSP DOWN devices form a narrow street-like line when address entities are incomplete and the broader locality fallback narrowly misses its radius or control threshold. Live outage 4395 exhibits this directional access-network pattern but is returned as `UNKNOWN`.

## What Changes

- Add Rule 4C after the existing house/gali and locality fibre rules and before `UNKNOWN`.
- Re-cluster all current DOWN comparison-polygon devices with the existing variable-density spatial method at a tighter reach, then identify a sufficiently large, narrow, one-direction component.
- Require healthy surrounding controls so an arbitrary line of stale DOWN devices cannot become fibre evidence.
- Attribute only the matched directional component, regardless of CSP or finalized outage membership, at confidence `0.60`.
- Expose the component's length, perpendicular width, directionality, controls, and affected-device provenance in the API and human-readable checklist.

## Capabilities

### New Capabilities

- `directional-fibre-evidence`: Recognize compact horizontal, vertical, or diagonal all-CSP DOWN-device components as final-stage access-fibre evidence.

### Modified Capabilities

None.

## Impact

- Directional profile helper in `attribution/spatial.py` and the shared Rule 4 path in `attribution/engine.py`.
- Rule contract in `RULES.md` and Rule 4C evidence in `static/index.html`.
- Focused positive and negative regression coverage in `test_system.py`.
- No new dependency or upstream API call.
