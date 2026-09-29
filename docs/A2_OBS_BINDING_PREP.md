# A2 preparation — OBS resource bindings and controlled repair

Preparation only. Do not implement before A1 is approved.

## Existing repository capabilities to reuse

The current OBS catalog already exposes useful identity evidence:

- SceneRef: name + scene UUID
- InputRef: name + input UUID
- SceneItemRef:
  - collection
  - root scene
  - container
  - container kind
  - path
  - source name
  - source UUID
  - source kind
  - occurrence
  - scene_item_id (ephemeral)
- FilterRef:
  - source
  - filter name
  - kind
  - enabled

The declarative observer already validates scene-item cardinality and source UUID freshness in some paths.

The current reference-repair service is mostly name/candidate based. Keep it useful for suggestions, but A2 must not promote fuzzy similarity into automatic ownership/binding.

## A2 identity principles from Astra

- logical SSR identity is separate from readable alias
- use OBS UUIDs when available
- old references remain readable
- rename and recreate are different events
- same proven identity can update its alias automatically
- new resource under the same old name must not inherit ownership automatically
- never persist sceneItemId as durable identity
- never use filter index as durable identity
- ambiguous candidate repair requires explicit user choice

## Proposed ResourceBinding scope

Do not create one universal binding object for every external system yet.

A2 only needs an OBS binding representation sufficient for:

- scene
- input/source
- scene-item occurrence
- filter reference where possible

Candidate fields:

- logical_id
- resource_kind
- collection context
- external_uuid when available
- readable_alias
- expected kind
- parent/container binding where applicable
- occurrence for scene items
- last-seen metadata for diagnostics

### Scene

Strong evidence:

- scene UUID if present

Alias:

- scene name

Rename with same UUID:

- update alias automatically

Same name with new UUID:

- treat as recreation/new target

### Input/source

Strong evidence:

- input/source UUID when OBS exposes it

Alias:

- input/source name

Rename with same UUID:

- update alias automatically

Same name/new UUID:

- do not auto-rebind

### Scene item

Do not persist sceneItemId.

Binding should use parent/container identity + source identity + occurrence/fingerprint evidence.

Freshly resolve sceneItemId at execution time.

If duplicate cardinality/order changed, block/replan rather than guess.

### Group

Current catalog has group names but no equally strong durable UUID contract in the repository.

Do not invent one.

Treat group binding as weaker/explicitly qualified until stronger evidence exists.

### Filter

Current FilterRef has no UUID.

Therefore:

- source binding must be resolved first
- filter name + kind are readable identity evidence, not universally stable identity
- automatic rename repair cannot be promised without stronger proof
- similar-name heuristic is candidate generation only
- helper filters from A1 are special because SSR owns their generated identity/manifest
- user filters remain a distinct class

Filter identity ambiguity is an Astra decision point before automatic repair is broadened.

## Migration strategy for old name-only references

For every old reference:

1. Read current catalog.
2. If one stable UUID-backed target uniquely matches the legacy alias and context, create a binding proposal.
3. Do not silently persist a new binding if multiple candidates are plausible.
4. Preserve the old readable alias for diagnostics/migration.
5. Once bound, later same-UUID rename updates the alias automatically.
6. Later same-name/different-UUID resource is reported as recreation.

Do not use fuzzy similarity for silent migration.

## Existing fuzzy repair service

`scan_obs_reference_repairs()` can remain useful for candidate suggestions.

A2 should separate:

- proven identity repair
- candidate suggestion

Only the former may be automatic.

Candidate confidence text must not become ownership proof.

## Tests to prepare

### Scene/input rename

- same UUID + different name -> binding survives, alias updates
- same name + new UUID -> no silent transfer
- UUID missing -> fallback behavior explicit/ambiguous

### Scene item

- fresh resolution after reconnect
- sceneItemId recycled
- duplicate source occurrence added/removed
- occurrence reorder
- parent scene renamed with same UUID where available
- source renamed with same UUID
- source recreated same name/new UUID

### Collection

- same collection name after OBS restart
- different collection same resource names
- collection changed during preparation/execution

Collection equivalence policy remains an Astra decision if no durable collection ID exists.

### Filter

- exact filter still present
- filter renamed without stable identity -> candidate only
- filter deleted/recreated same name -> no claimed continuity
- duplicate candidate filters on different sources
- A1 proven helper excluded from user-filter repair logic

## UX target

Simple mode:

- no UUID entry
- no sceneItemId entry
- same proven identity rename: automatic
- missing/new ambiguous target: show human-readable candidates
- one confirmation only when a genuinely new target must be chosen

Advanced mode may display identity evidence for diagnostics.

## A2 gate

Astra must review:

- binding identity rules
- migration behavior
- collection-equivalence assumptions
- filter ambiguity policy

B0 should only rely on bindings after this contract is stable.
