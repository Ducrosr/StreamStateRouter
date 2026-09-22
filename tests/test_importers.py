from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from stream_state_router.importers import (
    AdvancedSceneSwitcherImporter,
    SceneCollectionImporter,
)


class _ImportClient:
    session_generation = 1

    def __init__(self):
        self.calls = []

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetVersion":
            return {
                "availableRequests": [
                    "GetSceneCollectionList",
                    "GetSceneList",
                    "GetGroupList",
                    "GetSceneItemList",
                    "GetGroupSceneItemList",
                    "GetInputList",
                    "GetInputSettings",
                    "GetInputMute",
                    "GetInputVolume",
                    "GetSourceFilterList",
                    "GetSourceFilter",
                    "GetSceneTransitionList",
                    "GetVideoSettings",
                ]
            }
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": "Streaming"}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "In Game",
                "currentProgramSceneUuid": "scene-1",
                "scenes": [
                    {
                        "sceneName": "In Game",
                        "sceneUuid": "scene-1",
                        "sceneIndex": 0,
                    }
                ],
            }
        if request == "GetGroupList":
            return {"groups": []}
        if request == "GetSceneItemList":
            return {
                "sceneItems": [
                    {
                        "sourceName": "Game Capture",
                        "sourceUuid": "input-1",
                        "sceneItemId": 10,
                        "sceneItemEnabled": True,
                        "sourceType": "OBS_SOURCE_TYPE_INPUT",
                        "inputKind": "game_capture",
                    }
                ]
            }
        if request == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": "Game Capture",
                        "inputKind": "game_capture",
                        "inputUuid": "input-1",
                    }
                ]
            }
        if request == "GetInputSettings":
            return {
                "inputKind": "game_capture",
                "inputUuid": "input-1",
                "inputSettings": {
                    "window": "Overwatch.exe",
                    "rgb10a2_space": "2100pq",
                },
            }
        if request == "GetInputMute":
            return {"inputMuted": False}
        if request == "GetInputVolume":
            return {"inputVolumeDb": -6.0}
        if request == "GetSourceFilterList":
            if data and data.get("sourceName") == "Game Capture":
                return {
                    "filters": [
                        {
                            "filterName": "Tone Map",
                            "filterKind": "shader_filter",
                            "filterEnabled": True,
                        }
                    ]
                }
            return {"filters": []}
        if request == "GetSourceFilter":
            return {
                "filterName": "Tone Map",
                "filterKind": "shader_filter",
                "filterEnabled": True,
                "filterSettings": {"exposure": 1.25},
            }
        if request == "GetSceneTransitionList":
            return {"transitions": []}
        if request == "GetVideoSettings":
            return {"baseWidth": 2560, "baseHeight": 1440}
        raise AssertionError(f"Unexpected request: {request} {data!r}")


