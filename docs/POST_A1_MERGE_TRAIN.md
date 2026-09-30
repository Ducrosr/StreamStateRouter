# Post-A1 merge train

This document records the prepared feature stack while A1 / PR #152 is frozen
for Astra review. It is an integration plan, not an authorization to merge.

## Frozen gate

A1 remains isolated in PR #152. Do not rebase, merge, retarget or edit that
branch while its exact reviewed SHA is frozen.

The post-A1 branches below were developed independently and must be reconciled
only after A1 is approved.

## Linear feature spine

The main prepared feature spine is currently linear:

1. `feat/system-controls-filter-settings`
   - head: `4c756d24d50136de9fe146091949c2e1e8ddce09`
   - Windows per-app audio routing through the isolated SoundVolumeView adapter;
   - native Win32 DisplayConfig HDR control;
   - OBS filter settings actions;
   - OBS collection / Advanced Scene Switcher import foundations.

2. `feat/guided-current-state-capture`
   - head: `de15a26f2626779a51e174661b517dc4f8a6fd2c`
   - 43 commits ahead of the system-controls branch, 0 behind;
   - guided capture of the current OBS/logical state.

3. `feat/transactional-apply-recovery`
   - head: `3835a283bddde4850a72c8a1d926b5384bd741d0`
   - 38 commits ahead of guided capture, 0 behind;
   - transactional save/apply recovery and configuration insight hardening.

4. `feat/obs-drift-detection`
   - head: `be709bc7cfac259fd13d30c3c69123ca315e5dc5`
   - 63 commits ahead of transactional apply, 0 behind;
   - OBS drift detection plus the intervening UX/config-history/capture ownership
     work already present in its ancestry.

5. `feat/manual-operation-rollback`
   - head: `e281746ca05b36ae5ae8959e8c5484fa81afb72e`
   - 6 commits ahead of OBS drift, 0 behind;
   - OBS command feedback and manual-operation rollback.

6. `integration/sol-pre-astra-next`
   - draft PR #153;
   - based directly on `feat/manual-operation-rollback`;
   - adds read-only system readiness diagnostics and small manual-override
     observability/API coverage;
   - preparation only, not a production merge target yet.

## Diverged branches

Some older feature branches have diverged from the linear spine. Do not merge
those branch heads wholesale.

Examples:

- `feat/temporary-override-release`: its remaining useful follow-ups
  (timed-override remaining duration and API automatic-routing reset coverage)
  have been ported selectively into PR #153.
- `feat/read-only-system-check` / `feat/system-check-cli`: their useful
  read-only diagnostic concepts have been consolidated in PR #153 instead of
  merging either divergent branch.
- `feat/config-change-review`: the current spine already contains the newer
  `build_config_change_review` implementation; the older branch should not be
  replayed mechanically.
- `feat/config-edit-lock`: this represents an older edit-lock model and must
  not be merged blindly over the newer Mode édition semantics.

## Integration procedure after A1 approval

After Astra approves A1:

1. establish the approved A1 merge/base SHA;
2. create a fresh integration branch from that approved base;
3. replay/rebase the linear feature spine in the order above;
4. resolve A1 conflicts by preserving the approved A1 invariants, especially
   OBS mutation serialization, durable cleanup and Scene Collection identity;
5. run the full Windows Python/Ruff/Stream Deck/CodeQL gates after each logical
   layer rather than only at the end;
6. run real Windows/OBS validation for system controls, guided capture,
   transactional apply, drift detection and manual rollback;
7. only then retarget or replace the old draft feature PRs.

Do not use branch age or commit count as proof of correctness. The exact
post-rebase SHAs and their validation results become the new review targets.

## Real-machine validation still required

Prepared automated coverage does not replace hardware/application validation.
The post-A1 integration should explicitly exercise:

- SoundVolumeView routing against the intended Windows/RØDECaster endpoints;
- HDR ON/OFF readback on the real primary display;
- imported filter settings against representative OBS filters;
- Advanced Scene Switcher migration report on the user's real collection;
- guided current-state capture without ownership collisions;
- transactional apply failure/rollback;
- OBS drift detection and repair;
- manual operation rollback;
- `--system-check` / `--system-check-json` as strictly read-only commands.

Until those gates are complete, all post-A1 work remains preparatory.

## Precomputed overlap with frozen A1

This overlap was computed without rebasing or modifying PR #152. Frozen A1 head:
`0a6f9c9a2ac4725dbbb4318ff5f37d9e97b5fbf1`. Prepared PR #153 head at the
time of the check: `1c6ddedfa5964d42b9cb4f97db85d90523f389a3`.

Only four files are modified by both A1 and the current #153 delta:

| File | A1 invariant to preserve | #153 concern |
| --- | --- | --- |
| `main.py` | Fail closed on unreadable recovery marker; preserve recovered cleanup backlog in headless mode. | Diagnostic CLI modes must exit before single-instance/runtime startup. |
| `stream_state_router/ui/main_window.py` | Do not finalize the recovery marker while runtime shutdown is non-quiescent; checkpoint pending cleanup instead. | Shared read-only host capability probe. |
| `tests/test_main_cli.py` | Recovery-marker startup/headless backlog regressions remain authoritative. | System-check early-exit and invalid-config diagnostics. |
| `tests/test_runtime.py` | Reconnect cleanup retry and shutdown backlog preservation remain authoritative. | Manual-override observability tests. |

Post-A1 integration rule: resolve these overlaps by preserving the approved A1
lifecycle/recovery behavior first, then replay the #153 diagnostic/observability
changes around it. No conflict resolution may weaken A1's worker-serialization,
cleanup durability, Scene Collection qualification, or fail-closed behavior.

The remaining #153 files do not currently overlap the A1 delta. This is only a
conflict map, not proof that a later rebase will be conflict-free.

