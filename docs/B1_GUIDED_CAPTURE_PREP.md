# B1 preparation — guided capture centered on the application

Preparation only. Do not implement before B0 is approved.

## Reuse the existing capture service

SSR already has:

- `CurrentStateCaptureOptions`
- `CurrentStateCaptureDraft`
- `CurrentStateCaptureReport`
- `build_current_state_capture_draft()`
- foreground process/path/title context
- current OBS scene
- rule matching by process
- suggested unique names
- Scene Collection snapshot
- layout capture
- config validation before commit
- preview/confirmation before "Enregistrer et appliquer"

B1 should evolve this path rather than create a second capture engine.

## Product objective

Simple workflow:

1. application detected
2. current OBS state read
3. SSR builds one proposal
4. only meaningful differences/ambiguities shown
5. user confirms

The simple flow should not ask the user to assign technical domains.

## Current technical choices to hide in simple mode

The current dialog exposes four domain selectors:

- source settings -> GameProfile
- OBS mute/volume -> AudioProfile
- filters -> CaptureProfile
- visibility -> OverlayProfile

These already have sensible semantic defaults in code.

B1 simple mode should apply those defaults automatically.

The current include toggles should become:

- automatically inferred from observable, supported state
- summarized in the proposal
- editable in advanced mode

## Proposal model

Do not build a new UI-owned model.

Extend the existing draft/report service with enough evidence to answer:

- what was discovered
- what SSR proposes to control
- where it will be stored
- what differs from the parent/current profile
- which items were excluded as internal/transient
- which items are unsupported
- which items are ambiguous and require a human choice

The UI renders this result; it does not decide ownership/business semantics.

## Normal workflows

### New process with no matching rule

Automatic:

- process identity
- current scene
- suggested name
- standard domain placement
- current logical base
- supported observable properties

User action:

- confirm proposal

Optional:

- edit display name
- open advanced scope

### One existing matching rule

Automatic:

- select the unique rule
- compare current state to its effective profiles
- propose only meaningful changes

User action:

- confirm update

### Multiple matching rules

This is a real ambiguity.

Ask exactly one question:

- which rule/setup to update

Then return to the same proposal flow.

## Difference-first storage

When the profile model supports inheritance safely:

- compare captured state with effective parent/base
- persist only meaningful differences
- do not duplicate inherited values

Do not attempt this for data that lacks a stable typed comparison contract.

B1 depends on B0 for that reason.

## Internal resources

A1 proven internal helpers:

- never become business properties in the draft

Ambiguous helper-like filters:

- reported, not silently discarded

A2 bindings:

- use aliases for presentation
- preserve identity evidence in the draft

## Draft evidence

For each proposed item, retain enough information for diagnostics:

- source/capture origin
- current observed value
- proposed stored value
- target domain/profile
- capability status
- binding confidence/proof
- warning/ambiguity
- whether value is inherited or an explicit difference

Do not expose all of this by default.

## Preview

B1 preview should be a proposal/config preview first.

Do not promise cross-domain physical rollback until D0.

For OBS properties already safely previewable:

- reuse existing preview/undo mechanisms

For unsupported domains:

- show that physical preview is unavailable rather than faking it.

## Tests

Service tests first:

- no existing rule
- unique existing rule
- multiple matching rules
- suggested name collision
- current scene missing
- partial snapshot
- A1 internal helper excluded
- ambiguous lookalike reported
- A2 renamed binding
- unsupported property omitted/warned
- shared profile cloned before modification
- parent difference retained minimally
- no-op capture creates no unnecessary actions
- config validation failure prevents commit

UI tests:

- simple mode hides domain selectors
- advanced mode exposes them
- unique rule does not ask a redundant question
- ambiguous rules do
- confirmation summary matches the draft

## UX acceptance

Normal new setup:

- one confirmation required

Unique existing setup:

- one confirmation required

Multiple matching rules:

- one rule choice + one confirmation

No UUIDs, OBS request names, DesiredState terminology or profile-domain knowledge required.

## Gate

Astra should review B1 together with B2 for:

- inference boundaries
- conflict handling
- difference-to-parent semantics
- UI/service separation
