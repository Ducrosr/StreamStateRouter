from __future__ import annotations

import unittest

from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    DesiredStateConflict,
    ObservedState,
    ObservedValue,
    PlanDiagnostic,
    PropertyKey,
    build_execution_plan,
    render_execution_plan,
)


class DeclarativePlanningTests(unittest.TestCase):
    def test_property_key_distinguishes_scene_item_occurrences(self):
        left = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Camera",
            occurrence=0,
        )
        right = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Camera",
            occurrence=1,
        )

        self.assertNotEqual(left, right)
        self.assertNotEqual(hash(left), hash(right))

    def test_desired_state_binds_only_blank_scene_collection_keys(self):
        blank = PropertyKey.input_setting(
            collection="",
            input_name="Capture",
            setting="window",
        )
        explicit = PropertyKey.filter_enabled(
            collection="Other",
            source="Avatar",
            filter_name="Glitch",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(blank, "Overwatch"),
                DesiredAssignment.create(explicit, False),
            ]
        )

        bound = desired.bind_collection("Main")
        by_kind = {item.key.kind: item.key for item in bound.assignments}

        self.assertEqual(by_kind["input_setting"].collection, "Main")
        self.assertEqual(by_kind["filter_enabled"].collection, "Other")
        self.assertEqual(desired.by_key()[blank].key.collection, "")

    def test_collection_binding_detects_newly_colliding_properties(self):
        abstract = PropertyKey.input_setting(
            collection="",
            input_name="Capture",
            setting="window",
        )
        explicit = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="window",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(
                    abstract,
                    "Overwatch",
                    provenance="abstract",
                ),
                DesiredAssignment.create(
                    explicit,
                    "Dofus",
                    provenance="explicit",
                ),
            ]
        )

        with self.assertRaises(DesiredStateConflict):
            desired.bind_collection("Main")

    def test_conflict_exception_does_not_expose_setting_values(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Browser",
            setting="url",
        )

        with self.assertRaises(DesiredStateConflict) as captured:
            DesiredState.build(
                [
                    DesiredAssignment.create(
                        key,
                        "https://example.invalid/?token=secret-a",
                        provenance="A",
                    ),
                    DesiredAssignment.create(
                        key,
                        "https://example.invalid/?token=secret-b",
                        provenance="B",
                    ),
                ]
            )

        message = str(captured.exception)
        self.assertNotIn("secret-a", message)
        self.assertNotIn("secret-b", message)
        self.assertIn("A vs B", message)

    def test_property_key_distinguishes_input_settings(self):
        left = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="window",
        )
        right = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="rgb10a2_space",
        )

        self.assertNotEqual(left, right)

    def test_target_signature_is_stable_and_ignores_provenance(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="window",
        )
        first = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "Overwatch",
                    provenance="game:A",
                )
            ]
        )
        second = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "Overwatch",
                    provenance="capture:B",
                )
            ]
        )
        different = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "Dofus",
                    provenance="game:A",
                )
            ]
        )

        self.assertEqual(first.target_signature(), second.target_signature())
        self.assertNotEqual(first.target_signature(), different.target_signature())

    def test_plan_carries_desired_target_signature(self):
        key = PropertyKey.input_mute(
            collection="Main",
            input_name="Mic",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, True, provenance="audio")]
        )
        observed = ObservedState({key: ObservedValue.known_value(False)})

        plan = build_execution_plan(desired, observed)

        self.assertEqual(plan.target_signature, desired.target_signature())
        self.assertEqual(
            plan.as_mapping()["target_signature"],
            desired.target_signature(),
        )

    def test_identical_duplicate_assignments_merge_provenance(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Avatar Dynamic",
            filter_name="Swap Glitch",
        )
        state = DesiredState.build(
            [
                DesiredAssignment.create(key, False, provenance="Base"),
                DesiredAssignment.create(key, False, provenance="Avatar"),
            ]
        )

        self.assertEqual(len(state.assignments), 1)
        self.assertEqual(state.assignments[0].provenance, ("Base", "Avatar"))

    def test_conflicting_assignments_fail_instead_of_last_write_wins(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )

        with self.assertRaises(DesiredStateConflict):
            DesiredState.build(
                [
                    DesiredAssignment.create(key, True, provenance="Overlay"),
                    DesiredAssignment.create(key, False, provenance="Layout"),
                ]
            )

    def test_planner_is_empty_when_state_is_converged(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Avatar Dynamic",
            filter_name="Swap Glitch",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="Avatar")]
        )
        observed = ObservedState({key: ObservedValue.known_value(False)})

        plan = build_execution_plan(desired, observed)

        self.assertTrue(plan.converged)
        self.assertFalse(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diff[0].status, "converged")

    def test_planner_emits_one_operation_for_changed_value(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture de jeu",
            setting="rgb10a2_space",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, "2100pq", provenance="CaptureProfile Overwatch")]
        )
        observed = ObservedState({key: ObservedValue.known_value("srgb")})

        plan = build_execution_plan(desired, observed)

        self.assertFalse(plan.converged)
        self.assertFalse(plan.blocked)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].operation, "SetInputSetting")
        self.assertEqual(plan.operations[0].target, "2100pq")
        self.assertEqual(
            plan.operations[0].provenance,
            ("CaptureProfile Overwatch",),
        )

    def test_invalid_boolean_target_is_blocked(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, "yes", provenance="test")]
        )

        plan = build_execution_plan(desired, ObservedState.empty())

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "invalid_desired_value")

    def test_non_json_setting_target_is_blocked_without_signature_crash(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="unsupported",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, {1, 2}, provenance="test")]
        )

        signature = desired.target_signature()
        plan = build_execution_plan(desired, ObservedState.empty())

        self.assertEqual(len(signature), 64)
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "invalid_desired_value")

    def test_non_finite_volume_target_is_blocked(self):
        key = PropertyKey.input_volume_db(
            collection="Main",
            input_name="Music",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, float("nan"), provenance="test")]
        )

        plan = build_execution_plan(desired, ObservedState.empty())

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "invalid_desired_value")

    def test_incomplete_filter_key_is_rejected_at_construction(self):
        with self.assertRaisesRegex(ValueError, "filter_name"):
            PropertyKey(
                kind="filter_setting",
                collection="Main",
                source="Avatar",
                filter_name="",
                setting="strength",
            )

    def test_preflight_block_prevents_operation_even_with_known_diff(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="External Component",
            source="Internal Item",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="test")]
        )
        observed = ObservedState({key: ObservedValue.known_value(True)})

        plan = build_execution_plan(
            desired,
            observed,
            preflight={
                key: PlanDiagnostic(
                    "error",
                    "resource_out_of_scope",
                    "delegated",
                    key,
                )
            },
        )

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diff[0].status, "blocked")
        self.assertEqual(plan.diagnostics[0].code, "resource_out_of_scope")

    def test_unknown_observation_is_blocked_and_does_not_plan_blind_write(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Input Overlay",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="Layout Dofus")]
        )

        plan = build_execution_plan(desired, ObservedState.empty())

        self.assertTrue(plan.blocked)
        self.assertFalse(plan.converged)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diff[0].status, "unknown")
        self.assertEqual(plan.diagnostics[0].code, "observed_value_unknown")

    def test_numeric_roundoff_is_treated_as_converged(self):
        key = PropertyKey.input_volume_db(
            collection="Main",
            input_name="Music",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, -7.5, provenance="audio")]
        )
        observed = ObservedState(
            {key: ObservedValue.known_value(-7.5000001)}
        )

        plan = build_execution_plan(desired, observed)

        self.assertTrue(plan.converged)
        self.assertEqual(plan.operations, ())

    def test_dry_run_redacts_arbitrary_input_setting_values(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Browser",
            setting="url",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "https://example.invalid/?token=secret",
                    provenance="test",
                )
            ]
        )
        observed = ObservedState(
            {key: ObservedValue.known_value("https://old.invalid/?token=old")}
        )

        text = render_execution_plan(
            build_execution_plan(desired, observed)
        )

        self.assertNotIn("secret", text)
        self.assertNotIn("old", text)
        self.assertIn("<redacted>", text)

    def test_dry_run_redacts_arbitrary_filter_setting_values(self):
        key = PropertyKey.filter_setting(
            collection="Main",
            source="Browser Source",
            filter_name="Custom Filter",
            setting="endpoint",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    "https://example.invalid/?token=secret-filter",
                    provenance="test",
                )
            ]
        )
        observed = ObservedState(
            {
                key: ObservedValue.known_value(
                    "https://old.invalid/?token=old-filter"
                )
            }
        )

        plan = build_execution_plan(desired, observed)
        text = render_execution_plan(plan)
        mapped = plan.as_mapping()

        self.assertNotIn("secret-filter", text)
        self.assertNotIn("old-filter", text)
        self.assertEqual(mapped["diff"][0]["desired"], "<redacted>")
        self.assertEqual(mapped["operations"][0]["target"], "<redacted>")

    def test_planner_output_is_deterministic(self):
        first = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="window",
        )
        second = PropertyKey.filter_setting(
            collection="Main",
            source="Lost Signal",
            filter_name="Shader",
            setting="alpha_percent",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(second, 100, provenance="Signal"),
                DesiredAssignment.create(first, "Overwatch", provenance="Capture"),
            ]
        )
        observed = ObservedState(
            {
                first: ObservedValue.known_value("Dofus"),
                second: ObservedValue.known_value(80),
            }
        )

        one = build_execution_plan(desired, observed).as_mapping()
        two = build_execution_plan(desired, observed).as_mapping()

        self.assertEqual(one, two)

    def test_dry_run_renderer_is_deterministic_and_readable(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Avatar Dynamic",
            filter_name="Swap Glitch",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, True, provenance="Avatar")]
        )
        observed = ObservedState({key: ObservedValue.known_value(False)})

        plan = build_execution_plan(desired, observed)
        first = render_execution_plan(plan)
        second = render_execution_plan(plan)

        self.assertEqual(first, second)
        self.assertIn("Declarative dry-run", first)
        self.assertIn("SetFilterEnabled", first)
        self.assertIn("false", first.casefold())

    def test_layout_is_delegated_as_apply_layout_operation(self):
        key = PropertyKey.layout_profile(collection="Main", scene="In Game")
        desired = DesiredState.build(
            [DesiredAssignment.create(key, "Dofus", provenance="LayoutProfile")]
        )
        observed = ObservedState({key: ObservedValue.known_value("Vanilla")})

        plan = build_execution_plan(desired, observed)

        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].operation, "ApplyLayout")


    def test_boolean_and_number_are_not_treated_as_converged(self):
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

    def test_nested_boolean_and_number_are_not_treated_as_converged(self):
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

        self.assertEqual(len(build_execution_plan(desired, observed).operations), 1)

    def test_large_integers_are_compared_without_float_rounding(self):
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

        self.assertEqual(len(build_execution_plan(desired, observed).operations), 1)

    def test_property_key_rejects_irrelevant_identity_fields(self):
        with self.assertRaisesRegex(ValueError, "container"):
            PropertyKey(
                kind="input_setting",
                collection="Main",
                container="irrelevant",
                source="Capture",
                setting="window",
            )

    def test_plan_is_isolated_from_mutating_source_values(self):
        payload = {"items": [1, 2]}
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="payload",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, payload)])
        observed = ObservedState(
            {key: ObservedValue.known_value({"items": [0]})}
        )
        plan = build_execution_plan(desired, observed)
        signature = plan.target_signature

        payload["items"].append(3)

        self.assertEqual(plan.target_signature, signature)
        self.assertEqual(plan.operations[0].target, {"items": [1, 2]})

    def test_structured_unknown_reason_is_preserved(self):
        key = PropertyKey.input_mute(
            collection="Main",
            input_name="Mic",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, True)])
        observed = ObservedState(
            {
                key: ObservedValue.unknown(
                    code="stale_reference",
                    reason="binding changed",
                )
            }
        )

        plan = build_execution_plan(desired, observed)

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.diagnostics[0].code, "stale_reference")
        self.assertEqual(plan.diagnostics[0].message, "binding changed")

if __name__ == "__main__":
    unittest.main()
