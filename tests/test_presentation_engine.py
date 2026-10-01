from __future__ import annotations

import unittest

from stream_state_router.presentation import (
    CueExecutor,
    build_presentation_registry,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class PresentationEngineTests(unittest.TestCase):
    def test_profile_inheritance_merges_theme_and_resources(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={
                "Base": {
                    "enter_cue": "base-enter",
                    "transition_profile": "Mako",
                    "widget_theme": "Midgar",
                    "animation_intensity": "low",
                    "theme": {
                        "accent": "#00ffff",
                        "panel_opacity": 0.8,
                    },
                },
                "Combat": {
                    "extends": "Base",
                    "enter_cue": "combat-enter",
                    "shader_set": "Combat",
                    "animation_intensity": "high",
                    "theme": {
                        "panel_opacity": 0.95,
                        "glitch": 0.6,
                    },
                },
            },
            cues_raw={},
        )

        resolved = registry.profile("Combat")

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.lineage, ("Base", "Combat"))
        self.assertEqual(resolved.enter_cue, "combat-enter")
        self.assertEqual(resolved.transition_profile, "Mako")
        self.assertEqual(resolved.shader_set, "Combat")
        self.assertEqual(resolved.widget_theme, "Midgar")
        self.assertEqual(resolved.animation_intensity, "high")
        self.assertEqual(
            dict(resolved.theme),
            {
                "accent": "#00ffff",
                "panel_opacity": 0.95,
                "glitch": 0.6,
            },
        )

    def test_component_policy_supports_inherit_custom_and_hidden(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={
                "Midgar": {
                    "components": {
                        "chat": {
                            "mode": "custom",
                            "resource": "chat/midgar",
                            "settings": {"opacity": 0.85},
                        },
                        "radio": {
                            "mode": "custom",
                            "resource": "radio/midgar",
                            "settings": {"glow": 0.4},
                        },
                    }
                },
                "Game": {
                    "extends": "Midgar",
                    "components": {
                        "chat": {"mode": "hidden"},
                        "radio": {
                            "mode": "custom",
                            "settings": {"glow": 0.8},
                        },
                        "events": {"mode": "inherit"},
                    },
                },
            },
            cues_raw={},
        )

        resolved = registry.profile("Game")

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.components["chat"].mode, "hidden")
        self.assertEqual(resolved.components["radio"].mode, "custom")
        self.assertEqual(
            resolved.components["radio"].resource,
            "radio/midgar",
        )
        self.assertEqual(
            dict(resolved.components["radio"].settings),
            {"glow": 0.8},
        )
        self.assertNotIn("events", resolved.components)

    def test_sound_sets_are_parsed_as_reusable_resources(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={
                "Combat": {"sound_set": "CombatSounds"},
            },
            cues_raw={},
            sound_sets_raw={
                "CombatSounds": {
                    "enter": [
                        {
                            "input": "SSR Combat In",
                            "action": "restart",
                        },
                        {
                            "input": "SSR Ambience",
                            "action": "play",
                        },
                    ],
                    "exit": [
                        {
                            "input": "SSR Combat Out",
                            "action": "restart",
                        }
                    ],
                }
            },
        )

        profile = registry.profile("Combat")
        sound_set = registry.sound_set("CombatSounds")

        self.assertIsNotNone(profile)
        self.assertIsNotNone(sound_set)
        assert profile is not None
        assert sound_set is not None
        self.assertEqual(profile.sound_set, "CombatSounds")
        self.assertEqual(
            [(item.input_name, item.action) for item in sound_set.enter],
            [
                ("SSR Combat In", "restart"),
                ("SSR Ambience", "play"),
            ],
        )
        self.assertEqual(
            [(item.input_name, item.action) for item in sound_set.exit],
            [("SSR Combat Out", "restart")],
        )

    def test_profile_inheritance_rejects_cycle(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={
                "A": {"extends": "B"},
                "B": {"extends": "A"},
            },
            cues_raw={},
        )

        with self.assertRaisesRegex(
            ValueError,
            "Héritage circulaire PresentationProfile",
        ):
            registry.profile("A")

    def test_cue_frames_are_sorted_and_actions_preserve_order(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={},
            cues_raw={
                "Enter": {
                    "frames": [
                        {
                            "at_ms": 200,
                            "actions": [
                                {
                                    "type": "source_filter_enabled",
                                    "params": {
                                        "source": "Global",
                                        "filter": "Glow",
                                        "enabled": True,
                                    },
                                }
                            ],
                        },
                        {
                            "at_ms": 0,
                            "actions": [
                                {
                                    "type": "set_program_scene",
                                    "params": {"scene": "In Game"},
                                },
                                {
                                    "type": "wait_ms",
                                    "params": {"duration_ms": 10},
                                    "enabled": False,
                                },
                            ],
                        },
                    ]
                }
            },
        )

        cue = registry.cue("Enter")

        self.assertIsNotNone(cue)
        assert cue is not None
        self.assertEqual([frame.at_ms for frame in cue.frames], [0, 200])
        self.assertEqual(
            [action.type for action in cue.frames[0].actions],
            ["set_program_scene", "wait_ms"],
        )
        self.assertEqual(cue.duration_ms, 200)
        self.assertEqual(cue.action_count, 3)

    def test_timeline_waits_cooperatively_and_serializes_frame_actions(self) -> None:
        registry = build_presentation_registry(
            profiles_raw={},
            cues_raw={
                "Enter": {
                    "frames": [
                        {
                            "at_ms": 0,
                            "actions": [
                                {
                                    "type": "first",
                                    "params": {"value": 1},
                                },
                                {
                                    "type": "second",
                                    "params": {"value": 2},
                                },
                            ],
                        },
                        {
                            "at_ms": 120,
                            "actions": [
                                {
                                    "type": "third",
                                    "params": {"value": 3},
                                }
                            ],
                        },
                    ]
                }
            },
        )
        cue = registry.cue("Enter")
        assert cue is not None

        clock = _Clock()
        calls: list[tuple[str, dict, dict | None]] = []
        yields: list[float] = []

        def execute(action, variables) -> None:
            calls.append(
                (
                    action.type,
                    dict(action.params),
                    dict(variables) if variables is not None else None,
                )
            )

        executor = CueExecutor(
            action_executor=execute,
            cooperative_yield=lambda: yields.append(clock.now),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        result = executor.execute(
            cue,
            variables={"Game": "Overwatch"},
        )

        self.assertEqual(
            [item[0] for item in calls],
            ["first", "second", "third"],
        )
        self.assertEqual(calls[0][2], {"Game": "Overwatch"})
        self.assertGreaterEqual(len(yields), 4)
        self.assertAlmostEqual(clock.now, 0.12, places=6)
        self.assertEqual(result.frames_executed, 2)
        self.assertEqual(result.actions_executed, 3)
        self.assertEqual(result.actions_skipped, 0)


if __name__ == "__main__":
    unittest.main()
