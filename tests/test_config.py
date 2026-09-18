from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stream_state_router.services.config import (
    build_ruleset,
    load_config,
    migrate_config,
    validate_config,
)


class ConfigTests(unittest.TestCase):
    def sample(self):
        return {
            "schema_version": 4,
            "router": {
                "poll_ms": 50,
                "debounce_ms": 150,
                "fallback_debounce_ms": 350,
                "fallback_state": {"Game": "Vanilla", "LayoutProfile": "Vanilla"},
            },
            "obs": {"enabled": False, "host": "127.0.0.1", "port": 4455},
            "rules": [
                {"name": "Launcher", "behavior": "ignore", "exe": "launcher.exe"},
                {
                    "name": "Game",
                    "behavior": "match",
                    "exe": "game.exe",
                    "state": {"Game": "Game", "LayoutProfile": "Vanilla"},
                },
            ],
            "layout_profiles": {"Vanilla": {"scene": "", "modules": {}}},
            "profiles": {
                "game": {"Vanilla": {"actions": []}, "Game": {"actions": []}},
                "overlay": {"Vanilla": {"actions": []}},
                "capture": {"Default": {"actions": []}},
                "audio": {"Default": {"actions": []}},
            },
        }

    def test_valid_config_passes(self):
        self.assertEqual(validate_config(self.sample()), [])

    def test_invalid_duplicate_rule_is_reported(self):
        data = self.sample()
        data["rules"].append({"name": "Game", "behavior": "ignore", "exe": "x.exe"})
        self.assertTrue(any("dupliqué" in item for item in validate_config(data)))

    def test_load_and_build_rules(self):
        data = self.sample()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_config(path)
        rules, poll, debounce, fallback = build_ruleset(loaded)
        self.assertEqual((poll, debounce, fallback), (50, 150, 350))
        self.assertEqual(len(rules.rules), 2)

    def test_schema_v1_is_migrated_with_layout_profiles(self):
        data = self.sample()
        data["schema_version"] = 1
        data.pop("layout_profiles")
        data["router"]["fallback_state"].pop("LayoutProfile", None)
        data["rules"][1]["state"].pop("LayoutProfile", None)
        data["rules"][1]["state"]["OverlayProfile"] = "Vanilla"

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(migrated["router"]["fallback_state"]["LayoutProfile"], "Vanilla")
        self.assertIn("Vanilla", migrated["layout_profiles"])
        self.assertEqual(validate_config(migrated), [])

    def test_schema_v3_splits_prefix_grouped_sources_into_distinct_modules(self):
        data = self.sample()
        data["schema_version"] = 3
        data["layout_profiles"]["Vanilla"] = {
            "scene": "In Game",
            "coordinate_mode": "absolute",
            "modules": {
                "Global": {
                    "display_name": "Global",
                    "container": "In Game",
                    "visible": True,
                    "managed": True,
                    "locked": False,
                    "lock_aspect": True,
                    "anchor": "top_left",
                    "anchor_mode": "relative",
                    "base_bounds": {"x": 100, "y": 100, "width": 500, "height": 300},
                    "geometry": {"x": 100, "y": 100, "width": 500, "height": 300},
                    "elements": [
                        {
                            "source": "[Global] Date",
                            "element": "Date",
                            "container": "In Game",
                            "enabled": True,
                            "included": True,
                            "transform": {
                                "positionX": 100, "positionY": 100, "width": 200, "height": 50,
                                "scaleX": 1, "scaleY": 1, "alignment": 5, "rotation": 0,
                            },
                        },
                        {
                            "source": "[Global] Signature",
                            "element": "Signature",
                            "container": "In Game",
                            "enabled": True,
                            "included": True,
                            "transform": {
                                "positionX": 300, "positionY": 100, "width": 200, "height": 50,
                                "scaleX": 1, "scaleY": 1, "alignment": 5, "rotation": 0,
                            },
                        },
                    ],
                }
            },
            "transition": {"mode": "instant", "duration_ms": 0, "steps": 8},
            "conditions": {},
        }

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 4)
        modules = migrated["layout_profiles"]["Vanilla"]["modules"]
        self.assertEqual(set(modules), {"[Global] Date", "[Global] Signature"})
        self.assertEqual(modules["[Global] Date"]["module_type"], "Global")
        self.assertEqual(modules["[Global] Date"]["display_name"], "Date")
        self.assertEqual(len(modules["[Global] Date"]["elements"]), 1)



if __name__ == "__main__":
    unittest.main()
