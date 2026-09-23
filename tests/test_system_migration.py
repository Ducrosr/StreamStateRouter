from __future__ import annotations

import unittest

from stream_state_router.importers import (
    neutralize_referenced_test_layout_profiles,
    remove_game_profile_input_actions,
    set_capture_profile_for_process,
    set_fallback_capture_profile,
    set_game_profile_input_setting,
    wire_standard_multiclient_capture_profiles,
    wire_windows_hdr_capture_profiles,
)


class SystemMigrationTests(unittest.TestCase):
    def test_wires_hdr_and_sdr_without_mutating_default(self):
        config = {
            "profiles": {
                "capture": {
                    "HDR": {"actions": []},
                    "SDR": {"actions": []},
                    "Default": {"actions": []},
                }
            }
        }

        changed = wire_windows_hdr_capture_profiles(config)

        self.assertEqual(changed, ("HDR", "SDR"))
        hdr = config["profiles"]["capture"]["HDR"]["actions"][0]
        sdr = config["profiles"]["capture"]["SDR"]["actions"][0]
        self.assertEqual(config["profiles"]["capture"]["Default"]["actions"], [])
        self.assertEqual(
            hdr["params"],
            {"enabled": True, "display": "primary"},
        )
        self.assertEqual(
            sdr["params"],
            {"enabled": False, "display": "primary"},
        )

    def test_wires_standard_and_multiclient_capture_profiles(self):
        config = {
            "profiles": {
                "capture": {
                    "HDR": {"actions": []},
                    "SDR": {"actions": []},
                }
            }
        }

        changed = wire_standard_multiclient_capture_profiles(
            config,
            scene="Capture Root",
            standard_source="Game Capture",
            multiclient_source="DWM Pool",
            multiclient_hdr_profile="Dofus HDR",
            multiclient_sdr_profile="Dofus SDR",
        )

        self.assertEqual(
            changed,
            ("HDR", "SDR", "Dofus HDR", "Dofus SDR"),
        )
        capture = config["profiles"]["capture"]
        for name, hdr_enabled, standard_enabled in (
            ("HDR", True, True),
            ("SDR", False, True),
            ("Dofus HDR", True, False),
            ("Dofus SDR", False, False),
        ):
            actions = capture[name]["actions"]
            hdr = next(a for a in actions if a["type"] == "windows_hdr")
            standard = next(
                a for a in actions
                if a["type"] == "scene_item_enabled"
                and a["params"]["source"] == "Game Capture"
            )
            multi = next(
                a for a in actions
                if a["type"] == "scene_item_enabled"
                and a["params"]["source"] == "DWM Pool"
            )
            self.assertEqual(hdr["params"]["enabled"], hdr_enabled)
            self.assertEqual(
                standard["params"]["enabled"],
                standard_enabled,
            )
            self.assertEqual(
                multi["params"]["enabled"],
                not standard_enabled,
            )

        second = wire_standard_multiclient_capture_profiles(
            config,
            scene="Capture Root",
            standard_source="Game Capture",
            multiclient_source="DWM Pool",
            multiclient_hdr_profile="Dofus HDR",
            multiclient_sdr_profile="Dofus SDR",
        )
        self.assertEqual(second, ())

    def test_removes_imported_capture_input_actions_only(self):
        config = {
            "profiles": {
                "game": {
                    "Dofus Unity": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture de jeu",
                                    "settings": {"window": "old"},
                                },
                            },
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Avatar Dynamic",
                                    "settings": {"file": "avatar.png"},
                                },
                            },
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Avatar Dynamic",
                                    "filter": "FX",
                                },
                            },
                        ]
                    }
                }
            }
        }

        removed = remove_game_profile_input_actions(
            config,
            game_profile="Dofus Unity",
            input_name="Capture de jeu",
        )

        self.assertEqual(removed, 1)
        remaining = config["profiles"]["game"]["Dofus Unity"]["actions"]
        self.assertEqual(len(remaining), 2)
        self.assertEqual(
            remaining[0]["params"]["input"],
            "Avatar Dynamic",
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

    def test_sets_capture_profile_for_foreground_and_background_rules(self):
        config = {
            "profiles": {
                "capture": {
                    "HDR": {"actions": []},
                    "SDR": {"actions": []},
                }
            },
            "rules": [
                {
                    "name": "Dofus Unity",
                    "behavior": "match",
                    "exe": "Dofus.exe",
                    "conditions": {},
                    "state": {"CaptureProfile": "Default"},
                },
                {
                    "name": "Dofus background",
                    "behavior": "match",
                    "exe": "",
                    "conditions": {"process_running": "Dofus.exe"},
                    "state": {"CaptureProfile": "Default"},
                },
            ],
        }

        changed = set_capture_profile_for_process(
            config,
            process="Dofus.exe",
            capture_profile="HDR",
        )

        self.assertEqual(changed, ("Dofus Unity", "Dofus background"))
        self.assertEqual(config["rules"][0]["state"]["CaptureProfile"], "HDR")
        self.assertEqual(config["rules"][1]["state"]["CaptureProfile"], "HDR")

    def test_sets_fallback_capture_profile(self):
        config = {
            "router": {
                "fallback_state": {
                    "Game": "Vanilla",
                    "CaptureProfile": "Default",
                }
            },
            "profiles": {
                "capture": {
                    "Default": {"actions": []},
                    "SDR": {"actions": []},
                }
            },
        }

        previous = set_fallback_capture_profile(
            config,
            capture_profile="SDR",
        )

        self.assertEqual(previous, "Default")
        self.assertEqual(
            config["router"]["fallback_state"]["CaptureProfile"],
            "SDR",
        )

    def test_updates_one_game_input_setting(self):
        config = {
            "profiles": {
                "game": {
                    "Dofus Unity": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture de jeu",
                                    "settings": {
                                        "rgb10a2_space": "srgb",
                                        "capture_mode": "window",
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }

        changed = set_game_profile_input_setting(
            config,
            game_profile="Dofus Unity",
            input_name="Capture de jeu",
            setting="rgb10a2_space",
            value="2100pq",
        )

        self.assertEqual(changed, 1)
        self.assertEqual(
            config["profiles"]["game"]["Dofus Unity"]["actions"][0]
            ["params"]["settings"]["rgb10a2_space"],
            "2100pq",
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
