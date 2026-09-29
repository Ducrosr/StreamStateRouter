# D0 preparation — temporary surrender and safe Try / Keep / Revert

Preparation only. Do not implement before B0 and the relevant domain adapters are stable.

## Existing base to reuse

SSR already has useful narrow mechanisms:

### Layout preview

`OBSLayoutManager` already supports:

- capture of a restoration snapshot
- refusal when a complete restoration snapshot cannot be guaranteed
- apply without recording undo
- cancel preview
- commit preview into the undo stack
- Scene Collection/session guards during restore
- runtime-owned visibility exclusion

This is a strong domain-specific preview implementation.

### Manual override

SSR already has runtime manual override behavior and release modes.

D0 must not replace the override engine with a second system.

## Product objectives

Two distinct user intentions:

1. "Essayer ce réglage/setup" -> apply temporarily, then Keep or Revert when safe.
2. "Laisser OBS contrôler temporairement ce réglage" -> SSR stops asserting one owned property until control is explicitly/conditionally reacquired.

Do not conflate these.

## Temporary surrender

Internal concept may be ownership/lease suppression.

Simple UX:

- "Laisser OBS contrôler temporairement ce réglage"
- "Reprendre le contrôle"

A surrendered property must:

- be absent from active writes/reconciliation by SSR
- remain explainable in diagnostics
- not silently mutate its configured desired value
- be reacquired explicitly or by a clearly configured release condition
- re-observe before reasserting control

Do not expose "lease" terminology in simple mode.

## Scope

Surrender should be property-scoped, not entire-profile-scoped unless the user explicitly chooses a larger scope.

Examples:

- one filter saturation property
- one Windows app volume
- one HDR property

Layout visibility owned by another runtime subsystem remains a separate existing ownership mechanism; do not blindly unify all ownership implementations in D0.

## Try / Keep / Revert

A multi-domain "Try" cannot be a fake transaction.

Each property/domain needs:

- pre-state observation
- target binding identity/context
- whether compensation is supported
- restoration preconditions
- result status

The aggregate trial result may be:

- fully applied
- partially applied
- uncertain

The aggregate revert may be:

- fully restored
- partially restored
- not restorable
- uncertain

## Restoration rule

Restore only when:

- previous value was known
- target identity/context is still the same
- adapter supports compensation
- no external modification invalidates the restoration precondition

If an external actor changed the property after the trial, do not blindly overwrite it with the old value.

## Suggested restoration precondition

For each trialled property record:

- previous observed value
- trial target value
- binding generation/context
- result of application

Before revert:

1. re-resolve target
2. observe current value
3. verify current value is still compatible with "owned trial state"
4. only then compensate to the previous value

If the current value diverged unexpectedly:

- report external/conflicting change
- do not overwrite without explicit policy

## Crash behavior

A temporary trial that mutates external systems needs recovery obligations only for domains whose compensation contract justifies it.

Do not automatically journal every declarative property forever.

For temporary changes:

- write enough recovery state before mutation
- on restart, observe target/context
- offer/perform compensation only when safe under the domain contract

D0 can reuse A1's write-ahead lessons without turning the entire system into event sourcing.

## Interaction with manual override

Manual override expresses desired-state precedence.

Temporary surrender expresses "SSR temporarily does not own this property."

Try expresses temporary physical application with possible compensation.

Keep turns a trial into accepted configuration/state where supported.

These are three different concepts and should remain distinguishable in code.

## Tests

### Surrender

- surrender one property
- other properties still converge
- reconciliation does not reassert surrendered property
- external manual change persists during surrender
- reacquire observes first
- reacquire after target replacement
- override + surrender precedence
- pause interaction
- app/foreground release condition if supported

### Try/Revert

- known previous state
- unknown previous state -> preview limitation
- apply success + revert success
- partial apply
- target disappears before revert
- target recreated under same name
- session/collection changes
- external modification after trial
- response lost on apply
- response lost on revert
- crash during trial
- crash during compensation

### Layout compatibility

Preserve all existing layout preview/cancel/undo guards.

Do not weaken session identity rules to make generic D0 fit.

## UX target

Try:

- "Essayer"
- "Garder"
- "Revenir"

If full restoration cannot be guaranteed before applying, tell the user before mutation.

Surrender:

- "Laisser OBS contrôler temporairement"
- "Reprendre le contrôle"

Diagnostics may explain why Revert is unavailable.

## Gate

Astra review must focus on:

- compensation preconditions
- external modification detection
- ownership/surrender semantics
- crash behavior
- no fake cross-domain atomicity
