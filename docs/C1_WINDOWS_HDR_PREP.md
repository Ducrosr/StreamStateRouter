# C1 preparation — declarative Windows HDR

Preparation only. Do not implement before B0/A2 gates.

## Current repository state

SSR already has a substantial native `WindowsHDRController` using Windows DisplayConfig.

Current capabilities:

- enumerate active display paths
- identify primary display
- read HDR/Advanced Color support
- read enabled state
- set HDR state
- verify by polling after a write
- support primary/all scopes
- use the dedicated Windows 11 24H2 HDR API where applicable
- fall back to the earlier Advanced Color API on older supported Windows versions

The existing `HostControlController` exposes a one-shot `windows_hdr` action.

Therefore C1 should integrate and qualify this controller rather than replacing it.

## Main gap

HDR is currently an action, not a declarative observed property with:

- binding
- desired state
- planner diff
- ownership
- drift/reconciliation
- result semantics
- compensation boundaries

## Product semantics from Astra

Simple choice should be close to:

- HDR
- SDR
- Auto/Conserver according to defined policy

"Auto" must NOT silently mean enabling Windows Auto HDR.

Recommended meaning:

- apply the known game/profile HDR policy when one exists
- otherwise preserve current state

## Display binding

Current controller supports `primary` and `all`.

C1 should avoid pretending "primary" is a durable display identity if the topology changes.

Before mutation:

- observe active display topology
- resolve the intended target
- establish support
- bind enough target identity for the operation window
- verify the same target context after write

Long-term display binding may need a stronger identity than `primary`; do not invent a permanent cross-machine display identity prematurely.

## Property proposal

Specialized host property, not an OBS filter property.

Candidate logical value:

- HDR enabled boolean on a resolved display target

Separate policy-level UX value:

- HDR
- SDR
- preserve/auto-policy

The policy resolves to a concrete desired property only when appropriate.

## Execution

1. Resolve display target.
2. Observe supported/enabled.
3. If unsupported -> explicit unavailable result.
4. If already desired -> no write.
5. Write using current WindowsHDRController.
6. Read back until bounded timeout.
7. Publish converged/non-converged.
8. If topology changes during operation -> unknown/replan rather than applying to another display.

## Compensation/preview

Do not promise atomic rollback with OBS or audio.

A preview/temporary HDR change can only be restored safely if:

- previous state was observed
- display target is still the same
- compensation is supported
- no conflicting external change invalidated the assumption

Return states such as:

- restored
- partially restored
- not restorable
- uncertain

## OBS capture-color boundary

Windows HDR state does not by itself prove the correct OBS capture color settings.

Rec.2100 PQ or other source settings should only be proposed when:

- the CaptureProfile/binding identifies the relevant capture source
- that source/property is supported
- the game/profile policy requires it

Do not derive an entire OBS color preset from the Windows HDR boolean.

## Tests

Existing WindowsHDRController tests should be preserved and extended around declarative integration:

- unsupported display
- already desired state
- successful on/off
- verification timeout
- topology/primary target changes
- multiple active displays
- primary resolution failure
- Windows API failure
- shutdown during pending verification

Planning/runtime:

- HDR/SDR desired diff
- preserve/auto-policy produces no undesired write
- target unavailable -> pending/blocked
- stale binding -> replan
- manual override
- app transition HDR->SDR and SDR->HDR
- fallback desktop policy

## UX target

Normal game setup:

- "HDR"
- "SDR"
- "Conserver automatiquement" / equivalent final wording

No Windows API terminology.

Capture assistant should infer current HDR state and propose it, but the user chooses the intended policy rather than an implementation mechanism.

## Validation

Real Windows validation must include:

- current primary MSI display
- HDR ON/OFF round trip
- OBS running
- game/profile transition
- return to desktop/fallback
- no unexpected mutation of non-target displays
- verification after display topology changes when feasible
