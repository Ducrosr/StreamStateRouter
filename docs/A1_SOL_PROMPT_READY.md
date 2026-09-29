# Prompt SOL — A1 helper ownership and typed recovery

> DO NOT EXECUTE THIS IMPLEMENTATION UNTIL ASTRA HAS EXPLICITLY WRITTEN:
>
> `A0 EST FERMÉ. SOL PEUT COMMENCER A1.`

You act as the implementation engineer for StreamStateRouter.

Repository:

`Ducrosr/StreamStateRouter`

Expected A0 base at preparation time:

`6a288052001cd01cf7283251bc506d6219ea721c`

Before changing anything, verify the actually approved A0 SHA. If Astra approves a different SHA, stop and rebase/reconcile this plan before implementation.

## Role boundary

Implement A1 only.

Do not redesign A0.
Do not begin A2.
Do not introduce PropertyDescriptor/B0.
Do not change profile composition.
Do not change composite fade policy.
Do not add a general transaction framework.
Do not merge.

If an implementation decision conflicts with the explicitly reserved Astra decisions, stop at that decision and report it.

## A1 goal

Make OBS fade helper ownership and recovery:

- provable
- durable
- idempotent
- safe after crashes/reconnects
- incapable of recreating a missing helper during cleanup

Daily user input added: zero.

## Architecture already decided by Astra

1. Generated helper identity + persistent SSR manifest.
2. Recovery obligation is written before a temporary effect.
3. Cleanup actions are distinct.
4. Missing helper during cleanup is handled without creation.
5. Proven helper is neutralized then disabled, with readback verification.
6. Legacy ambiguous helpers are reported, never automatically adopted.
7. Name/prefix alone is never ownership proof.
8. Proven internal helpers are excluded from business capture.
9. User filters are never automatically deleted.
10. No systematic helper deletion in A1.
11. No borrowing/adoption of a pre-existing user filter.
12. One reusable owned fade helper at most per qualified source.

## Persistence boundary

Do not store ownership evidence only in `runtime.json`.

A reusable helper can remain in OBS after a clean shutdown while `runtime.json` has no pending cleanup.

Use two concepts:

- persistent helper ownership manifest
- transient recovery obligations

The recovery obligation references the persistent helper identity.

The current repo does not expose a durable filter UUID through its `FilterRef`; do not invent one.

## Current debt to remove

In `stream_state_router/obs/layouts.py`:

- `retry_pending_fade_cleanup()` calls `_set_source_opacity()`
- `_set_source_opacity()` may call `_ensure_fade_filter()`
- therefore recovery can currently create a helper

Also:

- `_ensure_fade_filter()` currently recognizes an existing helper by display name
- successful cleanup returns opacity to 1 but does not enforce verified disable
- helper ownership is not durable independently of pending cleanup

In `stream_state_router/services/recovery.py`:

- current cleanup schema is v2
- `layout_fade` rows do not prove helper ownership

In import/capture:

- all filters are currently treated as ordinary observable filters
- internal helper filtering must be based on proven ownership, not prefix matching

## Required implementation sequence

### 1. Characterization tests first

Before changing behavior, add focused tests covering:

- current v2 layout_fade marker round trip
- current write-ahead callback before opacity mutation
- current reconnect/shutdown transfer behavior
- current import of filters

Do not remove historical fixtures.

### 2. Persistent helper manifest

Implement the smallest persistent store needed for A1.

Properties:

- stored under SSR user-data directory
- versioned
- atomic write using temp + replace or equivalent
- read failures never create ownership proof
- unknown/newer schema fails safe
- malformed entries do not authorize mutations
- duplicate helper IDs fail safe for affected entries
- conflicting duplicate ownership for one target fails safe

Each proven helper record needs enough data to bind:

- generated helper_id
- purpose = layout_fade
- collection/context
- source target
- exact filter name
- exact filter kind
- creation metadata/version

Do not broaden this into A2 ResourceBinding.

### 3. Generated helper identity

Normal helper creation must use a generated identity that can be matched to the persistent manifest.

A filter that only happens to be named `[SSR] Layout Fade` is legacy/ambiguous.

Do not adopt it.

If exact helper display-name format requires a new architectural decision beyond the existing contract, stop and report the concrete alternatives rather than choosing a broad policy silently.

### 4. Split normal lifecycle from recovery lifecycle

Normal lifecycle may create a helper.

Recovery lifecycle must have no CreateSourceFilter path.

Make this separation structural enough that a future call cannot accidentally reuse a create-capable setter from cleanup.

Recovery may:

- observe
- neutralize existing proven helper
- disable existing proven helper
- restore explicitly recorded fields when required
- verify

Recovery may NOT:

- create
- adopt
- delete in A1

### 5. Write-ahead ordering

Before the first temporary opacity effect:

