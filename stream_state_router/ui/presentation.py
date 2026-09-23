from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DOMAIN_LABELS = {
    "game": "Jeu",
    "overlay": "Overlay",
    "capture": "Capture",
    "audio": "Audio",
    "layout": "Layout",
}

STATE_KEYS = {
    "game": "Game",
    "overlay": "OverlayProfile",
    "capture": "CaptureProfile",
    "audio": "AudioProfile",
    "layout": "LayoutProfile",
}

_OK_STATUSES = {"", "current", "unchanged", "applied", "noop", "complete", "ok"}
_BAD_STATUSES = {"failed", "missing", "error"}
_WARN_STATUSES = {"blocked", "partial", "pending", "queued"}


@dataclass(frozen=True, slots=True)
class DashboardDifference:
    domain: str
    label: str
    desired: str
    applied: str
    status: str
    status_label: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    decision: str
    reason: str
    health_text: str
    health_style: str
    differences: tuple[DashboardDifference, ...]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _status_label(status: str, *, desired: str, applied: str) -> str:
    normalized = str(status or "").strip().casefold()
    if normalized in _BAD_STATUSES:
        return "Erreur"
    if normalized in _WARN_STATUSES:
        return "À corriger" if normalized != "pending" else "À appliquer"
    if desired and applied and desired != applied:
        return "À appliquer"
    if desired and not applied and normalized in _OK_STATUSES:
        return "À vérifier"
    if normalized in _OK_STATUSES:
        return "Conforme"
    return status or "À vérifier"


def _decision_text(routing: Mapping[str, object]) -> str:
    kind = str(routing.get("kind") or "").strip().casefold()
    rule_name = str(routing.get("rule_name") or "").strip()
    state = _mapping(routing.get("effective_state"))
    game = str(state.get("Game") or "").strip()

    if kind == "manual_override":
        return f"Override manuel → {game}" if game else "Override manuel"
    if kind == "fallback":
        return f"Fallback → {game}" if game else "Fallback"
    if kind == "ignore":
        return "État conservé (IGNORE)"
    if rule_name and game:
        return f"{rule_name} → {game}"
    if rule_name:
        return rule_name
    if game:
        return game
    return "Aucune décision active"


def _reason_text(routing: Mapping[str, object]) -> str:
    checks = routing.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, Mapping) or not bool(check.get("matched")):
                continue
            reason = str(check.get("reason") or "").strip()
            name = str(check.get("name") or "").strip()
            if reason and name:
                return f"{name} : {reason}"
            if reason:
                return reason
            if name:
                return f"Règle correspondante : {name}"

    kind = str(routing.get("kind") or "").strip().casefold()
    if kind == "manual_override":
        return "Une configuration manuelle remplace temporairement le routage automatique."
    if kind == "fallback":
        return "Aucune règle prioritaire ne correspond ; la configuration de secours est utilisée."
    if kind == "ignore":
        return "La règle correspondante demande de conserver l’état logique actuel."
    return "La décision provient du routage automatique courant."


def build_dashboard_snapshot(
    explanation: Mapping[str, object] | None,
    routing_status: Mapping[str, object] | None,
    *,
    obs_enabled: bool,
    obs_connected: bool,
) -> DashboardSnapshot:
    explanation = _mapping(explanation)
    routing_status = _mapping(routing_status)
    routing = _mapping(explanation.get("routing"))
    plan = _mapping(explanation.get("obs_plan"))
    effective = _mapping(routing.get("effective_state"))

    current_rule = str(routing.get("rule_name") or "").strip()
    status_rule = str(routing_status.get("rule_name") or "").strip()
    status_is_current = bool(routing_status) and (
        not current_rule or not status_rule or current_rule == status_rule
    )

    status_details: dict[str, Mapping[str, object]] = {}
    raw_status_details = (
        routing_status.get("domain_details") if status_is_current else None
    )
    if isinstance(raw_status_details, list):
        for item in raw_status_details:
            if not isinstance(item, Mapping):
                continue
            domain = str(item.get("domain") or "").strip()
            if domain:
                status_details[domain] = item

    plan_details: dict[str, Mapping[str, object]] = {}
    raw_plan_details = plan.get("domains")
    if isinstance(raw_plan_details, list):
        for item in raw_plan_details:
            if not isinstance(item, Mapping):
                continue
            domain = str(item.get("domain") or "").strip()
            if domain:
                plan_details[domain] = item

    differences: list[DashboardDifference] = []
    for domain, label in DOMAIN_LABELS.items():
        plan_row = plan_details.get(domain, {})
        status_row = status_details.get(domain, {})
        desired = str(
            status_row.get("desired_profile")
            or plan_row.get("desired_profile")
            or effective.get(STATE_KEYS[domain])
            or ""
        ).strip()
        applied = str(
            status_row.get("applied_profile")
            or plan_row.get("applied_profile")
            or ""
        ).strip()
        status = str(status_row.get("status") or plan_row.get("status") or "").strip()
        message = str(status_row.get("message") or plan_row.get("message") or "").strip()
        differences.append(
            DashboardDifference(
                domain=domain,
                label=label,
                desired=desired or "—",
                applied=applied or "—",
                status=status,
                status_label=_status_label(status, desired=desired, applied=applied),
                message=message,
            )
        )

    if not obs_enabled:
        health_text, health_style = "OBS désactivé", "Muted"
    elif not obs_connected:
        health_text, health_style = "OBS déconnecté", "Bad"
    elif routing_status and not status_is_current:
        health_text, health_style = "Nouvelle décision · application en attente", "Warn"
    elif routing_status:
        if bool(routing_status.get("success", False)):
            health_text, health_style = "Configuration appliquée", "Good"
        elif routing_status.get("failed_domains"):
            health_text, health_style = "Application en erreur", "Bad"
        else:
            health_text, health_style = "Application incomplète", "Warn"
    else:
        health_text, health_style = "Connecté · vérification en cours", "Warn"

    if bool(explanation.get("paused")):
        health_text = f"{health_text} · routage suspendu"
        if health_style == "Good":
            health_style = "Warn"

    return DashboardSnapshot(
        decision=_decision_text(routing),
        reason=_reason_text(routing),
        health_text=health_text,
        health_style=health_style,
        differences=tuple(differences),
    )
