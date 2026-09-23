from __future__ import annotations

from dataclasses import dataclass
import copy
from typing import Any, Mapping

from .scene_collection import SceneCollectionImporter, SceneCollectionSnapshot


_STATE_KEYS = {
    "game": "Game",
    "overlay": "OverlayProfile",
    "capture": "CaptureProfile",
    "audio": "AudioProfile",
    "layout": "LayoutProfile",
}

_ACTION_DOMAINS = ("game", "overlay", "capture", "audio")
_DOMAIN_LABELS = {
    "game": "Jeu",
    "overlay": "Overlay",
    "capture": "Capture",
    "audio": "Audio",
    "layout": "Layout",
}


@dataclass(frozen=True, slots=True)
class CurrentStateCaptureOptions:
    name: str
    process: str
    existing_rule_name: str = ""
    include_input_settings: bool = True
    include_audio_state: bool = True
    include_filters: bool = True
    include_visibility: bool = True
    include_layout: bool = True
    # Keep programmatic callers backward-compatible: historically every
    # captured OBS action was stored in GameProfile. The guided UI supplies
    # explicit, more semantic destinations.
    input_settings_domain: str = "game"
    audio_state_domain: str = "game"
    filters_domain: str = "game"
    visibility_domain: str = "game"


@dataclass(frozen=True, slots=True)
class CurrentStateCaptureReport:
    mode: str
    rule_name: str
    process: str
    scene: str
    game_profile: str
    layout_profile: str
    added_actions: int
    replaced_actions: int
    layout_captured: bool
    captured_inputs: int
    captured_filters: int
    captured_scene_items: int
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    owned_profiles: tuple[tuple[str, str], ...] = ()

    def summary_lines(self) -> tuple[str, ...]:
        mode = "mise à jour" if self.mode == "update" else "création"
        lines = [
            f"Mode : {mode}",
            f"Règle : {self.rule_name}",
            f"Processus : {self.process}",
            f"Scène OBS : {self.scene or '—'}",
            f"GameProfile : {self.game_profile}",
            f"LayoutProfile : {self.layout_profile or 'inchangé'}",
            (
                "Actions OBS du GameProfile : "
                f"{self.added_actions} ajoutée(s), "
                f"{self.replaced_actions} remplacée(s)"
            ),
            (
                "Périmètre capturé : "
                f"{self.captured_inputs} source(s), "
                f"{self.captured_filters} filtre(s), "
                f"{self.captured_scene_items} Scene Item(s)"
            ),
            (
                "Layout courant : capturé"
                if self.layout_captured
                else "Layout courant : non capturé"
            ),
        ]
        if self.owned_profiles:
            ownership = " · ".join(
                f"{_DOMAIN_LABELS.get(domain, domain)} → {profile}"
                for domain, profile in self.owned_profiles
            )
            lines.append(f"Prise de contrôle SSR : {ownership}")
        lines.extend(self.notes)
        if self.warnings:
            lines.append(f"Avertissements : {len(self.warnings)}")
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class CurrentStateCaptureDraft:
    config: dict[str, Any]
    report: CurrentStateCaptureReport


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def find_process_rules(
    config: Mapping[str, Any],
    process: str,
) -> tuple[Mapping[str, Any], ...]:
    wanted = str(process or "").strip().casefold()
    if not wanted:
        return ()
    raw_rules = config.get("rules")
    rules = raw_rules if isinstance(raw_rules, list) else []
    matches = [
        rule
        for rule in rules
        if isinstance(rule, Mapping)
        and str(rule.get("behavior") or "match").strip().casefold() == "match"
        and str(rule.get("exe") or "").strip().casefold() == wanted
    ]
    return tuple(
        sorted(
            matches,
            key=lambda rule: int(rule.get("priority", 0))
            if str(rule.get("priority", 0)).lstrip("-").isdigit()
            else 0,
            reverse=True,
        )
    )


