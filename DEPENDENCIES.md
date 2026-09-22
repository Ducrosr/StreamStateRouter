# Python dependency policy

## Source of truth

`pyproject.toml` is the human-maintained source of direct dependency intent.

The Windows release toolchain also commits a resolver-generated artifact:

`constraints/windows-py312.txt`

It records the exact dependency closure validated for Windows x64 / Python 3.12. The file is generated, not hand-maintained.

## Regeneration

From a clean repository checkout on Windows with Python 3.12 available:

```powershell
.\scripts\refresh_python_constraints.ps1
```

The script creates a temporary isolated virtual environment, resolves `.[all]`, freezes the resulting package set, removes the local SSR editable package and pip itself, and rewrites the constraints file deterministically.

Dependency updates should therefore be performed by changing direct intent in `pyproject.toml` (or intentionally refreshing compatible transitive versions), running the script, reviewing the generated diff, and validating CI.

## Consumption

CI/release builds install the generated package set first, then install SSR with:

`--no-deps --no-build-isolation`

and run `pip check`.

This prevents a missing direct/transitive dependency from being silently resolved outside the reviewed artifact.

The general source installer remains intentionally less strict because SSR supports Python 3.12+ and the committed artifact targets the Windows/Python 3.12 release environment.

## Future standard lock format

PEP 751 `pylock.toml` is the preferred standards direction, but pip's lock/pylock support is still experimental in the current toolchain. Revisit migration when pip declares the lock creation/consumption workflow stable.
