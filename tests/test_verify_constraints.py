from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_constraints.py"
    spec = importlib.util.spec_from_file_location("verify_constraints", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load constraints verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_constraints = _load_module()


class VerifyConstraintsTests(unittest.TestCase):
    def test_exact_snapshot_matches_ignoring_comments_order_and_name_separators(self):
        expected = [
            "# generated",
            "PySide6_Addons==6.11.2",
            "",
            "obsws-python==1.8.0",
        ]
        actual = [
            "obsws_python==1.8.0",
            "pyside6-addons==6.11.2",
        ]

        self.assertEqual(
            verify_constraints.snapshot_diff(expected, actual),
            ((), ()),
        )

    def test_missing_package_is_reported(self):
        missing, extra = verify_constraints.snapshot_diff(
            ["one==1.0", "two==2.0"],
            ["one==1.0"],
        )

        self.assertEqual(missing, ("two==2.0",))
        self.assertEqual(extra, ())

    def test_extra_package_is_reported(self):
        missing, extra = verify_constraints.snapshot_diff(
            ["one==1.0"],
            ["one==1.0", "two==2.0"],
        )

        self.assertEqual(missing, ())
        self.assertEqual(extra, ("two==2.0",))

    def test_version_change_is_missing_plus_extra(self):
        missing, extra = verify_constraints.snapshot_diff(
            ["one==1.0"],
            ["one==1.1"],
        )

        self.assertEqual(missing, ("one==1.0",))
        self.assertEqual(extra, ("one==1.1",))


if __name__ == "__main__":
    unittest.main()
