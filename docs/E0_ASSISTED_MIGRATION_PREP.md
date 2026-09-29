# E0 preparation — assisted Scene Collection / Advanced Scene Switcher migration

Preparation only. Do not implement before B1/B2 and the required capability gates are stable.

## Existing base to preserve

The current `AdvancedSceneSwitcherImporter` is deliberately conservative:

- only translates patterns whose semantics SSR can reproduce
- rejects unsupported composed logic instead of approximating it
- rejects unsupported parallel execution
- rejects unsupported else-actions and timing semantics
- validates referenced OBS inputs against a snapshot where possible
- recognizes a narrow SoundVolumeView `/SetAppDefault` pattern
- converts supported OBS mute/volume actions
- imports control-variable defaults
- contains special handling for Game/Mood-related migration patterns
- records rejected macros with their raw representation and reason

This is the correct safety posture for E0.

Do not replace it with a "best effort" macro interpreter.

## Product objective

Migration UX:

1. Analyze existing configuration.
2. Group items by user intent.
3. Automatically translate only proven-equivalent patterns.
4. Surface only genuine ambiguities/incompatibilities.
5. Build the same business draft model used by guided capture.
6. Preview.
7. Confirm.

The user should not need to understand every ASC segment, internal profile domain or raw JSON field.

## Shared-draft direction

B1 and E0 must converge on one draft representation/service.

Do not create:

- one draft model for "capture current state"
- a different draft model for ASC
- a third draft model for Scene Collection import

All should be able to express:

- proposed business changes
- provenance/source
- capability/identity evidence
- warnings
- rejected/unsupported items
- ambiguities requiring a decision
- target profiles/fragments
- config diff/preview

## Migration categories

### Automatic, high confidence

Examples already close to supported behavior:

- direct foreground process rule
- exact supported scene/source visibility action when target binding is unique
- exact OBS mute/unmute
- exact dB volume within supported range
- exact supported input-setting assignment
- recognized control-variable assignment
- exact Game-variable mappings already modeled
- recognized SoundVolumeView routing pattern while that operation remains supported

After A2/B0, automatic translation must also require valid bindings/capabilities, not names alone.

### Automatic proposal requiring confirmation

Examples:

- unique legacy name can be bound to one proven current UUID-backed resource
- old profile/domain placement can be simplified into a clearer setup while preserving effective behavior
- a set of macros clearly forms one app setup but changes several domains

The translation is proposed, not silently activated.

### Semi-automatic

Examples:

- multiple rules target the same application
- several candidate resources could replace a missing reference
- source/filter lacks stable identity
- legacy helper-like filter is ambiguous
- composition can be represented by B2 but requires choosing how two same-level conflicting values should be resolved

Ask only the actual ambiguity.

### Manual / unsupported

Keep raw rejection and reason for:

- unsupported ASC logical composition
- parallel semantics not reproducible
- generic script/run action
- unsupported timing/retry behavior
- non-equivalent media semantics
- unknown plugin-specific action
- behavior whose ordering/concurrency is material but not representable in SSR

Never approximate these silently.

## Intent grouping

Instead of presenting a list of macros as the primary migration unit, group recognized macros into user-facing intents where reliable.

Candidate groups:

- application detection/setup
- avatar Game/Mood
- capture
- overlay visibility/layout
- OBS audio
- Windows audio routing
- HDR
- control-variable buttons

A group remains traceable to the source ASC macros.

## Advanced Scene Switcher coexistence

Migration is progressive.

Until an intent is fully migrated and validated:

- do not silently disable its ASC macro
- identify possible concurrent ownership
- warn when ASC and SSR may both control the same property
- allow the user to validate SSR before retiring the old macro

E0 should eventually produce a report:

- migrated
- migrated but awaiting validation
- not migrated
- conflict with existing SSR ownership
- external/ASC ownership still active

Do not auto-delete ASC macros.

## Special Game / Mood case

The importer already recognizes some changed-variable patterns involving Game and Mood.

After B2:

- preserve Game and Mood as independent dimensions where possible
- do not materialize a Cartesian set of combined profiles
- translate Stream Deck variable buttons into SSR control-variable intentions
- report exact button rebinds required

## SoundVolumeView / Windows audio

Existing recognized `/SetAppDefault` remains a separately qualified routing operation.

When C0 native volume/mute is available:

- migrate OBS audio and Windows app audio according to their actual domains
- do not reinterpret routing as volume/mute
- do not promise native replacement for `SetAppDefault` unless proven

## HDR

When C1 exists:

- translate only ASC patterns whose intended HDR state is semantically clear
- map to HDR/SDR/preserve policy
- do not infer Windows Auto HDR
- OBS color-space settings remain separate typed capture properties

## Migration report

User-facing summary should prioritize:

- what will work automatically
- what needs one decision
- what remains unsupported
- what old automation must stay active for now

Advanced diagnostics can include:

- source macro name
- raw rejection reason
- translated actions/properties
- provenance
- bindings
- capability evidence

## Tests

Importer characterization:

- all currently supported patterns remain equivalent
- rejected patterns stay rejected
- order-independent grouping
- Game/Mood special cases
- existing-rule attachment
- conflict rejection
- disabled/paused/parallel macros
- control-variable defaults
- SoundVolumeView exact pattern

Draft integration:

- same draft format as B1
- identity/binding ambiguity
- A1 helper exclusion
- existing SSR ownership conflict
- unsupported capability
- no auto-enable of unvalidated created rules
- config validation before export/apply

Migration round-trip/equivalence:

- compare effective SSR desired state to the subset of ASC behavior claimed equivalent
- never claim equivalence for rejected patterns

## UX budget

Normal migration:

- choose/import source only if not auto-detected
- confirm proposal

Additional inputs only for true ambiguities.

Do not ask the user to classify every macro manually.

## Gate

Astra should review the first shared migration draft and equivalence taxonomy before broadening supported ASC patterns.
