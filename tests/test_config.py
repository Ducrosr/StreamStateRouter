from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import copy

from stream_state_router.router.models import DEFAULT_PROFILE_NAMES, StreamState
from stream_state_router.services.config import (
    build_activation_policies,
    build_ruleset,
    export_config,
    list_valid_backups,
    load_config,
    load_config_unvalidated,
    migrate_config,
    save_config,
    validate_config,
    config_revision,
    release_runtime_visibility_ownership,
)


class ConfigTests(unittest.TestCase):
    def sample(self):
        return {
            "schema_version": 7,
            "router": {
                "poll_ms": 50,
                "debounce_ms": 150,
                "fallback_debounce_ms": 350,
                "fallback_state": {"Game": "Vanilla", "LayoutProfile": "Vanilla"},
            },
            "obs": {"enabled": False, "host": "127.0.0.1", "port": 4455},
            "api": {"enabled": True, "host": "127.0.0.1", "port": 8765, "token": ""},
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

    def test_partial_rule_inherits_unchecked_domains_from_fallback(self):
        data = self.sample()
        data["router"]["fallback_state"] = {
            "Game": "Vanilla",
            "OverlayProfile": "Vanilla",
            "CaptureProfile": "Default",
            "AudioProfile": "Default",
            "LayoutProfile": "Vanilla",
            "PresentationProfile": "Midgar",
        }
        data["presentation_profiles"] = {
            "Midgar": {"theme": {"accent": "#00ffff"}},
            "Combat": {"extends": "Midgar"},
        }
        data["cues"] = {}
        data["rules"][1]["state"] = {
            "Game": "Game",
            "PresentationProfile": "Combat",
        }

        ruleset, _poll, _debounce, _fallback = build_ruleset(data)
        state = ruleset.rules[1].state

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.game, "Game")
        self.assertEqual(state.overlay_profile, "Vanilla")
        self.assertEqual(state.capture_profile, "Default")
        self.assertEqual(state.audio_profile, "Default")
        self.assertEqual(state.layout_profile, "Vanilla")
        self.assertEqual(state.presentation_profile, "Combat")
        self.assertEqual(validate_config(data), [])

    def test_valid_config_passes(self):
        self.assertEqual(validate_config(self.sample()), [])

    def test_stream_state_defaults_match_canonical_profile_names(self):
        state = StreamState()

        self.assertEqual(
            {
                domain: state.profile_name(domain)
                for domain in DEFAULT_PROFILE_NAMES
            },
            dict(DEFAULT_PROFILE_NAMES),
        )

    def test_empty_default_domains_are_valid_as_unmanaged(self):
        data = self.sample()
        data["rules"] = []
        data["profiles"]["game"] = {}
        data["profiles"]["capture"] = {}
        data["layout_profiles"] = {}

        self.assertEqual(validate_config(data), [])

    def test_empty_domain_still_rejects_non_default_reference(self):
        data = self.sample()
        data["profiles"]["game"] = {}
        data["router"]["fallback_state"]["Game"] = "Custom"

        errors = validate_config(data)

        self.assertTrue(
            any(
                "router.fallback_state.Game référence un profil inexistant : Custom"
                in error
                for error in errors
            ),
            errors,
        )

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

    def test_load_config_unvalidated_preserves_semantic_errors(self):
        data = self.sample()
        data["schema_version"] = 999

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_config_unvalidated(path)

        self.assertEqual(loaded["schema_version"], 999)
        self.assertTrue(validate_config(loaded))

    def test_schema_v1_is_migrated_with_layout_profiles(self):
        data = self.sample()
        data["schema_version"] = 1
        data.pop("layout_profiles")
        data["router"]["fallback_state"].pop("LayoutProfile", None)
        data["rules"][1]["state"].pop("LayoutProfile", None)
        data["rules"][1]["state"]["OverlayProfile"] = "Vanilla"

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 7)
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

        self.assertEqual(migrated["schema_version"], 7)
        modules = migrated["layout_profiles"]["Vanilla"]["modules"]
        self.assertEqual(set(modules), {"[Global] Date", "[Global] Signature"})
        self.assertEqual(modules["[Global] Date"]["module_type"], "Global")
        self.assertEqual(modules["[Global] Date"]["display_name"], "Date")
        self.assertEqual(len(modules["[Global] Date"]["elements"]), 1)


    def test_shareable_export_redacts_secrets_and_is_valid(self):
        data = self.sample()
        data["obs"]["password"] = "obs-secret"
        data["api"]["token"] = "api-secret"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "share.json"
            export_config(data, path, include_secrets=False)
            exported = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(exported["obs"]["password"], "")
        self.assertEqual(exported["api"]["token"], "")
        self.assertEqual(validate_config(exported), [])

    def test_schema_v6_migration_preserves_implicit_rule_defaults(self):
        data = self.sample()
        data["schema_version"] = 6
        data["router"]["fallback_state"] = {
            "Game": "Vanilla",
            "OverlayProfile": "Special",
            "CaptureProfile": "Default",
            "AudioProfile": "Default",
            "LayoutProfile": "Vanilla",
        }
        data["profiles"]["overlay"]["Special"] = {"actions": []}
        data["rules"] = [
            {
                "name": "Legacy",
                "behavior": "match",
                "exe": "legacy.exe",
                "state": {"Game": "Vanilla"},
            }
        ]
        data.pop("presentation_profiles", None)
        data.pop("cues", None)
        data.pop("transition_profiles", None)
        data.pop("shader_sets", None)
        data.pop("sound_sets", None)
        data.pop("widget_runtime", None)

        migrated = migrate_config(data)
        state = migrated["rules"][0]["state"]

        self.assertEqual(state["Game"], "Vanilla")
        self.assertEqual(state["OverlayProfile"], "Vanilla")
        self.assertEqual(state["CaptureProfile"], "Default")
        self.assertEqual(state["AudioProfile"], "Default")
        self.assertEqual(state["LayoutProfile"], "Vanilla")
        self.assertEqual(state["PresentationProfile"], "Vanilla")

        ruleset, _poll, _debounce, _fallback = build_ruleset(migrated)
        resolved = ruleset.rules[0].state
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.overlay_profile, "Vanilla")

    def test_shareable_export_redacts_presentation_resource_settings(self):
        data = self.sample()
        data["cues"] = {
            "SecretCue": {
                "frames": [
                    {
                        "at_ms": 0,
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "enabled": True,
                                "params": {
                                    "input": "Browser",
                                    "settings": {
                                        "url": "https://example.test/?token=secret"
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        }
        data["shader_sets"] = {
            "SecretShaders": {
                "filters": [
                    {
                        "source": "Camera",
                        "filter": "Pulse",
                        "enabled": True,
                        "settings": {"token": "secret"},
                    }
                ]
            }
        }
        data["transition_profiles"] = {
            "SecretTransition": {
                "transition_name": "Fade",
                "settings": {"token": "secret"},
            }
        }
        data["presentation_profiles"] = {
            "Vanilla": {
                "components": {
                    "chat": {
                        "mode": "custom",
                        "resource": "chat",
                        "settings": {"token": "secret"},
                    }
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "share.json"
            export_config(data, path, include_secrets=False)
            exported = json.loads(path.read_text(encoding="utf-8"))

        cue_action = exported["cues"]["SecretCue"]["frames"][0]["actions"][0]
        self.assertEqual(cue_action["params"]["settings"], {})
        self.assertFalse(cue_action["enabled"])
        self.assertEqual(
            exported["shader_sets"]["SecretShaders"]["filters"][0]["settings"],
            {},
        )
        self.assertEqual(
            exported["transition_profiles"]["SecretTransition"]["settings"],
            {},
        )
        self.assertEqual(
            exported["presentation_profiles"]["Vanilla"]["components"]["chat"]["settings"],
            {},
        )
        self.assertEqual(validate_config(exported), [])

    def test_shareable_export_redacts_imported_obs_settings_and_host_path(self):
        data = self.sample()
        data["host_control"] = {
            "soundvolumeview_path": r"C:\\Tools\\SoundVolumeView.exe",
            "audio_timeout_seconds": 5.0,
        }
        data["profiles"]["capture"]["Default"]["actions"] = [
            {
                "type": "set_input_settings",
                "enabled": True,
                "params": {
                    "input": "Browser",
                    "settings": {
                        "url": "https://example.test/?token=secret",
                        "cookie": "secret",
                    },
                    "overlay": True,
                },
            },
            {
                "type": "source_filter_settings",
                "enabled": True,
                "params": {
                    "source": "Game",
                    "filter": "Shader",
                    "settings": {"api_token": "secret"},
                    "overlay": True,
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "share.json"
            export_config(data, path, include_secrets=False)
            exported = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(exported["host_control"]["soundvolumeview_path"], "")
        actions = exported["profiles"]["capture"]["Default"]["actions"]
        self.assertEqual(actions[0]["params"]["settings"], {})
        self.assertEqual(actions[1]["params"]["settings"], {})
        self.assertFalse(actions[0]["enabled"])
        self.assertFalse(actions[1]["enabled"])

    def test_backup_names_do_not_collide_and_retention_is_bounded(self):
        data = self.sample()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "config.json"
            backup_dir = root / "backups"
            with patch("stream_state_router.services.config.backups_dir", return_value=backup_dir):
                save_config(data, target, backup_limit=2)
                for value in (51, 52, 53):
                    data["router"]["poll_ms"] = value
                    save_config(data, target, backup_limit=2)
                backups = list(backup_dir.glob("config-*.json"))
        self.assertLessEqual(len(backups), 2)
        self.assertEqual(len({item.name for item in backups}), len(backups))

    def test_list_valid_backups_returns_newest_first_and_skips_invalid(self):
        data = self.sample()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "config.json"
            backup_dir = root / "backups"
            with patch(
                "stream_state_router.services.config.backups_dir",
                return_value=backup_dir,
            ):
                save_config(data, target)
                data["router"]["poll_ms"] = 51
                save_config(data, target)
                data["router"]["poll_ms"] = 52
                save_config(data, target)

                invalid = backup_dir / "config-99999999-invalid.json"
                invalid.write_text("{not-json", encoding="utf-8")
                invalid.touch()

                backups = list_valid_backups(limit=20)

        self.assertEqual(len(backups), 2)
        self.assertEqual(
            [payload["router"]["poll_ms"] for payload, _path in backups],
            [51, 50],
        )

    def test_list_valid_backups_honors_limit(self):
        data = self.sample()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "config.json"
            backup_dir = root / "backups"
            with patch(
                "stream_state_router.services.config.backups_dir",
                return_value=backup_dir,
            ):
                save_config(data, target)
                for value in (51, 52, 53):
                    data["router"]["poll_ms"] = value
                    save_config(data, target)
                backups = list_valid_backups(limit=2)

        self.assertEqual(len(backups), 2)

    def test_config_revision_is_stable_and_changes_with_content(self):
        data = self.sample()
        first = config_revision(data)
        second = config_revision(copy.deepcopy(data))
        changed = copy.deepcopy(data)
        changed["router"]["poll_ms"] += 1

        self.assertEqual(first, second)
        self.assertNotEqual(first, config_revision(changed))
        self.assertEqual(len(first), 12)

    def test_input_volume_db_validation_matches_obs_protocol_range(self):
        for value in (-100.0, 26.0, -12.5):
            data = self.sample()
            data["profiles"]["audio"]["Default"]["actions"] = [
                {
                    "type": "input_volume_db",
                    "params": {"input": "Music", "volume_db": value},
                }
            ]
            self.assertEqual(validate_config(data), [], value)

        for value in (
            -100.0001,
            26.0001,
            float("nan"),
            float("inf"),
            True,
            "-6.0",
        ):
            data = self.sample()
            data["profiles"]["audio"]["Default"]["actions"] = [
                {
                    "type": "input_volume_db",
                    "params": {"input": "Music", "volume_db": value},
                }
            ]
            errors = validate_config(data)
            self.assertTrue(
                any(".params.volume_db" in error for error in errors),
                (value, errors),
            )

    def test_input_volume_db_requires_explicit_volume_value(self):
        data = self.sample()
        data["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "input_volume_db",
                "params": {"input": "Music"},
            }
        ]

        errors = validate_config(data)

        self.assertTrue(
            any(".params.volume_db est obligatoire" in error for error in errors),
            errors,
        )

    def test_schema_v4_adds_activation_policies(self):
        data = self.sample()
        data["schema_version"] = 4
        data.pop("activation_policies", None)

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 7)
        self.assertEqual(migrated["activation_policies"], {})
        self.assertEqual(validate_config(migrated), [])

    def test_schema_v6_adds_presentation_defaults(self):
        data = self.sample()
        data["schema_version"] = 6
        data.pop("presentation_profiles", None)
        data.pop("cues", None)
        data["router"]["fallback_state"].pop(
            "PresentationProfile",
            None,
        )
        for rule in data["rules"]:
            if isinstance(rule.get("state"), dict):
                rule["state"].pop("PresentationProfile", None)

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 7)
        self.assertEqual(
            migrated["router"]["fallback_state"]["PresentationProfile"],
            "Vanilla",
        )
        self.assertIn("Vanilla", migrated["presentation_profiles"])
        self.assertEqual(migrated["cues"], {})
        self.assertEqual(validate_config(migrated), [])

    def test_presentation_profiles_and_cues_validate(self):
        data = self.sample()
        data["presentation_profiles"] = {
            "Vanilla": {
                "theme": {"accent": "#00ffff"},
            },
            "Combat": {
                "extends": "Vanilla",
                "enter_cue": "GameEnter",
                "exit_cue": "GameExit",
                "animation_intensity": "high",
            },
        }
        data["cues"] = {
            "GameEnter": {
                "frames": [
                    {
                        "at_ms": 0,
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Global",
                                    "filter": "Mako",
                                    "enabled": True,
                                },
                            }
                        ],
                    }
                ]
            },
            "GameExit": {
                "frames": [
                    {
                        "at_ms": 100,
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Global",
                                    "filter": "Mako",
                                    "enabled": False,
                                },
                            }
                        ],
                    }
                ]
            },
        }
        data["router"]["fallback_state"]["PresentationProfile"] = "Vanilla"
        data["rules"][1]["state"]["PresentationProfile"] = "Combat"

        self.assertEqual(validate_config(data), [])

    def test_presentation_sound_sets_validate_and_profile_refs_resolve(self):
        data = self.sample()
        data["presentation_profiles"] = {
            "Vanilla": {},
            "Combat": {"sound_set": "CombatSounds"},
        }
        data["sound_sets"] = {
            "CombatSounds": {
                "enter": [
                    {
                        "input": "SSR Combat In",
                        "action": "restart",
                    }
                ],
                "exit": [
                    {
                        "input": "SSR Combat Out",
                        "action": "stop",
                    }
                ],
            }
        }

        self.assertEqual(validate_config(data), [])

        data["presentation_profiles"]["Combat"]["sound_set"] = "Missing"
        errors = validate_config(data)
        self.assertTrue(
            any("sound_set référence un set inexistant" in item for item in errors),
            errors,
        )

    def test_presentation_validation_rejects_unknown_cue_and_unsafe_timeline(self):
        data = self.sample()
        data["presentation_profiles"] = {
            "Vanilla": {"enter_cue": "Missing"},
        }
        data["cues"] = {
            "TooLate": {
                "frames": [
                    {
                        "at_ms": 30001,
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Global",
                                    "filter": "Mako",
                                },
                            }
                        ],
                    }
                ]
            }
        }

        errors = validate_config(data)

        self.assertTrue(
            any("référence un cue inexistant" in item for item in errors),
            errors,
        )
        self.assertTrue(
            any(".at_ms doit être compris" in item for item in errors),
            errors,
        )

    def test_schema_v5_adds_host_control_defaults(self):
        data = self.sample()
        data["schema_version"] = 5
        data.pop("host_control", None)

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 7)
        self.assertEqual(
            migrated["host_control"],
            {
                "soundvolumeview_path": "",
                "audio_timeout_seconds": 5.0,
            },
        )
        self.assertEqual(validate_config(migrated), [])

    def test_host_and_filter_actions_validate(self):
        data = self.sample()
        data["schema_version"] = 7
        data["host_control"] = {
            "soundvolumeview_path": r"C:\\Tools\\SoundVolumeView.exe",
            "audio_timeout_seconds": 4.0,
        }
        data["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "app_audio_output",
                "params": {
                    "device": "Game",
                    "process": "Overwatch.exe",
                    "roles": "all",
                },
            }
        ]
        data["profiles"]["capture"]["Default"]["actions"] = [
            {
                "type": "windows_hdr",
                "params": {"enabled": True, "display": "primary"},
            },
            {
                "type": "source_filter_settings",
                "params": {
                    "source": "Capture",
                    "filter": "HDR Tone Map",
                    "settings": {"exposure": 1.0},
                    "overlay": True,
                },
            },
        ]

        self.assertEqual(validate_config(data), [])

    def test_invalid_host_actions_are_rejected(self):
        data = self.sample()
        data["schema_version"] = 7
        data["host_control"] = {
            "soundvolumeview_path": 42,
            "audio_timeout_seconds": 0,
        }
        data["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "app_audio_output",
                "params": {"device": "", "process": "", "roles": "gaming"},
            },
            {
                "type": "windows_hdr",
                "params": {"enabled": "yes", "display": "secondary"},
            },
            {
                "type": "source_filter_settings",
                "params": {
                    "source": "Capture",
                    "filter": "HDR",
                    "settings": [],
                },
            },
        ]

        errors = validate_config(data)

        self.assertTrue(any("soundvolumeview_path" in item for item in errors), errors)
        self.assertTrue(any("audio_timeout_seconds" in item for item in errors), errors)
        self.assertTrue(any(".params.device est requis" in item for item in errors), errors)
        self.assertTrue(any(".params.process est requis" in item for item in errors), errors)
        self.assertTrue(any(".params.roles" in item for item in errors), errors)
        self.assertTrue(any(".params.enabled doit être booléen" in item for item in errors), errors)
        self.assertTrue(any(".params.display" in item for item in errors), errors)
        self.assertTrue(any(".params.settings doit être un objet" in item for item in errors), errors)

    def test_random_activation_policy_is_valid_and_buildable(self):
        data = self.sample()
        data["activation_policies"] = {
            "[Module] EasterEgg": {
                "enabled": True,
                "type": "random",
                "active_when": "module_in_program_scene",
                "chance": 0.01,
                "interval_seconds": 60,
                "cooldown_seconds": 600,
                "default_duration_seconds": 10,
                "exclusive": True,
                "avoid_immediate_repeat": True,
                "targets": [
                    {
                        "container": "[Module] EasterEgg",
                        "source": "Cloud",
                        "enabled": True,
                        "weight": 1.0,
                        "duration_seconds": 10,
                    }
                ],
            }
        }

        self.assertEqual(validate_config(data), [])
        policies = build_activation_policies(data)
        self.assertEqual(policies["[Module] EasterEgg"].chance, 0.01)
        self.assertEqual(policies["[Module] EasterEgg"].targets[0].source, "Cloud")

    def test_invalid_activation_probability_and_weight_are_reported(self):
        data = self.sample()
        data["activation_policies"] = {
            "[Module] EasterEgg": {
                "type": "random",
                "chance": 1.5,
                "interval_seconds": 0,
                "cooldown_seconds": -1,
                "default_duration_seconds": 0,
                "targets": [
                    {
                        "container": "[Module] EasterEgg",
                        "source": "Cloud",
                        "weight": -0.1,
                    }
                ],
            }
        }

        errors = validate_config(data)
        self.assertTrue(any(".chance" in item for item in errors))
        self.assertTrue(any(".interval_seconds" in item for item in errors))
        self.assertTrue(any(".cooldown_seconds" in item for item in errors))
        self.assertTrue(any(".default_duration_seconds" in item for item in errors))
        self.assertTrue(any(".weight" in item for item in errors))

    def test_non_finite_activation_numbers_are_reported(self):
        for field, value in (
            ("chance", float("nan")),
            ("interval_seconds", float("inf")),
            ("cooldown_seconds", 1e309),
            ("default_duration_seconds", float("nan")),
        ):
            data = self.sample()
            policy = {
                "type": "random",
                "chance": 0.5,
                "interval_seconds": 10.0,
                "cooldown_seconds": 0.0,
                "default_duration_seconds": 2.0,
                "targets": [
                    {
                        "container": "Egg",
                        "source": "A",
                        "weight": 1.0,
                    }
                ],
            }
            policy[field] = value
            data["activation_policies"] = {"Egg": policy}
            errors = validate_config(data)
            self.assertTrue(
                any(f".{field}" in error for error in errors),
                (field, value, errors),
            )

        data = self.sample()
        data["activation_policies"] = {
            "Egg": {
                "type": "random",
                "targets": [
                    {
                        "container": "Egg",
                        "source": "A",
                        "weight": float("nan"),
                        "duration_seconds": float("inf"),
                    }
                ],
            }
        }
        errors = validate_config(data)
        self.assertTrue(any(".weight" in error for error in errors))
        self.assertTrue(any(".duration_seconds" in error for error in errors))

    def test_exact_duplicate_activation_targets_are_rejected(self):
        data = self.sample()
        target = {
            "container": "Egg",
            "container_kind": "scene",
            "source": "Cloud",
            "path": ["Gameplay", "Egg"],
        }
        data["activation_policies"] = {
            "Egg": {
                "type": "random",
                "targets": [dict(target), dict(target)],
            }
        }

        errors = validate_config(data)

        self.assertTrue(any("duplique exactement" in error for error in errors))

    def test_ancestor_descendant_activation_targets_are_rejected(self):
        data = self.sample()
        data["activation_policies"] = {
            "Egg": {
                "type": "random",
                "targets": [
                    {
                        "container": "Egg",
                        "container_kind": "scene",
                        "source": "ChildScene",
                        "path": ["Gameplay", "Egg"],
                    },
                    {
                        "container": "ChildScene",
                        "container_kind": "scene",
                        "source": "Inner",
                        "path": ["Gameplay", "Egg", "ChildScene"],
                    },
                ],
            }
        }

        errors = validate_config(data)

        self.assertTrue(any("ancêtre/descendant" in error for error in errors))

    def test_cross_policy_activation_target_ownership_is_rejected(self):
        data = self.sample()
        target = {"container": "Egg", "container_kind": "scene", "source": "Cloud"}
        data["activation_policies"] = {
            "One": {"type": "random", "targets": [dict(target)]},
            "Two": {"type": "random", "targets": [dict(target)]},
        }
        errors = validate_config(data)
        self.assertTrue(any("partage la cible" in error for error in errors), errors)

    def test_runtime_visibility_marker_can_be_explicitly_released(self):
        data = self.sample()
        data["layout_profiles"]["Vanilla"] = {
            "scene": "Gameplay",
            "modules": {
                "Egg": {
                    "base_bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
                    "geometry": {"x": 0, "y": 0, "width": 100, "height": 100},
                    "elements": [{
                        "container": "Egg",
                        "source": "Cloud",
                        "transform": {},
                        "visibility_owner": "runtime",
                        "follow_visibility": False,
                    }],
                }
            },
            "support_items": [{
                "container": "Egg",
                "source": "Cloud",
                "transform": {},
                "visibility_owner": "runtime",
            }],
        }

        changed = release_runtime_visibility_ownership(data, container="Egg", source="Cloud")

        self.assertEqual(changed, 2)
        element = data["layout_profiles"]["Vanilla"]["modules"]["Egg"]["elements"][0]
        self.assertEqual(element["visibility_owner"], "")
        self.assertTrue(element["follow_visibility"])

    def test_single_legacy_deep_target_remains_valid(self):
        data = self.sample()
        data["activation_policies"] = {
            "Egg": {
                "type": "random",
                "targets": [
                    {
                        "container": "ChildScene",
                        "container_kind": "scene",
                        "source": "Inner",
                        "path": ["Gameplay", "Egg", "ChildScene"],
                        "weight": 0.0,
                    }
                ],
            }
        }

        self.assertEqual(validate_config(data), [])


    def test_non_finite_layout_numbers_are_rejected(self):
        for key, value in (("x", float("nan")), ("width", float("inf"))):
            data = self.sample()
            data["layout_profiles"]["Vanilla"] = {
                "scene": "Gameplay",
                "modules": {
                    "Webcam": {
                        "base_bounds": {"x": 0, "y": 0, "width": 100, "height": 100},
                        "geometry": {"x": 0, "y": 0, "width": 100, "height": 100},
                        "elements": [],
                    }
                },
            }
            data["layout_profiles"]["Vanilla"]["modules"]["Webcam"]["geometry"][key] = value
            errors = validate_config(data)
            self.assertTrue(any(f".geometry.{key}" in error for error in errors), errors)

    def test_process_running_rule_is_valid_without_foreground_selector(self):
        data = self.sample()
        data["profiles"]["game"]["Dofus"] = {"actions": []}
        data["rules"] = [
            {
                "name": "Dofus background",
                "behavior": "match",
                "priority": 100,
                "enabled": True,
                "exe": "",
                "path": "",
                "title_regex": "",
                "state": {
                    "Game": "Dofus",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Vanilla",
                },
                "conditions": {"process_running": "Dofus.exe"},
                "apply_delay_ms": 0,
            }
        ]

        self.assertEqual(validate_config(data), [])

    def test_invalid_rule_regex_is_rejected(self):
        data = self.sample()
        data["rules"][1]["title_regex"] = "([unterminated"
        errors = validate_config(data)
        self.assertTrue(any("title_regex est invalide" in error for error in errors), errors)

    def test_action_parameters_are_validated(self):
        data = self.sample()
        data["profiles"]["game"]["Game"]["actions"] = [
            {"type": "scene_item_enabled", "params": {"scene": "", "source": "X", "enabled": "yes"}},
            {"type": "input_volume_db", "params": {"input": "Music", "volume_db": float("nan")}},
        ]
        errors = validate_config(data)
        self.assertTrue(any(".params.scene est requis" in error for error in errors), errors)
        self.assertTrue(any(".params.enabled doit être booléen" in error for error in errors), errors)
        self.assertTrue(any(".params.volume_db doit être un nombre fini" in error for error in errors), errors)

    def test_non_finite_obs_and_transition_values_are_rejected(self):
        data = self.sample()
        data["obs"]["timeout_seconds"] = float("inf")
        data["obs"]["reconnect_seconds"] = float("nan")
        data["layout_profiles"]["Vanilla"]["transition"] = {
            "mode": "move",
            "duration_ms": float("inf"),
            "steps": 0,
        }
        errors = validate_config(data)
        self.assertTrue(any("obs.timeout_seconds" in error for error in errors), errors)
        self.assertTrue(any("obs.reconnect_seconds" in error for error in errors), errors)
        self.assertTrue(any("transition.duration_ms" in error for error in errors), errors)
        self.assertTrue(any("transition.steps" in error for error in errors), errors)

    def test_condition_types_are_rejected_when_not_boolean(self):
        data = self.sample()
        data["profiles"]["game"]["Game"]["conditions"] = {"streaming": "true"}
        errors = validate_config(data)
        self.assertTrue(any(".conditions.streaming doit être booléen" in error for error in errors), errors)



    def test_launcher_and_preapply_flag_round_trip_through_config(self):
        data = self.sample()
        data["rules"][1]["launcher"] = "Battle.net.exe"
        data["profiles"]["game"]["Game"]["actions"] = [
            {
                "type": "wait_ms",
                "name": "pre-launch",
                "enabled": True,
                "preapply_on_launcher": True,
                "params": {"duration_ms": 0},
            }
        ]

        self.assertEqual(validate_config(data), [])
        rules, _poll, _debounce, _fallback = build_ruleset(data)
        game_rule = next(rule for rule in rules.rules if rule.name == "Game")
        self.assertEqual(game_rule.launcher, "Battle.net.exe")

    def test_launcher_and_preapply_types_are_validated(self):
        data = self.sample()
        data["rules"][1]["launcher"] = 123
        data["profiles"]["game"]["Game"]["actions"] = [
            {
                "type": "wait_ms",
                "preapply_on_launcher": "yes",
                "params": {"duration_ms": 0},
            }
        ]

        errors = validate_config(data)

        self.assertTrue(
            any(".launcher doit être une chaîne" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any(
                ".preapply_on_launcher doit être booléen" in error
                for error in errors
            ),
            errors,
        )

if __name__ == "__main__":
    unittest.main()
