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
    if normalized in {"pending", "queued", "planned"}:
        return "À appliquer"
    if normalized in {"blocked", "partial"}:
        return "À corriger"
    if normalized == "held":
        return "Maintenu manuellement"
    if normalized == "unmanaged":
        return "Non géré"
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

    plan_requires_apply = any(
        bool(row.get("needs_apply"))
        or str(row.get("status") or "").strip().casefold() == "planned"
        for row in plan_details.values()
    )
    plan_has_problem = any(
        str(row.get("status") or "").strip().casefold()
        in {"failed", "missing", "blocked", "partial"}
        for row in plan_details.values()
    )

    if not obs_enabled:
        health_text, health_style = "OBS désactivé", "Muted"
    elif not obs_connected:
        health_text, health_style = "OBS déconnecté", "Bad"
    elif routing_status and not status_is_current:
        if (
            routing.get("would_change") is False
            and not plan_requires_apply
            and not plan_has_problem
        ):
            health_text, health_style = "Configuration déjà conforme", "Good"
        else:
            health_text, health_style = (
                "Nouvelle décision · application en attente",
                "Warn",
            )
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


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    severity: str
    title: str
    detail: str
    action: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    status_text: str
    status_style: str
    summary: str
    items: tuple[DiagnosticItem, ...]


@dataclass(frozen=True, slots=True)
class UserActivityEntry:
    style: str
    message: str
    detail: str = ""


def _diagnostic_action(status: str, message: str) -> str:
    normalized = str(status or "").strip().casefold()
    text = str(message or "").casefold()
    if normalized == "missing" or "introuvable" in text:
        return (
            "Vérifiez que la scène, la source, le filtre ou le profil existe "
            "toujours sous le même nom."
        )
    if normalized == "blocked":
        return (
            "Vérifiez les conditions du profil puis relancez "
            "« Corriger les différences »."
        )
    if normalized in {"pending", "planned", "queued"}:
        return "Utilisez « Corriger les différences » pour demander une réapplication."
    if normalized in {"failed", "partial", "error"}:
        return (
            "Ouvrez le mode Expert si le détail ci-dessus ne suffit pas, "
            "puis consultez le journal technique."
        )
    return ""


