from __future__ import annotations

from types import SimpleNamespace
import unittest

from stream_state_router.ui.presentation import (
    build_automation_rows,
    build_dashboard_snapshot,
    build_diagnostic_report,
    build_manual_override_presentation,
    build_obs_drift_presentation,
    build_simulation_report,
    render_capability_report_text,
    user_activity_from_runtime_event,
)


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

    def test_disconnected_obs_marks_domains_unverifiable(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(),
            {
                "success": False,
                "failed_domains": ["game", "capture"],
                "domain_details": [
                    {
                        "domain": "game",
                        "desired_profile": "Overwatch",
                        "applied_profile": "",
                        "status": "failed",
                        "message": "transport offline",
                    },
                    {
                        "domain": "capture",
                        "desired_profile": "HDR",
                        "applied_profile": "",
                        "status": "failed",
                        "message": "transport offline",
                    },
                ],
            },
            obs_enabled=True,
            obs_connected=False,
        )

        self.assertEqual(snapshot.health_text, "OBS déconnecté")
        self.assertTrue(
            all(
                row.status_label == "Non vérifiable"
                for row in snapshot.differences
            )
        )
        self.assertTrue(
            all(
                "Connexion OBS requise" in row.message
                for row in snapshot.differences
            )
        )

    def test_disabled_obs_marks_domains_as_disabled(self) -> None:
        snapshot = build_dashboard_snapshot(
            _explanation(),
            {},
            obs_enabled=False,
            obs_connected=False,
        )

        self.assertEqual(snapshot.health_text, "OBS désactivé")
        self.assertTrue(
            all(
                row.status_label == "OBS désactivé"
                for row in snapshot.differences
            )
        )

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


    def test_diagnostic_reports_disconnected_obs_and_dirty_config(self) -> None:
        report = build_diagnostic_report(
            _explanation(),
            {},
            obs_enabled=True,
            obs_connected=False,
            obs_last_error="timed out",
            config_dirty=True,
        )

        self.assertEqual(report.status_text, "Problème détecté")
        self.assertEqual(report.status_style, "Bad")
        titles = [item.title for item in report.items]
        self.assertIn("SSR n’est pas connecté à OBS", titles)
        self.assertIn(
            "Des modifications ne sont pas encore enregistrées",
            titles,
        )

    def test_diagnostic_surfaces_unavailable_drift_monitoring(self) -> None:
        report = build_diagnostic_report(
            _explanation(),
            {"success": True},
            obs_enabled=True,
            obs_connected=True,
            drift_status={
                "available": False,
                "checked_at": 123.0,
                "reason": "Budget du probe de dérive OBS dépassé",
                "duration_ms": 2050.0,
                "obs_requests": 9,
            },
        )

        item = next(
            item
            for item in report.items
            if item.title == "Surveillance de dérive OBS indisponible"
        )
        self.assertEqual(item.severity, "warning")
        self.assertIn("Budget", item.detail)
        self.assertIn("2050 ms", item.detail)
        self.assertIn("9 requête(s) OBS", item.detail)
        self.assertEqual(report.status_style, "Warn")

    def test_diagnostic_surfaces_partial_drift_coverage(self) -> None:
        report = build_diagnostic_report(
            _explanation(),
            {"success": True},
            obs_enabled=True,
            obs_connected=True,
            drift_status={
                "available": True,
                "checked_at": 123.0,
                "coverage_limited": True,
                "unknown_count": 2,
                "duration_ms": 80.0,
                "obs_requests": 4,
            },
        )

        item = next(
            item
            for item in report.items
            if item.title == "Surveillance de dérive OBS partielle"
        )
        self.assertEqual(item.severity, "warning")
        self.assertIn("2 propriété(s)", item.detail)
        self.assertEqual(report.status_style, "Warn")

    def test_diagnostic_translates_failed_domain_into_action(self) -> None:
        report = build_diagnostic_report(
            _explanation(),
            {
                "rule_name": "Overwatch",
                "success": False,
                "failed_domains": ["layout"],
                "domain_details": [
                    {
                        "domain": "layout",
                        "desired_profile": "Overwatch",
                        "applied_profile": "Vanilla",
                        "status": "missing",
                        "message": "LayoutProfile introuvable",
                    }
                ],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(report.status_text, "Problème détecté")
        item = next(
            item for item in report.items if item.title.startswith("Layout")
        )
        self.assertIn("introuvable", item.detail)
        self.assertIn("même nom", item.action)

    def test_diagnostic_fallback_is_information_not_failure(self) -> None:
        report = build_diagnostic_report(
            _explanation(
                kind="fallback",
                rule_name="fallback",
                game="Vanilla",
            ),
            {
                "rule_name": "fallback",
                "success": True,
                "domain_details": [],
            },
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertEqual(report.status_text, "Aucun problème détecté")
        self.assertEqual(report.status_style, "Good")
        self.assertTrue(
            any("secours" in item.title for item in report.items)
        )

    def test_manual_override_reason_describes_release_condition(self) -> None:
        explanation = _explanation(
            kind="manual_override",
            rule_name="manual",
            game="Manual",
        )
        explanation["routing"]["override_release_mode"] = "foreground_change"

        snapshot = build_dashboard_snapshot(
            explanation,
            {},
            obs_enabled=True,
            obs_connected=True,
        )

        self.assertIn(
            "prochain changement d’application",
            snapshot.reason,
        )

    def test_manual_override_presentation_humanizes_duration(self) -> None:
        view = build_manual_override_presentation(
            {
                "active": True,
                "release_mode": "duration",
                "remaining_seconds": 125,
            }
        )

        self.assertTrue(view.active)
        self.assertEqual(view.title, "Override manuel actif")
        self.assertIn("2 min 5 s", view.detail)
        self.assertEqual(view.style, "Warn")

    def test_manual_override_presentation_stream_end_armed_then_active(self) -> None:
        armed = build_manual_override_presentation(
            {
                "active": True,
                "release_mode": "stream_end",
                "stream_seen_active": False,
            }
        )
        active = build_manual_override_presentation(
            {
                "active": True,
                "release_mode": "stream_end",
                "stream_seen_active": True,
            }
        )

        self.assertIn("attend", armed.detail)
        self.assertIn("stream en cours", active.detail)

    def test_manual_override_presentation_inactive_returns_automatic(self) -> None:
        view = build_manual_override_presentation(
            {"active": False}
        )

        self.assertFalse(view.active)
        self.assertEqual(view.style, "Good")
        self.assertIn("Aucun override", view.detail)

    def test_user_activity_humanizes_override_mode(self) -> None:
        activity = user_activity_from_runtime_event(
            "manual_override",
            "Override manuel appliqué",
            {
                "active": True,
                "release_mode": "foreground_change",
            },
        )

        self.assertIsNotNone(activity)
        assert activity is not None
        self.assertIn("changement d’application", activity.detail)

    def test_user_activity_surfaces_override_auto_release(self) -> None:
        activity = user_activity_from_runtime_event(
            "manual_override_released",
            "Override manuel terminé",
            {"reason": "fin du stream"},
        )

        self.assertIsNotNone(activity)
        assert activity is not None
        self.assertEqual(activity.style, "Good")
        self.assertEqual(activity.message, "Override manuel terminé")
        self.assertEqual(activity.detail, "fin du stream")

    def test_user_activity_filters_runtime_noise(self) -> None:
        self.assertIsNone(
            user_activity_from_runtime_event(
                "routing_decision",
                "internal routing detail",
                {},
            )
        )
        connected = user_activity_from_runtime_event(
            "obs_connected",
            "connected",
            {},
        )
        self.assertIsNotNone(connected)
        assert connected is not None
        self.assertEqual(connected.message, "OBS connecté")

    def test_capability_report_text_is_readable_and_complete(self) -> None:
        report = SimpleNamespace(
            summary="1 erreur · 1 avertissement",
            items=(
                SimpleNamespace(
                    label="OBS WebSocket",
                    status_label="Erreur",
                    detail="timed out",
                    action="Vérifiez OBS.",
                ),
                SimpleNamespace(
                    label="HDR Windows",
                    status_label="Prêt",
                    detail="HDR activé",
                    action="",
                ),
            ),
            findings=(
                SimpleNamespace(
                    severity="warning",
                    title="LayoutProfile non référencé",
                    detail="Test A",
                    action="Conservez-le ou nettoyez-le.",
                ),
            ),
        )

        text = render_capability_report_text(
            report,
            version="2.1.0",
        )

        self.assertIn("Stream State Router 2.1.0", text)
        self.assertIn("OBS WebSocket: Erreur", text)
        self.assertIn("Détail : timed out", text)
        self.assertIn("HDR Windows: Prêt", text)
        self.assertIn("Santé de la configuration", text)
        self.assertIn("À vérifier : LayoutProfile non référencé", text)

    def test_drift_presentation_surfaces_partial_verified_change(self) -> None:
        view = build_obs_drift_presentation(
            {
                "available": True,
                "detected": True,
                "signature": "sig",
                "count": 2,
                "unknown_count": 1,
                "changes": [
                    {
                        "property": {
                            "kind": "input_mute",
                            "source": "Mic",
                        }
                    },
                    {
                        "property": {
                            "kind": "filter_enabled",
                            "source": "Avatar",
                            "filter": "Glitch",
                        }
                    },
                ],
            }
        )

        self.assertTrue(view.visible)
        self.assertEqual(view.signature, "sig")
        self.assertIn("2 propriétés", view.title)
        self.assertIn("input_mute · Mic", view.detail)
        self.assertIn("filter_enabled · Avatar/Glitch", view.detail)
        self.assertIn("Détection partielle", view.detail)

    def test_drift_presentation_hides_ignored_signature(self) -> None:
        view = build_obs_drift_presentation(
            {
                "available": True,
                "detected": True,
                "signature": "sig",
                "count": 1,
                "changes": [],
            },
            ignored_signature="sig",
        )

        self.assertFalse(view.visible)

    def test_user_activity_surfaces_drift_lifecycle(self) -> None:
        detected = user_activity_from_runtime_event(
            "obs_drift",
            "Écart détecté",
            {"count": 2},
        )
        cleared = user_activity_from_runtime_event(
            "obs_drift_cleared",
            "Écart résolu",
            {},
        )

        self.assertIsNotNone(detected)
        self.assertIsNotNone(cleared)
        assert detected is not None
        assert cleared is not None
        self.assertEqual(detected.style, "Warn")
        self.assertIn("2 propriété", detected.detail)
        self.assertEqual(cleared.style, "Good")

    def test_user_activity_summarizes_incomplete_routing(self) -> None:
        activity = user_activity_from_runtime_event(
            "routing_result",
            "Application OBS incomplète",
            {
                "success": False,
                "failed_domains": ["layout"],
                "blocked_domains": ["capture"],
                "pending_domains": [],
            },
        )

        self.assertIsNotNone(activity)
        assert activity is not None
        self.assertEqual(activity.style, "Bad")
        self.assertEqual(activity.message, "Application incomplète")
        self.assertEqual(
            activity.detail,
            "Échec : Layout · Bloqué : Capture",
        )

    def test_user_activity_deduplicates_pending_failed_domains(self) -> None:
        activity = user_activity_from_runtime_event(
            "routing_result",
            "Application OBS incomplète",
            {
                "success": False,
                "failed_domains": ["game", "capture", "game"],
                "blocked_domains": [],
                "pending_domains": ["game", "capture", "layout", "layout"],
            },
        )

        self.assertIsNotNone(activity)
        assert activity is not None
        self.assertEqual(
            activity.detail,
            "Échec : Jeu, Capture · En attente : Layout",
        )


    def test_simulation_report_is_read_only_plan_summary(self) -> None:
        report = build_simulation_report(
            _explanation(
                domains=[
                    {
                        "domain": "capture",
                        "desired_profile": "HDR",
                        "applied_profile": "Default",
                        "status": "planned",
                        "needs_apply": True,
                        "operations": [
                            {"type": "windows_hdr"},
                            {"type": "source_filter_settings"},
                        ],
                    }
                ]
            )
        )

        self.assertTrue(report.would_change)
        self.assertIn("Aucune commande", report.summary)
        capture = next(
            step for step in report.steps if step.domain == "capture"
        )
        self.assertEqual(capture.status_label, "À appliquer")
        self.assertEqual(capture.operation_count, 2)

    def test_simulation_report_ignore_never_claims_mutation(self) -> None:
        explanation = _explanation(kind="ignore")
        explanation["obs_plan"]["domains"] = [
            {
                "domain": "game",
                "desired_profile": "Overwatch",
                "applied_profile": "Vanilla",
                "status": "planned",
                "needs_apply": True,
                "operations": [{"type": "set_program_scene"}],
            }
        ]

        report = build_simulation_report(explanation)

        self.assertFalse(report.would_change)
        self.assertIn("conserverait", report.summary)


    def test_automation_rows_translate_rules_and_fallback(self) -> None:
        config = {
            "rules": [
                {
                    "name": "Overwatch",
                    "enabled": True,
                    "priority": 100,
                    "behavior": "match",
                    "exe": "Overwatch.exe",
                    "path": "C:\\Games\\Overwatch.exe",
                    "title_regex": "^Overwatch$",
                    "state": {
                        "Game": "Overwatch",
                        "OverlayProfile": "FPS",
                        "CaptureProfile": "HDR",
                        "AudioProfile": "Game",
                        "LayoutProfile": "FPS",
                    },
                    "conditions": {},
                },
                {
                    "name": "Launcher",
                    "enabled": False,
                    "priority": 200,
                    "behavior": "ignore",
                    "exe": "Launcher.exe",
                    "conditions": {},
                },
            ],
            "router": {
                "fallback_state": {
                    "Game": "Vanilla",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Vanilla",
                }
            },
        }

        rows = build_automation_rows(config)

        self.assertEqual(rows[0].name, "Launcher")
        self.assertEqual(rows[0].status, "Désactivée")
        self.assertIn("Launcher.exe", rows[0].trigger)
        self.assertEqual(rows[0].result, "Conserver l’état courant")
        overwatch = next(row for row in rows if row.name == "Overwatch")
        self.assertIn("Overwatch.exe", overwatch.trigger)
        self.assertIn("C:\\Games\\Overwatch.exe", overwatch.trigger)
        self.assertIn("^Overwatch$", overwatch.trigger)
        self.assertIn("Jeu=Overwatch", overwatch.result)
        self.assertEqual(rows[-1].name, "Configuration de secours")
        self.assertIn("aucune règle", rows[-1].trigger)

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