def suggest_capture_name(
    config: Mapping[str, Any],
    base_name: str,
) -> str:
    base = str(base_name or "").strip() or "Nouvelle configuration"
    used: set[str] = set()
    raw_rules = config.get("rules")
    if isinstance(raw_rules, list):
        used.update(
            str(rule.get("name") or "").strip().casefold()
            for rule in raw_rules
            if isinstance(rule, Mapping)
            and str(rule.get("name") or "").strip()
        )
    profiles = _mapping(config.get("profiles"))
    for domain_profiles in profiles.values():
        if not isinstance(domain_profiles, Mapping):
            continue
        used.update(
            str(name).strip().casefold()
            for name in domain_profiles
            if str(name).strip()
        )
    layouts = _mapping(config.get("layout_profiles"))
    used.update(str(name).strip().casefold() for name in layouts)

    if base.casefold() not in used:
        return base
    for index in range(2, 1000):
        candidate = f"{base} {index}"
        if candidate.casefold() not in used:
            return candidate
    raise ValueError("Impossible de générer un nom de configuration unique.")


def _fallback_state(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(config.get("router")).get("fallback_state"))


def _logical_value(
    logical_state: Mapping[str, Any],
    fallback: Mapping[str, Any],
    key: str,
    default: str,
) -> str:
    return str(
        logical_state.get(key)
        or fallback.get(key)
        or default
    ).strip()


def _rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.setdefault("rules", [])
    if not isinstance(raw, list):
        raise ValueError("config.rules doit être une liste.")
    return [
        rule
        for rule in raw
        if isinstance(rule, dict)
    ]


def _find_rule_mutable(
    config: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    for rule in _rules(config):
        if str(rule.get("name") or "").strip().casefold() == wanted:
            return rule
    return None


def _profile_referenced_elsewhere(
    config: Mapping[str, Any],
    *,
    domain: str,
    profile_name: str,
    rule_name: str,
) -> bool:
    key = _STATE_KEYS[domain]
    wanted_profile = str(profile_name or "").strip()
    wanted_rule = str(rule_name or "").strip().casefold()
    if not wanted_profile:
        return False

    fallback = _fallback_state(config)
    if str(fallback.get(key) or "").strip() == wanted_profile:
        return True

    raw_rules = config.get("rules")
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, Mapping):
                continue
            if str(rule.get("name") or "").strip().casefold() == wanted_rule:
                continue
            state = _mapping(rule.get("state"))
            if str(state.get(key) or "").strip() == wanted_profile:
                return True

    if domain == "layout":
        domain_profiles = _mapping(config.get("layout_profiles"))
    else:
        domain_profiles = _mapping(_mapping(config.get("profiles")).get(domain))
    for child_name, child in domain_profiles.items():
        if str(child_name) == wanted_profile or not isinstance(child, Mapping):
            continue
        if str(child.get("extends") or "").strip() == wanted_profile:
            return True
    return False


def _ensure_profile(
    config: dict[str, Any],
    domain: str,
    name: str,
) -> dict[str, Any]:
    profiles = config.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("config.profiles doit être un objet.")
    domain_profiles = profiles.setdefault(domain, {})
    if not isinstance(domain_profiles, dict):
        raise ValueError(f"config.profiles.{domain} doit être un objet.")
    profile = domain_profiles.get(name)
    if profile is None:
        profile = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }
        domain_profiles[name] = profile
    if not isinstance(profile, dict):
        raise ValueError(f"Profil invalide : {domain}/{name}")
    return profile


def _normalize_action_domain(value: str, *, field: str) -> str:
    domain = str(value or "").strip().casefold()
    if domain not in _ACTION_DOMAINS:
        raise ValueError(
            f"{field} doit cibler game, overlay, capture ou audio."
        )
    return domain


def _clone_action_profile(
    config: dict[str, Any],
    *,
    domain: str,
    source_name: str,
    target_name: str,
) -> dict[str, Any]:
    profiles = config.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("config.profiles doit être un objet.")
    domain_profiles = profiles.setdefault(domain, {})
    if not isinstance(domain_profiles, dict):
        raise ValueError(f"config.profiles.{domain} doit être un objet.")

    source = domain_profiles.get(source_name)
    if isinstance(source, Mapping):
        profile = copy.deepcopy(dict(source))
    else:
        profile = {
            "actions": [],
            "extends": "",
            "conditions": {},
        }
    domain_profiles[target_name] = profile
    return profile


