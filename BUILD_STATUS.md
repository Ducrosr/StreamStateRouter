# Build / validation status — 2.1.0 candidate

## Consolidation

- Target: `main`.
- Release candidate PR: **#119** — `release/2.1.0-candidate`.
- Validated source stack:
  - #11 — Lot 2 guarded declarative executor;
  - #97 — post-Astra hardening/integration;
  - #111 — Lot 3 `input_volume_db`;
  - #113 — full non-publishing release smoke;
  - #116 — reversible real OBS volume lab.
- The Lot 3 source head is 276 commits ahead of `main` and 0 behind.

## Declarative execution

Executable properties:

- Scene Item visibility;
- input mute;
- input volume in dB.

The path is opt-in and serialized on `SSR-Router`. Preparation/execution use a
single-use ticket and validate session, Scene Collection, catalog epoch,
configuration revision, runtime generation, logical target and physical identity.

The final local runtime admission is atomic with the single OBS `Set*` request.
Writes are followed by targeted acknowledgement and a final convergence sweep.
No mutation retry or speculative rollback is performed.

## Volume model

`input_volume_db`:

- requires an explicit finite `int|float` target;
- rejects booleans, strings, NaN and infinities;
- writes only `[-100,+26] dB`;
- observes finite physical values without silent clamp/coercion;
- uses shared `INPUT_VOLUME_DB_ABS_TOLERANCE = 1e-4 dB`.

## Validated baseline before consolidation

Lot 3 head `cc54631f50c7b1d473799b8c9075c007e0432fdd`:

- **383 tests passed, 1 skipped**;
- Ruff: PASS;
- config smoke: PASS;
- declarative coverage text/JSON: PASS;
- PowerShell syntax: PASS;
- Stream Deck typecheck/build/validation: PASS;
- CodeQL Python: PASS;
- CodeQL JavaScript/TypeScript: PASS;
- CodeQL Actions: PASS.

## Packaging validation

Release smoke #113 validated:

- frozen Python dependency closure;
- `pip check`;
- portable PyInstaller EXE;
- portable EXE `--check-config`;
- ZIP;
- Stream Deck package;
- Inno Setup installer;
- silent install;
- installed EXE `--check-config`;
- silent uninstall;
- build provenance;
- artifact verification;
- SHA-256 generation and strict re-hash.

Artifact:

`release-smoke-6d8f84578928b5e2cf0568e640f29e088d6ddde7`

Digest:

`sha256:b0cc9133aaaae92ea66600edc3629834f8e29d60e6e3308bcf2156fbf8741d3d`

## Real OBS 32.2.2 validation

The real-machine Lot 3 lab confirmed:

- Input UUID discovery;
- `GetInputVolume`;
- guarded declarative `SetInputVolume`;
- targeted readback;
- final convergence;
- exact restoration through the original `inputVolumeMul`.

Measured sequence on `SSR Executor Mic`:

- initial: `0.000000 dB / mul=1.000000000`;
- temporary target: `-1.000000 dB`;
- converged readback: `-1.000000 dB`;
- restored: `0.000000 dB / mul=1.000000000`.

## 2.1.0 release-candidate changes

The consolidation branch additionally:

- bumps SSR to `2.1.0`;
- aligns Stream Deck source metadata to `2.1.0.0`;
- derives the packaged Stream Deck version from SSR at build time;
- adds a version-override package smoke test;
- guards Python/npm/manifest version consistency in unit tests;
- refreshes README, release notes and this status document.

## Remaining gates

Before a tag or GitHub Release:

1. complete Tests + CodeQL on PR #119;
2. run the full non-publishing Release workflow from the exact #119 head;
3. inspect the final diff to `main`;
4. only then decide whether to merge #119 and tag `v2.1.0`.

No release has been published.
