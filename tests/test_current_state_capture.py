from __future__ import annotations

import unittest

from stream_state_router.importers.current_state_capture import (
    CurrentStateCaptureOptions,
    build_current_state_capture_draft,
    find_ignored_process_rules,
    find_process_rules,
    suggest_capture_name,
)
from stream_state_router.importers.scene_collection import (
    ImportedFilter,
    ImportedInput,
    ImportedSceneItem,
    SceneCollectionSnapshot,
)
from stream_state_router.services.config import validate_config


def _config() -> dict:
    return {
        "schema_version": 6,
        "router": {
            "poll_ms": 50,
            "debounce_ms": 150,
            "fallback_debounce_ms": 350,
            "fallback_state": {
                "Game": "Vanilla",
                "OverlayProfile": "Vanilla",
                "CaptureProfile": "Default",
                "AudioProfile": "Default",
                "LayoutProfile": "Vanilla",
            }
        },
        "obs": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 4455,
            "password": "",
            "timeout_seconds": 2.0,
            "reconnect_seconds": 3.0,
        },
        "api": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8765,
            "token": "",
        },
        "host_control": {
            "soundvolumeview_path": "",
            "audio_timeout_seconds": 5.0,
        },
        "control_variables": {},
        "activation_policies": {},
        "layout_history": {},
        "ui": {
            "close_to_tray": True,
            "start_with_windows": False,
            "auto_detect_modules": True,
            "module_scan_seconds": 5,
        },
        "rules": [],
        "profiles": {
            "game": {
                "Vanilla": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                }
            },
            "overlay": {
                "Vanilla": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
                "FPS": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
            },
            "capture": {
                "Default": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
                "HDR": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
            },
            "audio": {
                "Default": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
                "Game": {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                },
            },
        },
        "layout_profiles": {
            "Vanilla": {
                "scene": "Just Chatting",
                "coordinate_mode": "normalized",
                "modules": {},
                "extends": "",
                "conditions": {},
                "transition": {
                    "mode": "instant",
                    "duration_ms": 0,
                    "steps": 8,
                },
            },
            "FPS": {
                "scene": "In Game",
                "coordinate_mode": "normalized",
                "modules": {},
                "extends": "",
                "conditions": {},
                "transition": {
                    "mode": "instant",
                    "duration_ms": 0,
                    "steps": 8,
                },
            },
        },
    }


def _snapshot() -> SceneCollectionSnapshot:
    return SceneCollectionSnapshot(
        collection="Streaming",
        current_program_scene="In Game",
        inputs=(
            ImportedInput(
                name="Game Capture",
                kind="game_capture",
                uuid="game-1",
                settings={"capture_mode": "window"},
                muted=False,
                volume_db=-3.0,
            ),
            ImportedInput(
                name="Micro",
                kind="wasapi_input_capture",
                uuid="mic-1",
                settings={"device_id": "mic"},
                muted=False,
                volume_db=-6.0,
            ),
        ),
        filters=(
            ImportedFilter(
                source="Game Capture",
                name="HDR Tone Map",
                kind="shader_filter",
                enabled=True,
                settings={"strength": 0.8},
            ),
            ImportedFilter(
                source="Micro",
                name="Noise Gate",
                kind="noise_gate_filter",
                enabled=True,
                settings={"close_threshold": -40.0},
            ),
        ),
        scene_items=(
            ImportedSceneItem(
                scene="In Game",
                source="Game Capture",
                occurrence=0,
                enabled=True,
            ),
            ImportedSceneItem(
                scene="Just Chatting",
                source="Micro",
                occurrence=0,
                enabled=True,
            ),
        ),
        scenes=("In Game", "Just Chatting"),
        warnings=(),
    )


