from __future__ import annotations

from dataclasses import dataclass
import copy
import difflib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from ..obs.layouts import resolve_layout_profile
from ..router.models import ForegroundApp
from .config import build_ruleset, validate_config


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


@dataclass(frozen=True, slots=True)
class HealthFinding:
    severity: str
    title: str
    detail: str
    action: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityItem:
    key: str
    label: str
    status: str
    status_label: str
    detail: str = ""
    action: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    status: str
    summary: str
    items: tuple[CapabilityItem, ...]
    findings: tuple[HealthFinding, ...]


@dataclass(frozen=True, slots=True)
class ProfileUsage:
    kind: str
    owner: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProfileContentEntry:
    source_profile: str
    kind: str
    name: str
    target: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ProvenanceRow:
    domain: str
    label: str
    profile: str
    selected_by: str
    lineage: tuple[str, ...]
    content_summary: str
    usages: tuple[ProfileUsage, ...]


@dataclass(frozen=True, slots=True)
class ScenarioCheck:
    name: str
    priority: int
    behavior: str
    matched: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    kind: str
    rule_name: str
    state: Mapping[str, str] | None
    checks: tuple[ScenarioCheck, ...]


@dataclass(frozen=True, slots=True)
class ReferenceRepair:
    kind: str
    location: str
    path: tuple[str | int, ...]
    current: str
    candidate: str
    confidence: float
    reason: str

    @property
    def repairable(self) -> bool:
        return bool(self.candidate)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def configured_action_types(config: Mapping[str, Any]) -> frozenset[str]:
    found: set[str] = set()
    profiles = _mapping(config.get("profiles"))
    for domain_profiles in profiles.values():
        if not isinstance(domain_profiles, Mapping):
            continue
        for profile in domain_profiles.values():
            if not isinstance(profile, Mapping):
                continue
            actions = profile.get("actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, Mapping):
                    continue
                kind = str(action.get("type") or "").strip().casefold()
                if kind:
                    found.add(kind)
    return frozenset(found)


def _rule_signature(rule: Mapping[str, Any]) -> str:
    conditions = rule.get("conditions")
    payload = {
        "behavior": str(rule.get("behavior") or "match").strip().casefold(),
        "exe": str(rule.get("exe") or "").strip().casefold(),
        "path": str(rule.get("path") or "").strip().casefold(),
        "title_regex": str(rule.get("title_regex") or "").strip(),
        "conditions": (
            dict(conditions) if isinstance(conditions, Mapping) else {}
        ),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _profile_maps(
    config: Mapping[str, Any],
    domain: str,
) -> Mapping[str, Any]:
    if domain == "layout":
        return _mapping(config.get("layout_profiles"))
    return _mapping(_mapping(config.get("profiles")).get(domain))


def profile_usages(
    config: Mapping[str, Any],
    domain: str,
    profile_name: str,
) -> tuple[ProfileUsage, ...]:
    name = str(profile_name or "").strip()
    if not name or domain not in STATE_KEYS:
        return ()

    key = STATE_KEYS[domain]
    usages: list[ProfileUsage] = []
    fallback = _mapping(_mapping(config.get("router")).get("fallback_state"))
    if str(fallback.get(key) or "").strip() == name:
        usages.append(
            ProfileUsage(
                "fallback",
                "Configuration de secours",
                f"{key}={name}",
            )
        )

    raw_rules = config.get("rules")
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, Mapping):
                continue
            if str(rule.get("behavior") or "match").strip().casefold() != "match":
                continue
            state = _mapping(rule.get("state"))
            if str(state.get(key) or "").strip() != name:
                continue
            usages.append(
                ProfileUsage(
                    "rule",
                    str(rule.get("name") or "Règle sans nom"),
                    f"{key}={name}",
                )
            )

    profiles = _profile_maps(config, domain)
    for child_name, child in profiles.items():
        if not isinstance(child, Mapping):
            continue
        if str(child.get("extends") or "").strip() != name:
            continue
        usages.append(
            ProfileUsage(
                "inheritance",
                str(child_name),
                f"Hérite de {name}",
            )
        )

    return tuple(usages)


def profile_lineage(
    config: Mapping[str, Any],
    domain: str,
    profile_name: str,
) -> tuple[str, ...]:
    profiles = _profile_maps(config, domain)
    current = str(profile_name or "").strip()
    lineage: list[str] = []
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        lineage.append(current)
        profile = profiles.get(current)
        if not isinstance(profile, Mapping):
            break
        current = str(profile.get("extends") or "").strip()
    return tuple(lineage)


def profile_content_entries(
    config: Mapping[str, Any],
    domain: str,
    profile_name: str,
) -> tuple[ProfileContentEntry, ...]:
    lineage = profile_lineage(config, domain, profile_name)
    profiles = _profile_maps(config, domain)
    entries: list[ProfileContentEntry] = []

    if domain == "layout":
        for source_profile in reversed(lineage):
            profile = profiles.get(source_profile)
            if not isinstance(profile, Mapping):
                continue
            modules = profile.get("modules")
            if not isinstance(modules, Mapping):
                continue
            for module_name, module in modules.items():
                if not isinstance(module, Mapping):
                    continue
                elements = module.get("elements")
                sources = [
                    str(element.get("source") or "").strip()
                    for element in elements
                    if isinstance(elements, list)
                    and isinstance(element, Mapping)
                    and bool(element.get("included", True))
                    and str(element.get("source") or "").strip()
                ] if isinstance(elements, list) else []
                entries.append(
                    ProfileContentEntry(
                        source_profile=source_profile,
                        kind="module",
                        name=str(module_name),
                        target=", ".join(sources) or str(
                            profile.get("scene") or ""
                        ),
                        enabled=bool(module.get("managed", True)),
                    )
                )
        return tuple(entries)

    for source_profile in reversed(lineage):
        profile = profiles.get(source_profile)
        if not isinstance(profile, Mapping):
            continue
        actions = profile.get("actions")
        if not isinstance(actions, list):
            continue
        for index, action in enumerate(actions):
            if not isinstance(action, Mapping):
                continue
            kind = str(action.get("type") or "").strip()
            params = _mapping(action.get("params"))
            target = ""
            normalized = kind.casefold()
            if normalized == "set_program_scene":
                target = str(params.get("scene") or "")
            elif normalized == "scene_item_enabled":
                target = (
                    f"{params.get('scene', '')}/{params.get('source', '')}"
                )
            elif normalized in {
                "source_filter_enabled",
                "source_filter_settings",
            }:
                target = (
                    f"{params.get('source', '')}/{params.get('filter', '')}"
                )
            elif normalized in {
                "input_mute",
                "input_volume_db",
                "set_input_settings",
            }:
                target = str(params.get("input") or "")
            elif normalized == "app_audio_output":
                process = str(params.get("process") or "")
                device = str(params.get("device") or "")
                target = (
                    f"{process} → {device}"
                    if process or device
                    else ""
                )
            elif normalized == "windows_hdr":
                target = str(params.get("display") or "primary")
            elif normalized == "wait_ms":
                target = f"{params.get('duration_ms', 0)} ms"

            entries.append(
                ProfileContentEntry(
                    source_profile=source_profile,
                    kind=kind or "action",
                    name=str(
                        action.get("name")
                        or f"Action {index + 1}"
                    ),
                    target=target,
                    enabled=bool(action.get("enabled", True)),
                )
            )
    return tuple(entries)


def detach_profile_inheritance(
    config: Mapping[str, Any],
    domain: str,
    profile_name: str,
) -> tuple[dict[str, Any], bool]:
    name = str(profile_name or "").strip()
    if not name:
        return copy.deepcopy(dict(config)), False

    draft = copy.deepcopy(dict(config))
    profiles = _profile_maps(draft, domain)
    raw = profiles.get(name)
    if not isinstance(raw, dict):
        return draft, False
    parent = str(raw.get("extends") or "").strip()
    if not parent:
        return draft, False

    if domain == "layout":
        resolved = resolve_layout_profile(
            name,
            _mapping(draft.get("layout_profiles")),
        )
        resolved["extends"] = ""
        layout_profiles = draft.get("layout_profiles")
        if not isinstance(layout_profiles, dict):
            raise ValueError("config.layout_profiles doit être un objet.")
        layout_profiles[name] = resolved
        return draft, True

    lineage = profile_lineage(draft, domain, name)
    domain_profiles = _mapping(_mapping(draft.get("profiles")).get(domain))
    actions: list[object] = []
    conditions: dict[str, object] = {}
    for source_name in reversed(lineage):
        source = domain_profiles.get(source_name)
        if not isinstance(source, Mapping):
            continue
        source_actions = source.get("actions")
        if isinstance(source_actions, list):
            actions.extend(copy.deepcopy(source_actions))
        source_conditions = source.get("conditions")
        if isinstance(source_conditions, Mapping):
            conditions.update(copy.deepcopy(dict(source_conditions)))

    raw["actions"] = actions
    raw["conditions"] = conditions
    raw["extends"] = ""
    return draft, True


def _profile_content_summary(
    config: Mapping[str, Any],
    domain: str,
    lineage: Sequence[str],
) -> str:
    profiles = _profile_maps(config, domain)
    if domain == "layout":
        modules = 0
        scenes: list[str] = []
        for name in reversed(tuple(lineage)):
            profile = profiles.get(name)
            if not isinstance(profile, Mapping):
                continue
            raw_modules = profile.get("modules")
            if isinstance(raw_modules, Mapping):
                modules += len(raw_modules)
            scene = str(profile.get("scene") or "").strip()
            if scene and scene not in scenes:
                scenes.append(scene)
        scene_text = ", ".join(scenes) if scenes else "scène non définie"
        return f"{modules} module(s) · {scene_text}"

    actions = 0
    for name in reversed(tuple(lineage)):
        profile = profiles.get(name)
        if not isinstance(profile, Mapping):
            continue
        raw_actions = profile.get("actions")
        if isinstance(raw_actions, list):
            actions += sum(
                1
                for action in raw_actions
                if isinstance(action, Mapping)
                and bool(action.get("enabled", True))
            )
    return f"{actions} action(s) active(s)"


def build_effective_provenance(
    config: Mapping[str, Any],
    explanation: Mapping[str, Any] | None,
) -> tuple[ProvenanceRow, ...]:
    explanation = _mapping(explanation)
    routing = _mapping(explanation.get("routing"))
    state = _mapping(routing.get("effective_state"))
    kind = str(routing.get("kind") or "").strip().casefold()
    rule_name = str(routing.get("rule_name") or "").strip()

    if kind == "manual_override":
        selected_by = "Override manuel"
    elif kind == "fallback":
        selected_by = "Configuration de secours"
    elif kind == "ignore":
        selected_by = "Règle IGNORE · état conservé"
    elif rule_name:
        selected_by = f"Règle « {rule_name} »"
    else:
        selected_by = "Routage courant"

    rows: list[ProvenanceRow] = []
    for domain, label in DOMAIN_LABELS.items():
        profile = str(state.get(STATE_KEYS[domain]) or "").strip()
        lineage = profile_lineage(config, domain, profile)
        rows.append(
            ProvenanceRow(
                domain=domain,
                label=label,
                profile=profile or "—",
                selected_by=selected_by,
                lineage=lineage,
                content_summary=_profile_content_summary(
                    config,
                    domain,
                    lineage,
                ) if profile else "—",
                usages=profile_usages(config, domain, profile),
            )
        )
    return tuple(rows)


def _conditions_can_overlap(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    for key in ("streaming", "recording", "obs_enabled", "program_scene"):
        if key not in left or key not in right:
            continue
        if left.get(key) != right.get(key):
            return False
    return True


def _rules_can_overlap(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    try:
        if int(left.get("priority", 0)) != int(right.get("priority", 0)):
            return False
    except (TypeError, ValueError, OverflowError):
        return False

    left_conditions = _mapping(left.get("conditions"))
    right_conditions = _mapping(right.get("conditions"))
    if not _conditions_can_overlap(left_conditions, right_conditions):
        return False

    shared_selector = False
    for key in ("exe", "path", "title_regex"):
        lvalue = str(left.get(key) or "").strip()
        rvalue = str(right.get(key) or "").strip()
        if not lvalue or not rvalue:
            continue
        if lvalue.casefold() != rvalue.casefold():
            return False
        shared_selector = True

    left_process = str(
        left_conditions.get("process_running") or ""
    ).strip()
    right_process = str(
        right_conditions.get("process_running") or ""
    ).strip()
    if left_process and right_process:
        if left_process.casefold() == right_process.casefold():
            shared_selector = True
        # Different background processes can both be running at once and are
        # therefore still compatible; they simply do not establish the common
        # selector by themselves.

    return shared_selector


def build_static_health_findings(
    config: Mapping[str, Any],
) -> tuple[HealthFinding, ...]:
    findings: list[HealthFinding] = []

    for error in validate_config(config):
        findings.append(
            HealthFinding(
                "error",
                "Configuration invalide",
                str(error),
                "Corrigez cette erreur avant d’enregistrer et appliquer.",
            )
        )

    rules = [
        rule
        for rule in (
            config.get("rules")
            if isinstance(config.get("rules"), list)
            else []
        )
        if isinstance(rule, Mapping)
        and bool(rule.get("enabled", True))
    ]
    signatures: dict[str, list[str]] = {}
    for rule in rules:
        signatures.setdefault(_rule_signature(rule), []).append(
            str(rule.get("name") or "Règle sans nom")
        )
    duplicate_pairs: set[frozenset[str]] = set()
    for names in signatures.values():
        if len(names) <= 1:
            continue
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1 :]:
                duplicate_pairs.add(
                    frozenset((left_name, right_name))
                )
        findings.append(
            HealthFinding(
                "warning",
                "Règles potentiellement redondantes",
                "Sélecteurs identiques : " + ", ".join(names),
                "Vérifiez si ces règles doivent réellement coexister.",
            )
        )

    for left_index, left in enumerate(rules):
        left_name = str(left.get("name") or "Règle sans nom")
        for right in rules[left_index + 1 :]:
            right_name = str(right.get("name") or "Règle sans nom")
            if frozenset((left_name, right_name)) in duplicate_pairs:
                continue
            if not _rules_can_overlap(left, right):
                continue
            findings.append(
                HealthFinding(
                    "warning",
                    "Règles concurrentes à même priorité",
                    (
                        f"« {left_name} » et « {right_name} » peuvent "
                        "correspondre au même contexte avec la priorité "
                        f"{left.get('priority', 0)}."
                    ),
                    (
                        "Donnez-leur des priorités distinctes ou rendez leurs "
                        "conditions mutuellement exclusives."
                    ),
                )
            )

    profiles = _mapping(config.get("profiles"))
    for domain in ("game", "overlay", "capture", "audio"):
        domain_profiles = _mapping(profiles.get(domain))
        for name in domain_profiles:
            usages = profile_usages(config, domain, str(name))
            if usages:
                continue
            findings.append(
                HealthFinding(
                    "info",
                    f"Profil {DOMAIN_LABELS[domain]} non référencé",
                    str(name),
                    "Conservez-le s’il sert aux tests manuels, sinon il peut être nettoyé.",
                )
            )
    for name in _mapping(config.get("layout_profiles")):
        usages = profile_usages(config, "layout", str(name))
        if usages:
            continue
        findings.append(
            HealthFinding(
                "info",
                "LayoutProfile non référencé",
                str(name),
                "Conservez-le s’il sert aux tests manuels, sinon il peut être nettoyé.",
            )
        )

    return tuple(findings)


def _capability_item(
    key: str,
    label: str,
    status: str,
    detail: str,
    action: str = "",
) -> CapabilityItem:
    labels = {
        "ready": "Prêt",
        "warning": "À vérifier",
        "error": "Erreur",
        "disabled": "Désactivé",
        "unused": "Non utilisé",
        "unknown": "Inconnu",
    }
    return CapabilityItem(
        key=key,
        label=label,
        status=status,
        status_label=labels.get(status, status),
        detail=detail,
        action=action,
    )


def live_output_active(
    context: Mapping[str, Any] | None,
) -> bool:
    values = _mapping(context)
    return bool(
        values.get("streaming", False)
        or values.get("recording", False)
    )


def build_capability_report(
    config: Mapping[str, Any],
    *,
    obs_enabled: bool,
    obs_connected: bool,
    obs_error: str = "",
    catalog_status: Mapping[str, Any] | None = None,
    audio_probe: Mapping[str, Any] | None = None,
    hdr_probe: Mapping[str, Any] | None = None,
) -> CapabilityReport:
    action_types = configured_action_types(config)
    findings = build_static_health_findings(config)
    items: list[CapabilityItem] = []

    if not obs_enabled:
        items.append(
            _capability_item(
                "obs",
                "OBS WebSocket",
                "disabled",
                "L’intégration OBS est désactivée.",
                "Activez OBS dans Paramètres si SSR doit piloter OBS.",
            )
        )
    elif obs_connected:
        items.append(
            _capability_item(
                "obs",
                "OBS WebSocket",
                "ready",
                "Connexion runtime active.",
            )
        )
    else:
        items.append(
            _capability_item(
                "obs",
                "OBS WebSocket",
                "error",
                str(obs_error or "Connexion OBS indisponible."),
                "Vérifiez OBS, le port WebSocket et le mot de passe.",
            )
        )

    catalog = _mapping(catalog_status)
    if not obs_enabled:
        catalog_status_name = "disabled"
        catalog_detail = "Catalogue indisponible tant qu’OBS est désactivé."
    elif not obs_connected:
        catalog_status_name = "warning"
        catalog_detail = "Catalogue non vérifiable sans connexion OBS."
    elif not bool(catalog.get("available", False)):
        catalog_status_name = "warning"
        catalog_detail = "Le catalogue OBS n’a pas encore été synchronisé."
    elif bool(catalog.get("stale", False)):
        catalog_status_name = "warning"
        catalog_detail = (
            "Catalogue OBS périmé : "
            + str(catalog.get("stale_reason") or "resynchronisation requise")
        )
    else:
        catalog_status_name = "ready"
        catalog_detail = (
            f"{int(catalog.get('scenes', 0) or 0)} scène(s), "
            f"{int(catalog.get('inputs', 0) or 0)} input(s), "
            f"{int(catalog.get('scene_items', 0) or 0)} Scene Item(s)."
        )
    items.append(
        _capability_item(
            "catalog",
            "Catalogue OBS",
            catalog_status_name,
            catalog_detail,
            "Synchronisez le catalogue OBS si des références ont changé."
            if catalog_status_name == "warning"
            else "",
        )
    )

    audio_used = "app_audio_output" in action_types
    audio = _mapping(audio_probe)
    if not audio_used:
        items.append(
            _capability_item(
                "audio",
                "Routage audio Windows",
                "unused",
                "Aucune action app_audio_output n’est configurée.",
            )
        )
    else:
        status = str(audio.get("status") or "unknown")
        items.append(
            _capability_item(
                "audio",
                "Routage audio Windows",
                status,
                str(audio.get("detail") or "État non vérifié."),
                str(audio.get("action") or ""),
            )
        )

    hdr_used = "windows_hdr" in action_types
    hdr = _mapping(hdr_probe)
    if not hdr_used:
        items.append(
            _capability_item(
                "hdr",
                "HDR Windows",
                "unused",
                "Aucune action windows_hdr n’est configurée.",
            )
        )
    else:
        status = str(hdr.get("status") or "unknown")
        items.append(
            _capability_item(
                "hdr",
                "HDR Windows",
                status,
                str(hdr.get("detail") or "État non vérifié."),
                str(hdr.get("action") or ""),
            )
        )

    errors = sum(
        1 for item in items if item.status == "error"
    ) + sum(1 for item in findings if item.severity == "error")
    warnings = sum(
        1 for item in items if item.status == "warning"
    ) + sum(1 for item in findings if item.severity == "warning")
    if errors:
        status = "error"
        summary = f"{errors} erreur(s) · {warnings} avertissement(s)"
    elif warnings:
        status = "warning"
        summary = f"{warnings} point(s) à vérifier"
    else:
        status = "ready"
        summary = "Les capacités utilisées sont prêtes."

    return CapabilityReport(
        status=status,
        summary=summary,
        items=tuple(items),
        findings=findings,
    )


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def _normalized_name(value: str) -> str:
    return _NORMALIZE_RE.sub("", str(value or "").casefold())


def _best_candidate(
    current: str,
    candidates: Iterable[str],
) -> tuple[str, float, str]:
    value = str(current or "").strip()
    unique = sorted(
        {str(item).strip() for item in candidates if str(item).strip()},
        key=str.casefold,
    )
    if not value or not unique:
        return "", 0.0, ""

    case_matches = [
        item for item in unique if item.casefold() == value.casefold()
    ]
    if len(case_matches) == 1:
        return case_matches[0], 1.0, "Différence de casse uniquement"

    normalized = _normalized_name(value)
    normalized_matches = [
        item for item in unique
        if _normalized_name(item) == normalized and normalized
    ]
    if len(normalized_matches) == 1:
        return normalized_matches[0], 0.98, "Nom équivalent après normalisation"

    scored = sorted(
        (
            difflib.SequenceMatcher(
                None,
                value.casefold(),
                item.casefold(),
            ).ratio(),
            item,
        )
        for item in unique
    )
    score, candidate = scored[-1]
    second = scored[-2][0] if len(scored) > 1 else 0.0
    if score >= 0.72 and score - second >= 0.08:
        return candidate, float(score), "Nom proche détecté"
    return "", 0.0, ""


def simulate_rule_scenario(
    config: Mapping[str, Any],
    *,
    exe: str = "",
    path: str = "",
    title: str = "",
    streaming: bool | None = None,
    recording: bool | None = None,
    program_scene: str = "",
    obs_enabled: bool | None = None,
    running_processes: Sequence[str] = (),
) -> ScenarioReport:
    ruleset, _poll, _debounce, _fallback = build_ruleset(config)
    has_foreground = bool(
        str(exe or "").strip()
        or str(path or "").strip()
        or str(title or "").strip()
    )
    app = (
        ForegroundApp(
            hwnd=0,
            pid=0,
            exe_name=str(exe or "").strip(),
            process_path=str(path or "").strip(),
            window_title=str(title or "").strip(),
        )
        if has_foreground
        else None
    )
    context: dict[str, object] = {
        "program_scene": str(program_scene or "").strip(),
        "running_processes": tuple(
            str(item).strip()
            for item in running_processes
            if str(item).strip()
        ),
    }
    if streaming is not None:
        context["streaming"] = bool(streaming)
    if recording is not None:
        context["recording"] = bool(recording)
    if obs_enabled is not None:
        context["obs_enabled"] = bool(obs_enabled)

    explanation = ruleset.explain(app, context=context)
    mapping = explanation.as_mapping()
    raw_state = mapping.get("state")
    state = (
        {
            str(key): str(value)
            for key, value in raw_state.items()
        }
        if isinstance(raw_state, Mapping)
        else None
    )
    return ScenarioReport(
        kind=str(mapping.get("kind") or ""),
        rule_name=str(mapping.get("rule_name") or ""),
        state=state,
        checks=tuple(
            ScenarioCheck(
                name=str(item.name),
                priority=int(item.priority),
                behavior=str(item.behavior),
                matched=bool(item.matched),
                reason=str(item.reason),
            )
            for item in explanation.checks
        ),
    )


def _reference_issue(
    *,
    kind: str,
    location: str,
    path: tuple[str | int, ...],
    current: str,
    candidates: Iterable[str],
) -> ReferenceRepair | None:
    value = str(current or "").strip()
    candidate_values = {
        str(item).strip() for item in candidates if str(item).strip()
    }
    if not value or value in candidate_values:
        return None
    candidate, confidence, reason = _best_candidate(value, candidate_values)
    return ReferenceRepair(
        kind=kind,
        location=location,
        path=path,
        current=value,
        candidate=candidate,
        confidence=confidence,
        reason=reason or "Référence absente de la collection OBS actuelle",
    )


def scan_obs_reference_repairs(
    config: Mapping[str, Any],
    snapshot,
) -> tuple[ReferenceRepair, ...]:
    scenes = {
        str(item).strip()
        for item in getattr(snapshot, "scenes", ())
        if str(item).strip()
    }
    inputs = {
        str(getattr(item, "name", "") or "").strip()
        for item in getattr(snapshot, "inputs", ())
        if str(getattr(item, "name", "") or "").strip()
    }
    scene_items = tuple(getattr(snapshot, "scene_items", ()) or ())
    scene_sources: dict[str, set[str]] = {}
    all_scene_sources: set[str] = set()
    for item in scene_items:
        scene = str(getattr(item, "scene", "") or "").strip()
        source = str(getattr(item, "source", "") or "").strip()
        if not source:
            continue
        all_scene_sources.add(source)
        if scene:
            scene_sources.setdefault(scene, set()).add(source)

    filters = tuple(getattr(snapshot, "filters", ()) or ())
    filters_by_source: dict[str, set[str]] = {}
    for item in filters:
        source = str(getattr(item, "source", "") or "").strip()
        name = str(getattr(item, "name", "") or "").strip()
        if source and name:
            filters_by_source.setdefault(source, set()).add(name)

    all_sources = set(inputs) | all_scene_sources | set(filters_by_source) | scenes
    issues: list[ReferenceRepair] = []

    profiles = _mapping(config.get("profiles"))
    for domain, domain_profiles in profiles.items():
        if not isinstance(domain_profiles, Mapping):
            continue
        for profile_name, profile in domain_profiles.items():
            if not isinstance(profile, Mapping):
                continue
            actions = profile.get("actions")
            if not isinstance(actions, list):
                continue
            for index, action in enumerate(actions):
                if not isinstance(action, Mapping):
                    continue
                kind = str(action.get("type") or "").strip().casefold()
                params = action.get("params")
                if not isinstance(params, Mapping):
                    continue
                base = (
                    "profiles",
                    str(domain),
                    str(profile_name),
                    "actions",
                    index,
                    "params",
                )
                location = f"{domain}/{profile_name} · action {index + 1}"

                if kind == "set_program_scene":
                    issue = _reference_issue(
                        kind="Scène",
                        location=location,
                        path=(*base, "scene"),
                        current=str(params.get("scene") or ""),
                        candidates=scenes,
                    )
                    if issue:
                        issues.append(issue)
                    continue

                if kind == "scene_item_enabled":
                    scene = str(params.get("scene") or "").strip()
                    issue = _reference_issue(
                        kind="Scène",
                        location=location,
                        path=(*base, "scene"),
                        current=scene,
                        candidates=scenes,
                    )
                    if issue:
                        issues.append(issue)
                    source_candidates = (
                        scene_sources.get(scene, set())
                        if scene in scenes
                        else all_scene_sources
                    )
                    issue = _reference_issue(
                        kind="Source",
                        location=location,
                        path=(*base, "source"),
                        current=str(params.get("source") or ""),
                        candidates=source_candidates,
                    )
                    if issue:
                        issues.append(issue)
                    continue

                if kind in {"source_filter_enabled", "source_filter_settings"}:
                    source = str(params.get("source") or "").strip()
                    issue = _reference_issue(
                        kind="Source de filtre",
                        location=location,
                        path=(*base, "source"),
                        current=source,
                        candidates=all_sources,
                    )
                    if issue:
                        issues.append(issue)
                    filter_candidates = filters_by_source.get(source, set())
                    if not filter_candidates:
                        filter_candidates = {
                            name
                            for values in filters_by_source.values()
                            for name in values
                        }
                    issue = _reference_issue(
                        kind="Filtre",
                        location=location,
                        path=(*base, "filter"),
                        current=str(params.get("filter") or ""),
                        candidates=filter_candidates,
                    )
                    if issue:
                        issues.append(issue)
                    continue

                if kind in {"input_mute", "input_volume_db", "set_input_settings"}:
                    issue = _reference_issue(
                        kind="Input",
                        location=location,
                        path=(*base, "input"),
                        current=str(params.get("input") or ""),
                        candidates=inputs,
                    )
                    if issue:
                        issues.append(issue)

    layouts = _mapping(config.get("layout_profiles"))
    for profile_name, profile in layouts.items():
        if not isinstance(profile, Mapping):
            continue
        scene = str(profile.get("scene") or "").strip()
        issue = _reference_issue(
            kind="Scène de layout",
            location=f"LayoutProfile {profile_name}",
            path=("layout_profiles", str(profile_name), "scene"),
            current=scene,
            candidates=scenes,
        )
        if issue:
            issues.append(issue)

        modules = profile.get("modules")
        if not isinstance(modules, Mapping):
            continue
        for module_name, module in modules.items():
            if not isinstance(module, Mapping):
                continue
            elements = module.get("elements")
            if not isinstance(elements, list):
                continue
            for index, element in enumerate(elements):
                if not isinstance(element, Mapping):
                    continue
                source = str(element.get("source") or "").strip()
                issue = _reference_issue(
                    kind="Source de layout",
                    location=f"Layout {profile_name} · {module_name}",
                    path=(
                        "layout_profiles",
                        str(profile_name),
                        "modules",
                        str(module_name),
                        "elements",
                        index,
                        "source",
                    ),
                    current=source,
                    candidates=all_sources,
                )
                if issue:
                    issues.append(issue)

    policies = _mapping(config.get("activation_policies"))
    for policy_name, policy in policies.items():
        if not isinstance(policy, Mapping):
            continue
        targets = policy.get("targets")
        if not isinstance(targets, list):
            continue
        for index, target in enumerate(targets):
            if not isinstance(target, Mapping):
                continue
            kind = str(target.get("container_kind") or "scene").strip()
            container = str(target.get("container") or "").strip()
            if kind == "scene":
                issue = _reference_issue(
                    kind="Conteneur d’activation",
                    location=f"Activation {policy_name} · cible {index + 1}",
                    path=(
                        "activation_policies",
                        str(policy_name),
                        "targets",
                        index,
                        "container",
                    ),
                    current=container,
                    candidates=scenes,
                )
                if issue:
                    issues.append(issue)
            source_candidates = (
                scene_sources.get(container, set())
                if kind == "scene" and container in scenes
                else all_sources
            )
            issue = _reference_issue(
                kind="Source d’activation",
                location=f"Activation {policy_name} · cible {index + 1}",
                path=(
                    "activation_policies",
                    str(policy_name),
                    "targets",
                    index,
                    "source",
                ),
                current=str(target.get("source") or ""),
                candidates=source_candidates,
            )
            if issue:
                issues.append(issue)

    return tuple(issues)


def _path_get(root: object, path: Sequence[str | int]) -> object:
    current = root
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise KeyError(path)
            current = current[part]
        else:
            if not isinstance(current, Mapping) or part not in current:
                raise KeyError(path)
            current = current[part]
    return current


def _path_set(root: object, path: Sequence[str | int], value: object) -> None:
    if not path:
        raise KeyError("empty path")
    current = root
    for part in path[:-1]:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise KeyError(path)
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise KeyError(path)
            current = current[part]
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(current, list):
            raise KeyError(path)
        current[last] = value
    else:
        if not isinstance(current, dict):
            raise KeyError(path)
        current[last] = value


def apply_reference_repairs(
    config: Mapping[str, Any],
    repairs: Sequence[ReferenceRepair],
) -> tuple[dict[str, Any], int]:
    draft = copy.deepcopy(dict(config))
    applied = 0
    for repair in repairs:
        if not repair.repairable:
            continue
        try:
            current = _path_get(draft, repair.path)
        except (KeyError, IndexError):
            continue
        if str(current or "").strip() != repair.current:
            continue
        _path_set(draft, repair.path, repair.candidate)
        applied += 1
    return draft, applied
