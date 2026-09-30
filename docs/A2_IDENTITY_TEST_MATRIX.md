# A2 identity safety test matrix

This document is preparation only. It does not authorize A2 implementation and
does not change the frozen A1 review target.

The machine-readable corpus lives in
`tests/fixtures/a2_identity_scenarios.json`. The fixture deliberately records
observable safety properties rather than prescribing the future binding schema.

## Gate categories

| Scenario | Risk being exercised | Required outcome |
| --- | --- | --- |
| rename, same UUID | Display-name drift | Never retarget by name; preserve/prove physical identity. |
| delete/recreate, same name | False continuity | Never adopt the replacement silently. |
| duplicate names | Ambiguous target | Fail closed unless the intended physical occurrence is proven. |
| duplicate reorder | Occurrence-index drift | Index alone cannot authorize a write. |
| sceneItemId renumber | Ephemeral OBS identifier | Re-resolve/revalidate; cached ID is not durable authority. |
| Collection A→B→A | Name-equality false continuity | Invalidate/requalify prepared work. |
| transport reconnect | Session discontinuity | Fresh observation/binding required. |
| legacy name-only profile | Migration ambiguity | Enrich only when unique; otherwise explicit repair. |
| nested group move | Container/path drift | Container identity must be revalidated. |
| duplicated Scene Item | Copy/duplicate false identity | The duplicate cannot inherit mutation authority from the original occurrence. |
| same source in multiple scenes | Container ambiguity | Source identity must remain qualified by the intended scene/container. |
| duplicated group | Child-name ambiguity | Never select the first matching child across similar groups. |
| renamed group | Container-name drift | Preserve proven continuity or require requalification; never guess by name. |
| duplicated/imported collection | Cross-collection false continuity | Invalidate/rebind prepared work in the new collection context. |
| same name, distinct UUIDs | Legacy migration ambiguity | Block name-only migration until one target is deterministically proven. |

## How the corpus should be used after A1

The first A2 implementation branch should consume these scenarios as black-box
acceptance cases. Implementation-specific fixtures may be added later, but the
safety expectations above should remain backend-agnostic so the same cases can
exercise a WebSocket backend or a future optional OBS bridge.

A2 is not complete merely because names resolve. The acceptance question is:
"Can SSR prove that the object about to be mutated is still the object the
planner intended?" When proof is insufficient, the correct result is a blocked
operation plus a diagnostic, not a heuristic rebind.
