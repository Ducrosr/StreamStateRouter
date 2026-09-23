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

    def test_process_running_condition_can_match_without_foreground_selector(self):
        rule = AppRule(
            "dofus-running",
            StreamState(game="Dofus"),
            priority=100,
            conditions={"process_running": "Dofus.exe"},
        )
        rules = RuleSet([rule], fallback=StreamState(game="Vanilla"))

        result = rules.resolve(
            ForegroundApp(1, 2, "explorer.exe"),
            {"running_processes": ("dofus.exe", "explorer.exe")},
        )

        self.assertEqual(result.kind, ResolutionKind.MATCH)
        self.assertEqual(result.rule_name, "dofus-running")
        self.assertEqual(result.state.game, "Dofus")

    def test_foreground_rule_can_outrank_background_process_rule(self):
        rules = RuleSet(
            [
                AppRule(
                    "overwatch",
                    StreamState(game="Overwatch"),
                    priority=200,
                    exe="Overwatch.exe",
                ),
                AppRule(
                    "dofus-running",
                    StreamState(game="Dofus"),
                    priority=100,
                    conditions={"process_running": "Dofus.exe"},
                ),
            ],
            fallback=StreamState(game="Vanilla"),
        )

        result = rules.resolve(
            ForegroundApp(1, 2, "Overwatch.exe"),
            {"running_processes": ("dofus.exe", "overwatch.exe")},
        )

        self.assertEqual(result.rule_name, "overwatch")
        self.assertEqual(result.state.game, "Overwatch")

    def test_process_running_rule_can_match_when_foreground_is_temporarily_missing(self):
        rules = RuleSet(
            [
                AppRule(
                    "dofus-running",
                    StreamState(game="Dofus"),
                    priority=100,
                    conditions={"process_running": "Dofus.exe"},
                )
            ]
        )

        result = rules.resolve(
            None,
            {"running_processes": ("dofus.exe",)},
        )

        self.assertEqual(result.kind, ResolutionKind.MATCH)
        self.assertEqual(result.state.game, "Dofus")

    def test_no_foreground_is_ignored_to_avoid_transient_fallback(self):
        result = RuleSet([]).resolve(None)
        self.assertEqual(result.kind, ResolutionKind.IGNORE)



    def test_launcher_candidates_are_process_context_not_routing_selectors(self):
        game = AppRule(
            "overwatch",
            StreamState(game="Overwatch"),
            priority=100,
            exe="Overwatch.exe",
            launcher="Battle.net.exe",
        )
        rules = RuleSet([game], fallback=StreamState(game="Vanilla"))

        self.assertTrue(rules.needs_process_context)
        candidates = rules.launcher_candidates(
            {"running_processes": ("battle.net.exe", "explorer.exe")}
        )

        self.assertEqual(candidates, (game,))
        routed = rules.resolve(
            ForegroundApp(1, 2, "explorer.exe"),
            {"running_processes": ("battle.net.exe", "explorer.exe")},
        )
        self.assertEqual(routed.kind, ResolutionKind.FALLBACK)

    def test_launcher_candidates_ignore_disabled_and_ignore_rules(self):
        match = AppRule(
            "game",
            StreamState(game="Game"),
            exe="game.exe",
            launcher="launcher.exe",
            enabled=False,
        )
        ignored = AppRule(
            "ignored",
            exe="ignored.exe",
            launcher="launcher.exe",
            behavior=ResolutionKind.IGNORE,
        )
        rules = RuleSet([match, ignored])

        self.assertEqual(
            rules.launcher_candidates(
                {"running_processes": ("launcher.exe",)}
            ),
            (),
        )

if __name__ == "__main__":
    unittest.main()
