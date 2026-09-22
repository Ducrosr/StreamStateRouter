from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from stream_state_router.services.control_variables import ControlVariableStore


class ControlVariableStoreTests(unittest.TestCase):
    def test_persistent_store_merges_defaults_and_runtime_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control-state.json"
            first = ControlVariableStore({"Mood": "Happy"}, path=path)
            self.assertTrue(first.set("Mood", "Focused"))
            self.assertFalse(first.set("Mood", "Focused"))

            second = ControlVariableStore(
                {"Mood": "Happy", "Variant": "Default"},
                path=path,
            )

            self.assertEqual(
                second.snapshot(),
                {"Mood": "Focused", "Variant": "Default"},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["variables"]["Mood"], "Focused")

    def test_reserved_logical_state_names_are_rejected(self):
        store = ControlVariableStore()
        with self.assertRaisesRegex(ValueError, "réservée"):
            store.set("Game", "Fake")


if __name__ == "__main__":
    unittest.main()
