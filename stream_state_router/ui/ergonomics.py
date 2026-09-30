from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


_STATE_KEYS = (
    ("game", "Jeu", "Game"),
    ("overlay", "Overlay", "OverlayProfile"),
    ("capture", "Capture", "CaptureProfile"),
    ("audio", "Audio", "AudioProfile"),
    ("layout", "Layout", "LayoutProfile"),
)


@dataclass(frozen=True, slots=True)
class StatusStripPresentation:
    interface_text: str
    interface_action: str
    config_text: str
    config_style: str
    config_action: str
    routing_text: str
    routing_style: str
    routing_action: str
    obs_text: str
    obs_style: str


def build_status_strip(
    *,
    expert_mode: bool,
    edit_mode: bool,
    paused: bool,
    obs_enabled: bool,
    obs_connected: bool,
    routing_incomplete: bool = False,
) -> StatusStripPresentation:
    if edit_mode:
        config_text, config_style, config_action = (
            "Édition active",
            "Warn",
            "Terminer",
        )
    else:
        config_text, config_style, config_action = (
            "Protégée",
            "Good",
            "Modifier…",
        )

    effective_paused = bool(paused or edit_mode)
    if effective_paused:
        routing_text = "Suspendu (édition)" if edit_mode else "Suspendu"
        routing_style = "Warn"
        routing_action = "Reprendre"
    else:
        routing_text = "Automatique actif"
        routing_style = "Good"
        routing_action = "Suspendre"

    if not obs_enabled:
        obs_text, obs_style = "Désactivé", "Muted"
    elif obs_connected and routing_incomplete:
        obs_text, obs_style = "Connecté · incomplet", "Warn"
    elif obs_connected:
        obs_text, obs_style = "Connecté", "Good"
    else:
        obs_text, obs_style = "Déconnecté", "Bad"

    return StatusStripPresentation(
        interface_text="Expert" if expert_mode else "Simple",
        interface_action=(
            "Passer en simple" if expert_mode else "Passer en expert"
        ),
        config_text=config_text,
        config_style=config_style,
        config_action=config_action,
        routing_text=routing_text,
        routing_style=routing_style,
        routing_action=routing_action,
        obs_text=obs_text,
        obs_style=obs_style,
    )


@dataclass(frozen=True, slots=True)
class DraftBannerPresentation:
    visible: bool
    title: str
    detail: str
    style: str


def build_draft_banner(
    *,
    draft_dirty: bool,
    saved_revision: str,
    applied_revision: str,
    change_categories: Iterable[str] = (),
) -> DraftBannerPresentation:
    saved = str(saved_revision or "")
    applied = str(applied_revision or "")
    counts = Counter(
        str(item or "").strip()
        for item in change_categories
        if str(item or "").strip()
    )
    change_count = sum(counts.values())

    if draft_dirty:
        title = (
            f"{change_count} modification(s) dans le brouillon"
            if change_count
            else "Modifications dans le brouillon"
        )
        ordered = [
            ("Règles", "règle(s)"),
            ("Profils", "profil(s)"),
            ("Layouts", "layout(s)"),
            ("Activations", "activation(s)"),
            ("Paramètres", "paramètre(s)"),
        ]
        details = [
            f"{counts[key]} {label}"
            for key, label in ordered
            if counts.get(key)
        ]
        return DraftBannerPresentation(
            True,
            title,
            " · ".join(details)
            or "Ces changements ne sont pas encore enregistrés ni appliqués.",
            "Warn",
        )

    if saved and applied and saved != applied:
        return DraftBannerPresentation(
            True,
            "Configuration enregistrée · runtime à resynchroniser",
            "La configuration persistée et la configuration active diffèrent.",
            "Warn",
        )

    return DraftBannerPresentation(
        False,
        "Configuration à jour",
        "Le brouillon, la configuration enregistrée et le runtime sont alignés.",
        "Good",
    )


