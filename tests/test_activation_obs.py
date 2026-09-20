from __future__ import annotations

import unittest
from types import SimpleNamespace

from stream_state_router.activation import (
    ActivationEvent,
    ActivationVisibilityUncertain,
    OBSActivationController,
    TriggerPolicyConfig,
    TriggerTargetConfig,
)
from stream_state_router.obs.client import OBSResourceNotFoundError, OBSUnavailableError


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class FakeClient:
    def __init__(self):
        self.config = SimpleNamespace(enabled=True)
        self.connected = True
        self.scene_collection = "Collection A"

    def send(self, request, data=None):
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": self.scene_collection}
        raise AssertionError(request)


class FakeLayoutManager:
    def __init__(self):
        self.enabled_calls = []
        self.reset_calls = 0
        self.catalog_by_scene = {}
        self.topology_calls = []
        self.runtime_visibility_owners = set()
        self.failures = {}

    def set_runtime_visibility_owners(self, owners):
        self.runtime_visibility_owners = set(owners)

    def reset_cache(self):
        self.reset_calls += 1

    def discover_scene(self, scene, recursive=True):
        return self.catalog_by_scene.get(scene, {})

    def scan_scene_topology(self, scene, recursive=True):
        self.topology_calls.append((scene, bool(recursive)))
        return tuple(
            SimpleNamespace(source=element.source)
            for elements in self.catalog_by_scene.get(scene, {}).values()
            for element in elements
        )

    def set_item_enabled(self, container, source, enabled, *, container_kind="scene"):
        self.enabled_calls.append((container, source, bool(enabled), container_kind))

    def set_activation_item_enabled(
        self,
        container,
        source,
        enabled,
        *,
        container_kind="scene",
    ):
        key = (container, source, bool(enabled), container_kind)
        failure = self.failures.get(key)
        self.enabled_calls.append(key)
        if failure:
            exc = failure.pop(0)
            if exc is not None:
                raise exc


class FakeDispatcher:
    def __init__(self):
        self.client = FakeClient()
        self.layout_manager = FakeLayoutManager()
        self.context = {
            "streaming": False,
            "program_scene": "In Game",
            "obs_enabled": True,
        }

    def obs_context(self):
        return dict(self.context)


class OBSActivationControllerTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "module_source": "[Module] EasterEgg",
            "active_when": "module_in_program_scene",
            "exclusive": True,
            "targets": (
                TriggerTargetConfig("[Module] EasterEgg", "A", weight=1.0),
                TriggerTargetConfig("[Module] EasterEgg", "B", weight=1.0),
            ),
        }
        values.update(overrides)
        return TriggerPolicyConfig(**values)

    def test_module_in_program_scene_is_required(self):
        dispatcher = FakeDispatcher()
        dispatcher.layout_manager.catalog_by_scene["In Game"] = {
            "[Module] EasterEgg": [SimpleNamespace(source="[Module] EasterEgg")]
        }
        policy = self.policy()
        controller = OBSActivationController(
            dispatcher,
            {"egg": policy},
            eligibility_cache_seconds=0.1,
        )

        self.assertTrue(controller.is_eligible("egg", policy))
        dispatcher.context["program_scene"] = "Pause"
        self.assertFalse(controller.is_eligible("egg", policy))

    def test_multiple_policies_share_one_scene_topology_scan(self):
        dispatcher = FakeDispatcher()
        dispatcher.layout_manager.catalog_by_scene["In Game"] = {
            "Egg A": [SimpleNamespace(source="[Module] EggA")],
            "Egg B": [SimpleNamespace(source="[Module] EggB")],
        }
        first = self.policy(module_source="[Module] EggA")
        second = self.policy(module_source="[Module] EggB")
        clock = FakeClock(10.0)
        controller = OBSActivationController(
            dispatcher,
            {"a": first, "b": second},
            clock=clock,
            eligibility_cache_seconds=0.5,
        )

        self.assertTrue(controller.is_eligible("a", first))
        self.assertTrue(controller.is_eligible("b", second))
        self.assertEqual(dispatcher.layout_manager.topology_calls, [("In Game", True)])

    def test_ten_policies_in_one_scene_still_use_one_topology_scan(self):
        dispatcher = FakeDispatcher()
        policies = {}
        dispatcher.layout_manager.catalog_by_scene["In Game"] = {}
        for index in range(10):
            source = f"[Module] Egg{index}"
            dispatcher.layout_manager.catalog_by_scene["In Game"][source] = [
                SimpleNamespace(source=source)
            ]
            policies[f"egg-{index}"] = self.policy(module_source=source)
        controller = OBSActivationController(
            dispatcher,
            policies,
            clock=FakeClock(5.0),
            eligibility_cache_seconds=0.5,
        )

        for name, policy in policies.items():
            self.assertTrue(controller.is_eligible(name, policy))

        self.assertEqual(dispatcher.layout_manager.topology_calls, [("In Game", True)])

    def test_scene_topology_cache_expires_and_observes_structure_change(self):
        dispatcher = FakeDispatcher()
        dispatcher.layout_manager.catalog_by_scene["In Game"] = {
            "Egg": [SimpleNamespace(source="[Module] EasterEgg")]
        }
        policy = self.policy()
        clock = FakeClock(1.0)
        controller = OBSActivationController(
            dispatcher,
            {"egg": policy},
            clock=clock,
            eligibility_cache_seconds=0.5,
        )

        self.assertTrue(controller.is_eligible("egg", policy))
        dispatcher.layout_manager.catalog_by_scene["In Game"] = {}
        clock.value = 1.2
        self.assertTrue(controller.is_eligible("egg", policy))
        clock.value = 1.6
        self.assertFalse(controller.is_eligible("egg", policy))
        self.assertEqual(len(dispatcher.layout_manager.topology_calls), 2)

    def test_streaming_mode_uses_obs_context(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(active_when="streaming")
        controller = OBSActivationController(dispatcher, {"egg": policy})

        self.assertFalse(controller.is_eligible("egg", policy))
        dispatcher.context["streaming"] = True
        self.assertTrue(controller.is_eligible("egg", policy))

    def test_show_is_exclusive_and_hide_targets_selected_source(self):
        dispatcher = FakeDispatcher()
        policy = self.policy()
        controller = OBSActivationController(dispatcher, {"egg": policy})
        show = ActivationEvent(
            "show",
            "egg",
            10.0,
            source="B",
            container="[Module] EasterEgg",
        )

        controller.apply_event(show)
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls,
            [
                ("[Module] EasterEgg", "A", False, "scene"),
                ("[Module] EasterEgg", "B", False, "scene"),
                ("[Module] EasterEgg", "B", True, "scene"),
            ],
        )

        controller.apply_event(
            ActivationEvent(
                "hide",
                "egg",
                12.0,
                source="B",
                container="[Module] EasterEgg",
            )
        )
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls[-1],
            ("[Module] EasterEgg", "B", False, "scene"),
        )

    def test_reconcile_hides_all_targets_and_remembers_collection(self):
        dispatcher = FakeDispatcher()
        policy = self.policy()
        clock = FakeClock(10.0)
        controller = OBSActivationController(
            dispatcher,
            {"egg": policy},
            clock=clock,
        )

        self.assertEqual(controller.reconcile(), ())
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls,
            [
                ("[Module] EasterEgg", "A", False, "scene"),
                ("[Module] EasterEgg", "B", False, "scene"),
            ],
        )
        self.assertFalse(controller.scene_collection_changed())

    def test_all_configured_targets_remain_runtime_visibility_owned(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(
            enabled=False,
            targets=(
                TriggerTargetConfig("[Module] EasterEgg", "A", enabled=True),
                TriggerTargetConfig("[Module] EasterEgg", "B", enabled=False),
            ),
        )

        OBSActivationController(dispatcher, {"egg": policy})

        self.assertEqual(
            dispatcher.layout_manager.runtime_visibility_owners,
            {
                ("[Module] EasterEgg", "A"),
                ("[Module] EasterEgg", "B"),
            },
        )

    def test_reconcile_hides_disabled_configured_target_too(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(
            targets=(
                TriggerTargetConfig("[Module] EasterEgg", "A", enabled=True),
                TriggerTargetConfig("[Module] EasterEgg", "B", enabled=False),
            )
        )
        controller = OBSActivationController(dispatcher, {"egg": policy})

        controller.reconcile()

        self.assertIn(
            ("[Module] EasterEgg", "B", False, "scene"),
            dispatcher.layout_manager.enabled_calls,
        )

    def test_pending_cleanup_can_be_transferred_even_if_policy_was_removed(self):
        dispatcher = FakeDispatcher()
        clock = FakeClock(10.0)
        controller = OBSActivationController(
            dispatcher,
            {"egg": self.policy()},
            clock=clock,
            retry_base_seconds=0.1,
        )
        controller.reconcile()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "A", False, "scene")
        ] = [OBSUnavailableError("timeout")]
        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent("hide", "egg", 10.0, source="A", container="[Module] EasterEgg")
            )
        snapshot = controller.export_pending_hides()

        replacement = OBSActivationController(dispatcher, {}, clock=clock, retry_base_seconds=0.1)
        imported = replacement.import_pending_hides(snapshot)

        self.assertEqual(imported, 1)
        self.assertEqual(len(replacement.pending_hides()), 1)
        self.assertIn(
            ("[Module] EasterEgg", "A"),
            dispatcher.layout_manager.runtime_visibility_owners,
        )
        clock.value = 10.2
        replacement.retry_pending_hides(now=clock.value)
        self.assertEqual(replacement.pending_hides(), ())

    def test_imported_cleanup_is_preserved_but_not_replayed_in_different_collection(self):
        dispatcher = FakeDispatcher()
        old = {
            "policy": "egg",
            "collection": "Collection A",
            "target": TriggerTargetConfig("[Module] EasterEgg", "A").to_mapping(),
            "created_at": 1.0,
            "attempts": 1,
            "next_retry_at": 999999.0,
            "last_error": "timeout",
        }
        dispatcher.client.scene_collection = "Collection B"
        controller = OBSActivationController(dispatcher, {})
        controller.import_pending_hides([old])
        dispatcher.layout_manager.enabled_calls.clear()

        controller.retry_pending_hides(now=10.0)

        self.assertEqual(len(controller.pending_hides()), 1)
        self.assertEqual(controller.pending_hides()[0].collection, "Collection A")
        self.assertEqual(dispatcher.layout_manager.enabled_calls, [])

        dispatcher.client.scene_collection = "Collection A"
        controller.retry_pending_hides(now=10.1)

        self.assertEqual(controller.pending_hides(), ())
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls[-1],
            ("[Module] EasterEgg", "A", False, "scene"),
        )

    def test_scene_collection_change_is_detected_after_probe_interval(self):
        dispatcher = FakeDispatcher()
        clock = FakeClock(1.0)
        controller = OBSActivationController(
            dispatcher,
            {"egg": self.policy()},
            clock=clock,
            collection_probe_seconds=0.5,
        )
        controller.reconcile()
        dispatcher.client.scene_collection = "Collection B"
        clock.value = 1.6

        self.assertTrue(controller.scene_collection_changed())
        self.assertEqual(dispatcher.layout_manager.reset_calls, 0)

    def test_eligibility_reports_reason(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(active_when="streaming")
        controller = OBSActivationController(dispatcher, {"egg": policy})

        eligible, reason = controller.eligibility("egg", policy)
        self.assertFalse(eligible)
        self.assertIn("inactif", reason)

        dispatcher.context["streaming"] = True
        eligible, reason = controller.eligibility("egg", policy)
        self.assertTrue(eligible)
        self.assertIn("actif", reason)

    def test_eligibility_reports_disconnected_obs(self):
        dispatcher = FakeDispatcher()
        dispatcher.client.connected = False
        policy = self.policy()
        controller = OBSActivationController(dispatcher, {"egg": policy})

        eligible, reason = controller.eligibility("egg", policy)

        self.assertFalse(eligible)
        self.assertIn("déconnecté", reason)

    def test_confirmed_missing_hide_is_acknowledged_without_pending_cleanup(self):
        dispatcher = FakeDispatcher()
        policy = self.policy()
        controller = OBSActivationController(dispatcher, {"egg": policy})
        controller.reconcile()
        dispatcher.layout_manager.enabled_calls.clear()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "B", False, "scene")
        ] = [OBSResourceNotFoundError("GetSceneItemId", "source not found")]

        controller.apply_event(
            ActivationEvent(
                "hide",
                "egg",
                9.0,
                source="B",
                container="[Module] EasterEgg",
            )
        )

        self.assertEqual(controller.pending_hides("egg"), ())
        self.assertEqual(controller.policy_cleanup_status("egg"), (False, ""))

    def test_hide_obligation_is_armed_before_visibility_io(self):
        dispatcher = FakeDispatcher()
        policy = self.policy()

        class ObservingController(OBSActivationController):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.prearmed = False

            def _mutate_visibility(self, target, enabled):
                if not enabled:
                    self.prearmed = bool(self.pending_hides())
                    return "uncertain", "transport lost"
                return super()._mutate_visibility(target, enabled)

        controller = ObservingController(dispatcher, {"egg": policy})
        controller.reconcile()

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "hide",
                    "egg",
                    10.0,
                    source="A",
                    container="[Module] EasterEgg",
                )
            )

        self.assertTrue(controller.prearmed)
        self.assertEqual(len(controller.pending_hides("egg")), 1)
        self.assertEqual(controller.pending_hides("egg")[0].collection, "Collection A")

    def test_uncertain_show_keeps_prearmed_compensating_hide_in_origin_collection(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(exclusive=False)
        controller = OBSActivationController(dispatcher, {"egg": policy})
        controller.reconcile()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "A", True, "scene")
        ] = [OBSUnavailableError("response lost")]

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "show",
                    "egg",
                    10.0,
                    source="A",
                    container="[Module] EasterEgg",
                )
            )

        pending = controller.pending_hides("egg")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].collection, "Collection A")
        self.assertEqual(pending[0].target.source, "A")

    def test_hide_failure_stays_pending_and_retries_until_acknowledged(self):
        dispatcher = FakeDispatcher()
        clock = FakeClock(10.0)
        policy = self.policy()
        controller = OBSActivationController(
            dispatcher,
            {"egg": policy},
            clock=clock,
            retry_base_seconds=0.1,
            retry_max_seconds=0.2,
        )
        controller.reconcile()
        dispatcher.layout_manager.enabled_calls.clear()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "B", False, "scene")
        ] = [OBSUnavailableError("response lost")]

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "hide",
                    "egg",
                    10.0,
                    source="B",
                    container="[Module] EasterEgg",
                )
            )

        blocked, reason = controller.policy_cleanup_status("egg")
        self.assertTrue(blocked)
        self.assertIn("B", reason)
        self.assertEqual(len(controller.pending_hides("egg")), 1)

        clock.value = 10.2
        messages = controller.retry_pending_hides(now=clock.value)

        self.assertTrue(messages)
        self.assertEqual(controller.pending_hides("egg"), ())

    def test_uncertain_show_schedules_compensating_hide(self):
        dispatcher = FakeDispatcher()
        clock = FakeClock(20.0)
        policy = self.policy()
        controller = OBSActivationController(
            dispatcher,
            {"egg": policy},
            clock=clock,
            retry_base_seconds=0.1,
            retry_max_seconds=0.2,
        )
        controller.reconcile()
        dispatcher.layout_manager.enabled_calls.clear()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "B", True, "scene")
        ] = [OBSUnavailableError("response lost after apply")]

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "show",
                    "egg",
                    20.0,
                    source="B",
                    container="[Module] EasterEgg",
                )
            )

        self.assertEqual(len(controller.pending_hides("egg")), 1)
        clock.value = 20.2
        controller.retry_pending_hides(now=clock.value)

        self.assertEqual(controller.pending_hides("egg"), ())
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls[-1],
            ("[Module] EasterEgg", "B", False, "scene"),
        )

    def test_exclusive_show_is_blocked_if_competitor_hide_is_uncertain(self):
        dispatcher = FakeDispatcher()
        controller = OBSActivationController(dispatcher, {"egg": self.policy()})
        controller.reconcile()
        dispatcher.layout_manager.enabled_calls.clear()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "A", False, "scene")
        ] = [OBSUnavailableError("timeout")]

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "show",
                    "egg",
                    30.0,
                    source="B",
                    container="[Module] EasterEgg",
                )
            )

        self.assertFalse(
            any(
                source == "B" and enabled
                for _container, source, enabled, _kind
                in dispatcher.layout_manager.enabled_calls
            )
        )

    def test_pending_hide_from_old_collection_is_suspended_until_original_collection_returns(self):
        dispatcher = FakeDispatcher()
        clock = FakeClock(40.0)
        controller = OBSActivationController(
            dispatcher,
            {"egg": self.policy()},
            clock=clock,
            retry_base_seconds=0.1,
        )
        controller.reconcile()
        dispatcher.layout_manager.enabled_calls.clear()
        dispatcher.layout_manager.failures[
            ("[Module] EasterEgg", "A", False, "scene")
        ] = [OBSUnavailableError("timeout")]

        with self.assertRaises(ActivationVisibilityUncertain):
            controller.apply_event(
                ActivationEvent(
                    "hide",
                    "egg",
                    40.0,
                    source="A",
                    container="[Module] EasterEgg",
                )
            )
        calls_before = len(dispatcher.layout_manager.enabled_calls)

        dispatcher.client.scene_collection = "Collection B"
        clock.value = 40.2
        controller.retry_pending_hides(now=clock.value)

        self.assertEqual(len(controller.pending_hides()), 1)
        self.assertEqual(controller.pending_hides()[0].collection, "Collection A")
        self.assertEqual(len(dispatcher.layout_manager.enabled_calls), calls_before)

        dispatcher.client.scene_collection = "Collection A"
        clock.value = 40.3
        controller.retry_pending_hides(now=clock.value)

        self.assertEqual(controller.pending_hides(), ())
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls[-1],
            ("[Module] EasterEgg", "A", False, "scene"),
        )

    def test_legacy_source_only_event_is_rejected_when_ambiguous(self):
        dispatcher = FakeDispatcher()
        policy = self.policy(
            exclusive=False,
            targets=(
                TriggerTargetConfig("Scene A", "Cloud", container_kind="scene"),
                TriggerTargetConfig("Group B", "Cloud", container_kind="group"),
            ),
        )
        controller = OBSActivationController(dispatcher, {"egg": policy})
        controller.reconcile()

        with self.assertRaisesRegex(RuntimeError, "ambiguë"):
            controller.apply_event(
                ActivationEvent("show", "egg", 50.0, source="Cloud", container="")
            )

        controller.apply_event(
            ActivationEvent(
                "show",
                "egg",
                50.0,
                source="Cloud",
                container="Group B",
                container_kind="group",
            )
        )
        self.assertEqual(
            dispatcher.layout_manager.enabled_calls[-1],
            ("Group B", "Cloud", True, "group"),
        )



if __name__ == "__main__":
    unittest.main()
