from __future__ import annotations

import json
from pathlib import Path
import unittest

from stream_state_router.planning import build_migration_coverage_report
from stream_state_router.services.declarative_execution import (
    DECLARATIVE_EXECUTOR_KINDS,
)


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
        report = build_migration_coverage_report(
            self.sample(),
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()

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
            "empty",
        )
        self.assertEqual(
            profiles[("overlay", "DisabledOnly")]["disabled_actions"],
            1,
        )
        self.assertEqual(
            profiles[("layout", "Gameplay")]["classification"],
            "delegated",
        )
        profile_summary = report["summary"]["profiles"]
        self.assertEqual(profile_summary["managed_nonempty_total"], 5)
        self.assertEqual(profile_summary["declarative_coverage_percent"], 60.0)
        self.assertEqual(profile_summary["executable_percent"], 20.0)
        self.assertEqual(
            report["domains"]["overlay"]["actions"]["executable_percent"],
            100.0,
        )
        self.assertEqual(
            report["domains"]["audio"]["actions"]["declarative_coverage_percent"],
            100.0,
        )
        self.assertEqual(
            report["domains"]["capture"]["profiles"]["executable_percent"],
            0.0,
        )
        self.assertEqual(
            report["domains"]["layout"]["profiles"]["managed_nonempty_total"],
            0,
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

        report = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        self.assertEqual(len(report.actions), 1)
        self.assertEqual(
            report.actions[0].classification,
            "declarative_plannable",
        )
        self.assertEqual(report.actions[0].property_kinds, ("filter_enabled",))

    def test_profile_inherits_parent_migration_maturity_without_double_counting_actions(self):
        config = {
            "profiles": {
                "game": {
                    "Base": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture",
                                    "settings": {"mode": "legacy"},
                                    "overlay": False,
                                },
                            }
                        ]
                    },
                    "Child": {
                        "extends": "Base",
                        "actions": [],
                    },
                }
            }
        }

        mapped = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()
        profiles = {
            row["profile"]: row
            for row in mapped["profiles"]
            if row["domain"] == "game"
        }

        self.assertEqual(mapped["summary"]["actions"]["total"], 1)
        self.assertEqual(profiles["Base"]["classification"], "legacy_only")
        self.assertEqual(profiles["Child"]["classification"], "legacy_only")
        self.assertEqual(profiles["Child"]["enabled_actions"], 0)
        self.assertEqual(profiles["Child"]["inherited_actions"], 1)
        self.assertEqual(profiles["Child"]["effective_actions"], 1)

    def test_invalid_profile_inheritance_is_reported_without_crashing(self):
        for extends, expected in (
            ("Missing", "missing parent profile"),
            ("Self", "circular profile inheritance"),
        ):
            config = {
                "profiles": {
                    "game": {
                        "Self": {
                            "extends": extends,
                            "actions": [],
                        }
                    }
                }
            }

            mapped = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()
            row = mapped["profiles"][0]

            self.assertEqual(row["classification"], "invalid")
            self.assertTrue(
                any(expected in reason for reason in row["reasons"]),
                row,
            )

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

        mapped = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()
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


    def test_future_executor_kind_updates_coverage_without_report_rewrite(self):
        config = {
            "profiles": {
                "audio": {
                    "Volume": {
                        "actions": [
                            {
                                "type": "input_volume_db",
                                "params": {"input": "Music", "volume_db": -8.5},
                            }
                        ]
                    }
                }
            }
        }

        current = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )
        future = build_migration_coverage_report(
            config,
            executable_kinds={
                *DECLARATIVE_EXECUTOR_KINDS,
                "input_volume_db",
            },
        )

        self.assertEqual(
            current.actions[0].classification,
            "declarative_plannable",
        )
        self.assertEqual(
            future.actions[0].classification,
            "declarative_executable",
        )

    def test_human_report_is_secret_safe_and_surfaces_non_executable_actions(self):
        from stream_state_router.planning.migration_coverage import (
            render_migration_coverage_report,
        )

        secret = "super-secret-value"
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
                                    "overlay": False,
                                },
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {
                "Gameplay": {"scene": "In Game", "modules": {}},
            },
        }
        report = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        rendered = render_migration_coverage_report(report)

        self.assertIn("Couverture de migration déclarative", rendered)
        self.assertIn("legacy_only", rendered)
        self.assertIn("layout/Gameplay", rendered)
        self.assertNotIn(secret, rendered)

    def test_repository_default_config_has_only_empty_action_profiles_and_delegated_layouts(self):
        path = Path(__file__).resolve().parents[1] / "config" / "default.json"
        config = json.loads(path.read_text(encoding="utf-8"))

        mapped = build_migration_coverage_report(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()

        self.assertEqual(mapped["summary"]["actions"]["total"], 0)
        self.assertEqual(
            mapped["summary"]["profiles"]["managed_nonempty_total"],
            0,
        )
        self.assertGreater(
            mapped["summary"]["profiles"]["counts"]["empty"],
            0,
        )
        self.assertEqual(
            mapped["summary"]["profiles"]["counts"]["delegated"],
            len(config["layout_profiles"]),
        )
        serialized = json.dumps(mapped, ensure_ascii=False)
        self.assertNotIn(str(config["obs"].get("password") or "<no-password>"), serialized)
        self.assertNotIn(str(config["api"].get("token") or "<no-token>"), serialized)

    def test_empty_configuration_has_complete_zero_action_coverage(self):
        mapped = build_migration_coverage_report(
            {},
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        ).as_mapping()

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
