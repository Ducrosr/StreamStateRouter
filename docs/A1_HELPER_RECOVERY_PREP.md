# A1 preparation — owned OBS helpers and typed recovery

Status: preparation only. Do not merge or implement A1 runtime behavior until A0 is approved.

Base A0 head: `6a288052001cd01cf7283251bc506d6219ea721c`.

## Architectural contract already decided by Astra

A1 must make temporary OBS helpers traceable and recoverable without turning recovery into a second mutation engine.

Required invariants:

1. A helper is never considered SSR-owned from its name alone.
2. Ownership combines a generated identifier, an SSR manifest, a bound target and creation context.
3. At most one reusable fade helper is owned per qualified source.
4. The recovery obligation is durably written before the first temporary effect.
5. Cleanup operations are distinct: observe, neutralize, disable, restore previous fields when applicable.
6. Cleanup never creates a missing helper.
7. A proven helper is neutralized, disabled and then verified.
8. Legacy helpers/obligations that cannot prove ownership are reported as ambiguous, not adopted.
9. A successful cleanup is acknowledged only after readback verifies the intended state.
10. Proven SSR helpers are excluded from business/profile capture. Ambiguous lookalikes are reported, not silently hidden.
11. No automatic deletion in A1. Deletion remains a controlled maintenance/migration operation.
12. No general transaction framework.

## Current code map

### `stream_state_router/obs/layouts.py`

Current responsibilities relevant to A1:

- `SSR_FADE_FILTER` and `SSR_FADE_FILTER_KIND`
- `PendingFadeCleanup`
- `_ensure_pending_fade()`
- `export_pending_fade_cleanup()`
- `import_pending_fade_cleanup()`
- `retry_pending_fade_cleanup()`
- `_neutralize_fade_sources()`
- `_ensure_fade_filter()`
- `_set_source_opacity()`
- transition code that arms cleanup before temporary opacity writes

Current debt:

- `retry_pending_fade_cleanup()` calls `_set_source_opacity()`.
- `_set_source_opacity()` can call `_ensure_fade_filter()` on confirmed absence.
- Therefore cleanup can currently recreate a missing helper.
- `_ensure_fade_filter()` recognizes an existing helper by filter name only.
- Successful transition cleanup neutralizes opacity but does not yet enforce verified disable.

### `stream_state_router/services/recovery.py`

Current durable schema: `CLEANUP_SCHEMA_VERSION = 2`.

Current `layout_fade` obligations persist mainly:

- kind
- source
- collection
- created_at
- attempts
- last_error

This is intentionally insufficient as ownership proof.

A1 must preserve reading of v2 markers without upgrading a legacy name-only obligation into proven ownership.

### `stream_state_router/services/runtime.py`

Relevant boundaries:

- imports transferred cleanup into the layout manager
- retries pending fade cleanup only from the serialized runtime path
- exports cleanup obligations into shutdown results / runtime marker
- reconnect path already triggers cleanup retry

Do not move OBS writes to Qt, HTTP or Stream Deck.

### Import/capture

`stream_state_router/importers/scene_collection.py` currently captures all source filters and projects them into actions.

A1 must distinguish:

- proven SSR helper -> excluded from business capture
- name lookalike without proof -> not excluded as SSR-owned; report ambiguity instead

## Proposed minimal A1 data model

This is an implementation proposal constrained by the Astra contract. If a new architectural decision becomes necessary, stop before coding that decision.

## Persistence boundary: manifest != runtime cleanup journal

A1 needs two lifetimes of state.

The helper ownership manifest is persistent inventory. A reusable helper can remain in OBS after a clean transition and after a clean SSR shutdown, so the evidence that SSR created/owns that helper must survive when no cleanup is pending.

The runtime cleanup journal is transient write-ahead recovery state. It records unfinished compensation work and can become empty after verified cleanup.

Do not use `runtime.json` as the only ownership manifest.

A minimal split is therefore:

- persistent helper manifest under the SSR user-data directory
- versioned `runtime.json` obligations referencing a manifest/helper identity

The repository does not currently expose a durable OBS filter UUID through `FilterRef`; do not invent one. A1 may use an SSR-generated helper identity plus an exact generated filter name and persistent manifest, while A2 remains responsible for broader resource binding/identity architecture.

Any exact filename/schema for the persistent helper manifest is still an implementation detail until the A0 gate is cleared. It must remain backward compatible and independently writeable/replaceable.

## Legacy boundary

The legacy filter name `[SSR] Layout Fade` is not proof of ownership.

A v2 cleanup obligation containing only collection + source is not proof of ownership either.

Therefore A1 migration must distinguish:

- proven new helper
- legacy ambiguous helper-like filter
- proven new obligation
- legacy ambiguous obligation

Legacy ambiguity must never trigger automatic adoption, mutation, deletion or recreation.


### Helper manifest

A small SSR-side persisted record, keyed by generated `helper_id`.

Minimum candidate fields:

- `schema`
- `helper_id`
- `purpose = "layout_fade"`
- `collection`
- `source_name`
- optional external stable source identity if OBS exposes one safely at this stage
- `filter_name`
- `filter_kind`
- `created_at`
- `created_by = "stream_state_router"`

Do not use a prefix or display name as ownership proof.

