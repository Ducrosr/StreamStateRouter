from __future__ import annotations

import unittest

from stream_state_router.router.models import ForegroundApp, StreamState
from stream_state_router.router.rules import AppRule, ResolutionKind, RuleSet


class RuleSetTests(unittest.TestCase):
    def test_highest_priority_matching_rule_wins(self):
        app = ForegroundApp(1, 10, "Dofus.exe", r"C:\Games\Dofus\Dofus.exe", "Dofus")
        lower = AppRule("generic", StreamState(game="Generic"), priority=1, exe="*.exe")
        higher = AppRule("dofus", StreamState(game="Dofus Unity"), priority=100, exe="Dofus.exe")

        result = RuleSet([lower, higher]).resolve(app)

        self.assertEqual(result.kind, ResolutionKind.MATCH)
        self.assertEqual(result.rule_name, "dofus")
        self.assertEqual(result.state.game, "Dofus Unity")

    def test_ignore_rule_keeps_state_outside_resolution(self):
        app = ForegroundApp(1, 10, "Ankama Launcher.exe")
        ignore = AppRule(
            "launcher",
            priority=200,
            exe="Ankama Launcher.exe",
            behavior=ResolutionKind.IGNORE,
        )
        result = RuleSet([ignore]).resolve(app)
        self.assertEqual(result.kind, ResolutionKind.IGNORE)
        self.assertIsNone(result.state)

    def test_path_and_title_can_refine_a_rule(self):
        app = ForegroundApp(
            1,
            10,
            "game.exe",
            r"C:\Games\Example\game.exe",
            "Example — Ranked",
        )
        rule = AppRule(
            "ranked",
            StreamState(game="Example Ranked"),
            exe="game.exe",
            path=r"C:\Games\*\game.exe",
            title_regex="Ranked$",
        )

        result = RuleSet([rule]).resolve(app)

        self.assertEqual(result.rule_name, "ranked")
        self.assertEqual(result.state.game, "Example Ranked")

    def test_fallback_is_used_when_nothing_matches(self):
        fallback = StreamState(game="Vanilla", overlay_profile="Vanilla")
        app = ForegroundApp(1, 10, "notepad.exe")
        result = RuleSet([], fallback=fallback).resolve(app)
        self.assertEqual(result.kind, ResolutionKind.FALLBACK)
        self.assertEqual(result.state, fallback)

    def test_explanation_uses_same_resolution_for_match_ignore_and_fallback(self):
        fallback = StreamState(game="Vanilla")
        rules = RuleSet(
            [
                AppRule("ignore", priority=300, exe="launcher.exe", behavior=ResolutionKind.IGNORE),
                AppRule("ranked", StreamState(game="Ranked"), priority=200, exe="game.exe", title_regex="Ranked$"),
                AppRule("generic", StreamState(game="Generic"), priority=100, exe="game.exe"),
            ],
            fallback=fallback,
        )
        cases = [
            ForegroundApp(1, 1, "game.exe", window_title="Game Ranked"),
            ForegroundApp(2, 2, "launcher.exe", window_title="Launcher"),
            ForegroundApp(3, 3, "notepad.exe", window_title="Notes"),
        ]

        for app in cases:
            with self.subTest(app=app.exe_name, title=app.window_title):
                resolved = rules.resolve(app, {})
                explained = rules.explain(app, {})
                self.assertEqual(explained.resolution, resolved)

    def test_explanation_reports_condition_rejection(self):
        rule = AppRule(
            "stream-only",
            StreamState(game="Live"),
            exe="game.exe",
            conditions={"streaming": True},
        )
        rules = RuleSet([rule], fallback=StreamState(game="Vanilla"))
        app = ForegroundApp(1, 1, "game.exe")

        explanation = rules.explain(app, {"streaming": False})

        self.assertEqual(explanation.resolution.kind, ResolutionKind.FALLBACK)
        self.assertEqual(len(explanation.checks), 1)
        self.assertFalse(explanation.checks[0].matched)
        self.assertIn("streaming=False", explanation.checks[0].reason)

    def test_no_foreground_is_ignored_to_avoid_transient_fallback(self):
        result = RuleSet([]).resolve(None)
        self.assertEqual(result.kind, ResolutionKind.IGNORE)


if __name__ == "__main__":
    unittest.main()