def _layouts() -> dict:
    return {
        "Import Streaming · In Game": {
            "scene": "In Game",
            "coordinate_mode": "normalized",
            "modules": {
                "Game Capture": {
                    "geometry": {
                        "x": 0.0,
                        "y": 0.0,
                        "width": 1920.0,
                        "height": 1080.0,
                    },
                    "visible": True,
                    "elements": [],
                }
            },
            "conditions": {},
            "extends": "",
        },
        "Import Streaming · Just Chatting": {
            "scene": "Just Chatting",
            "coordinate_mode": "normalized",
            "modules": {},
            "conditions": {},
            "extends": "",
        },
    }


class CurrentStateCaptureTests(unittest.TestCase):
    def test_new_capture_creates_rule_game_profile_and_current_layout(self) -> None:
        result = build_current_state_capture_draft(
            _config(),
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={
                "OverlayProfile": "FPS",
                "CaptureProfile": "HDR",
                "AudioProfile": "Game",
                "LayoutProfile": "FPS",
            },
            options=CurrentStateCaptureOptions(
                name="League of Legends",
                process="League of Legends.exe",
            ),
        )

        self.assertEqual(result.report.mode, "create")
        self.assertTrue(result.report.layout_captured)
        self.assertEqual(result.report.captured_inputs, 1)
        self.assertEqual(result.report.captured_filters, 1)
        self.assertEqual(result.report.captured_scene_items, 1)

        rule = result.config["rules"][0]
        self.assertEqual(rule["name"], "League of Legends")
        self.assertEqual(rule["exe"], "League of Legends.exe")
        self.assertEqual(rule["state"]["Game"], "League of Legends")
        self.assertEqual(rule["state"]["OverlayProfile"], "FPS")
        self.assertEqual(rule["state"]["CaptureProfile"], "HDR")
        self.assertEqual(rule["state"]["AudioProfile"], "Game")
        self.assertEqual(rule["state"]["LayoutProfile"], "League of Legends")

        game_profile = result.config["profiles"]["game"]["League of Legends"]
        actions = game_profile["actions"]
        rendered = str(actions)
        self.assertIn("Game Capture", rendered)
        self.assertNotIn("Micro", rendered)
        self.assertEqual(
            result.config["layout_profiles"]["League of Legends"]["scene"],
            "In Game",
        )
        self.assertEqual(validate_config(result.config), [])

    def test_update_unshared_profiles_preserves_other_logical_domains(self) -> None:
        config = _config()
        config["profiles"]["game"]["Overwatch"] = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }
        config["layout_profiles"]["Overwatch"] = {
            "scene": "Old Scene",
            "modules": {},
            "extends": "",
            "conditions": {},
        }
        config["rules"] = [
            {
                "name": "Overwatch",
                "behavior": "match",
                "priority": 100,
                "enabled": True,
                "exe": "Overwatch.exe",
                "path": "",
                "title_regex": "",
                "conditions": {},
                "state": {
                    "Game": "Overwatch",
                    "OverlayProfile": "FPS",
                    "CaptureProfile": "HDR",
                    "AudioProfile": "Game",
                    "LayoutProfile": "Overwatch",
                },
            }
        ]

        result = build_current_state_capture_draft(
            config,
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={},
            options=CurrentStateCaptureOptions(
                name="Overwatch",
                process="Overwatch.exe",
                existing_rule_name="Overwatch",
            ),
        )

        rule = result.config["rules"][0]
        self.assertEqual(result.report.mode, "update")
        self.assertEqual(rule["state"]["Game"], "Overwatch")
        self.assertEqual(rule["state"]["LayoutProfile"], "Overwatch")
        self.assertEqual(rule["state"]["OverlayProfile"], "FPS")
        self.assertEqual(rule["state"]["CaptureProfile"], "HDR")
        self.assertEqual(rule["state"]["AudioProfile"], "Game")
        self.assertEqual(
            result.config["layout_profiles"]["Overwatch"]["scene"],
            "In Game",
        )

    def test_update_shared_profiles_creates_dedicated_profiles(self) -> None:
        config = _config()
        config["profiles"]["game"]["Shared Game"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Existing shared action",
                    "enabled": True,
                    "params": {"ms": 25},
                }
            ],
            "extends": "",
            "conditions": {},
        }
        config["layout_profiles"]["Shared Layout"] = {
            "scene": "Shared",
            "modules": {},
            "extends": "",
            "conditions": {},
        }
        base_state = {
            "Game": "Shared Game",
            "OverlayProfile": "Vanilla",
            "CaptureProfile": "Default",
            "AudioProfile": "Default",
            "LayoutProfile": "Shared Layout",
        }
        config["rules"] = [
            {
                "name": "Target",
                "behavior": "match",
                "priority": 100,
                "enabled": True,
                "exe": "Target.exe",
                "state": dict(base_state),
                "conditions": {},
            },
            {
                "name": "Other",
                "behavior": "match",
                "priority": 90,
                "enabled": True,
                "exe": "Other.exe",
                "state": dict(base_state),
                "conditions": {},
            },
        ]

        result = build_current_state_capture_draft(
            config,
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={},
            options=CurrentStateCaptureOptions(
                name="Target",
                process="Target.exe",
                existing_rule_name="Target",
            ),
        )

        target = next(
            rule for rule in result.config["rules"] if rule["name"] == "Target"
        )
        other = next(
            rule for rule in result.config["rules"] if rule["name"] == "Other"
        )
        self.assertNotEqual(target["state"]["Game"], "Shared Game")
        self.assertNotEqual(target["state"]["LayoutProfile"], "Shared Layout")
        self.assertEqual(other["state"]["Game"], "Shared Game")
        self.assertEqual(other["state"]["LayoutProfile"], "Shared Layout")
        self.assertEqual(
            result.config["profiles"]["game"]["Shared Game"]["actions"][0]["name"],
            "Existing shared action",
        )
        dedicated_actions = result.config["profiles"]["game"][
            target["state"]["Game"]
        ]["actions"]
        self.assertTrue(
            any(
                action.get("name") == "Existing shared action"
                for action in dedicated_actions
            )
        )
        self.assertTrue(
            any("partagé" in note for note in result.report.notes)
        )

    def test_missing_layout_keeps_current_logical_layout_for_new_rule(self) -> None:
        result = build_current_state_capture_draft(
            _config(),
            snapshot=_snapshot(),
            raw_layouts={},
            logical_state={"LayoutProfile": "FPS"},
            options=CurrentStateCaptureOptions(
                name="New Game",
                process="NewGame.exe",
            ),
        )

        rule = result.config["rules"][0]
        self.assertFalse(result.report.layout_captured)
        self.assertEqual(rule["state"]["LayoutProfile"], "FPS")
        self.assertTrue(
            any("Layout" in warning for warning in result.report.warnings)
        )

    def test_planner_never_mutates_source_config(self) -> None:
        config = _config()
        before = str(config)

        build_current_state_capture_draft(
            config,
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={},
            options=CurrentStateCaptureOptions(
                name="New Game",
                process="NewGame.exe",
            ),
        )

        self.assertEqual(str(config), before)

    def test_shared_layout_is_not_repointed_when_layout_capture_is_disabled(self) -> None:
        config = _config()
        config["profiles"]["game"]["Target"] = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }
        config["layout_profiles"]["Shared Layout"] = {
            "scene": "Existing",
            "coordinate_mode": "normalized",
            "modules": {},
            "extends": "",
            "conditions": {},
            "transition": {
                "mode": "instant",
                "duration_ms": 0,
                "steps": 8,
            },
        }
        state = {
            "Game": "Target",
            "OverlayProfile": "Vanilla",
            "CaptureProfile": "Default",
            "AudioProfile": "Default",
            "LayoutProfile": "Shared Layout",
        }
        config["rules"] = [
            {
                "name": "Target",
                "behavior": "match",
                "priority": 100,
                "enabled": True,
                "exe": "Target.exe",
                "state": dict(state),
                "conditions": {},
            },
            {
                "name": "Other",
                "behavior": "match",
                "priority": 90,
                "enabled": True,
                "exe": "Other.exe",
                "state": dict(state),
                "conditions": {},
            },
        ]

        result = build_current_state_capture_draft(
            config,
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={},
            options=CurrentStateCaptureOptions(
                name="Target",
                process="Target.exe",
                existing_rule_name="Target",
                include_layout=False,
            ),
        )

        target = next(
            rule for rule in result.config["rules"] if rule["name"] == "Target"
        )
        self.assertEqual(target["state"]["LayoutProfile"], "Shared Layout")
        self.assertFalse(result.report.layout_captured)
        self.assertFalse(
            any("LayoutProfile existant était partagé" in note for note in result.report.notes)
        )

    def test_find_process_rules_is_case_insensitive_and_priority_sorted(self) -> None:
        config = _config()
        config["rules"] = [
            {
                "name": "Low",
                "behavior": "match",
                "priority": 10,
                "exe": "game.exe",
            },
            {
                "name": "High",
                "behavior": "match",
                "priority": 20,
                "exe": "GAME.EXE",
            },
        ]

        matches = find_process_rules(config, "Game.exe")

        self.assertEqual([rule["name"] for rule in matches], ["High", "Low"])

    def test_profile_inheritance_counts_as_shared(self) -> None:
        config = _config()
        config["profiles"]["game"]["Parent"] = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }
        config["profiles"]["game"]["Child"] = {
            "actions": [],
            "extends": "Parent",
            "conditions": {},
        }
        config["layout_profiles"]["Parent Layout"] = {
            "scene": "Old",
            "modules": {},
            "extends": "",
            "conditions": {},
        }
        config["layout_profiles"]["Child Layout"] = {
            "scene": "Other",
            "modules": {},
            "extends": "Parent Layout",
            "conditions": {},
        }
        config["rules"] = [
            {
                "name": "Target",
                "behavior": "match",
                "priority": 100,
                "enabled": True,
                "exe": "Target.exe",
                "state": {
                    "Game": "Parent",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Parent Layout",
                },
                "conditions": {},
            }
        ]

        result = build_current_state_capture_draft(
            config,
            snapshot=_snapshot(),
            raw_layouts=_layouts(),
            logical_state={},
            options=CurrentStateCaptureOptions(
                name="Target",
                process="Target.exe",
                existing_rule_name="Target",
            ),
        )

        rule = result.config["rules"][0]
        self.assertNotEqual(rule["state"]["Game"], "Parent")
        self.assertNotEqual(rule["state"]["LayoutProfile"], "Parent Layout")
        self.assertEqual(
            result.config["profiles"]["game"]["Child"]["extends"],
            "Parent",
        )
        self.assertEqual(
            result.config["layout_profiles"]["Child Layout"]["extends"],
            "Parent Layout",
        )

    def test_find_ignored_process_rules_is_case_insensitive(self) -> None:
        config = _config()
        config["rules"] = [
            {
                "name": "Ignore launcher",
                "behavior": "ignore",
                "priority": 200,
                "exe": "Launcher.exe",
            },
            {
                "name": "Normal",
                "behavior": "match",
                "priority": 100,
                "exe": "Launcher.exe",
            },
        ]

        ignored = find_ignored_process_rules(config, "launcher.EXE")

        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["name"], "Ignore launcher")

    def test_suggest_capture_name_avoids_rule_and_profile_collisions(self) -> None:
        config = _config()
        config["rules"] = [{"name": "Game", "behavior": "match"}]
        config["profiles"]["game"]["Game 2"] = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }

        self.assertEqual(suggest_capture_name(config, "Game"), "Game 3")


if __name__ == "__main__":
    unittest.main()