def _capture_groups(
    options: CurrentStateCaptureOptions,
) -> dict[str, dict[str, bool]]:
    groups: dict[str, dict[str, bool]] = {}

    def add(
        *,
        included: bool,
        domain_value: str,
        flag: str,
        field: str,
    ) -> None:
        if not included:
            return
        domain = _normalize_action_domain(
            domain_value,
            field=field,
        )
        group = groups.setdefault(
            domain,
            {
                "include_input_settings": False,
                "include_audio_state": False,
                "include_filters": False,
                "include_visibility": False,
            },
        )
        group[flag] = True

    add(
        included=bool(options.include_input_settings),
        domain_value=options.input_settings_domain,
        flag="include_input_settings",
        field="input_settings_domain",
    )
    add(
        included=bool(options.include_audio_state),
        domain_value=options.audio_state_domain,
        flag="include_audio_state",
        field="audio_state_domain",
    )
    add(
        included=bool(options.include_filters),
        domain_value=options.filters_domain,
        flag="include_filters",
        field="filters_domain",
    )
    add(
        included=bool(options.include_visibility),
        domain_value=options.visibility_domain,
        flag="include_visibility",
        field="visibility_domain",
    )
    return groups


def _scene_snapshot(snapshot: SceneCollectionSnapshot) -> SceneCollectionSnapshot:
    scene = str(snapshot.current_program_scene or "").strip()
    if not scene:
        return SceneCollectionSnapshot(
            collection=snapshot.collection,
            current_program_scene="",
            inputs=(),
            filters=(),
            scene_items=(),
            scenes=(),
            warnings=(
                *snapshot.warnings,
                "Aucune scène programme courante : capture limitée.",
            ),
        )

    scene_items = tuple(
        item for item in snapshot.scene_items if item.scene == scene
    )
    source_names = {
        item.source
        for item in scene_items
        if str(item.source or "").strip()
    }
    inputs = tuple(
        item for item in snapshot.inputs if item.name in source_names
    )
    filters = tuple(
        item for item in snapshot.filters if item.source in source_names
    )
    return SceneCollectionSnapshot(
        collection=snapshot.collection,
        current_program_scene=scene,
        inputs=inputs,
        filters=filters,
        scene_items=scene_items,
        scenes=(scene,),
        warnings=snapshot.warnings,
    )


def _find_current_layout(
    raw_layouts: Mapping[str, Any] | None,
    scene: str,
) -> Mapping[str, Any] | None:
    if not isinstance(raw_layouts, Mapping) or not scene:
        return None
    matches = [
        profile
        for profile in raw_layouts.values()
        if isinstance(profile, Mapping)
        and str(profile.get("scene") or "").strip() == scene
    ]
    return matches[0] if len(matches) == 1 else None


def _next_rule_priority(config: Mapping[str, Any]) -> int:
    raw_rules = config.get("rules")
    rules = raw_rules if isinstance(raw_rules, list) else []
    priorities = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        if str(rule.get("behavior") or "match").strip().casefold() != "match":
            continue
        try:
            priorities.append(int(rule.get("priority", 0)))
        except (TypeError, ValueError, OverflowError):
            continue
    return max(priorities, default=99) + 1


