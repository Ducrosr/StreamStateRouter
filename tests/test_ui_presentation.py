from __future__ import annotations

from stream_state_router.ui.presentation import build_dashboard_snapshot


def _explanation(*, kind="match", rule_name="Overwatch", game="Overwatch", domains=None, checks=None, paused=False):
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


def test_dashboard_snapshot_reports_successful_routing() -> None:
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

    assert snapshot.health_text == "Configuration appliquée"
    assert snapshot.health_style == "Good"
    assert snapshot.decision == "Overwatch → Overwatch"
    assert snapshot.reason == "Overwatch : Overwatch.exe au premier plan"
    capture = next(row for row in snapshot.differences if row.domain == "capture")
    assert capture.status_label == "Conforme"
    assert capture.desired == "HDR"
    assert capture.applied == "HDR"


def test_dashboard_snapshot_surfaces_failed_domain() -> None:
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

    assert snapshot.health_text == "Application en erreur"
    assert snapshot.health_style == "Bad"
    layout = next(row for row in snapshot.differences if row.domain == "layout")
    assert layout.status_label == "Erreur"
    assert layout.desired == "Overwatch"
    assert layout.applied == "Vanilla"
    assert layout.message == "Source introuvable"


def test_dashboard_snapshot_explains_fallback() -> None:
    snapshot = build_dashboard_snapshot(
        _explanation(kind="fallback", rule_name="fallback", game="Vanilla"),
        {},
        obs_enabled=True,
        obs_connected=True,
    )

    assert snapshot.decision == "Fallback → Vanilla"
    assert "configuration de secours" in snapshot.reason
    assert snapshot.health_style == "Warn"


def test_dashboard_snapshot_prioritizes_connection_health() -> None:
    snapshot = build_dashboard_snapshot(
        _explanation(paused=True),
        {"success": True},
        obs_enabled=True,
        obs_connected=False,
    )

    assert snapshot.health_style == "Bad"
    assert snapshot.health_text.startswith("OBS déconnecté")
    assert "routage suspendu" in snapshot.health_text


def test_dashboard_snapshot_does_not_reuse_previous_rule_success() -> None:
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

    assert snapshot.health_text == "Nouvelle décision · application en attente"
    assert snapshot.health_style == "Warn"
    game = next(row for row in snapshot.differences if row.domain == "game")
    assert game.desired == "League of Legends"
    assert game.applied == "—"
    assert game.status_label == "À vérifier"
