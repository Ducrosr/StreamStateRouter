from __future__ import annotations

import unittest

from stream_state_router.planning import (
    DeclarativePlanner,
    DesiredProperty,
    DesiredState,
    DesiredStateConflict,
    ObservedProperty,
    ObservedState,
    ResourceKey,
)


class DeclarativePlanningTests(unittest.TestCase):
    def test_resource_key_equality_and_hash_are_stable(self):
        first = ResourceKey.scene_item_visibility(
            collection="Midgar",
            container="In Game",
            source="Chat",
            occurrence=1,
        )
        second = ResourceKey.scene_item_visibility(
            collection="Midgar",
            container="In Game",
            source="Chat",
            occurrence=1,
        )
        different_occurrence = ResourceKey.scene_item_visibility(
            collection="Midgar",
            container="In Game",
            source="Chat",
            occurrence=2,
        )
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, different_occurrence)

    def test_input_settings_are_distinct_resources(self):
        window = ResourceKey.input_setting("Capture de jeu", "window")
        color_space = ResourceKey.input_setting("Capture de jeu", "rgb10a2_space")
        self.assertNotEqual(window, color_space)

    def test_duplicate_identical_property_merges_provenance(self):
        key = ResourceKey.filter_enabled("Avatar Dynamic", "Avatar FX")
        state = DesiredState.build(
            [
                DesiredProperty.create(key, False, provenance="AvatarBase"),
                DesiredProperty.create(key, False, provenance="AvatarRecipe"),
            ]
        )
        self.assertEqual(len(state.properties), 1)
        self.assertEqual(state.properties[0].provenance, ("AvatarBase", "AvatarRecipe"))

    def test_conflicting_property_is_rejected(self):
        key = ResourceKey.filter_enabled("Avatar Dynamic", "Avatar FX")
        with self.assertRaises(DesiredStateConflict):
            DesiredState.build(
                [
                    DesiredProperty.create(key, False, provenance="A"),
                    DesiredProperty.create(key, True, provenance="B"),
                ]
            )

    def test_converged_state_produces_empty_plan(self):
        key = ResourceKey.input_setting("Capture de jeu", "rgb10a2_space")
        desired = DesiredState.build(
            [DesiredProperty.create(key, "2100pq", provenance="CaptureProfile Overwatch")]
        )
        observed = ObservedState.from_values({key: "2100pq"})
        plan = DeclarativePlanner().plan(desired, observed)
        self.assertTrue(plan.converged)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics, ())

    def test_changed_value_produces_one_operation(self):
        key = ResourceKey.input_setting("Capture de jeu", "rgb10a2_space")
        desired = DesiredState.build([DesiredProperty.create(key, "2100pq")])
        observed = ObservedState.from_values({key: "srgb"})
        plan = DeclarativePlanner().plan(desired, observed)
        self.assertFalse(plan.converged)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].operation_type, "set_input_setting")
        self.assertEqual(plan.operations[0].reason, "observed_value_differs")

    def test_unknown_value_is_explicit_and_not_converged(self):
        key = ResourceKey.filter_enabled("Avatar Dynamic", "Avatar FX")
        desired = DesiredState.build([DesiredProperty.create(key, False)])
        observed = ObservedState.build([ObservedProperty.unknown(key, "filter_not_read")])
        plan = DeclarativePlanner().plan(desired, observed)
        self.assertFalse(plan.converged)
        self.assertEqual(len(plan.operations), 1)
        self.assertFalse(plan.operations[0].observed_known)
        self.assertEqual(plan.diagnostics[0].code, "observation_unknown")
        effective = plan.observed_state.lookup(key)
        self.assertFalse(effective.known)
        self.assertEqual(effective.detail, "filter_not_read")

    def test_missing_observation_is_materialized_as_unknown_in_dry_run(self):
        desired_key = ResourceKey.input_setting("Capture de jeu", "window")
        unrelated_key = ResourceKey.input_setting("Autre source", "file")
        desired = DesiredState.build(
            [DesiredProperty.create(desired_key, "Overwatch:Class:Overwatch.exe")]
        )
        observed = ObservedState.from_values({unrelated_key: "ignored.png"})

        plan = DeclarativePlanner().plan(desired, observed)

        self.assertEqual(len(plan.observed_state.properties), 1)
        effective = plan.observed_state.lookup(desired_key)
        self.assertFalse(effective.known)
        self.assertEqual(effective.detail, "not_observed")
        self.assertFalse(any(item.key == unrelated_key for item in plan.observed_state.properties))

    def test_mapping_values_with_string_collisions_compare_without_type_errors(self):
        key = ResourceKey.input_setting("Source", "settings")
        desired = DesiredState.build(
            [DesiredProperty.create(key, {1: {"a": 1}, "1": ["x"]})]
        )
        observed = ObservedState.from_values({key: {"1": ["x"], 1: {"a": 1}}})

        plan = DeclarativePlanner().plan(desired, observed)

        self.assertTrue(plan.converged)

    def test_multiple_independent_properties_are_deterministic(self):
        chat = ResourceKey.scene_item_visibility(
            collection="Midgar", container="In Game", source="Chat"
        )
        radio = ResourceKey.scene_item_visibility(
            collection="Midgar", container="In Game", source="Radio"
        )
        desired = DesiredState.build(
            [
                DesiredProperty.create(radio, False, provenance="Overwatch"),
                DesiredProperty.create(chat, False, provenance="Overwatch"),
            ]
        )
        observed = ObservedState.from_values({chat: True, radio: True})
        planner = DeclarativePlanner()
        first = planner.plan(desired, observed).as_mapping()
        second = planner.plan(desired, observed).as_mapping()
        self.assertEqual(first, second)
        self.assertEqual(len(first["operations"]), 2)

    def test_layout_is_delegated_as_apply_layout_operation(self):
        key = ResourceKey.layout_profile()
        desired = DesiredState.build(
            [DesiredProperty.create(key, "Dofus", provenance="LayoutProfile Dofus")]
        )
        observed = ObservedState.from_values({key: "Vanilla"})
        plan = DeclarativePlanner().plan(desired, observed)
        self.assertEqual(plan.operations[0].operation_type, "apply_layout")


if __name__ == "__main__":
    unittest.main()
