from __future__ import annotations

import unittest

from stream_state_router.ui.presentation import build_dashboard_snapshot


def _explanation(
    *,
    kind="match",
    rule_name="Overwatch",
    game="Overwatch",
    domains=None,
    checks=None,
    paused=False,
):
    return {
        "paused": paused,
        "routing": {
            "kind": kind,
            "rule_name": rule_name,
            "effective_state": {
                "Game": game,
                "OverlayProfile": "InGame",
                "CaptureProfile": "HDR",
                "AudioProfile": "Gaming",
                "LayoutProfile": "Overwatch",
            },
            "checks": checks or [],
        },
        "obs_plan": {
            "domains": domains or [],
        },
    }


class DashboardPresentationTests(unittest.TestCase):
    def test_reports_successful_routing(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(
                domains=[
                    {
                        "domain": "capture",
                        "desired_profile": "HDR",
                        "applied_profile": "HDR",
                        "status": "current",
                    }
                ],
                checks=[
                    {
                        "name": "Overwatch",
                        "matched": True,
                        "reason": "Overwatch.exe au premier plan",
                    }
                ],
            ),
            {
                "success": True,
                "domain_details": [
                    {
                        "domain": "capture",
                        "desired_profile": "HDR",
                        "applied_profile": "HDR",
                        "status": "applied",
                        "message": "",
                    }
                ],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(snapshot.health_text, "Configuration appliquée")
        self.assertEqual(snapshot.health_style, "Good")
        self.assertEqual(snapshot.decision, "Overwatch → Overwatch")
        self.assertEqual(
            snapshot.reason,
            "Overwatch : Overwatch.exe au premier plan",
        )
        capture = next(
            row for row in snapshot.differences if row.domain == "capture"
        )
        self.assertEqual(capture.status_label, "Conforme")
        self.assertEqual(capture.desired, "HDR")
        self.assertEqual(capture.applied, "HDR")

    def test_surfaces_failed_domain(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(),
            {
                "success": False,
                "failed_domains": ["layout"],
                "domain_details": [
                    {
                        "domain": "layout",
                        "desired_profile": "Overwatch",
                        "applied_profile": "Vanilla",
                        "status": "failed",
                        "message": "Source introuvable",
                    }
                ],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(snapshot.health_text, "Application en erreur")
        self.assertEqual(snapshot.health_style, "Bad")
        layout = next(
            row for row in snapshot.differences if row.domain == "layout"
        )
        self.assertEqual(layout.status_label, "Erreur")
        self.assertEqual(layout.desired, "Overwatch")
        self.assertEqual(layout.applied, "Vanilla")
        self.assertEqual(layout.message, "Source introuvable")

    def test_explains_fallback(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(kind="fallback", rule_name="fallback", game="Vanilla"),
            {},
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(snapshot.decision, "Fallback → Vanilla")
        self.assertIn("configuration de secours", snapshot.reason)
        self.assertEqual(snapshot.health_style, "Warn")

    def test_prioritizes_connection_health(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(paused=True),
            {"success": True},
            obs_enabled=True,
            obs_connected=False,
        )

        self.assertEqual(snapshot.health_style, "Bad")
        self.assertTrue(snapshot.health_text.startswith("OBS déconnecté"))
        self.assertIn("routage suspendu", snapshot.health_text)

    def test_does_not_reuse_previous_rule_success(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(rule_name="League", game="League of Legends"),
            {
                "rule_name": "Overwatch",
                "success": True,
                "domain_details": [
                    {
                        "domain": "game",
                        "desired_profile": "Overwatch",
                        "applied_profile": "Overwatch",
                        "status": "applied",
                        "message": "",
                    }
                ],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(
            snapshot.health_text,
            "Nouvelle décision · application en attente",
        )
        self.assertEqual(snapshot.health_style, "Warn")
        game = next(row for row in snapshot.differences if row.domain == "game")
        self.assertEqual(game.desired, "League of Legends")
        self.assertEqual(game.applied, "—")
        self.assertEqual(game.status_label, "À vérifier")

    def test_accepts_new_rule_when_state_is_already_converged(self) -> None:
        explanation = _explanation(
            rule_name="Dofus background",
            game="Dofus",
        )
        explanation["routing"]["would_change"] = False
        explanation["obs_plan"]["domains"] = [
            {
                "domain": "game",
                "desired_profile": "Dofus",
                "applied_profile": "Dofus",
                "status": "noop",
                "needs_apply": False,
            }
        ]

        snapshot = build_dashboard_snapshot(
            explanation,
            {
                "rule_name": "Dofus foreground",
                "success": True,
                "domain_details": [],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(snapshot.health_text, "Configuration déjà conforme")
        self.assertEqual(snapshot.health_style, "Good")

    def test_translates_planned_and_manual_hold_statuses(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(
                domains=[
                    {
                        "domain": "capture",
                        "desired_profile": "HDR",
                        "applied_profile": "Default",
                        "status": "planned",
                        "needs_apply": True,
                    },
                    {
                        "domain": "layout",
                        "desired_profile": "Overwatch",
                        "applied_profile": "Manual",
                        "status": "held",
                        "needs_apply": False,
                    },
                ]
            ),
            {},
            obs_enabled=True,
            obs_connected=True,
        )

        capture = next(
            row for row in snapshot.differences if row.domain == "capture"
        )
        layout = next(
            row for row in snapshot.differences if row.domain == "layout"
        )
        self.assertEqual(capture.status_label, "À appliquer")
        self.assertEqual(layout.status_label, "Maintenu manuellement")


if __name__ == "__main__":
    unittest.main()
