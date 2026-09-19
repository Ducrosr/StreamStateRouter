from __future__ import annotations

import unittest
from types import SimpleNamespace

from stream_state_router.activation import (
    ActivationEvent,
    OBSActivationController,
    TriggerPolicyConfig,
    TriggerTargetConfig,
)


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

    def set_runtime_visibility_owners(self, owners):
        self.runtime_visibility_owners = set(owners)

    def reset_cache(self):
        self.reset_calls += 1

    def discover_scene(self, scene, recursive=True):
        return self.catalog_by_scene.get(scene, {})

    def set_item_enabled(self, container, source, enabled, *, container_kind="scene"):
        self.enabled_calls.append((container, source, bool(enabled), container_kind))


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
        self.assertGreaterEqual(dispatcher.layout_manager.reset_calls, 2)

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


if __name__ == "__main__":
    unittest.main()
