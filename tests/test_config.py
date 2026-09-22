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
    load_config,
    migrate_config,
    save_config,
    validate_config,
    config_revision,
    release_runtime_visibility_ownership,
)


class ConfigTests(unittest.TestCase):
    def sample(self):
        return {
            "schema_version": 5,
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

    def test_schema_v1_is_migrated_with_layout_profiles(self):
        data = self.sample()
        data["schema_version"] = 1
        data.pop("layout_profiles")
        data["router"]["fallback_state"].pop("LayoutProfile", None)
        data["rules"][1]["state"].pop("LayoutProfile", None)
        data["rules"][1]["state"]["OverlayProfile"] = "Vanilla"

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 5)
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

        self.assertEqual(migrated["schema_version"], 5)
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

    def test_config_revision_is_stable_and_changes_with_content(self):
        data = self.sample()
        first = config_revision(data)
        second = config_revision(copy.deepcopy(data))
        changed = copy.deepcopy(data)
        changed["router"]["poll_ms"] += 1

        self.assertEqual(first, second)
        self.assertNotEqual(first, config_revision(changed))
        self.assertEqual(len(first), 12)

    def test_schema_v4_adds_activation_policies(self):
        data = self.sample()
        data["schema_version"] = 4
        data.pop("activation_policies", None)

        migrated = migrate_config(data)

        self.assertEqual(migrated["schema_version"], 5)
        self.assertEqual(migrated["activation_policies"], {})
        self.assertEqual(validate_config(migrated), [])

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


if __name__ == "__main__":
    unittest.main()