@dataclass(frozen=True, slots=True)
class DecisionTrailStep:
    kind: str
    label: str
    target: str = ""
    domain: str = ""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def build_decision_trail(
    explanation: Mapping[str, object] | None,
) -> tuple[DecisionTrailStep, ...]:
    data = _mapping(explanation)
    foreground = _mapping(data.get("foreground"))
    routing = _mapping(data.get("routing"))
    state = _mapping(routing.get("effective_state"))

    steps: list[DecisionTrailStep] = []
    exe = str(
        foreground.get("exe")
        or foreground.get("exe_name")
        or ""
    ).strip()
    if exe:
        steps.append(DecisionTrailStep("app", exe, exe))

    kind = str(routing.get("kind") or "").strip().casefold()
    rule = str(routing.get("rule_name") or "").strip()
    if kind == "manual_override":
        steps.append(
            DecisionTrailStep("override", "Override manuel", "manual")
        )
    elif kind == "fallback":
        steps.append(
            DecisionTrailStep("fallback", "Configuration de secours", "fallback")
        )
    elif rule:
        steps.append(DecisionTrailStep("rule", rule, rule))

    for domain, label, key in _STATE_KEYS:
        value = str(state.get(key) or "").strip()
        if value:
            steps.append(
                DecisionTrailStep(
                    "profile",
                    f"{label}={value}",
                    value,
                    domain,
                )
            )
    return tuple(steps)


@dataclass(frozen=True, slots=True)
class ContextualActionPresentation:
    key: str
    title: str
    detail: str
    button_text: str
    style: str = "Muted"

    @property
    def actionable(self) -> bool:
        return bool(self.key and self.button_text)


def build_contextual_action(
    *,
    obs_enabled: bool,
    obs_connected: bool,
    paused: bool,
    draft_dirty: bool,
    revision_mismatch: bool,
    override_active: bool,
    drift_detected: bool,
    difference_statuses: Sequence[str] = (),
) -> ContextualActionPresentation:
    statuses = {
        str(item or "").strip().casefold()
        for item in difference_statuses
    }

    if not obs_enabled:
        return ContextualActionPresentation(
            "obs_settings",
            "OBS n’est pas piloté par SSR",
            "Activez l’intégration OBS pour appliquer automatiquement les profils.",
            "Configurer OBS",
            "Warn",
        )
    if not obs_connected:
        return ContextualActionPresentation(
            "obs_test",
            "OBS est déconnecté",
            "Vérifiez la connexion WebSocket avant de corriger l’état du stream.",
            "Tester la connexion",
            "Bad",
        )
    if draft_dirty:
        return ContextualActionPresentation(
            "review_draft",
            "Des modifications attendent votre validation",
            "Relisez les conséquences avant de les enregistrer et de les appliquer.",
            "Revoir les modifications",
            "Warn",
        )
    if revision_mismatch:
        return ContextualActionPresentation(
            "apply_config",
            "Le runtime n’utilise pas encore la configuration enregistrée",
            "Réappliquez la configuration pour réaligner SSR.",
            "Enregistrer et appliquer",
            "Warn",
        )
    if paused:
        return ContextualActionPresentation(
            "resume",
            "Le routage automatique est suspendu",
            "Aucun changement d’application ne sera appliqué tant que SSR reste suspendu.",
            "Reprendre le routage",
            "Warn",
        )
    if override_active:
        return ContextualActionPresentation(
            "clear_override",
            "Un override manuel remplace le routage automatique",
            "Revenez au routage automatique lorsque cet override n’est plus nécessaire.",
            "Revenir au routage automatique",
            "Warn",
        )
    if drift_detected:
        return ContextualActionPresentation(
            "reapply",
            "OBS ne correspond plus à la cible SSR",
            "Des propriétés gérées ont été modifiées en dehors de SSR.",
            "Réappliquer SSR",
            "Warn",
        )
    if statuses & {"failed", "missing", "error", "blocked", "partial"}:
        return ContextualActionPresentation(
            "diagnose",
            "Une partie de la configuration ne peut pas être appliquée",
            "Ouvrez le diagnostic guidé pour voir la cause et l’action corrective.",
            "Diagnostiquer",
            "Bad",
        )
    if statuses & {"pending", "queued", "planned"}:
        return ContextualActionPresentation(
            "reapply",
            "Des changements OBS sont en attente",
            "SSR connaît la cible mais elle n’est pas encore totalement appliquée.",
            "Corriger les différences",
            "Warn",
        )

    return ContextualActionPresentation(
        "",
        "Aucune action requise",
        "SSR est aligné avec la configuration active.",
        "",
        "Good",
    )