class ImporterTests(unittest.TestCase):
    def test_scene_collection_snapshot_projects_stable_actions(self):
        importer = SceneCollectionImporter(_ImportClient())

        snapshot = importer.snapshot()
        actions, skipped = importer.actions_from_snapshot(
            snapshot,
            include_visibility=True,
        )

        self.assertEqual(snapshot.collection, "Streaming")
        self.assertEqual(snapshot.current_program_scene, "In Game")
        self.assertEqual(len(snapshot.inputs), 1)
        self.assertEqual(len(snapshot.filters), 1)
        self.assertEqual(skipped, [])
        kinds = [action["type"] for action in actions]
        self.assertEqual(
            kinds,
            [
                "set_input_settings",
                "input_mute",
                "input_volume_db",
                "source_filter_enabled",
                "source_filter_settings",
                "scene_item_enabled",
            ],
        )

    def test_scene_collection_merge_replaces_same_target_instead_of_duplicating(self):
        importer = SceneCollectionImporter(_ImportClient())
        snapshot = importer.snapshot()
        config = {
            "profiles": {
                "capture": {
                    "HDR": {
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "name": "old",
                                "enabled": True,
                                "params": {
                                    "source": "Game Capture",
                                    "filter": "Tone Map",
                                    "enabled": False,
                                },
                            }
                        ]
                    }
                }
            }
        }

        report = importer.merge_actions_into_profile(
            config,
            domain="capture",
            profile_name="HDR",
            snapshot=snapshot,
            include_visibility=False,
        )

        self.assertEqual(report.replaced_actions, 1)
        actions = config["profiles"]["capture"]["HDR"]["actions"]
        enabled_actions = [
            item
            for item in actions
            if item["type"] == "source_filter_enabled"
        ]
        self.assertEqual(len(enabled_actions), 1)
        self.assertTrue(enabled_actions[0]["params"]["enabled"])

    def test_advss_collection_file_is_auto_discovered_by_collection_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "other.json").write_text(
                json.dumps({"name": "Other"}),
                encoding="utf-8",
            )
            expected = root / "streaming.json"
            expected.write_text(
                json.dumps(
                    {
                        "name": "Streaming",
                        "advanced-scene-switcher": {
                            "macros": [{"name": "One", "group": False}]
                        },
                    }
                ),
                encoding="utf-8",
            )

            found = AdvancedSceneSwitcherImporter.find_scene_collection_file(
                "Streaming",
                scenes_dir=root,
            )

        self.assertEqual(found, expected)

    def test_advss_nested_export_is_loaded(self):
        payload = {
            "advanced-scene-switcher": {
                "macros": [{"name": "One", "group": False}]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collection.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = AdvancedSceneSwitcherImporter.load(path)

        self.assertEqual(loaded["macros"][0]["name"], "One")

    def test_advss_process_macro_attaches_actions_to_existing_ssr_rule(self):
        asc = {
            "macros": [
                {
                    "name": "OW Scene",
                    "group": False,
                    "pause": False,
                    "parallel": False,
                    "skipExecOnStart": False,
                    "conditions": [
                        {
                            "id": "process",
                            "process": "Overwatch.exe",
                            "focus": True,
                            "checkPath": False,
                            "regexConfig": {
                                "enable": False,
                                "partial": False,
                                "options": 0,
                            },
                        }
                    ],
                    "actions": [
                        {
                            "id": "scene_switch",
                            "action": 0,
                            "sceneSelection": {
                                "type": 0,
                                "name": "In Game",
                                "canvasSelection": "Main",
                            },
                        },
                        {
                            "id": "filter",
                            "action": 3,
                            "source": {"type": 0, "name": "Game Capture"},
                            "filter": {"type": 0, "name": "Tone Map"},
                            "inputMethod": 2,
                            "settings": json.dumps({"exposure": 1.5}),
                        },
                    ],
                    "elseActions": [],
                }
            ]
        }
        config = {
            "router": {
                "fallback_state": {
                    "Game": "Vanilla",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Vanilla",
                }
            },
            "rules": [
                {
                    "name": "Overwatch",
                    "behavior": "match",
                    "exe": "Overwatch.exe",
                    "path": "",
                    "title_regex": "",
                    "state": {
                        "Game": "Overwatch",
                        "OverlayProfile": "FPS",
                        "CaptureProfile": "HDR",
                        "AudioProfile": "Game",
                        "LayoutProfile": "FPS",
                    },
                }
            ],
            "profiles": {
                "game": {
                    "Vanilla": {"actions": []},
                    "Overwatch": {"actions": []},
                }
            },
        }

        report = AdvancedSceneSwitcherImporter.apply_to_config(asc, config)

        self.assertEqual(report.macros_converted, 1)
        self.assertEqual(report.actions_converted, 2)
        self.assertEqual(report.attached_to_existing_rules, 1)
        self.assertEqual(report.rules_created, 0)
        actions = config["profiles"]["game"]["Overwatch"]["actions"]
        self.assertEqual(
            [item["type"] for item in actions],
            ["set_program_scene", "source_filter_settings"],
        )

    def test_advss_process_path_macro_creates_disabled_rule(self):
        asc = {
            "macros": [
                {
                    "name": "Exact path",
                    "group": False,
                    "conditions": [
                        {
                            "id": "process",
                            "process": "game.exe",
                            "focus": True,
                            "checkPath": True,
                            "processPath": r"C:\\Games\\game.exe",
                            "regexConfig": {"enable": False},
                            "pathRegex": {"enable": False},
                        }
                    ],
                    "actions": [
                        {
                            "id": "scene_switch",
                            "action": 0,
                            "sceneSelection": {"type": 0, "name": "Gameplay"},
                            "sceneType": 0,
                        }
                    ],
                    "elseActions": [],
                }
            ]
        }
        config = {
            "router": {"fallback_state": {"Game": "Vanilla"}},
            "rules": [],
            "profiles": {"game": {"Vanilla": {"actions": []}}},
        }

        report = AdvancedSceneSwitcherImporter.apply_to_config(asc, config)

        self.assertEqual(report.macros_converted, 1)
        self.assertEqual(report.rules_created, 1)
        self.assertFalse(config["rules"][0]["enabled"])
        self.assertEqual(config["rules"][0]["exe"], "game.exe")
        self.assertEqual(
            config["rules"][0]["path"],
            r"C:\\Games\\game.exe",
        )

    def test_advss_window_macro_and_audio_actions_are_converted(self):
        asc = {
            "macros": [
                {
                    "name": "Window Audio",
                    "group": False,
                    "conditions": [
                        {
                            "id": "window",
                            "focus": True,
                            "checkTitle": True,
                            "window": "Overwatch.*",
                            "windowRegexConfig": {
                                "enable": True,
                                "partial": True,
                                "options": 1,
                            },
                            "fullscreen": False,
                            "maximized": False,
                            "windowFocusChanged": False,
                            "checkWindowText": False,
                        }
                    ],
                    "actions": [
                        {
                            "id": "audio",
                            "action": 0,
                            "audioSource": {"type": 0, "name": "Game Audio"},
                        },
                        {
                            "id": "audio",
                            "action": 2,
                            "audioSource": {"type": 0, "name": "Game Audio"},
                            "fade": False,
                            "useDb": True,
                            "volumeDB": {"type": 0, "value": -8.5},
                        },
                    ],
                    "elseActions": [],
                }
            ]
        }
        config = {
            "router": {"fallback_state": {"Game": "Vanilla"}},
            "rules": [],
            "profiles": {"game": {"Vanilla": {"actions": []}}},
        }

        report = AdvancedSceneSwitcherImporter.apply_to_config(asc, config)

        self.assertEqual(report.macros_converted, 1)
        self.assertEqual(report.actions_converted, 2)
        self.assertEqual(report.rules_created, 1)
        rule = config["rules"][0]
        self.assertEqual(rule["title_regex"], "(?i)Overwatch.*")
        self.assertFalse(rule["enabled"])
        profile = config["profiles"]["game"][rule["state"]["Game"]]
        self.assertEqual(
            [item["type"] for item in profile["actions"]],
            ["input_mute", "input_volume_db"],
        )
        self.assertEqual(
            profile["actions"][1]["params"]["volume_db"],
            -8.5,
        )

    def test_advss_visibility_with_custom_transition_is_rejected_atomically(self):
        asc = {
            "macros": [
                {
                    "name": "Transition",
                    "group": False,
                    "conditions": [
                        {
                            "id": "process",
                            "process": "game.exe",
                            "focus": True,
                            "checkPath": False,
                            "regexConfig": {"enable": False},
                        }
                    ],
                    "actions": [
                        {
                            "id": "scene_visibility",
                            "action": 0,
                            "sceneSelection": {"type": 0, "name": "Gameplay"},
                            "sceneItemSelection": {
                                "type": 0,
                                "idxType": 0,
                                "idx": 0,
                                "item": "Chat",
                            },
                            "updateTransition": True,
                            "updateDuration": False,
                        }
                    ],
                    "elseActions": [],
                }
            ]
        }
        config = {
            "router": {"fallback_state": {"Game": "Vanilla"}},
            "rules": [],
            "profiles": {"game": {"Vanilla": {"actions": []}}},
        }

        report = AdvancedSceneSwitcherImporter.apply_to_config(asc, config)

        self.assertEqual(report.macros_converted, 0)
        self.assertTrue(any("transition" in item for item in report.skipped))
        self.assertEqual(config["rules"], [])

    def test_advss_unsupported_macro_is_reported_without_partial_conversion(self):
        asc = {
            "macros": [
                {
                    "name": "Timed",
                    "group": False,
                    "conditions": [
                        {
                            "id": "process",
                            "process": "Game.exe",
                            "focus": True,
                            "checkPath": False,
                            "regexConfig": {"enable": False},
                        }
                    ],
                    "actions": [
                        {"id": "wait"},
                        {
                            "id": "scene_switch",
                            "action": 0,
                            "sceneSelection": {"type": 0, "name": "Game"},
                        },
                    ],
                    "elseActions": [],
                }
            ]
        }
        config = {
            "router": {"fallback_state": {"Game": "Vanilla"}},
            "rules": [],
            "profiles": {"game": {"Vanilla": {"actions": []}}},
        }

        report = AdvancedSceneSwitcherImporter.apply_to_config(asc, config)

        self.assertEqual(report.macros_converted, 0)
        self.assertEqual(report.actions_converted, 0)
        self.assertTrue(any("wait" in item for item in report.skipped))
        self.assertEqual(config["rules"], [])


if __name__ == "__main__":
    unittest.main()
