from __future__ import annotations

import unittest

from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.models import ForegroundApp, StreamState
from stream_state_router.router.rules import AppRule, ResolutionKind, RuleSet


class StateRouterEngineTests(unittest.TestCase):
    def setUp(self):
        self.overwatch = StreamState("Overwatch", "FPS", "HDR", "Game")
        self.dofus = StreamState("Dofus Unity", "Dofus", "Default", "Game")
        self.vanilla = StreamState()
        self.rules = RuleSet(
            [
                AppRule("ignore launcher", priority=200, exe="Ankama Launcher.exe", behavior=ResolutionKind.IGNORE),
                AppRule("Overwatch", self.overwatch, priority=100, exe="Overwatch.exe"),
                AppRule("Dofus", self.dofus, priority=90, exe="Dofus.exe"),
            ],
            fallback=self.vanilla,
        )

    @staticmethod
    def app(exe: str) -> ForegroundApp:
        return ForegroundApp(1, 10, exe, rf"C:\Games\{exe}", exe)

    def test_debounce_requires_stable_candidate(self):
        engine = StateRouterEngine(self.rules, debounce_ms=150)
        app = self.app("Overwatch.exe")
        self.assertIsNone(engine.observe(app, now=1.000))
        self.assertIsNone(engine.observe(app, now=1.149))
        change = engine.observe(app, now=1.151)
        self.assertEqual(change.current, self.overwatch)

    def test_fallback_can_have_longer_debounce(self):
        engine = StateRouterEngine(self.rules, debounce_ms=100, fallback_debounce_ms=350)
        engine.observe(self.app("Dofus.exe"), now=0.0)
        self.assertEqual(engine.observe(self.app("Dofus.exe"), now=0.1).current, self.dofus)
        unknown = self.app("explorer.exe")
        self.assertIsNone(engine.observe(unknown, now=1.0))
        self.assertIsNone(engine.observe(unknown, now=1.349))
        self.assertEqual(engine.observe(unknown, now=1.351).current, self.vanilla)

    def test_identical_state_is_not_reapplied(self):
        engine = StateRouterEngine(self.rules, debounce_ms=0)
        app = self.app("Dofus.exe")
        self.assertIsNotNone(engine.observe(app))
        self.assertIsNone(engine.observe(app))

    def test_brief_focus_change_is_ignored(self):
        engine = StateRouterEngine(self.rules, debounce_ms=150)
        overwatch = self.app("Overwatch.exe")
        dofus = self.app("Dofus.exe")
        engine.observe(overwatch, now=0.000)
        first = engine.observe(overwatch, now=0.150)
        self.assertEqual(first.current, self.overwatch)
        self.assertIsNone(engine.observe(dofus, now=1.000))
        self.assertIsNone(engine.observe(overwatch, now=1.050))
        self.assertEqual(engine.current_state, self.overwatch)

    def test_ignore_rule_preserves_current_state(self):
        engine = StateRouterEngine(self.rules, debounce_ms=0)
        engine.observe(self.app("Dofus.exe"))
        self.assertIsNone(engine.observe(self.app("Ankama Launcher.exe")))
        self.assertEqual(engine.current_state, self.dofus)

    def test_none_foreground_preserves_current_state(self):
        engine = StateRouterEngine(self.rules, debounce_ms=0)
        engine.observe(self.app("Dofus.exe"))
        self.assertIsNone(engine.observe(None))
        self.assertEqual(engine.current_state, self.dofus)

    def test_frozen_routing_context_bypasses_context_provider(self):
        target = StreamState("Live")
        rules = RuleSet(
            [
                AppRule(
                    "Live",
                    target,
                    exe="game.exe",
                    conditions={"streaming": True},
                )
            ],
            fallback=self.vanilla,
        )
        calls = []

        def provider():
            calls.append(True)
            raise AssertionError("context provider must not run")

        engine = StateRouterEngine(rules, debounce_ms=0, context_provider=provider)
        change = engine.observe(
            self.app("game.exe"),
            context={"streaming": True},
            use_context_provider=False,
        )

        self.assertEqual(change.current, target)
        self.assertEqual(calls, [])

    def test_manual_override_blocks_foreground_changes_until_cleared(self):
        engine = StateRouterEngine(self.rules, debounce_ms=0)
        custom = StreamState("Manual", "Manual", "Manual", "Manual")
        manual = engine.set_manual_override(custom)
        self.assertEqual(manual.current, custom)
        self.assertIsNone(engine.observe(self.app("Overwatch.exe")))
        resumed = engine.clear_manual_override(self.app("Dofus.exe"))
        self.assertEqual(resumed.current, self.dofus)


if __name__ == "__main__":
    unittest.main()