def build_diagnostic_report(
    explanation: Mapping[str, object] | None,
    routing_status: Mapping[str, object] | None,
    *,
    obs_enabled: bool,
    obs_connected: bool,
    obs_last_error: str = "",
    config_dirty: bool = False,
    runtime_revision_mismatch: bool = False,
) -> DiagnosticReport:
    explanation = _mapping(explanation)
    routing_status = _mapping(routing_status)
    routing = _mapping(explanation.get("routing"))
    items: list[DiagnosticItem] = []

    if not obs_enabled:
        items.append(
            DiagnosticItem(
                "error",
                "OBS est désactivé dans SSR",
                "Aucune commande OBS ne peut être appliquée.",
                "Activez OBS dans Paramètres, puis enregistrez et appliquez.",
            )
        )
    elif not obs_connected:
        detail = str(obs_last_error or "").strip()
        items.append(
            DiagnosticItem(
                "error",
                "SSR n’est pas connecté à OBS",
                detail or "La connexion OBS WebSocket n’est pas disponible.",
                (
                    "Vérifiez qu’OBS est lancé, que WebSocket est actif et que "
                    "l’hôte, le port et le mot de passe correspondent."
                ),
            )
        )

    if config_dirty:
        items.append(
            DiagnosticItem(
                "warning",
                "Des modifications ne sont pas encore enregistrées",
                "Le runtime continue d’utiliser la dernière configuration appliquée.",
                "Utilisez « Enregistrer et appliquer » avant de tester le résultat.",
            )
        )
    elif runtime_revision_mismatch:
        items.append(
            DiagnosticItem(
                "warning",
                "La configuration enregistrée n’est pas celle du runtime",
                "Le runtime n’a pas encore adopté la dernière révision enregistrée.",
                "Utilisez « Enregistrer et appliquer » pour resynchroniser SSR.",
            )
        )

    if bool(explanation.get("paused")):
        items.append(
            DiagnosticItem(
                "warning",
                "Le routage automatique est suspendu",
                "SSR observe toujours l’état mais ne change pas automatiquement de profil.",
                "Cliquez sur « Reprendre » pour réactiver le routage automatique.",
            )
        )

    kind = str(routing.get("kind") or "").strip().casefold()
    if kind == "fallback":
        items.append(
            DiagnosticItem(
                "info",
                "La configuration de secours est active",
                (
                    "Aucune règle prioritaire ne correspond actuellement. "
                    "Ce comportement est normal si aucun jeu configuré n’est détecté."
                ),
                (
                    "Si vous attendiez un autre profil, ouvrez « Pourquoi cette décision ? » "
                    "pour voir quelle règle n’a pas correspondu."
                ),
            )
        )
    elif kind == "manual_override":
        items.append(
            DiagnosticItem(
                "info",
                "Un override manuel remplace le routage automatique",
                "La configuration manuelle reste prioritaire tant qu’elle est active.",
                (
                    "Utilisez « Revenir au routage automatique » si vous souhaitez "
                    "laisser SSR décider à nouveau."
                ),
            )
        )

    current_rule = str(routing.get("rule_name") or "").strip()
    status_rule = str(routing_status.get("rule_name") or "").strip()
    status_is_current = bool(routing_status) and (
        not current_rule or not status_rule or current_rule == status_rule
    )
    raw_details = routing_status.get("domain_details") if status_is_current else []
    if isinstance(raw_details, list):
        for row in raw_details:
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status") or "").strip().casefold()
            if status not in {
                "failed",
                "missing",
                "partial",
                "blocked",
                "pending",
                "planned",
                "queued",
            }:
                continue
            domain = str(row.get("domain") or "").strip()
            desired = str(row.get("desired_profile") or "").strip()
            applied = str(row.get("applied_profile") or "").strip()
            message = str(row.get("message") or "").strip()
            label = DOMAIN_LABELS.get(domain, domain or "Élément")
            detail_parts = []
            if desired:
                detail_parts.append(f"Attendu : {desired}")
            if applied:
                detail_parts.append(f"Actuel : {applied}")
            if message:
                detail_parts.append(message)
            items.append(
                DiagnosticItem(
                    "error" if status in {"failed", "missing"} else "warning",
                    f"{label} : {_status_label(status, desired=desired, applied=applied)}",
                    " · ".join(detail_parts) or "État incomplet.",
                    _diagnostic_action(status, message),
                )
            )

    if routing_status and not status_is_current:
        items.append(
            DiagnosticItem(
                "info",
                "Une nouvelle décision est en cours de stabilisation",
                (
                    "Le dernier résultat OBS appartient à la règle précédente. "
                    "SSR attend la décision courante avant de conclure."
                ),
                "Attendez la fin du debounce ou utilisez Actualiser après le changement.",
            )
        )
    elif (
        obs_enabled
        and obs_connected
        and routing_status
        and bool(routing_status.get("success", False))
        and not any(item.severity in {"error", "warning"} for item in items)
    ):
        return DiagnosticReport(
            "Aucun problème détecté",
            "Good",
            "SSR et OBS sont synchronisés pour la décision courante.",
            tuple(items),
        )

    has_error = any(item.severity == "error" for item in items)
    has_warning = any(item.severity == "warning" for item in items)
    if has_error:
        status_text, status_style = "Problème détecté", "Bad"
    elif has_warning:
        status_text, status_style = "Vérification recommandée", "Warn"
    else:
        status_text, status_style = "Aucune anomalie bloquante", "Good"

    first_actionable = next(
        (item for item in items if item.severity in {"error", "warning"}),
        items[0] if items else None,
    )
    summary = (
        first_actionable.title
        if first_actionable is not None
        else "Aucune information de diagnostic supplémentaire."
    )
    return DiagnosticReport(
        status_text,
        status_style,
        summary,
        tuple(items),
    )


def user_activity_from_runtime_event(
    kind: str,
    message: str,
    payload: Mapping[str, object] | None = None,
) -> UserActivityEntry | None:
    kind = str(kind or "").strip()
    payload = _mapping(payload)
    if kind == "obs_connected":
        return UserActivityEntry("Good", "OBS connecté")
    if kind == "obs_disconnected":
        return UserActivityEntry(
            "Bad",
            "OBS déconnecté",
            str(message or "").strip(),
        )
    if kind == "obs_error":
        return UserActivityEntry("Bad", "Erreur OBS", str(message or "").strip())
    if kind == "pause":
        paused = "suspendu" in str(message or "").casefold()
        return UserActivityEntry(
            "Warn" if paused else "Good",
            "Automatisation suspendue" if paused else "Automatisation reprise",
        )
    if kind == "routing_result":
        if bool(payload.get("success", False)):
            return UserActivityEntry(
                "Good",
                "Configuration appliquée",
                str(payload.get("rule_name") or "").strip(),
            )
        details = (
            list(payload.get("failed_domains") or [])
            + list(payload.get("blocked_domains") or [])
            + list(payload.get("pending_domains") or [])
        )
        return UserActivityEntry(
            "Bad" if payload.get("failed_domains") else "Warn",
            "Application incomplète",
            ", ".join(str(item) for item in details if str(item).strip()),
        )
    return None
