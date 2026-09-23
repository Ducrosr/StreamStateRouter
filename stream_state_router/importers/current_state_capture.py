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
    games = _mapping(profiles.get("game"))
    used.update(str(name).strip().casefold() for name in games)
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

    existing_rule = _find_rule_mutable(draft, options.existing_rule_name)
    mode = "update" if existing_rule is not None else "create"
    if options.existing_rule_name and existing_rule is None:
        raise ValueError(
            f"Règle à mettre à jour introuvable : {options.existing_rule_name}"
        )

    layout_shared = False
    if existing_rule is not None:
        rule_name = str(existing_rule.get("name") or requested_name).strip()
        existing_state = _mapping(existing_rule.get("state"))
        original_game_profile = (
            str(existing_state.get("Game") or "").strip() or requested_name
        )
        game_profile = original_game_profile
        if _profile_referenced_elsewhere(
            draft,
            domain="game",
            profile_name=original_game_profile,
            rule_name=rule_name,
        ):
            game_profile = suggest_capture_name(draft, f"{rule_name} · Jeu")
            profiles = draft.setdefault("profiles", {})
            if not isinstance(profiles, dict):
                raise ValueError("config.profiles doit être un objet.")
            games = profiles.setdefault("game", {})
            if not isinstance(games, dict):
                raise ValueError("config.profiles.game doit être un objet.")
            original = games.get(original_game_profile)
            games[game_profile] = (
                copy.deepcopy(dict(original))
                if isinstance(original, Mapping)
                else {
                    "actions": [],
                    "extends": "",
                    "conditions": {},
                }
            )
            notes.append(
                "Le GameProfile existant était partagé : une copie dédiée "
                "a été créée avant la capture."
            )

        current_layout_name = str(existing_state.get("LayoutProfile") or "").strip()
        layout_profile = current_layout_name
        layout_shared = bool(
            current_layout_name
            and _profile_referenced_elsewhere(
                draft,
                domain="layout",
                profile_name=current_layout_name,
                rule_name=rule_name,
            )
        )
    else:
        rule_name = requested_name
        existing_rule_names = {
            str(rule.get("name") or "").strip().casefold()
            for rule in _rules(draft)
            if str(rule.get("name") or "").strip()
        }
        profiles = _mapping(draft.get("profiles"))
        games = _mapping(profiles.get("game"))
        layouts = _mapping(draft.get("layout_profiles"))
        if rule_name.casefold() in existing_rule_names:
            raise ValueError(
                f"Une règle nommée '{rule_name}' existe déjà."
            )
        used_profiles = {
            str(name).strip().casefold()
            for name in (*games.keys(), *layouts.keys())
        }
        if rule_name.casefold() in used_profiles:
            raise ValueError(
                f"Le nom '{rule_name}' est déjà utilisé par un profil SSR."
            )
        game_profile = requested_name
        layout_profile = requested_name

    _ensure_profile(draft, "game", game_profile)
    scoped = _scene_snapshot(snapshot)
    import_report = SceneCollectionImporter.merge_actions_into_profile(
        draft,
        domain="game",
        profile_name=game_profile,
        snapshot=scoped,
        include_input_settings=bool(options.include_input_settings),
        include_audio_state=bool(options.include_audio_state),
        include_filters=bool(options.include_filters),
        include_visibility=bool(options.include_visibility),
    )
    warnings.extend(import_report.skipped)

    current_scene = str(snapshot.current_program_scene or "").strip()
    captured_layout = False
    if options.include_layout:
        layout = _find_current_layout(raw_layouts, current_scene)
        if layout is None:
            warnings.append(
                "Layout de la scène courante indisponible ou ambigu : "
                "le LayoutProfile n’a pas été modifié."
            )
        else:
            if existing_rule is not None and layout_shared:
                layout_profile = suggest_capture_name(
                    draft,
                    f"{rule_name} · Layout",
                )
                notes.append(
                    "Le LayoutProfile existant était partagé : un profil "
                    "dédié a été créé depuis la scène courante."
                )
            elif not layout_profile:
                layout_profile = (
                    suggest_capture_name(draft, f"{rule_name} · Layout")
                    if existing_rule is not None
                    else requested_name
                )
            layouts = draft.setdefault("layout_profiles", {})
            if not isinstance(layouts, dict):
                raise ValueError("config.layout_profiles doit être un objet.")
            layouts[layout_profile] = copy.deepcopy(dict(layout))
            captured_layout = True

    if existing_rule is not None:
        state = dict(_mapping(existing_rule.get("state")))
        state["Game"] = game_profile
        if captured_layout:
            state["LayoutProfile"] = layout_profile
        existing_rule["state"] = state
        existing_rule["behavior"] = "match"
    else:
        overlay = _logical_value(
            logical,
            fallback,
            "OverlayProfile",
            "Vanilla",
        )
        capture = _logical_value(
            logical,
            fallback,
            "CaptureProfile",
            "Default",
        )
        audio = _logical_value(
            logical,
            fallback,
            "AudioProfile",
            "Default",
        )
        if not captured_layout:
            layout_profile = _logical_value(
                logical,
                fallback,
                "LayoutProfile",
                "Vanilla",
            )
        state = {
            "Game": game_profile,
            "OverlayProfile": overlay,
            "CaptureProfile": capture,
            "AudioProfile": audio,
            "LayoutProfile": layout_profile,
        }
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
            "Overlay/Capture/Audio reprennent l’état logique courant ; "
            "l’assistant ne les devine pas."
        )

    return CurrentStateCaptureDraft(
        config=draft,
        report=CurrentStateCaptureReport(
            mode=mode,
            rule_name=rule_name,
            process=process,
            scene=current_scene,
            game_profile=game_profile,
            layout_profile=layout_profile,
            added_actions=import_report.added_actions,
            replaced_actions=import_report.replaced_actions,
            layout_captured=captured_layout,
            captured_inputs=len(scoped.inputs),
            captured_filters=len(scoped.filters),
            captured_scene_items=len(scoped.scene_items),
            warnings=tuple(warnings),
            notes=tuple(notes),
        ),
    )
