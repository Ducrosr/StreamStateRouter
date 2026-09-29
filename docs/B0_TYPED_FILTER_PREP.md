# B0 preparation — first typed OBS filter property

Preparation only. Do not implement before A2 is approved.

## Important finding from the current repository

SSR already contains most of the generic declarative scaffolding B0 needs.

`PropertyKey` already supports:

- `filter_enabled`
- `filter_setting`

`DesiredAssignment`, `DesiredState`, `ObservedValue` and `ObservedState` already exist.

The planning service already:

- discovers filter sources
- reads GetSourceFilterList
- preflights filter existence
- observes GetSourceFilter
- reads filter enabled state
- reads one named filter setting

Therefore B0 should NOT introduce a second desired/observed/property framework.

The main missing piece is qualified execution plus a minimal typed descriptor/capability contract.

## Current executor boundary

The current declarative executor allowlist is limited to:

- scene_item_visibility
- input_mute
- input_volume_db

B0 should add only the first explicitly qualified filter slice.

## First slice from Astra

Target:

- OBS Color Correction v2
- enabled
- saturation

Do not generalize to arbitrary filter settings.

The repository currently contains no trusted saturation bounds for Color Correction v2.

Do not invent a numeric range from memory.

Before implementation, derive/verify the supported field contract from:

- actual OBS response on the supported version
- project fixtures if added
- authoritative OBS/plugin contract if needed

Then encode that contract explicitly.

## Minimal descriptor proposal

A small static descriptor is enough.

Candidate information:

- logical property kind
- supported filter kind(s)
- setting name
- value type
- validation function
- equality/tolerance
- required OBS read/write requests
- readable/writable/verifiable flags
- redaction policy if relevant

Avoid:

- class hierarchy per property
- arbitrary JSON schema engine
- plugin framework
- runtime reflection that treats unknown settings as safe

## Capability decision

Keep property and capability separate.

Examples:

- property exists conceptually: saturation
- current target capability may be:
  - allowed
  - unavailable
  - unknown
  - degraded

Capability should consider:

- exact filter kind
- supported OBS requests
- target binding validity
- current collection/session
- readback availability

Unknown is not allowed.

## B0 execution sequence

For one supported property:

1. resolve binding from A2
2. verify filter kind and descriptor
3. observe current value
4. validate desired value
5. plan pure diff
6. execute only the exact supported write
7. read back
8. compare with typed comparator/tolerance
9. publish converged / partial / unknown result

For `filter_enabled`:

- write only enabled state

For saturation:

- write only the saturation setting using overlay semantics appropriate to the existing OBS action model
- do not replace unrelated settings

## Executor changes likely required

Extend the executor allowlist with only:

- filter_enabled
- filter_setting for specifically qualified descriptor(s)

Add physical binding/evidence sufficient to ensure the source/filter observed during preparation is still the intended target during execution.

Do not rely only on display names once A2 bindings exist.

## Tests

### Descriptor

- supported exact filter kind
- unsupported filter kind
- supported setting
- unknown setting rejected
- wrong type rejected
- non-finite numeric value rejected
- boundary behavior based on verified contract

### Planning

- known equal value -> no write
- known different value -> write operation
- unknown observed value -> no blind write unless the established planner contract explicitly permits this idempotent assignment
- missing filter -> blocked
- partial catalog -> blocked
- wrong collection -> blocked

### Execution

- target changed after plan -> replan
- session generation changed -> replan
- filter removed after plan -> fail safely
- filter replaced/recreated -> binding mismatch/block
- SetSourceFilterEnabled ACK + readback
- SetSourceFilterSettings ACK + readback
- response lost -> observe before deciding
- unrelated settings preserved
- readback mismatch -> non-converged result

### A1 interaction

- proven SSR helper filter is never exposed as an ordinary user filter property
- user Color Correction filter remains distinct from A1 helper
- same display prefix does not confer ownership

## UX target

Simple workflow:

1. SSR discovers compatible Color Correction filters.
2. User selects a human-readable filter only when more than one candidate is relevant.
3. UI shows "Saturation" and "Activé".
4. Current values are prefilled.
5. Capture-current-state can populate the property automatically later.

Do not show:

- PropertyKey
- request names
- raw filter JSON
- binding UUIDs

Advanced diagnostics may show technical evidence.

## B0 exit criteria

- first complete typed slice works read -> plan -> write -> verify
- no arbitrary filter JSON introduced
- existing generic property models reused
- executor remains opt-in/allowlisted
- target identity honors A2
- A1 helpers remain excluded
- tests + real OBS validation green
- Astra reviews the first property/capability contract with A2
