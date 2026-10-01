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
    ("presentation", "Présentation", "PresentationProfile"),
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
    runtime_available: bool = True,
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
    if not runtime_available:
        routing_text = "Indisponible"
        routing_style = "Bad"
        routing_action = "—"
    elif effective_paused:
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
    difference_count: int = 0,
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
        count = max(0, int(difference_count or 0))
        detail = (
            f"{count} différence(s) visible(s) seront réévaluée(s) "
            "avant la réapplication."
            if count
            else "Des propriétés gérées ont été modifiées en dehors de SSR."
        )
        return ContextualActionPresentation(
            "reapply",
            "OBS ne correspond plus à la cible SSR",
            detail,
            (
                f"Réappliquer SSR ({count})"
                if count
                else "Réappliquer SSR"
            ),
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
        count = max(0, int(difference_count or 0))
        return ContextualActionPresentation(
            "reapply",
            "Des changements OBS sont en attente",
            (
                f"{count} différence(s) seront réévaluée(s) avant écriture."
                if count
                else (
                    "SSR connaît la cible mais elle n’est pas encore "
                    "totalement appliquée."
                )
            ),
            (
                f"Corriger les différences ({count})"
                if count
                else "Corriger les différences"
            ),
            "Warn",
        )

    return ContextualActionPresentation(
        "",
        "Aucune action requise",
        "SSR est aligné avec la configuration active.",
        "",
        "Good",
    )



@dataclass(frozen=True, slots=True)
class HealthBadgePresentation:
    text: str
    style: str
    detail: str = ""


def humanize_rule(rule: Mapping[str, object]) -> str:
    behavior = str(rule.get("behavior") or "match").strip().casefold()
    conditions = _mapping(rule.get("conditions"))
    selectors: list[str] = []
    exe = str(rule.get("exe") or "").strip()
    path = str(rule.get("path") or "").strip()
    title = str(rule.get("title_regex") or "").strip()
    process = str(conditions.get("process_running") or "").strip()
    if exe:
        selectors.append(f"{exe} au premier plan")
    if path:
        selectors.append(f"chemin {path}")
    if title:
        selectors.append(f"titre /{title}/")
    if process:
        selectors.append(f"{process} en cours")
    if conditions.get("streaming") is True:
        selectors.append("stream actif")
    elif conditions.get("streaming") is False:
        selectors.append("stream inactif")
    if conditions.get("recording") is True:
        selectors.append("enregistrement actif")
    elif conditions.get("recording") is False:
        selectors.append("enregistrement inactif")
    program_scene = str(conditions.get("program_scene") or "").strip()
    if program_scene:
        selectors.append(f"scène programme {program_scene}")

    when = " + ".join(selectors) if selectors else "conditions générales"
    if behavior == "ignore":
        return f"Quand {when} → conserver l’état courant"

    state = _mapping(rule.get("state"))
    targets = []
    for _domain, label, key in _STATE_KEYS:
        value = str(state.get(key) or "").strip()
        if value:
            targets.append(f"{label}={value}")
    then = " · ".join(targets) if targets else "aucun profil défini"
    delay = int(rule.get("apply_delay_ms") or 0)
    suffix = f" · après {delay} ms" if delay > 0 else ""
    return f"Quand {when} → {then}{suffix}"


def build_rule_health(
    rule: Mapping[str, object],
    config: Mapping[str, object],
) -> HealthBadgePresentation:
    if not bool(rule.get("enabled", True)):
        return HealthBadgePresentation(
            "○ Désactivée",
            "Muted",
            "La règle est conservée dans la configuration mais n’est pas évaluée.",
        )
    if str(rule.get("behavior") or "match").strip().casefold() != "match":
        return HealthBadgePresentation(
            "✓ Ignore",
            "Good",
            "Cette règle conserve volontairement l’état courant.",
        )

    state = _mapping(rule.get("state"))
    router = _mapping(config.get("router"))
    fallback = _mapping(router.get("fallback_state"))
    profiles = _mapping(config.get("profiles"))
    layouts = _mapping(config.get("layout_profiles"))
    missing: list[str] = []
    for domain, label, key in _STATE_KEYS:
        target = str(
            state.get(key)
            if key in state
            else fallback.get(key)
            or ""
        ).strip()
        # A missing key means "inherit". If the fallback is also omitted,
        # config validation/runtime canonical defaults remain authoritative;
        # the compact health badge must not invent a broken reference.
        if not target:
            continue
        if domain == "layout":
            pool = layouts
        elif domain == "presentation":
            pool = _mapping(config.get("presentation_profiles"))
        else:
            pool = _mapping(profiles.get(domain))
        if target not in pool:
            # Empty domain maps are allowed for canonical/unmanaged defaults.
            if not pool and key not in state:
                continue
            missing.append(f"{label}={target}")

    if missing:
        return HealthBadgePresentation(
            "⚠ Référence manquante",
            "Bad",
            "Profil(s) introuvable(s) : " + ", ".join(missing),
        )
    return HealthBadgePresentation(
        "✓ Cohérente",
        "Good",
        "Toutes les références de profil de cette règle existent.",
    )


@dataclass(frozen=True, slots=True)
class AttentionItemPresentation:
    key: str
    severity: str
    title: str
    detail: str
    action: str
    source: str = ""


def build_attention_items(
    report: Mapping[str, object] | None,
) -> tuple[AttentionItemPresentation, ...]:
    data = _mapping(report)
    items: list[AttentionItemPresentation] = []
    seen: set[tuple[str, str, str]] = set()

    config = _mapping(data.get("config"))
    errors = config.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
        for raw in errors:
            detail = str(raw or "").strip()
            if not detail:
                continue
            key = ("config", "Configuration invalide", detail)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                AttentionItemPresentation(
                    "config",
                    "error",
                    "Configuration invalide",
                    detail,
                    "Revoir le brouillon",
                    "configuration",
                )
            )

    capabilities = _mapping(data.get("capabilities"))
    raw_items = capabilities.get("items")
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        for raw in raw_items:
            item = _mapping(raw)
            status = str(item.get("status") or "").strip().casefold()
            if status not in {"warning", "error"}:
                continue
            title = str(item.get("label") or "Capacité").strip()
            detail = str(item.get("detail") or "").strip()
            item_key = str(item.get("key") or "capability").strip()
            dedupe = (item_key, title, detail)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            items.append(
                AttentionItemPresentation(
                    item_key,
                    status,
                    title,
                    detail,
                    str(item.get("action") or "").strip(),
                    "capability",
                )
            )

    raw_findings = capabilities.get("findings")
    if isinstance(raw_findings, Sequence) and not isinstance(
        raw_findings,
        (str, bytes),
    ):
        for raw in raw_findings:
            finding = _mapping(raw)
            severity = str(
                finding.get("severity") or "warning"
            ).strip().casefold()
            title = str(finding.get("title") or "Diagnostic").strip()
            detail = str(finding.get("detail") or "").strip()
            dedupe = ("finding", title, detail)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            items.append(
                AttentionItemPresentation(
                    "finding",
                    severity,
                    title,
                    detail,
                    str(finding.get("action") or "").strip(),
                    "finding",
                )
            )

    severity_order = {"error": 0, "warning": 1, "info": 2}
    return tuple(
        sorted(
            items,
            key=lambda item: (
                severity_order.get(item.severity, 9),
                item.title.casefold(),
                item.detail.casefold(),
            ),
        )
    )
