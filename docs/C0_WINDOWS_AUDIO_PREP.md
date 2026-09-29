# C0 preparation — native Windows application volume/mute

Preparation only. Do not implement before the property/binding gates are stable.

## Current repository state

SSR already has `SoundVolumeViewAudioRouter`.

Its responsibility is deliberately narrow:

- route one application's default audio endpoint
- execute SoundVolumeView `/SetAppDefault`
- validate device/process/roles
- isolate the external executable behind an adapter

Do not discard this adapter prematurely.

It currently does NOT provide native per-application session volume/mute.

## Architectural direction from Astra

For C0:

- native Windows volume/mute is the preferred target
- Core Audio session APIs are the likely backend
- one process is not always one audio session
- Core Audio may require a dedicated COM thread
- that thread is an executor subordinate to SSR runtime, not a second decision engine
- arbitrary per-app output routing should not be promised as equivalent to volume/mute
- keep SoundVolumeView behind its narrow interface for routing unless a stable replacement is proven

## Scope split

### Native C0

Implement only:

- observe app/session volume
- set app/session volume
- verify app/session volume
- observe mute
- set mute
- verify mute

### Existing external routing

Keep:

- SoundVolumeView app default endpoint routing

Do not pretend routing has the same observation/rollback guarantees as native session volume/mute.

## Target semantics

The user should express:

- application
- desired volume
- desired mute

SSR must discover the matching live sessions.

Do not require the user to know:

- session GUIDs
- endpoint IDs
- COM interfaces
- process IDs that change every launch

## Session fan-out

A process may map to:

- no active session
- one session
- multiple sessions

The adapter must report this explicitly.

Candidate policy to review at the C0 gate:

- no session -> pending/unavailable, not false success
- one session -> straightforward
- multiple sessions owned by the same selected application -> apply/verify all matching sessions, with a composite result

Do not silently choose one arbitrary session.

## Threading

If COM initialization/apartment rules require a dedicated thread:

- runtime submits one logical operation
- adapter thread executes Windows API calls
- adapter returns observation/result
- adapter thread does not select profiles, resolve rules or retry business logic independently

Shutdown must stop/drain this executor predictably.

## Identity/binding preparation

A stable logical app-audio target should not be just PID.

Candidate evidence:

- normalized executable identity/path where available
- current process/session metadata
- endpoint context
- session grouping metadata if exposed reliably

A2/B0-style binding principles should be reused, but do not force OBS ResourceBinding directly onto Windows.

## Property model

After B0 stabilizes the contract, add specialized Windows property keys rather than overloading OBS inputs.

Candidate logical kinds:

- windows_app_volume
- windows_app_mute

Values:

- volume scalar/percentage with one normalized internal representation
- mute boolean

The actual names are implementation details until the C0 gate.

## Observation/result states

Must distinguish:

- known/converged
- unavailable because no session exists yet
- ambiguous/multiple sessions
- partial convergence
- target disappeared during operation
- verification failed/unknown

Do not report success just because a setter returned.

## Tests

Pure adapter tests:

- zero sessions
- one session
- multiple sessions
- session disappears between observation/write
- session appears after initial absence
- process PID changes but executable identity remains
- volume boundary validation
- mute boolean validation
- partial failure across multiple sessions
- readback mismatch
- COM initialization failure
- executor shutdown

Runtime integration:

- serialized command path
- no mutation from Qt/API/Stream Deck threads
- app launch after profile already active
- app exit while desired state remains
- reconnect/reconciliation style re-observation
- pause/override behavior as defined by runtime ownership

## UX target

Simple configuration:

- choose/detect application
- set volume
- set mute

Prefer capture:

"Utiliser le volume actuel pour ce jeu"

No manual session identifiers.

If no session is currently active:

"Réglage audio en attente du lancement de l'application."

## SoundVolumeView boundary

Keep app endpoint routing as a separately qualified operation.

Do not automatically retry external routing on an uncertain response unless its idempotence/result contract is explicitly established.

Do not block native C0 volume/mute on replacing SoundVolumeView routing.
