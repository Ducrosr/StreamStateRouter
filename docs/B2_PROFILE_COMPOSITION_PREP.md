# B2 preparation — composable profiles and independent variables

Preparation only. Do not implement before B0/B1 architecture is stable.

## Existing concepts to preserve

SSR already has:

- GameProfile
- OverlayProfile
- CaptureProfile
- AudioProfile
- LayoutProfile
- profile `extends`
- profile `conditions`
- control variables
- deterministic desired-state conflict detection
- provenance on desired assignments

B2 should compose these primitives rather than replace them.

## Product problem

Avoid generating profiles for every Cartesian combination:

Game × Mood × Capture × Audio × HDR × Layout.

Real example:

Game:

- Vanilla
- Overwatch
- League of Legends

Mood:

- Happy
- Focused
- Angry
- Tired

Mood is independent from most game-specific capture/audio/layout choices.

The model should preserve that independence.

## Recommended resolution layers from Astra

Conceptual order:

1. configured/base state
2. application/game-specific differences
3. independent fragments such as Mood
4. temporary manual override
5. ownership/exclusion constraints

Do not let dictionary iteration order decide conflicts.

## Profile inheritance

Use `extends` for true parent/child relationships.

Example:

Default
  -> common stream state

Overwatch
  -> extends Default
  -> only capture/HDR/layout differences

Do not copy the whole parent into every child.

## Independent fragments

Mood is better represented as an independent conditional fragment than as:

- Overwatch_Focused
- Overwatch_Angry
- LoL_Focused
- LoL_Angry
- etc.

A fragment should contribute only the properties it owns.

If two fragments at the same resolution level assign different values to the same property:

- produce an explicit conflict
- preserve provenance
- do not choose one from ordering

## Game × Mood avatar use case

Target resolution:

- active app determines Game
- control variable determines Mood
- avatar property/state is resolved from both where explicitly modeled
- unrelated Capture/Audio/Layout properties remain game-derived and are not duplicated per mood

Fallback:

- no game match -> Vanilla
- missing mood-specific mapping -> explicit fallback defined by the avatar model, not silent arbitrary choice

## Temporary overrides

Existing release modes remain:

- manual/permanent
- duration
- foreground change
- stream end

B2 must ensure override precedence is explicit and does not mutate base profile definitions.

SSR's own foreground window must not trigger foreground-release semantics.

## Conditions

Keep conditions specialized and readable.

Do not introduce a general DSL.

Only add condition forms required by real composition cases.

## Difference-to-parent interaction with B1

B1 capture should be able to say:

"This setup inherits Default and changes only these properties."

B2 resolver must make the effective value and provenance inspectable so B1 can compute/store a minimal child safely.

## Tests

### Inheritance

- one parent
- multi-level inheritance
- missing parent
- circular inheritance
- child explicit override
- inherited no-op
- provenance preserved

### Fragments

- game-only
- mood-only
- game + mood disjoint properties
- game + mood same value
- game + mood conflict
- fallback fragment
- missing variable

### Overrides

- override over composed state
- duration expiry
- true foreground change
- SSR foreground ignored
- stream-end release
- pause interaction
- Auto while paused preserves established semantics

### Determinism

- reordering profile dictionaries does not change result
- same logical inputs produce same desired state/signature
- conflicts are stable and explainable

## UX target

Simple mode should show:

- "Jeu : Overwatch"
- "Humeur : Focused"
- effective setup/result

Not:

- merge precedence tables
- fragment IDs
- raw condition expressions

Diagnostic mode can answer:

"Why is this value active?"

with provenance such as:

- Default
- Overwatch
- Mood=Focused
- temporary override

## Gate

Astra checkpoint after B1+B2 should verify:

- composition semantics
- conflict policy
- difference-to-parent correctness
- no combinatorial profile explosion
- no business logic moved into Qt
