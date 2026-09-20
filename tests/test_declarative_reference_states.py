from __future__ import annotations

import unittest

from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    PropertyKey,
    validate_state_coverage,
)


def _visibility(source: str) -> PropertyKey:
    return PropertyKey.scene_item_visibility(
        collection="Streaming",
        container="In Game [Midgar]",
        source=source,
    )


class DeclarativeReferenceStateTests(unittest.TestCase):
    """Regression examples derived from the ASS migration inventory.

    The test contains scenario names only as reference data. No Dofus/Overwatch
    branch exists in production planner code.
    """

    def test_path_dependent_legacy_visibility_is_detected(self):
        chat = _visibility("[Module] Chat Twitch")
        radio = _visibility("[Module] Radio Midgar")
        input_overlay = _visibility("[Module] Input Overlay")

        overwatch = DesiredState.build(
            [
                DesiredAssignment.create(chat, False, provenance="Overwatch"),
                DesiredAssignment.create(radio, False, provenance="Overwatch"),
                DesiredAssignment.create(
                    input_overlay,
                    True,
                    provenance="Overwatch",
                ),
            ]
        )
        # Mirrors the legacy problem: the Dofus macro did not explicitly
        # restore Input Overlay, so its result could depend on the previous game.
        incomplete_dofus = DesiredState.build(
            [
                DesiredAssignment.create(chat, True, provenance="Dofus"),
                DesiredAssignment.create(radio, True, provenance="Dofus"),
            ]
        )

        issues = validate_state_coverage(
            {
                "Overwatch": overwatch,
                "Dofus": incomplete_dofus,
            }
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].state, "Dofus")
        self.assertEqual(issues[0].key, input_overlay)

    def test_explicit_final_visibility_removes_path_dependency(self):
        chat = _visibility("[Module] Chat Twitch")
        radio = _visibility("[Module] Radio Midgar")
        input_overlay = _visibility("[Module] Input Overlay")

        states = {
            "Overwatch": DesiredState.build(
                [
                    DesiredAssignment.create(chat, False),
                    DesiredAssignment.create(radio, False),
                    DesiredAssignment.create(input_overlay, True),
                ]
            ),
            "Dofus": DesiredState.build(
                [
                    DesiredAssignment.create(chat, True),
                    DesiredAssignment.create(radio, True),
                    DesiredAssignment.create(input_overlay, False),
                ]
            ),
            "Vanilla": DesiredState.build(
                [
                    DesiredAssignment.create(chat, True),
                    DesiredAssignment.create(radio, True),
                    DesiredAssignment.create(input_overlay, False),
                ]
            ),
        }

        self.assertEqual(validate_state_coverage(states), ())


if __name__ == "__main__":
    unittest.main()
