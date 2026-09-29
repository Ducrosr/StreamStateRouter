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