1. resolve collection/context
2. determine planned helper identity
3. persist ownership evidence required to recognize the helper
4. persist recovery obligation
5. only then create/enable/mutate
6. on uncertain transport result, observe before deciding next step

A persistence failure must prevent the temporary opacity mutation.

### 6. Recovery schema evolution

Upgrade recovery schema with backward-compatible reading.

Legacy activation cleanup must remain readable.

Legacy v2 `layout_fade` rows containing only collection/source must remain readable but must be explicitly treated as unproven ownership.

Do not synthesize helper ownership from them.

A new proven obligation must reference enough evidence to cross-check the manifest.

Preserve unknown additive fields where practical.

### 7. Cleanup terminal states

Implement behavior equivalent to:

- verified safe cleanup -> obligation removed
- proven exact helper absent -> no creation; handle as safe terminal absence only if contract/evidence supports it
- OBS unavailable / foreign collection / unresolved target -> retain obligation
- uncertain mutation/readback -> retain obligation
- legacy/unproven collision -> no mutation, report ambiguity

Do not clear an obligation merely because a request was sent successfully.

### 8. Neutralize + disable + verify

For a proven helper:

1. observe current state
2. set neutral opacity if needed
3. verify neutral opacity
4. disable helper if needed
5. verify disabled state
6. then clear durable obligation

A successful transport ACK is not sufficient.

### 9. Import/capture filtering

Update Scene Collection/current-state capture so:

- proven SSR helper is excluded from business/profile filter capture
- unproven helper-looking/user filter is not classified as owned from its name
- ambiguity is reported

Do not silently delete ambiguous filters from a snapshot.

### 10. Runtime/reconnect/shutdown

Preserve the runtime as the single OBS mutation authority.

Pending helper cleanup must still transfer through:

- restart/replacement runtime
- reconnect
- shutdown snapshot

Do not issue OBS mutations from Qt, HTTP or Stream Deck.

## Mandatory fault-injection tests

At minimum:

1. crash/error before helper creation
2. manifest persisted, obligation persistence fails
3. obligation persisted, create not yet sent
4. create accepted, response lost
5. crash after create before opacity write
6. opacity response lost
7. crash during verification
8. crash during neutralization
9. crash after neutralization before disable
10. disable response lost
11. verified cleanup before obligation removal
12. helper absent during recovery
13. foreign Scene Collection
14. source missing
15. source recreated under same name
16. user filter name collision
17. manifest missing/corrupt
18. manifest mismatch
19. external modification of proven helper
20. shutdown with transition/backlog
21. reconnect with pending obligation
22. repeated recovery idempotence

For every relevant case assert:

- no duplicate helper
- no CreateSourceFilter from recovery
- no mutation of unproven user filter
- obligation retained while outcome is uncertain
- obligation cleared only after verified terminal state

## Existing tests to preserve

Especially:

- `tests/test_layouts.py`
- `tests/test_recovery.py`
- `tests/test_runtime.py`
- `tests/test_apply_recovery.py`
- `tests/test_importers.py`
- `tests/test_current_state_capture.py`

Preserve A0's entire fade eligibility matrix.

## Likely files

Expected scope may include:

- `stream_state_router/obs/layouts.py`
- a small dedicated helper-management module if justified
- `stream_state_router/services/recovery.py`
- `stream_state_router/services/runtime.py`
- `stream_state_router/importers/scene_collection.py`
- current-state capture path where needed
- focused tests

Do not refactor unrelated code.

## Explicitly forbidden

- ownership from `[SSR]` prefix alone
- ownership from `[SSR] Layout Fade` name alone
- cleanup calling a create-capable opacity setter
- automatic deletion of user filters
- adoption of pre-existing user filter
- helper creation during recovery
- generic transaction engine
- A2 binding system
- B0 property framework
- new composite fade strategy
- changes to Stream Deck business logic

## UX budget

Simple mode gets no new configuration field.

Automatic:

- provenance
- cleanup
- retry
- helper exclusion from capture

Only genuinely ambiguous legacy ownership may require a future explicit user confirmation.

User-facing simple diagnostics must be action-oriented, not schema-oriented.

## Acceptance criteria

Automated:

- full test suite green
- lint green
- CodeQL green
- recovery idempotence proven
- no cleanup CreateSourceFilter path
- v2 markers readable without ownership promotion
- proven helpers excluded from capture
- ambiguous lookalikes preserved/reported
- obligations removed only after verified result

Real Windows/OBS:

- kill/restart during fade
- helper absent during recovery
- name collision
- foreign collection
- source recreated same name
- shutdown with backlog
- reconnect with pending cleanup

## Gate after implementation

Do not merge.

Keep the implementation in a draft PR/branch.

Prepare an Astra re-audit focused on:

- provenance
- persistent manifest
- write-ahead ordering
- recovery idempotence
- v2 migration
- no-create cleanup guarantee
- import exclusion proof

Astra must validate A1 before any new temporary-helper mechanism or A2 work begins.
