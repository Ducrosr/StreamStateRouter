from __future__ import annotations

import unittest

from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    PropertyKey,
    validate_state_coverage,
)


class StateCoverageTests(unittest.TestCase):
    def test_union_coverage_flags_property_left_to_previous_state(self):
        chat = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        input_overlay = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Input Overlay",
        )
        states = {
            "Overwatch": DesiredState.build(
                [
                    DesiredAssignment.create(chat, False),
                    DesiredAssignment.create(input_overlay, True),
                ]
            ),
            "Dofus": DesiredState.build(
                [
                    DesiredAssignment.create(chat, True),
                ]
            ),
        }

        issues = validate_state_coverage(states)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].state, "Dofus")
        self.assertEqual(issues[0].key, input_overlay)

    def test_explicit_delegation_satisfies_coverage(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Shinra",
            filter_name="Internal Cycle",
        )
        states = {
            "A": DesiredState.build([DesiredAssignment.create(key, True)]),
            "B": DesiredState.empty(),
        }

        issues = validate_state_coverage(
            states,
            delegated={"B": {key}},
        )

        self.assertEqual(issues, ())

    def test_explicit_required_set_can_validate_empty_state(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture",
            setting="rgb10a2_space",
        )

        issues = validate_state_coverage(
            {"Vanilla": DesiredState.empty()},
            required={key},
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].state, "Vanilla")
        self.assertEqual(issues[0].key, key)


if __name__ == "__main__":
    unittest.main()
