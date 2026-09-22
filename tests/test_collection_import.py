from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from stream_state_router.importing.obs_collection import (
    CollectionImportPlan,
    ImportedMacro,
    apply_collection_import_plan,
    convert_advanced_scene_switcher,
    find_obs_collection_file,
)


def _segment(kind: str, **values):
    return {
        "segmentSettings": {"enabled": True, "version": 1},
        "id": kind,
        **values,
    }


class CollectionImportTests(unittest.TestCase):
    def test_find_obs_collection_file_matches_embedded_collection_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "opaque-file-name.json").write_text(
                json.dumps(
                    {
                        "name": "Streaming",
                        "advanced-scene-switcher": {"macros": []},
                    }
                ),
                encoding="utf-8",
            )

            found = find_obs_collection_file("Streaming", root=root)

            self.assertEqual(found, root / "opaque-file-name.json")

    def test_simple_process_scene_switch_macro_is_convertible(self):
        payload = {
            "advanced-scene-switcher": {
                "macros": [
                    {
                        "name": "Overwatch",
                        "conditions": [
                            _segment(
                                "process",
                                process="Overwatch.exe",
                                focus=True,
                                checkPath=False,
                                regexConfig={"enable": False},
                            )
                        ],
                        "actions": [
                            _segment(
                                "scene_switch",
                                action=0,
                                sceneSelection={
                                    "type": 0,
                                    "name": "In Game",
                                    "canvasSelection": "Main",
                                },
                            )
                        ],
                        "elseActions": [],
                    }
                ]
            }
        }

        macros = convert_advanced_scene_switcher(payload)

        self.assertEqual(len(macros), 1)
        macro = macros[0]
        self.assertEqual(macro.status, "converted")
        self.assertEqual(macro.selector["exe"], "Overwatch.exe")
        self.assertEqual(
            macro.actions[0],
            {
                "type": "set_program_scene",
                "params": {"scene": "In Game"},
                "enabled": True,
                "name": "Import ASC",
            },
        )

    def test_filter_json_settings_are_converted(self):
        payload = {
            "macros": [
                {
                    "name": "Filter",
                    "conditions": [
                        _segment(
                            "process",
                            process="game.exe",
                            focus=True,
                            checkPath=False,
                            regexConfig={"enable": False},
                        )
                    ],
                    "actions": [
                        _segment(
                            "filter",
                            source={"type": 0, "name": "Game Capture"},
                            filter={"type": 0, "name": "HDR Tone Map"},
                            action=3,
                            inputMethod=2,
                            settings='{"exposure": 1.25}',
                        )
                    ],
                    "elseActions": [],
                }
            ]
        }

        macro = convert_advanced_scene_switcher(payload)[0]

        self.assertEqual(macro.status, "converted")
        self.assertEqual(
            macro.actions[0]["type"],
            "source_filter_settings",
        )
        self.assertEqual(
            macro.actions[0]["params"]["settings"],
            {"exposure": 1.25},
        )

    def test_unsupported_action_preserves_partial_conversion_report(self):
        payload = {
            "macros": [
                {
                    "name": "Sequence",
                    "conditions": [
                        _segment(
                            "process",
                            process="game.exe",
                            focus=True,
                            checkPath=False,
                            regexConfig={"enable": False},
                        )
                    ],
                    "actions": [
                        _segment(
                            "scene_switch",
                            action=0,
                            sceneSelection={"type": 0, "name": "A"},
                        ),
                        _segment("wait", waitType=0),
                    ],
                    "elseActions": [],
                }
            ]
        }

        macro = convert_advanced_scene_switcher(payload)[0]

        self.assertEqual(macro.status, "partial")
        self.assertEqual(len(macro.actions), 1)
        self.assertIn("wait", macro.reason)

    def test_window_regex_is_not_silently_reinterpreted(self):
        payload = {
            "macros": [
                {
                    "name": "Window",
                    "conditions": [
                        _segment(
                            "window",
                            focus=True,
                            checkTitle=True,
                            window="Overwatch.*",
                            windowRegexConfig={
                                "enable": True,
                                "partial": True,
                                "options": 3,
                            },
                        )
                    ],
                    "actions": [
                        _segment(
                            "scene_switch",
                            action=0,
                            sceneSelection={"type": 0, "name": "Game"},
                        )
                    ],
                    "elseActions": [],
                }
            ]
        }

        macro = convert_advanced_scene_switcher(payload)[0]

        self.assertEqual(macro.status, "unsupported")
        self.assertIn("regex", macro.reason)

    def test_apply_plan_imports_macros_as_disabled_rules(self):
        config = {
            "router": {
                "fallback_state": {
                    "Game": "Vanilla",
                    "OverlayProfile": "FPS",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "FPS",
                }
            },
            "rules": [],
            "profiles": {
                "game": {"Vanilla": {"actions": []}},
                "capture": {"Default": {"actions": []}},
            },
            "layout_profiles": {},
        }
        plan = CollectionImportPlan(
            collection="Streaming",
            collection_file=r"C:\obs\Streaming.json",
            snapshot_profile_name="Import Collection :: Streaming",
            snapshot_actions=(
                {
                    "type": "source_filter_enabled",
                    "params": {
                        "source": "Game",
                        "filter": "HDR",
                        "enabled": True,
                    },
                },
            ),
            layout_profiles={
                "Import Streaming :: In Game": {
                    "scene": "In Game",
                    "modules": {},
                }
            },
            macros=(
                ImportedMacro(
                    name="OW",
                    status="converted",
                    selector={
                        "exe": "Overwatch.exe",
                        "path": "",
                        "title_regex": "",
                    },
                    actions=(
                        {
                            "type": "set_program_scene",
                            "params": {"scene": "In Game"},
                        },
                    ),
                    profile_name="ASC :: OW",
                ),
            ),
        )

        imported, counts = apply_collection_import_plan(
            config,
            plan,
            import_snapshot=True,
            import_layouts=True,
            import_macros=True,
        )

        self.assertEqual(counts["snapshot_profiles"], 1)
        self.assertEqual(counts["layout_profiles"], 1)
        self.assertEqual(counts["macros"], 1)
        self.assertEqual(counts["rules"], 1)
        rule = imported["rules"][0]
        self.assertFalse(rule["enabled"])
        self.assertEqual(rule["exe"], "Overwatch.exe")
        self.assertEqual(rule["state"]["OverlayProfile"], "FPS")
        self.assertEqual(rule["state"]["Game"], "ASC :: OW")


if __name__ == "__main__":
    unittest.main()
