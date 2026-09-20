from __future__ import annotations

import unittest

from stream_state_router.obs.models import OBSAction
from stream_state_router.planning import (
    DesiredOwnershipConflict,
    PropertyKey,
    UnsupportedIntentAction,
    desired_assignments_from_actions,
    desired_state_from_action_sets,
)


class DeclarativeIntentTests(unittest.TestCase):
    def test_existing_stable_obs_actions_map_to_managed_properties(self):
        actions = (
            OBSAction("set_program_scene", {"scene": "Gameplay"}),
            OBSAction(
                "scene_item_enabled",
                {"scene": "Gameplay", "source": "Chat", "enabled": False},
            ),
            OBSAction(
                "source_filter_enabled",
                {"source": "Avatar", "filter": "Glitch", "enabled": True},
            ),
            OBSAction("input_mute", {"input": "Mic", "muted": True}),
            OBSAction("input_volume_db", {"input": "Music", "volume_db": -8.0}),
            OBSAction(
                "set_input_settings",
                {
                    "input": "Capture",
                    "settings": {
                        "window": "Overwatch",
                        "rgb10a2_space": "2100pq",
                    },
                },
            ),
        )

        assignments = desired_assignments_from_actions(
            actions,
            collection="Main",
            provenance="game:Overwatch",
        )
        state = desired_state_from_action_sets(
            [("game:Overwatch", actions)],
            collection="Main",
        )
        kinds = [item.key.kind for item in assignments]

        self.assertEqual(
            kinds,
            [
                "program_scene",
                "scene_item_visibility",
                "filter_enabled",
                "input_mute",
                "input_volume_db",
                "input_setting",
                "input_setting",
            ],
        )
        self.assertEqual(len(state.assignments), 7)
        self.assertTrue(
            all(item.provenance == ("game:Overwatch",) for item in state.assignments)
        )

    def test_disabled_action_does_not_create_intent(self):
        assignments = desired_assignments_from_actions(
            [
                OBSAction(
                    "scene_item_enabled",
                    {"scene": "Gameplay", "source": "Chat", "enabled": False},
                    enabled=False,
                )
            ],
            provenance="overlay:Base",
        )

        self.assertEqual(assignments, ())

    def test_cross_profile_property_conflict_is_rejected(self):
        first = (
            OBSAction(
                "scene_item_enabled",
                {"scene": "Gameplay", "source": "Chat", "enabled": True},
            ),
        )
        second = (
            OBSAction(
                "scene_item_enabled",
                {"scene": "Gameplay", "source": "Chat", "enabled": False},
            ),
        )

        with self.assertRaises(DesiredOwnershipConflict):
            desired_state_from_action_sets(
                [
                    ("overlay:Base", first),
                    ("game:Overwatch", second),
                ]
            )

    def test_cross_domain_same_value_still_has_ownership_conflict(self):
        action = (
            OBSAction(
                "scene_item_enabled",
                {
                    "scene": "Gameplay",
                    "source": "Chat",
                    "enabled": True,
                },
            ),
        )

        with self.assertRaises(DesiredOwnershipConflict):
            desired_state_from_action_sets(
                [
                    ("game:A", action),
                    ("overlay:B", action),
                ]
            )

    def test_duplicate_same_owner_is_allowed_when_value_matches(self):
        action = OBSAction(
            "scene_item_enabled",
            {
                "scene": "Gameplay",
                "source": "Chat",
                "enabled": True,
            },
        )

        state = desired_state_from_action_sets(
            [("game:A", (action, action))]
        )

        self.assertEqual(len(state.assignments), 1)

    def test_unknown_action_is_not_interpreted_as_macro(self):
        with self.assertRaises(UnsupportedIntentAction):
            desired_assignments_from_actions(
                [OBSAction("wait", {"milliseconds": 70})],
                provenance="test",
            )

    def test_property_factories_match_expected_identity(self):
        key = PropertyKey.input_mute(collection="Main", input_name="Mic")

        self.assertEqual(key.kind, "input_mute")
        self.assertEqual(key.collection, "Main")
        self.assertEqual(key.source, "Mic")


if __name__ == "__main__":
    unittest.main()
