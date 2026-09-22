from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _normalize_requirement(line: str) -> str:
    value = line.strip()
    if not value or value.startswith("#"):
        return ""
    if "==" not in value:
        return value.casefold()
    name, version = value.split("==", 1)
    normalized_name = _NAME_SEPARATORS.sub("-", name.strip()).casefold()
    return f"{normalized_name}=={version.strip()}"


def normalize_snapshot(lines: Iterable[str]) -> tuple[str, ...]:
    values = (
        normalized
        for line in lines
        if (normalized := _normalize_requirement(line))
    )
    return tuple(sorted(values))


def snapshot_diff(
    expected_lines: Iterable[str],
    actual_lines: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected = Counter(normalize_snapshot(expected_lines))
    actual = Counter(normalize_snapshot(actual_lines))
    missing = tuple(sorted((expected - actual).elements()))
    extra = tuple(sorted((actual - expected).elements()))
    return missing, extra


def _pip_freeze() -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "freeze",
            "--exclude-editable",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the current Python environment matches a constraints snapshot."
    )
    parser.add_argument(
        "constraints",
        nargs="?",
        default="constraints/windows-release.txt",
        help="Tracked constraints snapshot.",
    )
    parser.add_argument(
        "--actual-file",
        help="Read the actual freeze from a file instead of invoking pip (tests/debugging).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    expected_path = Path(args.constraints)
    if not expected_path.is_file():
        print(f"Constraints snapshot not found: {expected_path}", file=sys.stderr)
        return 2

    expected_text = expected_path.read_text(encoding="utf-8")
    if args.actual_file:
        actual_text = Path(args.actual_file).read_text(encoding="utf-8")
    else:
        try:
            actual_text = _pip_freeze()
        except subprocess.CalledProcessError as exc:
            print(f"pip freeze failed with exit code {exc.returncode}", file=sys.stderr)
            return 2

    missing, extra = snapshot_diff(
        expected_text.splitlines(),
        actual_text.splitlines(),
    )
    if missing or extra:
        print("Python dependency snapshot drift detected.", file=sys.stderr)
        if missing:
            print("Missing from environment:", file=sys.stderr)
            for item in missing:
                print(f"  - {item}", file=sys.stderr)
        if extra:
            print("Unexpected in environment:", file=sys.stderr)
            for item in extra:
                print(f"  + {item}", file=sys.stderr)
        return 1

    print(
        f"Python dependency snapshot verified "
        f"({len(normalize_snapshot(expected_text.splitlines()))} packages)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
