from __future__ import annotations

import unittest

from stream_state_router.obs.catalog import OBSResourceCatalog, SceneItemRef
from stream_state_router.obs.models import OBSAction
from stream_state_router.obs.observed import observe_desired_state
from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    ObservedState,
    ObservedValue,
    PlanDiagnostic,
    PropertyKey,
    UnsupportedIntentAction,
    build_execution_plan,
    desired_assignments_from_actions,
)


class FakeObservedClient:
    def __init__(self, *, lookup_id=0, rows=None, enabled=False):
        self.lookup_id = lookup_id
        self.rows = list(rows or [])
        self.enabled = enabled
        self.calls = []

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetSceneItemId":
            return {"sceneItemId": self.lookup_id}
        if request == "GetSceneItemList":
            return {"sceneItems": list(self.rows)}
        if request == "GetGroupSceneItemList":
            return {"sceneItems": list(self.rows)}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": self.enabled}
        raise AssertionError(f"Unexpected request: {request}")


def catalog_for_item(*, scene_item_id=0, source_uuid="uuid-a"):
    return OBSResourceCatalog(
        collection="Main",
        current_program_scene="Gameplay",
        current_program_scene_uuid="scene-uuid",
        canvas=(1920, 1080),
        scenes=(),
        groups=(),
        scene_items=(
            SceneItemRef(
                collection="Main",
                root_scene="Gameplay",
                container="Gameplay",
                container_kind="scene",
                path=("Gameplay",),
                source="Chat",
                source_uuid=source_uuid,
                source_kind="input",
                occurrence=0,
                scene_item_id=scene_item_id,
                enabled=True,
            ),
        ),
        inputs=(),
        transitions=(),
    )


class DeclarativeAuditRegressionTests(unittest.TestCase):
    def test_boolean_and_zero_are_not_equal(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="flag",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, False)])
        observed = ObservedState({key: ObservedValue.known_value(0)})

        plan = build_execution_plan(desired, observed)

        self.assertFalse(plan.converged)
        self.assertEqual(len(plan.operations), 1)

    def test_nested_boolean_and_zero_are_not_equal(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="payload",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, {"nested": [False]})]
        )
        observed = ObservedState(
            {key: ObservedValue.known_value({"nested": [0]})}
        )

        plan = build_execution_plan(desired, observed)

        self.assertFalse(plan.converged)
        self.assertEqual(len(plan.operations), 1)

    def test_large_integer_difference_is_not_tolerated_for_settings(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="counter",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, 1_000_000_001)]
        )
        observed = ObservedState(
            {key: ObservedValue.known_value(1_000_000_000)}
        )

        plan = build_execution_plan(desired, observed)

        self.assertFalse(plan.converged)
        self.assertEqual(len(plan.operations), 1)

    def test_volume_keeps_small_numeric_tolerance(self):
        key = PropertyKey.input_volume_db(
            collection="Main",
            input_name="Game",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, -6.0)])
        observed = ObservedState(
            {key: ObservedValue.known_value(-6.0000001)}
        )

        plan = build_execution_plan(desired, observed)

        self.assertTrue(plan.converged)
        self.assertEqual(plan.operations, ())

    def test_property_key_rejects_irrelevant_identity_fields(self):
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            PropertyKey(
                "input_setting",
                collection="Main",
                container="Gameplay",
                source="Capture",
                setting="mode",
            )

    def test_plan_is_detached_from_mutable_desired_value(self):
        original = {"items": [1, 2]}
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="payload",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, original)]
        )
        observed = ObservedState(
            {key: ObservedValue.known_value({"items": [1]})}
        )
        plan = build_execution_plan(desired, observed)
        signature = plan.target_signature

        original["items"].append(3)

        self.assertEqual(plan.target_signature, signature)
        self.assertEqual(plan.operations[0].target, {"items": [1, 2]})

    def test_overlay_false_is_not_projected_as_independent_settings(self):
        action = OBSAction(
            "set_input_settings",
            {
                "input": "Capture",
                "settings": {"a": 1},
                "overlay": False,
            },
        )

        with self.assertRaises(UnsupportedIntentAction):
            desired_assignments_from_actions(
                [action],
                provenance="capture:Test",
            )

    def test_global_resolution_error_blocks_empty_plan(self):
        plan = build_execution_plan(
            DesiredState.empty(),
            ObservedState.empty(),
            extra_diagnostics=(
                PlanDiagnostic(
                    "error",
                    "profile_resolution_failed",
                    "overlay: profil introuvable",
                ),
            ),
        )

        self.assertTrue(plan.blocked)
        self.assertFalse(plan.converged)
        self.assertEqual(plan.operations, ())

    def test_scene_item_id_zero_is_valid_when_binding_matches(self):
        client = FakeObservedClient(
            lookup_id=0,
            rows=[
                {
                    "sourceName": "Chat",
                    "sourceUuid": "uuid-a",
                    "sceneItemId": 0,
                }
            ],
            enabled=False,
        )
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="Gameplay",
            source="Chat",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, False)])

        observed = observe_desired_state(
            client,
            catalog_for_item(scene_item_id=0),
            desired,
        )

        self.assertTrue(observed.get(key).known)
        self.assertFalse(observed.get(key).value)

    def test_reordered_occurrence_is_reported_unknown(self):
        client = FakeObservedClient(
            lookup_id=8,
            rows=[
                {
                    "sourceName": "Chat",
                    "sourceUuid": "uuid-a",
                    "sceneItemId": 8,
                },
                {
                    "sourceName": "Chat",
                    "sourceUuid": "uuid-a",
                    "sceneItemId": 7,
                },
            ],
            enabled=False,
        )
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="Gameplay",
            source="Chat",
            occurrence=0,
        )
        desired = DesiredState.build([DesiredAssignment.create(key, False)])

        observed = observe_desired_state(
            client,
            catalog_for_item(scene_item_id=7),
            desired,
        )

        self.assertFalse(observed.get(key).known)


if __name__ == "__main__":
    unittest.main()
