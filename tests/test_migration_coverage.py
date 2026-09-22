from __future__ import annotations

import json
from pathlib import Path
import unittest

from stream_state_router.planning.migration_coverage import (
    DECLARATIVE_EXECUTABLE,
    DECLARATIVE_INTENT_ONLY,
    DECLARATIVE_PLANNABLE,
    DELEGATED,
    DISABLED,
    LEGACY_ONLY,
    build_migration_coverage,
    render_migration_coverage,
)
from stream_state_router.services.declarative_execution import (
    DECLARATIVE_EXECUTOR_KINDS,
)


class MigrationCoverageTests(unittest.TestCase):
    def test_classifies_current_executor_and_plannable_actions(self):
        config = {
            "profiles": {
                "game": {
                    "Main": {
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Chat",
                                    "enabled": True,
                                },
                            },
                            {
                                "type": "input_mute",
                                "params": {"input": "Mic", "muted": True},
                            },
                            {
                                "type": "input_volume_db",
                                "params": {"input": "Music", "volume_db": -12.0},
                            },
                            {
                                "type": "set_program_scene",
                                "params": {"scene": "In Game"},
                            },
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Avatar",
                                    "filter": "Glow",
                                    "enabled": True,
                                },
                            },
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        profile = report.profiles[0]
        self.assertEqual(profile.classification, DECLARATIVE_PLANNABLE)
        self.assertEqual(
            [item.classification for item in profile.actions],
            [
                DECLARATIVE_EXECUTABLE,
                DECLARATIVE_EXECUTABLE,
                DECLARATIVE_PLANNABLE,
                DECLARATIVE_PLANNABLE,
                DECLARATIVE_PLANNABLE,
            ],
        )
        summary = report.summary()["actions"]
        self.assertEqual(summary["active"], 5)
        self.assertEqual(summary["represented_percent"], 100.0)
        self.assertEqual(summary["executable_percent"], 40.0)

    def test_inherited_actions_are_included_in_effective_profile_coverage(self):
        config = {
            "profiles": {
                "audio": {
                    "Base": {
                        "actions": [
                            {
                                "type": "input_volume_db",
                                "params": {"input": "Music", "volume_db": -10.0},
                            }
                        ]
                    },
                    "Child": {
                        "extends": "Base",
                        "actions": [
                            {
                                "type": "input_mute",
                                "params": {"input": "Music", "muted": False},
                            }
                        ],
                    },
                }
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )
        child = next(
            item
            for item in report.profiles
            if item.domain == "audio" and item.profile == "Child"
        )

        self.assertEqual(child.classification, DECLARATIVE_PLANNABLE)
        self.assertEqual(len(child.actions), 2)
        self.assertEqual(
            [item.action_type for item in child.actions],
            ["input_volume_db", "input_mute"],
        )

    def test_overlay_false_input_settings_remain_legacy_only(self):
        config = {
            "profiles": {
                "capture": {
                    "Legacy": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture",
                                    "overlay": False,
                                    "settings": {"token": "SECRET"},
                                },
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )
        profile = report.profiles[0]

        self.assertEqual(profile.classification, LEGACY_ONLY)
        self.assertEqual(profile.actions[0].classification, LEGACY_ONLY)
        serialized = json.dumps(report.as_mapping(), ensure_ascii=False)
        rendered = render_migration_coverage(report)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("SECRET", rendered)

    def test_empty_input_settings_are_intent_only(self):
        config = {
            "profiles": {
                "capture": {
                    "Empty": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture",
                                    "settings": {},
                                },
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        self.assertEqual(
            report.profiles[0].classification,
            DECLARATIVE_INTENT_ONLY,
        )

    def test_disabled_actions_do_not_reduce_active_coverage(self):
        config = {
            "profiles": {
                "overlay": {
                    "Disabled": {
                        "actions": [
                            {
                                "type": "set_program_scene",
                                "enabled": False,
                                "params": {"scene": "Other"},
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        self.assertEqual(report.profiles[0].classification, DECLARATIVE_EXECUTABLE)
        self.assertEqual(report.profiles[0].actions[0].classification, DISABLED)
        self.assertEqual(report.summary()["actions"]["active"], 0)
        self.assertIsNone(report.summary()["actions"]["executable_percent"])
        self.assertIsNone(report.summary()["actions"]["represented_percent"])
        self.assertIn(
            "Aucune action OBS active à mesurer",
            render_migration_coverage(report),
        )

    def test_layout_profiles_are_delegated_not_failed(self):
        config = {
            "profiles": {},
            "layout_profiles": {
                "Gameplay": {"scene": "In Game", "modules": {}},
                "Chat": {"scene": "Just Chatting", "modules": {}},
            },
        }

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        self.assertEqual(
            [item.classification for item in report.profiles],
            [DELEGATED, DELEGATED],
        )
        self.assertEqual(
            report.summary()["profiles"]["counts"],
            {DELEGATED: 2},
        )

    def test_repository_default_config_produces_coverage_report(self):
        path = Path(__file__).resolve().parents[1] / "config" / "default.json"
        config = json.loads(path.read_text(encoding="utf-8"))

        report = build_migration_coverage(
            config,
            executable_kinds=DECLARATIVE_EXECUTOR_KINDS,
        )

        mapping = report.as_mapping()
        self.assertIn("summary", mapping)
        self.assertIn("profiles", mapping)
        self.assertEqual(mapping["summary"]["actions"]["active"], 0)
        self.assertIsNone(mapping["summary"]["actions"]["represented_percent"])
        self.assertIsNone(mapping["summary"]["actions"]["executable_percent"])
        self.assertNotIn("password", json.dumps(mapping, ensure_ascii=False))
        self.assertNotIn("token", json.dumps(mapping, ensure_ascii=False))

    def test_future_executor_kinds_change_coverage_without_report_rewrite(self):
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
            },
            "layout_profiles": {},
        }

        report = build_migration_coverage(
            config,
            executable_kinds={
                *DECLARATIVE_EXECUTOR_KINDS,
                "input_volume_db",
            },
        )

        self.assertEqual(
            report.profiles[0].classification,
            DECLARATIVE_EXECUTABLE,
        )
        self.assertEqual(
            report.summary()["actions"]["executable_percent"],
            100.0,
        )


if __name__ == "__main__":
    unittest.main()
