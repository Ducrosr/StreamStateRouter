from __future__ import annotations

import unittest
from types import SimpleNamespace

from stream_state_router.obs.dispatcher import OBSDispatcher, profile_map_from_raw
from stream_state_router.router.engine import StateChange
from stream_state_router.obs.models import OBSAction
from stream_state_router.router.models import ForegroundApp, StreamState


class FakeHostController:
    def __init__(self):
        self.calls = []

    def execute(self, kind, params):
        self.calls.append((kind, dict(params)))
        return True


class FakeClient:
    def __init__(self):
        self.calls = []
        self.scene_item_ids = [42]
        self.streaming = False
        self.config = SimpleNamespace(enabled=True)

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetSceneItemId":
            if len(self.scene_item_ids) > 1:
                return {"sceneItemId": self.scene_item_ids.pop(0)}
            return {"sceneItemId": self.scene_item_ids[0]}
        if request == "GetStreamStatus":
            return {"outputActive": self.streaming}
        if request == "GetRecordStatus":
            return {"outputActive": False}
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "OW"}
        return {}


class OBSDispatcherTests(unittest.TestCase):
    def test_obs_context_checks_shutdown_between_requests(self):
        client = FakeClient()
        client.config = SimpleNamespace(enabled=True)
        dispatcher = OBSDispatcher(client, {})
        checkpoints = []

        def checkpoint():
            checkpoints.append(len(client.calls))
            if len(checkpoints) == 2:
                raise RuntimeError("shutdown requested")

        dispatcher.set_cooperative_yield(checkpoint)

        with self.assertRaisesRegex(RuntimeError, "shutdown requested"):
            dispatcher.obs_context()

        self.assertEqual([request for request, _ in client.calls], ["GetStreamStatus"])

    def test_only_changed_profile_domains_are_dispatched(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Vanilla": {"actions": []},
                    "Overwatch": {"actions": [{"type": "set_program_scene", "params": {"scene": "OW"}}]},
                },
                "overlay": {
                    "FPS": {"actions": [{"type": "scene_item_enabled", "params": {"scene": "OW", "source": "FPS", "enabled": True}}]},
                },
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        state = StreamState("Overwatch", "FPS", "Default", "Default")
        first = dispatcher.dispatch_state(state)
        second = dispatcher.dispatch_state(state)
        self.assertEqual(first.executed, 2)
        self.assertEqual(second.executed, 0)
        self.assertEqual(len(client.calls), 3)  # scene change + lookup + scene item enabled

    def test_obs_context_prefers_modern_program_scene_field(self):
        client = FakeClient()
        original_send = client.send

        def send(request, data=None):
            if request == "GetCurrentProgramScene":
                client.calls.append((request, data))
                return {
                    "sceneName": "Modern",
                    "currentProgramSceneName": "Legacy",
                }
            return original_send(request, data)

        client.send = send
        dispatcher = OBSDispatcher(client, {})

        context = dispatcher.obs_context(force_refresh=True)

        self.assertEqual(context["program_scene"], "Modern")

    def test_scene_item_id_is_resolved_fresh_for_each_visibility_action(self):
        client = FakeClient()
        client.scene_item_ids = [42, 99]
        dispatcher = OBSDispatcher(client, {})
        action = OBSAction("scene_item_enabled", {"scene": "A", "source": "B", "enabled": True})

        dispatcher.execute_action(action)
        dispatcher.execute_action(action)

        lookups = [call for call in client.calls if call[0] == "GetSceneItemId"]
        writes = [call for call in client.calls if call[0] == "SetSceneItemEnabled"]
        self.assertEqual(len(lookups), 2)
        self.assertEqual([call[1]["sceneItemId"] for call in writes], [42, 99])

    def test_dispatch_change_uses_applied_state_not_router_previous_state(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "A": {"actions": [{"type": "set_program_scene", "params": {"scene": "A"}}]},
                    "B": {"actions": [{"type": "set_program_scene", "params": {"scene": "B"}}]},
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        state_a = StreamState(overlay_profile="A")
        state_b = StreamState(overlay_profile="B")

        dispatcher.dispatch_state(state_a)
        client.calls.clear()
        change = StateChange(
            previous=state_b,
            current=state_b,
            reason="delayed replacement",
            rule_name="B",
            app=None,
        )
        result = dispatcher.dispatch_change(change)

        self.assertIn("overlay", result.changed_domains)
        self.assertTrue(
            any(request == "SetCurrentProgramScene" and payload["sceneName"] == "B" for request, payload in client.calls)
        )
        self.assertEqual(dispatcher.applied_profiles().get("overlay"), "B")

    def test_blocked_domain_stays_pending_and_retries_when_condition_changes(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Live": {
                        "conditions": {"streaming": True},
                        "actions": [{"type": "set_program_scene", "params": {"scene": "Live"}}],
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        state = StreamState(game="Live")

        first = dispatcher.dispatch_state(state)
        self.assertIn("game", dispatcher.pending_domains(state))
        self.assertTrue(any(s.status == "blocked" for s in first.domain_statuses))

        client.streaming = True
        dispatcher._context_cache = None
        second = dispatcher.dispatch_state(state)

        self.assertNotIn("game", dispatcher.pending_domains(state))
        self.assertTrue(any(s.status == "applied" for s in second.domain_statuses))

    def test_manual_layout_hold_survives_session_invalidation(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(
            client,
            {},
            {
                "A": {"scene": "OW", "modules": {}},
                "B": {"scene": "OW", "modules": {}},
            },
        )
        state_a = StreamState(layout_profile="A")
        state_b = StreamState(layout_profile="B")

        dispatcher.set_manual_layout_hold("A")
        dispatcher.invalidate_applied_state()

        self.assertNotIn("layout", dispatcher.pending_domains(state_a))
        self.assertIn("layout", dispatcher.pending_domains(state_b))

        plan = dispatcher.plan_state(
            state_a,
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "OW",
            },
        )
        layout = next(row for row in plan["domains"] if row["domain"] == "layout")
        self.assertFalse(layout["needs_apply"])
        self.assertEqual(layout["status"], "held")
        self.assertIn(
            {
                "provenance": "layout:A",
                "reason": "LayoutProfile maintenu manuellement",
            },
            plan["declarative_blocks"],
        )

    def test_manual_layout_hold_releases_when_routing_wants_another_layout(self):
        dispatcher = OBSDispatcher(FakeClient(), {})
        state_a = StreamState(layout_profile="A")
        state_b = StreamState(layout_profile="B")

        dispatcher.set_manual_layout_hold("A")
        self.assertNotIn("layout", dispatcher.pending_domains(state_a))

        # Only a genuine router state change releases the manual divergence;
        # periodic reconciliation of the same state must never do so.
        dispatcher.dispatch_change(
            StateChange(
                previous=state_a,
                current=state_b,
                reason="foreground",
                rule_name="B",
                app=None,
            )
        )

        self.assertIn("layout", dispatcher.pending_domains(state_a))

    def test_manual_layout_hold_with_unknown_baseline_adopts_first_routed_layout(self):
        dispatcher = OBSDispatcher(FakeClient(), {})
        state_a = StreamState(layout_profile="A")

        dispatcher.set_manual_layout_hold("")
        dispatcher.invalidate_applied_state()

        # Reconciliation alone cannot release an unknown-baseline manual hold.
        self.assertNotIn("layout", dispatcher.pending_domains(state_a))
        dispatcher.dispatch_state(state_a)
        self.assertNotIn("layout", dispatcher.pending_domains(state_a))

        # The first real routing decision establishes A as the baseline while
        # keeping the explicit manual layout visible.
        dispatcher.dispatch_change(
            StateChange(
                previous=None,
                current=state_a,
                reason="foreground",
                rule_name="A",
                app=None,
            )
        )
        self.assertEqual(dispatcher._manual_layout_routing_baseline, "A")
        self.assertNotIn("layout", dispatcher.pending_domains(state_a))

    def test_explicit_layout_apply_records_current_routing_baseline(self):
        dispatcher = OBSDispatcher(
            FakeClient(),
            {},
            {
                "A": {"scene": "OW", "modules": {}},
                "B": {"scene": "OW", "modules": {}},
            },
        )
        state_a = StreamState(layout_profile="A")
        dispatcher._desired_state = state_a
        dispatcher._layout_manager.apply_profile = lambda _profile: SimpleNamespace(
            elements_applied=1,
            elements_skipped=0,
            warnings=(),
            missing_sources=(),
        )

        dispatcher.execute_layout_profile("B")
        dispatcher.invalidate_applied_state()

        self.assertNotIn("layout", dispatcher.pending_domains(state_a))

    def test_read_only_plan_reuses_inheritance_and_performs_no_obs_calls(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Base": {
                        "actions": [
                            {"type": "input_mute", "params": {"input": "Mic", "muted": False}}
                        ]
                    },
                    "Child": {
                        "extends": "Base",
                        "conditions": {"streaming": True},
                        "actions": [
                            {"type": "set_program_scene", "params": {"scene": "Gameplay"}}
                        ],
                    },
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        before = len(client.calls)

        plan = dispatcher.plan_state(
            StreamState(game="Child"),
            context={
                "obs_enabled": True,
                "streaming": True,
                "recording": False,
                "program_scene": "Gameplay",
            },
        )

        self.assertEqual(len(client.calls), before)
        game = next(row for row in plan["domains"] if row["domain"] == "game")
        self.assertEqual(game["status"], "planned")
        self.assertEqual(
            [item["type"] for item in game["operations"]],
            ["input_mute", "set_program_scene"],
        )

    def test_read_only_plan_exposes_declarative_intent_from_same_resolution(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Base": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture",
                                    "settings": {"rgb10a2_space": "srgb"},
                                },
                            }
                        ]
                    },
                    "Overwatch": {
                        "extends": "Base",
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Input Overlay",
                                    "enabled": True,
                                },
                            }
                        ],
                    },
                }
            }
        )
        dispatcher = OBSDispatcher(
            client,
            profiles,
            {"OW": {"scene": "In Game", "modules": {}}},
        )

        plan = dispatcher.plan_state(
            StreamState(game="Overwatch", layout_profile="OW"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(client.calls, [])
        self.assertEqual(plan["declarative_blocks"], [])
        properties = plan["declarative_desired"]["properties"]
        identities = {
            (
                row["property"]["kind"],
                row["property"]["source"],
                row["property"]["setting"],
            )
            for row in properties
        }
        self.assertIn(("input_setting", "Capture", "rgb10a2_space"), identities)
        self.assertIn(("scene_item_visibility", "Input Overlay", ""), identities)
        self.assertIn(("layout_profile", "", ""), identities)

    def test_read_only_plan_reports_cross_domain_declarative_conflict(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "A": {
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
                    }
                },
                "overlay": {
                    "B": {
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Chat",
                                    "enabled": False,
                                },
                            }
                        ]
                    }
                },
            }
        )
        dispatcher = OBSDispatcher(client, profiles)

        plan = dispatcher.plan_state(
            StreamState(game="A", overlay_profile="B"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(client.calls, [])
        self.assertEqual(
            plan["declarative_error"]["code"],
            "property_ownership_conflict",
        )
        self.assertEqual(plan["declarative_desired"], {"properties": []})

    def test_resolve_desired_state_raises_same_cross_domain_conflict(self):
        profiles = profile_map_from_raw(
            {
                "game": {
                    "A": {
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Avatar",
                                    "filter": "Glitch",
                                    "enabled": True,
                                },
                            }
                        ]
                    }
                },
                "overlay": {
                    "B": {
                        "actions": [
                            {
                                "type": "source_filter_enabled",
                                "params": {
                                    "source": "Avatar",
                                    "filter": "Glitch",
                                    "enabled": False,
                                },
                            }
                        ]
                    }
                },
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)

        with self.assertRaisesRegex(ValueError, "multiple owners"):
            dispatcher.resolve_desired_state(
                StreamState(game="A", overlay_profile="B"),
                context={
                    "obs_enabled": True,
                    "streaming": False,
                    "recording": False,
                    "program_scene": "In Game",
                },
            )

    def test_layout_visibility_ownership_conflicts_with_action_profile(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "B": {
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
                    }
                }
            }
        )
        layout = {
            "scene": "In Game",
            "modules": {
                "Chat": {
                    "managed": True,
                    "locked": False,
                    "elements": [
                        {
                            "source": "Chat",
                            "container": "In Game",
                            "included": True,
                            "locked": False,
                            "follow_visibility": True,
                            "visibility_owner": "",
                        }
                    ],
                }
            },
        }
        dispatcher = OBSDispatcher(client, profiles, {"Layout": layout})

        plan = dispatcher.plan_state(
            StreamState(overlay_profile="B", layout_profile="Layout"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(
            plan["declarative_error"]["code"],
            "property_ownership_conflict",
        )
        self.assertIn("layout:Layout", plan["declarative_error"]["message"])
        self.assertIn("overlay:B", plan["declarative_error"]["message"])

    def test_runtime_owned_layout_visibility_is_reserved_against_action_profile(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "B": {
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Alert",
                                    "enabled": True,
                                },
                            }
                        ]
                    }
                }
            }
        )
        layout = {
            "scene": "In Game",
            "modules": {
                "Alert": {
                    "managed": True,
                    "locked": False,
                    "elements": [
                        {
                            "source": "Alert",
                            "container": "In Game",
                            "included": True,
                            "locked": False,
                            "follow_visibility": False,
                            "visibility_owner": "runtime",
                        }
                    ],
                }
            },
        }
        dispatcher = OBSDispatcher(client, profiles, {"Layout": layout})

        plan = dispatcher.plan_state(
            StreamState(overlay_profile="B", layout_profile="Layout"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(
            plan["declarative_error"]["code"],
            "property_ownership_conflict",
        )
        self.assertIn("runtime:visibility", plan["declarative_error"]["message"])
        self.assertIn("overlay:B", plan["declarative_error"]["message"])

    def test_runtime_owner_table_conflicts_without_persisted_marker(self):
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "B": {
                        "actions": [
                            {
                                "type": "scene_item_enabled",
                                "params": {
                                    "scene": "In Game",
                                    "source": "Alert",
                                    "enabled": True,
                                },
                            }
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)
        dispatcher.layout_manager.set_runtime_visibility_owners(
            [("In Game", "Alert")]
        )

        plan = dispatcher.plan_state(
            StreamState(overlay_profile="B"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(
            plan["declarative_error"]["code"],
            "property_ownership_conflict",
        )
        self.assertIn("runtime:visibility", plan["declarative_error"]["message"])

    def test_false_condition_blocks_even_when_profile_was_already_applied(self):
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "B": {
                        "conditions": {"streaming": True},
                        "actions": [
                            {
                                "type": "input_mute",
                                "params": {"input": "Mic", "muted": True},
                            }
                        ],
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)
        dispatcher._applied_profiles["overlay"] = "B"

        plan = dispatcher.plan_state(
            StreamState(overlay_profile="B"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(plan["domains"][1]["status"], "blocked")
        self.assertEqual(
            plan["declarative_blocks"],
            [
                {
                    "provenance": "overlay:B",
                    "reason": "conditions OBS non satisfaites",
                }
            ],
        )

    def test_unconfigured_default_domains_are_unmanaged_not_missing(self):
        dispatcher = OBSDispatcher(FakeClient(), {})

        plan = dispatcher.plan_state(
            StreamState(),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertEqual(plan["declarative_blocks"], [])
        self.assertTrue(
            all(row["status"] == "unmanaged" for row in plan["domains"])
        )

    def test_unconfigured_default_domains_are_not_pending(self):
        dispatcher = OBSDispatcher(FakeClient(), {})

        self.assertEqual(dispatcher.pending_domains(StreamState()), ())

    def test_force_reapply_reports_unmanaged_defaults_without_obs_writes(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(client, {})

        result = dispatcher.dispatch_state(StreamState(), force=True)

        self.assertEqual(client.calls, [])
        self.assertEqual(result.executed, 0)
        self.assertEqual(result.skipped, 5)
        self.assertEqual(
            [item.status for item in result.domain_statuses],
            ["unmanaged"] * 5,
        )
        self.assertEqual(result.warnings, ())

    def test_non_default_missing_profile_remains_pending_and_missing(self):
        dispatcher = OBSDispatcher(FakeClient(), {})
        state = StreamState(overlay_profile="Missing")

        self.assertIn("overlay", dispatcher.pending_domains(state))

        result = dispatcher.dispatch_state(state)
        overlay = next(
            item
            for item in result.domain_statuses
            if item.domain == "overlay"
        )

        self.assertEqual(overlay.status, "missing")
        self.assertIn("overlay", dispatcher.pending_domains(state))

    def test_populated_domain_does_not_implicitly_unmanage_default_name(self):
        profiles = profile_map_from_raw(
            {
                "overlay": {
                    "Other": {"actions": []},
                }
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)
        state = StreamState()

        self.assertIn("overlay", dispatcher.pending_domains(state))

        result = dispatcher.dispatch_state(state)
        overlay = next(
            item
            for item in result.domain_statuses
            if item.domain == "overlay"
        )

        self.assertEqual(overlay.status, "missing")

    def test_missing_profile_is_reported_as_declarative_block(self):
        dispatcher = OBSDispatcher(FakeClient(), {})

        plan = dispatcher.plan_state(
            StreamState(overlay_profile="Missing"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "In Game",
            },
        )

        self.assertIn(
            {
                "provenance": "overlay:Missing",
                "reason": "Profil OBS introuvable",
            },
            plan["declarative_blocks"],
        )

    def test_read_only_plan_reports_blocked_conditions_without_mutation(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Live": {
                        "conditions": {"streaming": True},
                        "actions": [{"type": "set_program_scene", "params": {"scene": "Live"}}],
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)

        plan = dispatcher.plan_state(
            StreamState(game="Live"),
            context={"obs_enabled": True, "streaming": False, "recording": False, "program_scene": "Idle"},
        )

        self.assertEqual(client.calls, [])
        game = next(row for row in plan["domains"] if row["domain"] == "game")
        self.assertEqual(game["status"], "blocked")
        self.assertEqual(
            plan["declarative_blocks"],
            [
                {
                    "provenance": "game:Live",
                    "reason": "conditions OBS non satisfaites",
                }
            ],
        )
        properties = plan["declarative_desired"]["properties"]
        self.assertTrue(
            any(
                row["property"]["kind"] == "program_scene"
                and row["value"] == "Live"
                for row in properties
            )
        )

    def test_supported_action_shapes(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(client, {})
        actions = [
            OBSAction("set_program_scene", {"scene": "Game"}),
            OBSAction("source_filter_enabled", {"source": "Game", "filter": "HDR", "enabled": False}),
            OBSAction("source_filter_settings", {"source": "Game", "filter": "HDR", "settings": {"exposure": 1.0}}),
            OBSAction("input_mute", {"input": "Mic", "muted": True}),
            OBSAction("input_volume_db", {"input": "Music", "volume_db": -12.5}),
            OBSAction("set_input_settings", {"input": "Text", "settings": {"text": "Hello"}, "overlay": True}),
        ]
        for action in actions:
            dispatcher.execute_action(action)
        self.assertEqual([r for r, _ in client.calls], [
            "SetCurrentProgramScene",
            "SetSourceFilterEnabled",
            "SetSourceFilterSettings",
            "SetInputMute",
            "SetInputVolume",
            "SetInputSettings",
        ])

    def test_filter_settings_action_uses_obs_request_with_overlay(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(client, {})

        dispatcher.execute_action(
            OBSAction(
                "source_filter_settings",
                {
                    "source": "Game",
                    "filter": "HDR Tone Map",
                    "settings": {"exposure": 1.25, "enabled": True},
                    "overlay": True,
                },
            )
        )

        self.assertEqual(
            client.calls,
            [
                (
                    "SetSourceFilterSettings",
                    {
                        "sourceName": "Game",
                        "filterName": "HDR Tone Map",
                        "filterSettings": {
                            "exposure": 1.25,
                            "enabled": True,
                        },
                        "overlay": True,
                    },
                )
            ],
        )

    def test_host_actions_are_delegated_outside_obs(self):
        client = FakeClient()
        host = FakeHostController()
        dispatcher = OBSDispatcher(client, {}, host_controller=host)

        dispatcher.execute_action(
            OBSAction(
                "app_audio_output",
                {
                    "device": "Game",
                    "process": "Dofus.exe",
                    "roles": "all",
                },
            )
        )
        dispatcher.execute_action(
            OBSAction(
                "windows_hdr",
                {"enabled": True, "display": "primary"},
            )
        )

        self.assertEqual(client.calls, [])
        self.assertEqual(
            host.calls,
            [
                (
                    "app_audio_output",
                    {
                        "device": "Game",
                        "process": "Dofus.exe",
                        "roles": "all",
                    },
                ),
                (
                    "windows_hdr",
                    {"enabled": True, "display": "primary"},
                ),
            ],
        )

    def test_input_volume_execution_rejects_missing_or_invalid_target(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(client, {})

        for params in (
            {"input": "Music"},
            {"input": "Music", "volume_db": True},
            {"input": "Music", "volume_db": float("nan")},
            {"input": "Music", "volume_db": 26.1},
        ):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    dispatcher.execute_action(
                        OBSAction("input_volume_db", params)
                    )

        self.assertEqual(client.calls, [])

    def test_layout_profile_is_dispatched_as_fifth_state_domain(self):
        client = FakeClient()
        layout = {
            "scene": "Gameplay",
            "modules": {
                "Webcam": {
                    "visible": True,
                    "lock_aspect": True,
                    "anchor": "top_left",
                    "base_bounds": {"x": 10, "y": 20, "width": 100, "height": 50},
                    "geometry": {"x": 20, "y": 40, "width": 200, "height": 100},
                    "elements": [
                        {
                            "source": "[Webcam] Cadre",
                            "included": True,
                            "enabled": True,
                            "transform": {
                                "positionX": 10,
                                "positionY": 20,
                                "scaleX": 1.0,
                                "scaleY": 1.0,
                                "boundsType": "OBS_BOUNDS_NONE",
                            },
                        }
                    ],
                }
            },
        }
        dispatcher = OBSDispatcher(client, {}, {"FPS": layout})
        state = StreamState(layout_profile="FPS")

        result = dispatcher.dispatch_state(state)

        self.assertIn("layout", result.changed_domains)
        self.assertEqual(result.executed, 1)
        requests = [request for request, _payload in client.calls]
        self.assertIn("SetSceneItemTransform", requests)
        self.assertIn("SetSceneItemEnabled", requests)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            OBSDispatcher(FakeClient(), {}).execute_action(OBSAction("nope", {}))


    def test_dynamic_templates_use_control_and_logical_variables(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Overwatch": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Avatar Dynamic",
                                    "settings": {
                                        "file": r"C:\\Avatars\\${Mood}_${Game}.png"
                                    },
                                    "overlay": True,
                                },
                            }
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        dispatcher.set_control_variables({"Mood": "Happy"})

        dispatcher.dispatch_state(StreamState(game="Overwatch"))

        write = next(
            payload
            for request, payload in client.calls
            if request == "SetInputSettings"
        )
        self.assertEqual(
            write["inputSettings"]["file"],
            r"C:\\Avatars\\Happy_Overwatch.png",
        )

    def test_foreground_follow_refreshes_only_matching_process_window(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Dofus Unity": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Capture de jeu",
                                    "settings": {
                                        "capture_mode": "window",
                                        "window": "fallback:UnityWndClass:Dofus.exe",
                                    },
                                    "overlay": True,
                                    "follow_foreground_process": "Dofus.exe",
                                },
                            }
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        state = StreamState(game="Dofus Unity")
        first = ForegroundApp(
            101,
            1001,
            "Dofus.exe",
            r"C:\Games\Dofus.exe",
            "Dofus - Perso A",
            "UnityWndClass",
        )
        second = ForegroundApp(
            102,
            1002,
            "Dofus.exe",
            r"C:\Games\Dofus.exe",
            "Dofus - Perso B",
            "UnityWndClass",
        )
        browser = ForegroundApp(
            201,
            2001,
            "chrome.exe",
            r"C:\Chrome\chrome.exe",
            "ChatGPT",
            "Chrome_WidgetWin_1",
        )

        dispatcher.update_foreground(first)
        dispatcher.dispatch_state(state)
        first_write = next(
            payload
            for request, payload in client.calls
            if request == "SetInputSettings"
        )
        self.assertEqual(
            first_write["inputSettings"]["window"],
            "Dofus - Perso A:UnityWndClass:Dofus.exe",
        )

        client.calls.clear()
        dispatcher.update_foreground(second)
        refreshed = dispatcher.refresh_foreground_actions(state, second)
        self.assertEqual(refreshed.executed, 1)
        self.assertEqual(refreshed.changed_domains, ("game",))
        self.assertEqual(
            client.calls[0][1]["inputSettings"]["window"],
            "Dofus - Perso B:UnityWndClass:Dofus.exe",
        )

        client.calls.clear()
        dispatcher.update_foreground(browser)
        ignored = dispatcher.refresh_foreground_actions(state, browser)
        self.assertEqual(ignored.executed, 0)
        self.assertEqual(client.calls, [])

    def test_wait_ms_is_classic_only_in_declarative_plan(self):
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Animated": {
                        "actions": [
                            {"type": "wait_ms", "params": {"duration_ms": 0}},
                            {
                                "type": "input_mute",
                                "params": {"input": "Mic", "muted": True},
                            },
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)

        plan = dispatcher.plan_state(
            StreamState(game="Animated"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "OW",
            },
        )

        self.assertTrue(
            any("wait_ms" in row["reason"] for row in plan["declarative_blocks"]),
            plan["declarative_blocks"],
        )
        kinds = {
            row["property"]["kind"]
            for row in plan["declarative_desired"]["properties"]
        }
        self.assertIn("input_mute", kinds)

    def test_template_profile_is_classic_only_in_declarative_plan(self):
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Animated": {
                        "actions": [
                            {
                                "type": "set_input_settings",
                                "params": {
                                    "input": "Avatar",
                                    "settings": {"file": "${Mood}.png"},
                                },
                            }
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(FakeClient(), profiles)

        plan = dispatcher.plan_state(
            StreamState(game="Animated"),
            context={
                "obs_enabled": True,
                "streaming": False,
                "recording": False,
                "program_scene": "OW",
            },
        )

        self.assertTrue(
            any(
                "paramètres dynamiques" in row["reason"]
                for row in plan["declarative_blocks"]
            ),
            plan["declarative_blocks"],
        )

if __name__ == "__main__":
    unittest.main()
