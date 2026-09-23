from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from stream_state_router.importers.scene_collection import (
    ImportedFilter,
    ImportedInput,
    ImportedSceneItem,
    SceneCollectionSnapshot,
)
from stream_state_router.services.config_insights import (
    apply_reference_repairs,
    build_capability_report,
    build_config_change_review,
    build_effective_dependency_tree,
    build_effective_provenance,
    build_static_health_findings,
    detach_profile_inheritance,
    live_output_active,
    profile_content_entries,
    profile_usages,
    scan_obs_reference_repairs,
    simulate_rule_scenario,
)


def _config() -> dict:
    return json.loads(
        Path("config/default.json").read_text(encoding="utf-8")
    )


def _snapshot() -> SceneCollectionSnapshot:
    return SceneCollectionSnapshot(
        collection="Streaming",
        current_program_scene="In Game",
        scenes=("In Game", "Just Chatting"),
        inputs=(
            ImportedInput(
                name="Game Capture",
                kind="game_capture",
                uuid="input-1",
                settings={},
                muted=False,
                volume_db=0.0,
            ),
            ImportedInput(
                name="Micro",
                kind="wasapi_input_capture",
                uuid="input-2",
                settings={},
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
                settings={},
            ),
            ImportedFilter(
                source="Micro",
                name="Noise Gate",
                kind="noise_gate_filter",
                enabled=True,
                settings={},
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
                scene="In Game",
                source="Webcam",
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
        warnings=(),
    )


class ConfigInsightsTests(unittest.TestCase):
    def test_profile_usages_reports_rule_fallback_and_inheritance(self) -> None:
        config = _config()
        config["profiles"]["game"]["Child"] = {
            "actions": [],
            "extends": "Vanilla",
            "conditions": {},
        }

        usages = profile_usages(config, "game", "Vanilla")
        kinds = {item.kind for item in usages}

        self.assertIn("fallback", kinds)
        self.assertIn("inheritance", kinds)

    def test_detach_action_profile_preserves_effective_sequence_and_conditions(self) -> None:
        config = _config()
        config["profiles"]["game"]["Base"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Parent",
                    "enabled": True,
                    "params": {"duration_ms": 10},
                }
            ],
            "extends": "",
            "conditions": {"streaming": True},
        }
        config["profiles"]["game"]["Overwatch"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Child",
                    "enabled": True,
                    "params": {"duration_ms": 20},
                }
            ],
            "extends": "Base",
            "conditions": {"program_scene": "In Game"},
        }

        before = profile_content_entries(config, "game", "Overwatch")
        draft, changed = detach_profile_inheritance(
            config,
            "game",
            "Overwatch",
        )
        after = profile_content_entries(draft, "game", "Overwatch")

        self.assertTrue(changed)
        self.assertEqual(
            [entry.name for entry in before],
            [entry.name for entry in after],
        )
        detached = draft["profiles"]["game"]["Overwatch"]
        self.assertEqual(detached["extends"], "")
        self.assertEqual(
            detached["conditions"],
            {"streaming": True, "program_scene": "In Game"},
        )
        self.assertEqual(config["profiles"]["game"]["Overwatch"]["extends"], "Base")

    def test_detach_layout_profile_preserves_resolved_layout(self) -> None:
        config = _config()
        config["layout_profiles"]["Base"] = {
            "scene": "In Game",
            "modules": {
                "A": {
                    "base_bounds": {
                        "x": 0,
                        "y": 0,
                        "width": 100,
                        "height": 100,
                    },
                    "geometry": {
                        "x": 0,
                        "y": 0,
                        "width": 100,
                        "height": 100,
                    },
                    "elements": [],
                }
            },
            "extends": "",
            "coordinate_mode": "normalized",
            "conditions": {},
            "transition": {"mode": "instant", "duration_ms": 0, "steps": 8},
        }
        config["layout_profiles"]["Child"] = {
            "scene": "",
            "modules": {
                "A": {
                    "geometry": {
                        "x": 20,
                        "y": 30,
                        "width": 100,
                        "height": 100,
                    }
                }
            },
            "extends": "Base",
            "coordinate_mode": "normalized",
            "conditions": {},
            "transition": {"mode": "instant", "duration_ms": 0, "steps": 8},
        }

        from stream_state_router.obs.layouts import resolve_layout_profile

        before = resolve_layout_profile(
            "Child",
            config["layout_profiles"],
        )
        draft, changed = detach_profile_inheritance(
            config,
            "layout",
            "Child",
        )
        after = resolve_layout_profile(
            "Child",
            draft["layout_profiles"],
        )

        self.assertTrue(changed)
        self.assertEqual(
            {key: value for key, value in before.items() if key != "extends"},
            {key: value for key, value in after.items() if key != "extends"},
        )
        self.assertEqual(
            draft["layout_profiles"]["Child"]["extends"],
            "",
        )

    def test_profile_content_entries_identify_parent_and_local_ownership(self) -> None:
        config = _config()
        config["profiles"]["game"]["Base"] = {
            "actions": [
                {
                    "type": "set_program_scene",
                    "name": "Parent scene",
                    "enabled": True,
                    "params": {"scene": "In Game"},
                }
            ],
            "extends": "",
            "conditions": {},
        }
        config["profiles"]["game"]["Overwatch"]["extends"] = "Base"
        config["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "wait_ms",
                "name": "Local delay",
                "enabled": True,
                "params": {"duration_ms": 25},
            }
        ]

        entries = profile_content_entries(
            config,
            "game",
            "Overwatch",
        )

        self.assertEqual(
            [entry.source_profile for entry in entries],
            ["Base", "Overwatch"],
        )
        self.assertEqual(entries[0].target, "In Game")
        self.assertEqual(entries[1].target, "25 ms")

    def test_change_review_reports_rules_profiles_layouts_and_settings(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)

        draft["router"]["debounce_ms"] = 250
        draft["obs"]["password"] = "super-secret"
        draft["rules"][0]["priority"] = 999
        draft["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "wait_ms",
                "name": "Delay",
                "enabled": True,
                "params": {"duration_ms": 25},
            }
        ]
        draft["layout_profiles"]["Vanilla"]["scene"] = "In Game"

        review = build_config_change_review(saved, draft)

        self.assertTrue(review.has_changes)
        categories = {item.category for item in review.changes}
        self.assertIn("Routage", categories)
        self.assertIn("OBS", categories)
        self.assertIn("Règles", categories)
        self.assertIn("Profils", categories)
        self.assertIn("Layouts", categories)
        password = next(
            item
            for item in review.changes
            if item.target == "Mot de passe WebSocket"
        )
        self.assertIn("sensible", password.detail)
        self.assertNotIn("super-secret", password.detail)

    def test_change_review_reports_shared_profile_impact(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)
        draft["rules"].append(
            {
                **copy.deepcopy(draft["rules"][0]),
                "name": "Overwatch secondary",
                "priority": 50,
            }
        )
        draft["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "wait_ms",
                "name": "Delay",
                "enabled": True,
                "params": {"duration_ms": 10},
            }
        ]

        review = build_config_change_review(saved, draft)

        profile_change = next(
            item
            for item in review.changes
            if item.target == "Jeu / Overwatch"
        )
        self.assertIn("Overwatch", profile_change.impact)
        self.assertIn("Overwatch secondary", profile_change.impact)

    def test_change_review_noop_is_empty(self) -> None:
        saved = _config()

        review = build_config_change_review(
            saved,
            copy.deepcopy(saved),
        )

        self.assertFalse(review.has_changes)
        self.assertEqual(review.changes, ())
        self.assertIn("Aucune modification", review.summary)

    def test_change_review_surfaces_draft_validation_errors(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)
        draft["rules"][0]["state"]["Game"] = "Missing profile"

        review = build_config_change_review(saved, draft)

        self.assertTrue(review.validation_errors)
        self.assertIn("erreur", review.summary)

    def test_change_review_does_not_expose_control_variable_values(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)
        saved["control_variables"] = {"TokenLike": "old-secret"}
        draft["control_variables"] = {"TokenLike": "new-secret"}

        review = build_config_change_review(saved, draft)

        change = next(
            item
            for item in review.changes
            if item.target == "Variables de contrôle"
        )
        self.assertIn("TokenLike", change.detail)
        self.assertNotIn("old-secret", change.detail)
        self.assertNotIn("new-secret", change.detail)

    def test_dependency_tree_tracks_decision_domains_and_content_origin(self) -> None:
        config = _config()
        config["profiles"]["game"]["Base"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Base delay",
                    "enabled": True,
                    "params": {"duration_ms": 10},
                }
            ],
            "extends": "",
            "conditions": {},
        }
        config["profiles"]["game"]["Overwatch"]["extends"] = "Base"
        explanation = {
            "routing": {
                "kind": "match",
                "rule_name": "Overwatch",
                "effective_state": config["rules"][0]["state"],
            }
        }

        tree = build_effective_dependency_tree(config, explanation)

        self.assertEqual(tree.label, "Décision courante")
        self.assertEqual(tree.value, "Règle « Overwatch »")
        game = next(node for node in tree.children if node.label == "Jeu")
        self.assertEqual(game.value, "Overwatch")
        origins = {node.value: node for node in game.children}
        self.assertIn("Base", origins)
        self.assertIn("Overwatch", origins)
        self.assertTrue(
            any(
                child.value == "Base delay"
                for child in origins["Base"].children
            )
        )

    def test_effective_provenance_includes_lineage_and_selection_source(self) -> None:
        config = _config()
        config["profiles"]["game"]["Base OW"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Base",
                    "enabled": True,
                    "params": {"duration_ms": 1},
                }
            ],
            "extends": "",
            "conditions": {},
        }
        config["profiles"]["game"]["Overwatch"]["extends"] = "Base OW"
        explanation = {
            "routing": {
                "kind": "match",
                "rule_name": "Overwatch",
                "effective_state": config["rules"][0]["state"],
            }
        }

        rows = build_effective_provenance(config, explanation)
        game = next(row for row in rows if row.domain == "game")

        self.assertEqual(game.profile, "Overwatch")
        self.assertEqual(game.selected_by, "Règle « Overwatch »")
        self.assertEqual(game.lineage, ("Overwatch", "Base OW"))
        self.assertIn("1 action", game.content_summary)

    def test_static_health_finds_duplicate_rules_and_orphan_profile(self) -> None:
        config = _config()
        duplicate = copy.deepcopy(config["rules"][0])
        duplicate["name"] = "Overwatch duplicate"
        config["rules"].append(duplicate)
        config["profiles"]["game"]["Unused"] = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }

        findings = build_static_health_findings(config)

        self.assertTrue(
            any(
                item.title == "Règles potentiellement redondantes"
                for item in findings
            )
        )
        self.assertTrue(
            any(
                item.title == "Profil Jeu non référencé"
                and item.detail == "Unused"
                for item in findings
            )
        )

    def test_static_health_finds_same_priority_overlap(self) -> None:
        config = _config()
        left = copy.deepcopy(config["rules"][0])
        left["name"] = "Overwatch streaming"
        left["priority"] = 100
        left["conditions"] = {"streaming": True}
        right = copy.deepcopy(config["rules"][0])
        right["name"] = "Overwatch scene"
        right["priority"] = 100
        right["conditions"] = {"program_scene": "In Game"}
        config["rules"] = [left, right]

        findings = build_static_health_findings(config)

        self.assertTrue(
            any(
                item.title == "Règles concurrentes à même priorité"
                for item in findings
            ),
            findings,
        )

    def test_static_health_does_not_flag_mutually_exclusive_conditions(self) -> None:
        config = _config()
        left = copy.deepcopy(config["rules"][0])
        left["name"] = "Streaming"
        left["priority"] = 100
        left["conditions"] = {"streaming": True}
        right = copy.deepcopy(config["rules"][0])
        right["name"] = "Not streaming"
        right["priority"] = 100
        right["conditions"] = {"streaming": False}
        config["rules"] = [left, right]

        findings = build_static_health_findings(config)

        self.assertFalse(
            any(
                item.title == "Règles concurrentes à même priorité"
                for item in findings
            ),
            findings,
        )

    def test_live_output_active_for_stream_or_recording(self) -> None:
        self.assertFalse(live_output_active({}))
        self.assertFalse(
            live_output_active(
                {"streaming": False, "recording": False}
            )
        )
        self.assertTrue(live_output_active({"streaming": True}))
        self.assertTrue(live_output_active({"recording": True}))
        self.assertTrue(
            live_output_active(
                {"streaming": True, "recording": True}
            )
        )

    def test_capability_report_reflects_used_host_features(self) -> None:
        config = _config()
        config["profiles"]["audio"]["Game"]["actions"] = [
            {
                "type": "app_audio_output",
                "name": "Route game",
                "enabled": True,
                "params": {
                    "device": "Headphones",
                    "process": "Overwatch.exe",
                    "roles": "all",
                },
            }
        ]
        config["profiles"]["capture"]["HDR"]["actions"] = [
            {
                "type": "windows_hdr",
                "name": "HDR on",
                "enabled": True,
                "params": {"enabled": True, "display": "primary"},
            }
        ]

        report = build_capability_report(
            config,
            obs_enabled=True,
            obs_connected=True,
            catalog_status={
                "available": True,
                "stale": False,
                "scenes": 4,
                "inputs": 12,
                "scene_items": 30,
            },
            audio_probe={
                "status": "ready",
                "detail": "SoundVolumeView trouvé.",
            },
            hdr_probe={
                "status": "ready",
                "detail": "HDR pris en charge.",
            },
        )

        statuses = {item.key: item.status for item in report.items}
        self.assertEqual(statuses["obs"], "ready")
        self.assertEqual(statuses["catalog"], "ready")
        self.assertEqual(statuses["audio"], "ready")
        self.assertEqual(statuses["hdr"], "ready")

    def test_scenario_simulator_reuses_rule_matching_and_conditions(self) -> None:
        config = _config()
        config["rules"][0]["conditions"] = {
            "streaming": True,
            "program_scene": "In Game",
        }

        matching = simulate_rule_scenario(
            config,
            exe="Overwatch.exe",
            streaming=True,
            program_scene="In Game",
            obs_enabled=True,
        )
        blocked = simulate_rule_scenario(
            config,
            exe="Overwatch.exe",
            streaming=False,
            program_scene="In Game",
            obs_enabled=True,
        )

        self.assertEqual(matching.kind, "match")
        self.assertEqual(matching.rule_name, "Overwatch")
        self.assertEqual(matching.state["Game"], "Overwatch")
        self.assertEqual(blocked.kind, "fallback")
        self.assertTrue(
            any(
                check.name == "Overwatch"
                and not check.matched
                and "streaming" in check.reason
                for check in blocked.checks
            )
        )

    def test_scenario_simulator_previews_effective_profile_content(self) -> None:
        config = _config()
        config["profiles"]["game"]["Base"] = {
            "actions": [
                {
                    "type": "wait_ms",
                    "name": "Base delay",
                    "enabled": True,
                    "params": {"duration_ms": 10},
                }
            ],
            "extends": "",
            "conditions": {},
        }
        config["profiles"]["game"]["Overwatch"]["extends"] = "Base"
        config["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "set_program_scene",
                "name": "Gameplay scene",
                "enabled": True,
                "params": {"scene": "In Game"},
            }
        ]

        report = simulate_rule_scenario(
            config,
            exe="Overwatch.exe",
        )

        game = next(
            item for item in report.domains if item.domain == "game"
        )
        self.assertTrue(game.exists)
        self.assertEqual(game.profile, "Overwatch")
        self.assertEqual(game.lineage, ("Overwatch", "Base"))
        self.assertEqual(
            [entry.source_profile for entry in game.entries],
            ["Base", "Overwatch"],
        )
        self.assertEqual(
            [entry.name for entry in game.entries],
            ["Base delay", "Gameplay scene"],
        )

    def test_scenario_simulator_marks_missing_profile_without_crashing(self) -> None:
        config = _config()
        config["rules"][0]["state"]["CaptureProfile"] = "Missing HDR"

        report = simulate_rule_scenario(
            config,
            exe="Overwatch.exe",
        )

        capture = next(
            item for item in report.domains if item.domain == "capture"
        )
        self.assertFalse(capture.exists)
        self.assertEqual(capture.profile, "Missing HDR")
        self.assertEqual(capture.content_summary, "Profil introuvable")
        self.assertEqual(capture.entries, ())

    def test_scenario_simulator_supports_background_process_rules(self) -> None:
        config = _config()
        config["rules"].insert(
            0,
            {
                "name": "Discord running",
                "behavior": "match",
                "priority": 500,
                "enabled": True,
                "exe": "",
                "path": "",
                "title_regex": "",
                "state": {
                    "Game": "Vanilla",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Vanilla",
                },
                "apply_delay_ms": 0,
                "conditions": {
                    "process_running": "Discord.exe",
                },
            },
        )

        report = simulate_rule_scenario(
            config,
            running_processes=("Discord.exe",),
            obs_enabled=True,
        )

        self.assertEqual(report.kind, "match")
        self.assertEqual(report.rule_name, "Discord running")

    def test_reference_scan_suggests_scene_input_filter_and_source_repairs(self) -> None:
        config = _config()
        config["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "set_program_scene",
                "name": "Scene",
                "enabled": True,
                "params": {"scene": "In Gmae"},
            },
            {
                "type": "input_mute",
                "name": "Mic",
                "enabled": True,
                "params": {"input": "Microo", "muted": False},
            },
            {
                "type": "source_filter_enabled",
                "name": "Tone",
                "enabled": True,
                "params": {
                    "source": "Game Captur",
                    "filter": "HDR Tone Mapp",
                    "enabled": True,
                },
            },
        ]

        issues = scan_obs_reference_repairs(config, _snapshot())
        by_current = {item.current: item for item in issues}

        self.assertEqual(by_current["In Gmae"].candidate, "In Game")
        self.assertEqual(by_current["Microo"].candidate, "Micro")
        self.assertEqual(by_current["Game Captur"].candidate, "Game Capture")
        self.assertEqual(by_current["HDR Tone Mapp"].candidate, "HDR Tone Map")

    def test_apply_reference_repairs_is_copy_on_write_and_checks_current_value(self) -> None:
        config = _config()
        config["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "set_program_scene",
                "name": "Scene",
                "enabled": True,
                "params": {"scene": "In Gmae"},
            }
        ]
        before = copy.deepcopy(config)
        repairs = scan_obs_reference_repairs(config, _snapshot())

        draft, applied = apply_reference_repairs(config, repairs)

        self.assertEqual(applied, 1)
        self.assertEqual(
            draft["profiles"]["game"]["Overwatch"]["actions"][0]["params"]["scene"],
            "In Game",
        )
        self.assertEqual(config, before)

    def test_reference_scan_does_not_invent_ambiguous_candidate(self) -> None:
        config = _config()
        config["profiles"]["game"]["Overwatch"]["actions"] = [
            {
                "type": "set_program_scene",
                "name": "Scene",
                "enabled": True,
                "params": {"scene": "Chat"},
            }
        ]
        snapshot = SceneCollectionSnapshot(
            collection="Streaming",
            current_program_scene="",
            scenes=("Chat A", "Chat B"),
            inputs=(),
            filters=(),
            scene_items=(),
            warnings=(),
        )

        issues = scan_obs_reference_repairs(config, snapshot)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].candidate, "")


if __name__ == "__main__":
    unittest.main()