### Recovery obligation

Candidate v3 fields for `kind="layout_fade"`:

- `helper_id`
- `collection`
- `source_name`
- `filter_name`
- `filter_kind`
- `cleanup_action`
- `previous_enabled` when known
- `previous_opacity` when relevant/known
- `created_at`
- `attempts`
- `last_error`
- provenance/version metadata required to bind the obligation to the manifest

Legacy v2 obligation:

- readable
- explicitly legacy/ambiguous
- must not create a helper
- must not claim ownership from `[SSR] Layout Fade` alone

## Operation split

A1 should separate creation/use from recovery.

### Normal transition path

Allowed primitives:

- observe existing filters
- resolve a proven SSR helper
- create the planned helper when normal transition logic requires it
- verify the created helper
- write opacity
- neutralize
- disable
- verify
- clear obligation

### Recovery path

Allowed primitives:

- observe
- resolve by proven identity/provenance
- neutralize an existing proven helper
- disable an existing proven helper
- restore explicitly recorded fields when applicable
- verify
- retain/clear obligation based on verified result

Forbidden primitive in recovery:

- CreateSourceFilter

Confirmed absence must never result in recreation.

## Write-ahead ordering

Before any temporary opacity mutation:

1. Determine collection/context.
2. Determine planned helper identity.
3. Persist helper provenance/manifest state needed to recognize the intended resource.
4. Persist the recovery obligation.
5. Only then create/enable/mutate the helper.
6. After each uncertain transport result, observe before deciding the next action.
7. Clear the obligation only after neutral + disabled state is verified or the absence is safely classified according to proven ownership semantics.

## Crash matrix to implement

1. Crash before helper creation.
2. Create request accepted, response lost.
3. Crash after create before opacity write.
4. Crash during opacity mutation.
5. Crash after opacity mutation before verification.
6. Crash during neutralization.
7. Crash after neutralization before disable.
8. Crash after disable before verification.
9. Crash after verified cleanup before durable obligation removal.
10. Persistence failure before first temporary write.
11. Persistence failure while updating cleanup attempts.
12. Helper absent during recovery.
13. Wrong Scene Collection active.
14. Source removed.
15. Source recreated under same name.
16. User filter with the same display name.
17. Proven helper externally modified.
18. Proven helper duplicated/cloned.
19. Shutdown with active transition/backlog.
20. Reconnect while obligation is pending.

For every case, assert:

- no duplicate helper
- no recovery creation
- no user-filter mutation
- obligation retained on uncertainty
- obligation cleared only after a verified terminal condition

## Existing tests to preserve

At minimum:

- `tests/test_layouts.py` fade/recovery tests
- `tests/test_recovery.py`
- `tests/test_runtime.py` cleanup/shutdown/reconnect tests
- `tests/test_apply_recovery.py`
- `tests/test_importers.py`
- `tests/test_current_state_capture.py`

## Expected files in A1

Likely:

- `stream_state_router/obs/layouts.py`
- a small helper-management module if extraction materially reduces responsibilities
- `stream_state_router/services/recovery.py`
- `stream_state_router/services/runtime.py`
- `stream_state_router/importers/scene_collection.py`
- focused tests

Avoid unrelated UI/config/profile refactors.

## UX budget

Daily user input added: 0.

Automatic:

- helper provenance
- helper recognition
- cleanup
- retry
- capture exclusion for proven internal helpers

Human decision only:

- legacy/ambiguous resource that cannot be safely attributed to SSR

Simple messages should say what the user can act on, e.g.:

- "Récupération terminée"
- "Nettoyage en attente : filtre interne non attribuable avec certitude"

Do not expose manifest IDs, leases or recovery schema in the simple UI.

## A1 exit gate

A1 is not complete until:

- recovery is idempotent
- no cleanup path can create a helper
- ownership is not derived from name alone
- helper neutralization + disable are verified
- v2 markers remain readable without unsafe ownership promotion
- proven helpers are excluded from capture
- ambiguous lookalikes are reported
- real OBS kill/restart and shutdown-backlog tests are completed
- Astra re-audits provenance, write-ahead, idempotence and migration

## Decisions to return to Astra if implementation reaches them

These are not reasons to block preparation, but SOL must not silently choose a broader policy.

1. Exact persistent manifest filename and long-term schema if it needs to become a public compatibility contract.
2. Exact generated helper display-name format if a user-visible OBS name is considered part of the UX contract.
3. Whether a legacy ambiguous v2 obligation can be explicitly dismissed after a read-only proof that no matching helper exists, or must remain visible until user acknowledgement.
4. Any automatic deletion policy. A1 currently assumes no automatic deletion.
5. Any ownership transfer/adoption of a pre-existing user filter. A1 currently forbids it.
6. Any new strategy for composites. A1 inherits A0's direct fallback and does not change it.
7. Any attempt to broaden the generated-helper mechanism to non-fade filters.

## Mechanical work that can be prepared without those decisions

- pure manifest serialization/deserialization helpers
- v2 marker characterization fixtures
- read-only helper observation
- recovery path that contains no CreateSourceFilter capability
- verification helpers for opacity/enabled
- import filtering hook based on proven ownership predicate
- fault-injection fixtures
- real-OBS diagnostic tooling
