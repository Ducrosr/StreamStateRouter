# Windows / OBS validation — A0 freeze and A1 gate

This checklist is intentionally operational. It does not authorize A1 before A0 approval.

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