def build_current_state_capture_draft(
    config: Mapping[str, Any],
    *,
    snapshot: SceneCollectionSnapshot,
    raw_layouts: Mapping[str, Any] | None,
    logical_state: Mapping[str, Any] | None,
    options: CurrentStateCaptureOptions,
) -> CurrentStateCaptureDraft:
    process = str(options.process or "").strip()
    requested_name = str(options.name or "").strip()
    if not process:
        raise ValueError("Le processus à détecter est requis.")
    if not requested_name:
        raise ValueError("Le nom de configuration est requis.")

    draft = copy.deepcopy(dict(config))
    logical = _mapping(logical_state)
    fallback = _fallback_state(draft)
    notes: list[str] = []
    warnings: list[str] = []
    ownership: list[tuple[str, str]] = []

    existing_rule = _find_rule_mutable(
        draft,
        options.existing_rule_name,
    )
    mode = "update" if existing_rule is not None else "create"
    if options.existing_rule_name and existing_rule is None:
        raise ValueError(
            f"Règle à mettre à jour introuvable : {options.existing_rule_name}"
        )

    if existing_rule is not None:
        rule_name = str(
            existing_rule.get("name") or requested_name
        ).strip()
        state = dict(_mapping(existing_rule.get("state")))
        state["Game"] = (
            str(state.get("Game") or "").strip() or requested_name
        )
    else:
        rule_name = requested_name
        existing_rule_names = {
            str(rule.get("name") or "").strip().casefold()
            for rule in _rules(draft)
            if str(rule.get("name") or "").strip()
        }
        if rule_name.casefold() in existing_rule_names:
            raise ValueError(
                f"Une règle nommée '{rule_name}' existe déjà."
            )

        profiles = _mapping(draft.get("profiles"))
        layouts = _mapping(draft.get("layout_profiles"))
        used_profiles: set[str] = {
            str(name).strip().casefold()
            for name in layouts
            if str(name).strip()
        }
        for domain_profiles in profiles.values():
            if not isinstance(domain_profiles, Mapping):
                continue
            used_profiles.update(
                str(name).strip().casefold()
                for name in domain_profiles
                if str(name).strip()
            )
        if rule_name.casefold() in used_profiles:
            raise ValueError(
                f"Le nom '{rule_name}' est déjà utilisé par un profil SSR."
            )

        state = {
            "Game": requested_name,
            "OverlayProfile": _logical_value(
                logical,
                fallback,
                "OverlayProfile",
                "Vanilla",
            ),
            "CaptureProfile": _logical_value(
                logical,
                fallback,
                "CaptureProfile",
                "Default",
            ),
            "AudioProfile": _logical_value(
                logical,
                fallback,
                "AudioProfile",
                "Default",
            ),
            "LayoutProfile": _logical_value(
                logical,
                fallback,
                "LayoutProfile",
                "Vanilla",
            ),
        }
        _ensure_profile(draft, "game", requested_name)

    groups = _capture_groups(options)
    scoped = _scene_snapshot(snapshot)
    added_actions = 0
    replaced_actions = 0

    for domain in _ACTION_DOMAINS:
        flags = groups.get(domain)
        if flags is None:
            continue

        key = _STATE_KEYS[domain]
        source_profile = str(state.get(key) or "").strip()
        target_profile = source_profile

        if existing_rule is None:
            if domain == "game":
                target_profile = requested_name
                _ensure_profile(
                    draft,
                    domain,
                    target_profile,
                )
            else:
                target_profile = suggest_capture_name(
                    draft,
                    f"{rule_name} · {_DOMAIN_LABELS[domain]}",
                )
                _clone_action_profile(
                    draft,
                    domain=domain,
                    source_name=source_profile,
                    target_name=target_profile,
                )
                notes.append(
                    f"{_DOMAIN_LABELS[domain]} : un profil dédié "
                    "a été créé afin de ne pas modifier le profil "
                    "logique actuellement partagé."
                )
        else:
            shared = bool(
                source_profile
                and _profile_referenced_elsewhere(
                    draft,
                    domain=domain,
                    profile_name=source_profile,
                    rule_name=rule_name,
                )
            )
            if not source_profile or shared:
                target_profile = suggest_capture_name(
                    draft,
                    f"{rule_name} · {_DOMAIN_LABELS[domain]}",
                )
                _clone_action_profile(
                    draft,
                    domain=domain,
                    source_name=source_profile,
                    target_name=target_profile,
                )
                if shared:
                    notes.append(
                        f"{_DOMAIN_LABELS[domain]} : le profil existant "
                        "était partagé ; une copie dédiée a été créée "
                        "avant la capture."
                    )
                else:
                    notes.append(
                        f"{_DOMAIN_LABELS[domain]} : aucun profil cible "
                        "n’était défini ; un profil dédié a été créé."
                    )
            else:
                _ensure_profile(
                    draft,
                    domain,
                    target_profile,
                )

        state[key] = target_profile
        report = SceneCollectionImporter.merge_actions_into_profile(
            draft,
            domain=domain,
            profile_name=target_profile,
            snapshot=scoped,
            include_input_settings=bool(
                flags["include_input_settings"]
            ),
            include_audio_state=bool(
                flags["include_audio_state"]
            ),
            include_filters=bool(
                flags["include_filters"]
            ),
            include_visibility=bool(
                flags["include_visibility"]
            ),
        )
        added_actions += report.added_actions
        replaced_actions += report.replaced_actions
        warnings.extend(report.skipped)
        ownership.append((domain, target_profile))

    game_profile = str(state.get("Game") or "").strip() or requested_name
    _ensure_profile(draft, "game", game_profile)

    current_scene = str(
        snapshot.current_program_scene or ""
    ).strip()
    layout_profile = str(
        state.get("LayoutProfile") or ""
    ).strip()
    captured_layout = False

    if options.include_layout:
        layout = _find_current_layout(
            raw_layouts,
            current_scene,
        )
        if layout is None:
            warnings.append(
                "Layout de la scène courante indisponible ou ambigu : "
                "le LayoutProfile n’a pas été modifié."
            )
        else:
            if existing_rule is None:
                layout_profile = requested_name
            else:
                layout_shared = bool(
                    layout_profile
                    and _profile_referenced_elsewhere(
                        draft,
                        domain="layout",
                        profile_name=layout_profile,
                        rule_name=rule_name,
                    )
                )
                if layout_shared or not layout_profile:
                    layout_profile = suggest_capture_name(
                        draft,
                        f"{rule_name} · Layout",
                    )
                    notes.append(
                        (
                            "Layout : le profil existant était partagé ; "
                            "une copie dédiée a été créée depuis la scène "
                            "courante."
                        )
                        if layout_shared
                        else (
                            "Layout : aucun profil cible n’était défini ; "
                            "un profil dédié a été créé."
                        )
                    )

            layouts = draft.setdefault(
                "layout_profiles",
                {},
            )
            if not isinstance(layouts, dict):
                raise ValueError(
                    "config.layout_profiles doit être un objet."
                )
            layouts[layout_profile] = copy.deepcopy(dict(layout))
            state["LayoutProfile"] = layout_profile
            captured_layout = True
            ownership.append(("layout", layout_profile))

    if existing_rule is not None:
        existing_rule["state"] = state
        existing_rule["behavior"] = "match"
    else:
        raw_rules = draft.setdefault("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("config.rules doit être une liste.")
        raw_rules.append(
            {
                "name": rule_name,
                "behavior": "match",
                "priority": _next_rule_priority(draft),
                "enabled": True,
                "exe": process,
                "launcher": "",
                "path": "",
                "title_regex": "",
                "state": state,
                "apply_delay_ms": 0,
                "conditions": {},
            }
        )
        notes.append(
            "Les domaines non capturés conservent l’état logique courant ; "
            "SSR ne prend aucun réglage OBS supplémentaire sous contrôle."
        )

    unique_warnings = tuple(dict.fromkeys(warnings))
    unique_ownership = tuple(dict.fromkeys(ownership))
    return CurrentStateCaptureDraft(
        config=draft,
        report=CurrentStateCaptureReport(
            mode=mode,
            rule_name=rule_name,
            process=process,
            scene=current_scene,
            game_profile=game_profile,
            layout_profile=layout_profile,
            added_actions=added_actions,
            replaced_actions=replaced_actions,
            layout_captured=captured_layout,
            captured_inputs=len(scoped.inputs),
            captured_filters=len(scoped.filters),
            captured_scene_items=len(scoped.scene_items),
            warnings=unique_warnings,
            notes=tuple(notes),
            owned_profiles=unique_ownership,
        ),
    )
