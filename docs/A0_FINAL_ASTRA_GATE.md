# A0 final Astra gate package

Frozen candidate:

`6a288052001cd01cf7283251bc506d6219ea721c`

PR:

`#148 fix/post-audit-r1-r6`

At preparation time:

- PR open
- PR draft
- mergeable
- Tests success
- CodeQL success
- Windows job: 617 tests passed
- Ruff passed

## What changed through A0

A0 now uses one live fade-eligibility decision based on:

- saved profile source type
- current OBS source type
- inventory completeness
- source occurrence count

Direct fallback occurs for:

- composites
- unknown current type
- stale/mismatched type
- missing saved type
- shared source
- incomplete/ambiguous topology
- unknown layout-owned visibility

Runtime-owned visibility remains excluded from layout writes.

## Final residual issues fixed since the previous Astra block

- ambiguous group discriminator no longer becomes a trusted scene
- referenced scene missing from GetSceneList makes inventory incomplete
- non-text scene/source names are not coerced into apparently valid identifiers
- numeric strings remain valid identifiers

## Stress audit performed while Astra quota was unavailable

All `_set_source_opacity` call sites in `layouts.py` were inspected.

Transition fade/move_fade opacity writes are reached through the specialized paths that consume `fade_eligible`.

The generic transition function still contains dead legacy fade branches, but `fade` and `move_fade` return to their specialized implementations before that branch. No cleanup/refactor was made because changing the frozen audit SHA would add unnecessary noise.

`_animate_opacity()` appears unused; it was likewise left untouched.

No additional demonstrated A0 defect was found.

## One scope question to challenge, not silently change

A nested scene can itself be instantiated more than once by parent scenes.

An internal source may therefore render multiple times while only existing once as a Scene Item inside the nested scene.

Question for Astra:

Does A0's source-occurrence safety criterion need to account for multiplicity of a containing nested scene, or is one internal source-level fade intentionally valid because all instances of that nested scene share the same internal source/geometry/effect anyway?

This is not currently classified as a bug. Do not change behavior solely from this question without a concrete ownership/intent argument.

## Required final verdict

Astra must choose:

- APPROUVER A0
- BLOQUER A0

If approved, explicitly state:

`A0 EST FERMÉ. SOL PEUT COMMENCER A1.`

Do not merge as part of the audit.
