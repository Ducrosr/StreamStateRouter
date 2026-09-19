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
from stream_state_router.obs.client import OBSUnavailableError


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
        self.runtime_visibility_owners = set()
        self.failures = {}

    def set_runtime_visibility_owners(self, owners):
        self.runtime_visibility_owners = set(owners)

    def reset_cache(self):
        self.reset_calls += 1

    def discover_scene(self, scene, recursive=True):
        return self.catalog_by_scene.get(scene, {})

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

    def test_pending_hide_from_old_collection_is_not_replayed_in_new_collection(self):
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

        self.assertEqual(controller.pending_hides(), ())
        self.assertEqual(len(dispatcher.layout_manager.enabled_calls), calls_before)

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
