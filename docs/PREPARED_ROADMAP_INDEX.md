# StreamStateRouter — prepared roadmap execution index

This branch is planning/tooling only.

Frozen A0 reference:

`6a288052001cd01cf7283251bc506d6219ea721c`

Do not merge preparation material into the A0 validation PR merely to make it visible.

## Status legend

- FROZEN: implementation exists; awaiting architecture gate
- READY-PREP: implementation plan is prepared, but gate/dependencies are not cleared
- LATER-PREP: architecture/UX notes prepared for a later gate

## Current status

| Lot | Status | Gate before implementation | Prepared material |
|---|---|---|---|
| A0 fade safety | FROZEN | Astra final approval | PR #148 head itself |
| A1 helper ownership/recovery | READY-PREP | A0 approved | A1 helper/recovery docs + Sol prompt + validation |
| A2 OBS bindings/repair | READY-PREP | A1 approved | A2 binding prep |
| B0 typed filter slice | READY-PREP | A2 approved | B0 typed filter prep |
| B1 guided capture | READY-PREP | B0 stable | B1 guided capture + UX inventory |
| B2 profile composition | READY-PREP | B0 stable; coordinate with B1 | B2 composition prep |
| C0 Windows volume/mute | LATER-PREP | B0 contract; B1 integration | C0 audio prep |
| C1 declarative HDR | LATER-PREP | B0 contract; B1 integration | C1 HDR prep |
| D0 Try/Revert + surrender | LATER-PREP | B0; adapters to be trialled | D0 prep |
| E0 assisted migration | LATER-PREP | B1/B2 + available capabilities | E0 prep |
| E1 diagnostics/replay | LATER-PREP | B0 contracts stable | E1 prep |
| F0 API/Stream Deck intents | LATER-PREP | B1/B2 + result contract | F0 prep |
| G0 bounded media | LATER-PREP | B0 + F0 | G0 prep |

## Required Astra checkpoints

### Gate A0

Review:

- all opacity paths
- current OBS kind resolution
- completeness fail-safe
- source-vs-occurrence scope
- full visibility matrix

Required approval text:

`A0 EST FERMÉ. SOL PEUT COMMENCER A1.`

### Gate A1

Review:

- helper provenance
- persistent manifest
- write-ahead
- no-create cleanup
- idempotence
- v2 migration
- import/capture helper exclusion

After approval, A2 can begin.

### Gate A2 + B0

Review:

- identity/binding rules
- rename vs recreate
- collection assumptions
- filter identity ambiguity
- first PropertyDescriptor/capability slice
- read/write/verify contract

After approval, B1/B2 and independent C0/C1 work can expand.

### Gate B1 + B2

Review:

- inference boundaries
- composition precedence
- deterministic conflicts
- difference-to-parent
- UI/service separation
- user-input reduction

### Gate C0 + C1

Review:

- Windows identity
- COM/threading
- external availability
- result guarantees
- HDR target topology

### Gate D0

Review:

- compensation preconditions
- surrender/reacquire ownership
- external-change detection
- crash recovery
- no fake distributed transaction

### New-format gates E0/E1/F0/G0

Review first implementation of each semantic boundary, then allow homogeneous expansion.

## User-complexity budget

Target simple-mode decisions:

| Workflow | Target |
|---|---:|
| Add a normal new game/setup | 1 confirmation |
| Update a unique existing setup | 1 confirmation |
| Several matching rules | 1 rule choice + 1 confirmation |
| Capture current setup | 1 confirmation |
| Proven rename repair | 0 |
| New ambiguous replacement | 1 selection |
| Configure Stream Deck target | 1 selection from catalog |
| Daily Stream Deck use | 1 press |
| A1 helper recovery | 0 |
| Windows audio volume/mute | app inferred/selected + desired result |
| HDR policy | HDR / SDR / preserve-auto policy |
| Try/Revert | Try, then Keep or Revert |
| Temporary surrender | one explicit surrender/reacquire intention |

Do not measure simplicity by hidden widget count only. Count decisions the user must understand.

## Parallelization rules

Do not parallelize:

- A1 with another recovery-journal redesign
- B0 with independent incompatible property frameworks
- B1 and E0 with separate draft models
- B2 and E0 with competing composition semantics

After A2/B0 stabilize:

- C0 and C1 can progress independently
- E1 can use the stable contracts for replay
- F0 UI/plugin can evolve from stable catalogs/results
- test fixtures and real-OBS validation can progress continuously

## Existing code that should be reused

- serialized runtime mutation path
- OBS catalog
- PropertyKey / DesiredState / ObservedValue
- declarative planner
- request IDs / request status
- CurrentStateCaptureDraft path
- conservative ASC importer
- SoundVolumeView narrow routing adapter
- WindowsHDRController
- layout preview/undo/session guards
- runtime diagnostics/provenance

## Things not to build

- second business engine in Qt/TypeScript
- general macro DSL
- arbitrary OBS JSON property writes
- plugin framework without a concrete need
- universal distributed transaction
- full media/animation editor
- DWM reimplementation
- auto-adoption of resources by similar names
- automatic helper deletion in A1
