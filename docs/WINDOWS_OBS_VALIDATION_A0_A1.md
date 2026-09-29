# Windows / OBS validation — A0 closed / A1 checkpoint

This checklist is intentionally operational.

A0 is closed by Astra. A1 may be validated, but must remain unmerged and must not be
declared closed before the Astra A1 checkpoint.

## A0 current reference

Expected commit:

`6a288052001cd01cf7283251bc506d6219ea721c`

Before every real-OBS A0 run:

```powershell
Set-Location "C:\Streaming\StreamStateRouter\Source"
git fetch origin
git switch --detach 6a288052001cd01cf7283251bc506d6219ea721c
git rev-parse HEAD
```

Do not run an old executable by accident. Rebuild from that detached commit when needed.

## A0 visual scenarios

Use a disposable test scene/profile whenever possible.

For both a nested scene and a group:

- visible -> visible
- visible -> hidden
- hidden -> visible
- hidden -> hidden

Run with:

- fade
- move_fade

Expected:

- geometry correct
- requested visibility correct
- no `[SSR] Layout Fade` filter created on the composite
- no residual opacity
- no disappearance after OBS restart

Also test:

- input isolated + known visibility: fade still works
- source reused in multiple observed occurrences: direct fallback
- OBS reconnect during normal use
- repeated profile/layout changes

Do not intentionally crash a live production stream.

## Evidence to save

For every anomaly record:

- approximate local time
- SSR commit SHA
- OBS log file
- SSR log excerpt
- active Scene Collection
- scene/profile name
- source/group/nested-scene name
- before/after visibility
- whether `[SSR] Layout Fade` exists
- its enabled state and opacity if present
- `runtime.json`

The read-only script `scripts/inspect_layout_fade_state.py` can produce a JSON snapshot without mutating OBS.

## A1 real-OBS scenarios after implementation

### Kill/restart during temporary fade

1. Start a fade on a qualified isolated input.
2. Kill SSR after a durable obligation is present.
3. Preserve `runtime.json`.
4. Restart SSR.
5. Verify:
   - helper is observed, not recreated
   - opacity reaches neutral state
   - helper becomes disabled
   - readback confirms the final state
   - obligation disappears only afterward

### Helper absent during recovery

1. Create a pending obligation in a controlled test.
2. Remove the proven helper externally while SSR is stopped.
3. Restart SSR.

Required:

- no `CreateSourceFilter`
- no duplicate
- terminal result follows the ownership/recovery contract
- diagnostic is explicit

### Name collision

Create a user filter whose display name resembles or equals the old helper name.

Required:

- SSR must not adopt it from the name
- SSR must not mutate or delete it
- ambiguity is reported

### Response lost after creation

Use a fake/controlled transport test first; real OBS reproduction is optional if unsafe.

Required:

- observe before retry
- never create a duplicate

### Foreign Scene Collection

Pending helper obligation belongs to Collection A, but OBS is on Collection B.

Required:

- no mutation
- obligation retained
- clear diagnostic

### Source recreation under same name

Delete/recreate the source under the same display name.

Required:

- old provenance is not silently transferred
- no mutation until identity is proven according to A1's contract

### Shutdown with backlog

Quit SSR with an active transition / pending commands.

Required:

- worker stops cleanly or reports incomplete shutdown
- durable obligations survive
- no residual opacity after recovery


## A1 implementation validation workflow

PR:

`#152 — A1: durable fade helper ownership and safe recovery`

Branch:

`feat/a1-helper-recovery`

Base:

`6a288052001cd01cf7283251bc506d6219ea721c`

Before a real-OBS run, replace `<A1_GREEN_SHA>` below with the exact SHA whose
Tests + CodeQL are green. Never validate an older desktop executable by accident.

### Sync exact SHA

```powershell
Set-Location "C:\Streaming\StreamStateRouter\Source"

git fetch origin
git switch --detach <A1_GREEN_SHA>
git rev-parse HEAD
```

Expected output must be exactly `<A1_GREEN_SHA>`.

### Rebuild executable

```powershell
Set-Location "C:\Streaming\StreamStateRouter\Source"

Remove-Item ".\build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item ".\dist"  -Recurse -Force -ErrorAction SilentlyContinue

.\.venv\Scripts\python.exe -m PyInstaller `
    --clean `
    --noconfirm `
    .\StreamStateRouter.spec

Copy-Item `
    ".\dist\StreamStateRouter.exe" `
    "$env:USERPROFILE\Desktop\StreamStateRouter-A1.exe" `
    -Force

Get-Item "$env:USERPROFILE\Desktop\StreamStateRouter-A1.exe" |
    Select-Object FullName, Length, LastWriteTime
```

Close any older SSR instance before launching the A1 build.

### Files to preserve during crash/restart scenarios

User-data directory:

`%APPDATA%\StreamStateRouter`

Relevant A1 evidence:

- `runtime.json`
- `helper-manifest.json`
- latest SSR logs
- latest OBS log
- exact Git SHA
- approximate local time of the event

Do not edit either JSON file during a scenario.

### Expected A1 helper lifecycle

Normal owned input helper:

1. manifest identity exists;
2. cleanup obligation is durable before temporary mutation;
3. filter name is `[SSR] Layout Fade::<helper_id>`;
4. temporary opacity may change during transition;
5. cleanup returns opacity to `1`;
6. readback verifies it;
7. helper is disabled;
8. readback verifies disabled;
9. only then is the cleanup obligation removed.

A helper may remain present in OBS for safe reuse. A1 does not automatically delete it.

### Fast non-destructive smoke test

Use one disposable isolated input.

1. Apply a layout using `fade`.
2. Apply a second layout using `move_fade`.
3. Verify requested geometry and visibility.
4. Inspect the input filters after each transition.

Expected after settling:

- at most one owned generated fade helper for that qualified source;
- opacity = 1;
- enabled = false;
- no pending `layout_fade` obligation in `runtime.json`.

### Composite regression check

For one nested scene and one group, run both transitions.

Expected:

- geometry/visibility follow target;
- no generated fade helper on the composite;
- no source-level opacity mutation;
- no persistent invisibility.

### Crash test safety

Use a disposable collection/profile only.

Never kill SSR during a production stream for validation.

For the controlled kill/restart test:

1. verify a live `layout_fade` obligation exists;
2. kill SSR;
3. do not touch OBS/filter state;
4. copy `runtime.json` and `helper-manifest.json` as evidence;
5. restart the same A1 executable;
6. let recovery settle;
7. verify opacity = 1 and helper disabled;
8. verify the obligation disappears only after recovery.

### Stop conditions

Stop the test and preserve evidence if:

- a composite receives a generated helper;
- more than one generated helper appears for one qualified input;
- recovery emits a new Create;
- opacity remains non-neutral after recovery;
- source UUID changed but SSR still mutates the old ownership target;
- collection changes and cleanup still mutates the previous collection;
- `runtime.json` or `helper-manifest.json` becomes unreadable.

Do not manually “repair” the state before evidence is copied.
