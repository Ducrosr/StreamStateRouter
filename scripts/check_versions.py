from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_app_version() -> str:
    init_path = ROOT / "stream_state_router" / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"\s*$',
        init_path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("Unable to resolve stream_state_router.__version__")
    return match.group(1)


def main() -> int:
    version = _read_app_version()

    package = json.loads(
        (ROOT / "streamdeck-plugin" / "package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "streamdeck-plugin" / "package-lock.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (
            ROOT
            / "streamdeck-plugin"
            / "com.remyducros.streamstaterouter.sdPlugin"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    expected_manifest = f"{version}.0"
    errors: list[str] = []

    if package.get("version") != version:
        errors.append(
            f"package.json version={package.get('version')!r}, expected {version!r}"
        )
    if lock.get("version") != version:
        errors.append(
            f"package-lock.json version={lock.get('version')!r}, expected {version!r}"
        )
    root_package = lock.get("packages", {}).get("", {})
    if root_package.get("version") != version:
        errors.append(
            "package-lock.json root package version="
            f"{root_package.get('version')!r}, expected {version!r}"
        )
    if manifest.get("Version") != expected_manifest:
        errors.append(
            f"Stream Deck manifest Version={manifest.get('Version')!r}, "
            f"expected {expected_manifest!r}"
        )

    if errors:
        print("Version metadata mismatch:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        "Version metadata aligned: "
        f"SSR/package/lock={version}, manifest={expected_manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
