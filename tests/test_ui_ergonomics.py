from __future__ import annotations

import unittest

from stream_state_router.ui.ergonomics import (
    build_contextual_action,
    build_decision_trail,
    build_draft_banner,
    build_status_strip,
)


class ErgonomicsPresentationTests(unittest.TestCase):
    def test_status_strip_separates_state_from_action(self) -> None:
        view = build_status_strip(
            expert_mode=False,
            edit_mode=False,
            paused=False,
            obs_enabled=True,
            obs_connected=True,
        )
        self.assertEqual(view.interface_text, "Simple")
        self.assertEqual(view.interface_action, "Passer en expert")
        self.assertEqual(view.config_text, "Protégée")
        self.assertEqual(view.config_action, "Modifier…")
        self.assertEqual(view.routing_text, "Automatique actif")
        self.assertEqual(view.routing_action, "Suspendre")
        self.assertEqual(view.obs_text, "Connecté")

    def test_edit_mode_makes_routing_state_explicit(self) -> None:
        view = build_status_strip(
            expert_mode=True,
            edit_mode=True,
            paused=False,
            obs_enabled=True,
            obs_connected=True,
        )
        self.assertEqual(view.config_text, "Édition active")
        self.assertEqual(view.routing_text, "Suspendu (édition)")
        self.assertEqual(view.config_action, "Terminer")

    def test_draft_banner_summarizes_change_categories(self) -> None:
        view = build_draft_banner(
            draft_dirty=True,
            saved_revision="saved",
            applied_revision="saved",
            change_categories=("Règles", "Profils", "Profils", "Layouts"),
        )
        self.assertTrue(view.visible)
        self.assertIn("4 modification", view.title)
        self.assertIn("1 règle", view.detail)
        self.assertIn("2 profil", view.detail)
        self.assertIn("1 layout", view.detail)

    def test_saved_runtime_mismatch_is_visible_without_dirty_draft(self) -> None:
        view = build_draft_banner(
            draft_dirty=False,
            saved_revision="new",
            applied_revision="old",
        )
        self.assertTrue(view.visible)
        self.assertIn("runtime", view.title)

    def test_decision_trail_exposes_app_rule_and_effective_profiles(self) -> None:
        trail = build_decision_trail(
            {
                "foreground": {"exe": "Overwatch.exe"},
                "routing": {
                    "kind": "match",
                    "rule_name": "Overwatch",
                    "effective_state": {
                        "Game": "Overwatch",
                        "OverlayProfile": "FPS",
                        "CaptureProfile": "HDR",
                        "AudioProfile": "Game",
                        "LayoutProfile": "FPS",
                    },
                },
            }
        )
        self.assertEqual(trail[0].label, "Overwatch.exe")
        self.assertEqual(trail[1].kind, "rule")
        self.assertEqual(trail[1].target, "Overwatch")
        self.assertTrue(
            any(
                step.domain == "layout" and step.target == "FPS"
                for step in trail
            )
        )

    def test_contextual_action_prioritizes_connection_then_draft(self) -> None:
        disconnected = build_contextual_action(
            obs_enabled=True,
            obs_connected=False,
            paused=False,
            draft_dirty=True,
            revision_mismatch=False,
            override_active=False,
            drift_detected=False,
        )
        self.assertEqual(disconnected.key, "obs_test")

        dirty = build_contextual_action(
            obs_enabled=True,
            obs_connected=True,
            paused=False,
            draft_dirty=True,
            revision_mismatch=False,
            override_active=False,
            drift_detected=True,
        )
        self.assertEqual(dirty.key, "review_draft")

    def test_contextual_action_surfaces_override_and_drift(self) -> None:
        override = build_contextual_action(
            obs_enabled=True,
            obs_connected=True,
            paused=False,
            draft_dirty=False,
            revision_mismatch=False,
            override_active=True,
            drift_detected=True,
        )
        self.assertEqual(override.key, "clear_override")

        drift = build_contextual_action(
            obs_enabled=True,
            obs_connected=True,
            paused=False,
            draft_dirty=False,
            revision_mismatch=False,
            override_active=False,
            drift_detected=True,
        )
        self.assertEqual(drift.key, "reapply")

    def test_contextual_action_reports_healthy_state(self) -> None:
        healthy = build_contextual_action(
            obs_enabled=True,
            obs_connected=True,
            paused=False,
            draft_dirty=False,
            revision_mismatch=False,
            override_active=False,
            drift_detected=False,
            difference_statuses=("current", "applied"),
        )
        self.assertFalse(healthy.actionable)
        self.assertEqual(healthy.style, "Good")


if __name__ == "__main__":
    unittest.main()
