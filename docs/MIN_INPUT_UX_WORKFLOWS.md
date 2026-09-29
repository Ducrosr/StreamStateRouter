# Minimal-input UX inventory — preparation for B1/B2

This document records product constraints only. It does not authorize architecture changes before their roadmap gates.

## Principle

The user describes the intended result. SSR discovers, infers and configures as much of the procedure as safely possible.

Internal concepts such as DesiredState, ownership, capabilities, recovery obligations and bindings should not be required knowledge in the simple workflow.

## Target workflow: add a game

Current conceptual burden can span:

- executable/rule
- GameProfile
- OverlayProfile
- CaptureProfile
- AudioProfile
- LayoutProfile
- HDR policy
- avatar game state

Target simple path:

1. SSR shows recently/actively observed applications.
2. User chooses the game.
3. SSR captures the current relevant OBS/host state.
4. SSR proposes one setup with provenance and warnings.
5. User changes only meaningful differences.
6. Preview.
7. Confirm.

Advanced mode exposes domain placement and conflict resolution only when required.

## Target workflow: capture current setup

1. User arranges OBS visually.
2. "Capturer cet état".
3. SSR compares against the selected/default parent.
4. SSR stores only meaningful differences where the model supports it.
5. SSR excludes proven internal helpers/transient state.
6. SSR reports unknown/ambiguous items separately.
7. User names/confirms the setup.

## Target workflow: repair a missing reference

1. SSR detects a missing binding.
2. If the same stable identity proves a rename, repair automatically and update the readable alias.
3. If a new resource merely resembles the old one, present candidates.
4. User selects only when identity is genuinely ambiguous.

Never ask for UUIDs or internal OBS IDs in simple mode.

## Target workflow: temporary manual control

Simple wording:

- "Laisser OBS contrôler temporairement ce réglage"
- "Reprendre le contrôle"

Do not expose lease/ownership terminology unless the user opens diagnostics.

## Target diagnostics

Answer:

- what triggered the change
- which setup/rule won
- what SSR intended
- what was actually applied
- what is waiting/unknown
- what the user can do next

Example:

"Overwatch est actif. SSR utilise les réglages capturés pour ce jeu. Le layout est appliqué. L'audio attend le lancement de la session du jeu."

## Complexity budget template for every future lot

Record:

- mandatory simple-mode inputs added
- mandatory inputs removed
- values auto-discovered
- values inferred
- defaults applied
- confirmations retained
- ambiguities still requiring a human decision

A technically complete feature that requires unnecessary manual configuration is not product-complete.

## Current guided-capture baseline observed in the code

The existing `CurrentStateCaptureDialog` already auto-detects and displays:

- foreground process
- process path
- foreground window title
- current OBS scene
- matching process rules
- suggested configuration name

This is good and should be preserved.

The simple-user burden is still higher than necessary because the dialog exposes:

- choose target rule when several rules match
- configuration name for a new rule
- include source settings?
- include OBS mute/volume?
- include filters?
- include Scene Item visibility?
- include layout?
- choose domain for source settings
- choose domain for audio
- choose domain for filters
- choose domain for visibility

The four domain selectors already have semantic defaults in the UI:

- source settings -> GameProfile
- OBS audio -> AudioProfile
- filters -> CaptureProfile
- visibility -> OverlayProfile
- layout -> LayoutProfile

These defaults are evidence that simple mode does not need to ask the user to understand the domains.

## Recommended future simple-mode capture

No implementation before the B1 gate.

### Normal case: no existing rule

1. Detect application and current scene.
2. Generate the name automatically.
3. Capture the observable state.
4. Infer the standard domain placement.
5. Exclude proven SSR-internal/transient resources.
6. Show a concise diff/proposal.
7. User presses "Créer ce setup".

Mandatory decisions: 1 confirmation.

The generated name remains editable, but editing is optional rather than required.

### Normal case: exactly one matching rule

1. Detect the unique existing rule.
2. Build an update proposal automatically.
3. Show only the meaningful differences.
4. User presses "Mettre à jour ce setup".

Mandatory decisions: 1 confirmation.

### Ambiguous case: multiple matching rules

Only here ask:

"Quelle configuration voulez-vous mettre à jour ?"

Then show the same proposal/confirmation.

Mandatory decisions: 1 rule selection + 1 confirmation.

### Advanced mode

Keep explicit controls for:

- inclusion/exclusion by category
- domain placement
- inheritance target
- raw technical warnings
- ownership details

Advanced controls must not weaken safety checks.

## Immediate UX metrics to preserve for later B1 acceptance

Track these values before/after B1:

| Workflow | Current possible explicit decisions | Target simple-mode decisions |
|---|---:|---:|
| New setup, normal case | name + 5 include toggles + 4 domain selectors | 1 confirmation |
| Update unique matching rule | 5 include toggles + 4 domain selectors | 1 confirmation |
| Multiple matching rules | rule + 5 include toggles + 4 domain selectors | rule + confirmation |
| Layout-only capture | profile/scene/options available in dedicated editor | capture + confirmation, advanced options optional |

The target does not mean hiding uncertainty. Any genuine ambiguity must still become an explicit question.

## Things SSR can infer without new architecture

These are already available in the current implementation and therefore do not need to become user inputs:

- foreground executable
- process path
- window title
- current OBS scene
- existing rules for the process
- a unique matching rule
- a suggested unique configuration name
- default semantic domain placement

These can be used immediately by future UI work once its roadmap gate opens.

## Things that should wait for later primitives

Do not fake these before their respective gates:

- safe exclusion of SSR-owned helpers -> A1
- durable resource identity/rename repair -> A2
- typed property capture/diff -> B0
- parent-difference minimization and richer guided draft -> B1/B2
- Windows audio session inference -> C0
- HDR policy inference -> C1

This avoids creating a "simple" UI that silently guesses beyond what SSR can currently prove.
