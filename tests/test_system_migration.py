from __future__ import annotations

import unittest

from stream_state_router.importers import (
    neutralize_referenced_test_layout_profiles,
    wire_windows_hdr_capture_profiles,
)


class SystemMigrationTests(unittest.TestCase):
    def test_wires_empty_hdr_and_default_capture_profiles(self):
        config = {
            "profiles": {
                "capture": {
                    "HDR": {"actions": []},
                    "Default": {"actions": []},
                }
            }
        }

        changed = wire_windows_hdr_capture_profiles(config)

        self.assertEqual(changed, ("HDR", "Default"))
        hdr = config["profiles"]["capture"]["HDR"]["actions"][0]
        sdr = config["profiles"]["capture"]["Default"]["actions"][0]
        self.assertEqual(
            hdr["params"],
            {"enabled": True, "display": "primary"},
        )
        self.assertEqual(
            sdr["params"],
            {"enabled": False, "display": "primary"},
        )

    def test_neutralizes_only_referenced_test_layout_profiles(self):
        config = {
            "router": {
                "fallback_state": {"LayoutProfile": "Vanilla"}
            },
            "rules": [
                {"enabled": True, "state": {"LayoutProfile": "Dofus"}},
                {"enabled": True, "state": {"LayoutProfile": "FPS"}},
            ],
            "layout_profiles": {
                "Vanilla": {
                    "scene": "[Module] TEST SSR",
                    "modules": {"A": {}},
                },
                "Dofus": {
                    "scene": "[Module] TEST SSR",
                    "modules": {"B": {}},
                },
                "FPS": {"scene": "", "modules": {}},
                "Test A": {
                    "scene": "[Module] TEST SSR",
                    "modules": {"C": {}},
                },
            },
        }

        changed = neutralize_referenced_test_layout_profiles(config)

        self.assertEqual(changed, ("Dofus", "Vanilla"))
        self.assertEqual(config["layout_profiles"]["Dofus"]["scene"], "")
        self.assertEqual(config["layout_profiles"]["Vanilla"]["scene"], "")
        self.assertEqual(
            config["layout_profiles"]["Test A"]["scene"],
            "[Module] TEST SSR",
        )

    def test_rejects_contradictory_existing_hdr_action(self):
        config = {
            "profiles": {
                "capture": {
                    "HDR": {
                        "actions": [
                            {
                                "type": "windows_hdr",
                                "params": {
                                    "enabled": False,
                                    "display": "primary",
                                },
                            }
                        ]
                    },
                    "Default": {"actions": []},
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "contradictoire"):
            wire_windows_hdr_capture_profiles(config)


if __name__ == "__main__":
    unittest.main()
