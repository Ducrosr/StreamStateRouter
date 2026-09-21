from __future__ import annotations

import json
import unittest

from stream_state_router.planning import build_migration_coverage_report


class MigrationCoverageTests(unittest.TestCase):
    def sample(self):
        return {
            "profiles": {
                "game": {
                    "Scenes": {
                        "actions": [
                            {
                                "type": "set_program_scene",
                                "params": {"scene": "Gameplay"},
                            }
                        ]
                    }
                },
                "overlay": {
                    "Visibility": {
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Chat",
                                    "enabled": True,
                                },
                            }
                        ]
                    },
                    "DisabledOnly": {
                        "actions": [
                            {
                                "type": "future_unknown_action",
                                "enabled": False,
                                "params": {"secret": "must-not-appear"},
                            }
                        ]
                    },
                },
                "capture": {
                    "LegacySettings": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture",
                                    "settings": {"url": "secret-value"},
                                    "overlay": False,
                                },
                            }
                        ]
                    },
                    "Invalid": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {"input": "Capture"},
                            }
                        ]
                    },
                },
                "audio": {
                    "Mix": {
                        "actions": [
                            {
                                "type": "input_mute",
                                "params": {"input": "Mic", "muted": True},
                            },
                            {
                                "type": "input_volume_db",
                                "params": {"input": "Music", "volume_db": -10.0},
                            },
                        ]
                    }
                },
            },
            "layout_profiles": {
                "Gameplay": {"scene": "In Game", "modules": {}},
            },
        }

    def test_classifies_actions_and_profiles_by_current_maturity(self):
        report = build_migration_coverage_report(self.sample()).as_mapping()

        actions = report["summary"]["actions"]
        self.assertEqual(actions["total"], 6)
        self.assertEqual(actions["counts"]["declarative_executable"], 2)
        self.assertEqual(actions["counts"]["declarative_plannable"], 2)
        self.assertEqual(actions["counts"]["legacy_only"], 1)
        self.assertEqual(actions["counts"]["invalid"], 1)

        profiles = {
            (row["domain"], row["profile"]): row
            for row in report["profiles"]
        }
        self.assertEqual(
            profiles[("overlay", "Visibility")]["classification"],
            "declarative_executable",
        )
        self.assertEqual(
            profiles[("audio", "Mix")]["classification"],
            "declarative_plannable",
        )
        self.assertEqual(
            profiles[("capture", "LegacySettings")]["classification"],
            "legacy_only",
        )
        self.assertEqual(
            profiles[("capture", "Invalid")]["classification"],
            "invalid",
        )
        self.assertEqual(
            profiles[("overlay", "DisabledOnly")]["classification"],
            "declarative_executable",
        )
        self.assertEqual(
            profiles[("overlay", "DisabledOnly")]["disabled_actions"],
            1,
        )
        self.assertEqual(
            profiles[("layout", "Gameplay")]["classification"],
            "delegated",
        )

    def test_filter_enable_is_plannable_but_not_executor_enabled(self):
        config = {
            "profiles": {
                "game": {
                    "Filtered": {
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Camera",
                                    "filter": "Blur",
                                    "enabled": True,
                                },
                            }
                        ]
                    }
                }
            }
        }

        report = build_migration_coverage_report(config)

        self.assertEqual(len(report.actions), 1)
        self.assertEqual(
            report.actions[0].classification,
            "declarative_plannable",
        )
        self.assertEqual(report.actions[0].property_kinds, ("filter_enabled",))

    def test_safe_input_setting_report_never_exposes_setting_values(self):
        secret = "https://example.invalid/?token=super-secret"
        config = {
            "profiles": {
                "capture": {
                    "Browser": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Browser",
                                    "settings": {"url": secret},
                                    "overlay": True,
                                },
                            }
                        ]
                    }
                }
            }
        }

        mapped = build_migration_coverage_report(config).as_mapping()
        rendered = json.dumps(mapped, ensure_ascii=False)

        self.assertNotIn(secret, rendered)
        self.assertEqual(
            mapped["actions"][0]["classification"],
            "declarative_plannable",
        )
        self.assertEqual(
            mapped["actions"][0]["property_kinds"],
            ["input_setting"],
        )

    def test_empty_configuration_has_complete_zero_action_coverage(self):
        mapped = build_migration_coverage_report({}).as_mapping()

        self.assertEqual(mapped["summary"]["actions"]["total"], 0)
        self.assertEqual(
            mapped["summary"]["actions"]["declarative_coverage_percent"],
            100.0,
        )
        self.assertEqual(
            mapped["summary"]["actions"]["executable_percent"],
            100.0,
        )
        self.assertEqual(mapped["profiles"], [])
        self.assertEqual(mapped["actions"], [])


if __name__ == "__main__":
    unittest.main()
