from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from ..obs.catalog import OBSResourceCatalogReader
from ..obs.client import OBSClientManager, OBSRequestError
from ..obs.layouts import OBSLayoutManager


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def _segment_enabled(segment: Mapping[str, Any]) -> bool:
    settings = _mapping(segment.get("segmentSettings"))
    return bool(settings.get("enabled", True))


def _fixed_selection(
    segment: Mapping[str, Any],
    key: str,
) -> str:
    raw = segment.get(key)
    if isinstance(raw, str):
        return raw.strip()
    selection = _mapping(raw)
    if selection and int(selection.get("type", 0) or 0) == 0:
        return str(selection.get("name") or "").strip()
    return ""


def _number_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = _mapping(value)
    candidate = raw.get("value")
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        return None
    return float(candidate)


def _safe_name(prefix: str, name: str) -> str:
    cleaned = " ".join(str(name or "").split()).strip()
    return f"{prefix}{cleaned or 'Sans nom'}"


@dataclass(frozen=True, slots=True)
class ImportedMacro:
    name: str
    status: str
    reason: str = ""
    selector: Mapping[str, str] = field(default_factory=dict)
    actions: tuple[Mapping[str, Any], ...] = ()
    profile_name: str = ""

    def safe_summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "selector": dict(self.selector),
            "action_types": [str(item.get("type") or "") for item in self.actions],
            "profile_name": self.profile_name,
        }


@dataclass(frozen=True, slots=True)
class CollectionImportPlan:
    collection: str
    collection_file: str
    snapshot_profile_name: str
    snapshot_actions: tuple[Mapping[str, Any], ...]
    layout_profiles: Mapping[str, Mapping[str, Any]]
    macros: tuple[ImportedMacro, ...]
    warnings: tuple[str, ...] = ()

    @property
    def converted_macros(self) -> tuple[ImportedMacro, ...]:
        return tuple(item for item in self.macros if item.status == "converted")

    def safe_summary(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "collection_file": self.collection_file,
            "snapshot_profile_name": self.snapshot_profile_name,
            "snapshot_actions": len(self.snapshot_actions),
            "layout_profiles": sorted(self.layout_profiles),
            "macros_total": len(self.macros),
            "macros_converted": len(self.converted_macros),
            "macros": [item.safe_summary() for item in self.macros],
            "warnings": list(self.warnings),
        }


def find_obs_collection_file(
    collection_name: str,
    *,
    root: str | Path | None = None,
) -> Path | None:
    name = str(collection_name or "").strip()
    if not name:
        return None
    if root is None:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return None
        root_path = Path(appdata) / "obs-studio" / "basic" / "scenes"
    else:
        root_path = Path(root)
    if not root_path.is_dir():
        return None

    stem_match: Path | None = None
    for path in sorted(root_path.glob("*.json"), key=lambda item: item.name.casefold()):
        if path.stem.casefold() == name.casefold():
            stem_match = path
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("name") or "").strip().casefold() == name.casefold():
            return path
    return stem_match


def _asc_root(collection_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = collection_payload.get("advanced-scene-switcher")
    if isinstance(direct, Mapping):
        return direct
    # Advanced Scene Switcher exports may already contain the plugin settings
    # object at the root rather than the full OBS collection.
    if isinstance(collection_payload.get("macros"), list):
        return collection_payload
    return {}


def _condition_to_selector(
    condition: Mapping[str, Any],
) -> tuple[dict[str, str] | None, str]:
    if not _segment_enabled(condition):
        return None, "condition désactivée"
    kind = str(condition.get("id") or "").strip()

    if kind == "process":
        if not bool(condition.get("focus", False)):
            return None, "condition process non limitée au processus au premier plan"
        regex = _mapping(condition.get("regexConfig"))
        if bool(regex.get("enable", False)):
            return None, "condition process regex non représentable exactement"
        process = str(condition.get("process") or "").strip()
        if not process:
            return None, "processus vide"
        selector = {"exe": process, "path": "", "title_regex": ""}
        if bool(condition.get("checkPath", False)):
            path_regex = _mapping(condition.get("pathRegex"))
            if bool(path_regex.get("enable", False)):
                return None, "chemin process regex non représentable exactement"
            process_path = str(condition.get("processPath") or "").strip()
            if not process_path:
                return None, "chemin process demandé mais vide"
            selector["path"] = process_path
        return selector, ""

    if kind == "window":
        if not bool(condition.get("focus", False)):
            return None, "condition fenêtre non limitée à la fenêtre au premier plan"
        if not bool(condition.get("checkTitle", True)):
            return None, "condition fenêtre sans titre"
        if any(
            bool(condition.get(key, False))
            for key in (
                "fullscreen",
                "maximized",
                "windowFocusChanged",
                "checkWindowText",
            )
        ):
            return None, "condition fenêtre avancée non représentable exactement"
        title = str(condition.get("window") or "").strip()
        if not title:
            return None, "titre de fenêtre vide"
        regex = _mapping(condition.get("windowRegexConfig"))
        if bool(regex.get("enable", False)):
            # QRegularExpression flags/partial semantics do not map one-to-one
            # to Python re in all cases; avoid silently changing behavior.
            return None, "regex de titre Advanced Scene Switcher à valider manuellement"
        return {
            "exe": "",
            "path": "",
            "title_regex": re.escape(title),
        }, ""

    return None, f"condition ASC non prise en charge : {kind or '<vide>'}"


def _convert_asc_action(
    action: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    if not _segment_enabled(action):
        return {}, ""
    kind = str(action.get("id") or "").strip()

    if kind == "scene_switch":
        if int(action.get("action", 0) or 0) != 0:
            return None, "scene_switch autre que changement vers une scène"
        scene = _mapping(action.get("sceneSelection"))
        if int(scene.get("type", 0) or 0) != 0:
            return None, "scene_switch dynamique/non nommé"
        name = str(scene.get("name") or "").strip()
        if not name:
            return None, "scene_switch sans scène"
        return {
            "type": "set_program_scene",
            "params": {"scene": name},
            "enabled": True,
            "name": "Import ASC",
        }, ""

    if kind == "scene_visibility":
        scene = _mapping(action.get("sceneSelection"))
        item = _mapping(action.get("sceneItemSelection"))
        action_code = int(action.get("action", 0) or 0)
        if int(scene.get("type", 0) or 0) != 0:
            return None, "visibilité liée à une scène dynamique/courante"
        if int(item.get("type", 0) or 0) != 0:
            return None, "sélection de source dynamique"
        if action_code not in {0, 1}:
            return None, "toggle de visibilité non déterministe"
        scene_name = str(scene.get("name") or "").strip()
        source_name = str(item.get("item") or "").strip()
        if not scene_name or not source_name:
            return None, "visibilité sans scène/source fixe"
        return {
            "type": "scene_item_enabled",
            "params": {
                "scene": scene_name,
                "source": source_name,
                "enabled": action_code == 0,
            },
            "enabled": True,
            "name": "Import ASC",
        }, ""

    if kind == "filter":
        source = _fixed_selection(action, "source")
        filter_name = _fixed_selection(action, "filter")
        if not source or not filter_name:
            return None, "filtre avec source/filtre dynamique"
        action_code = int(action.get("action", 0) or 0)
        if action_code in {0, 1}:
            return {
                "type": "source_filter_enabled",
                "params": {
                    "source": source,
                    "filter": filter_name,
                    "enabled": action_code == 0,
                },
                "enabled": True,
                "name": "Import ASC",
            }, ""
        if action_code == 2:
            return None, "toggle de filtre non déterministe"
        if action_code == 3:
            if int(action.get("inputMethod", 0) or 0) != 2:
                return None, "réglage filtre ASC non fourni en JSON fixe"
            raw = str(action.get("settings") or "").strip()
            try:
                settings = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return None, "JSON de filtre ASC invalide"
            if not isinstance(settings, Mapping):
                return None, "réglages filtre ASC non objets"
            return {
                "type": "source_filter_settings",
                "params": {
                    "source": source,
                    "filter": filter_name,
                    "settings": dict(settings),
                    "overlay": True,
                },
                "enabled": True,
                "name": "Import ASC",
            }, ""
        return None, "action filtre ASC non prise en charge"

    if kind == "source":
        source = _fixed_selection(action, "source")
        if not source:
            return None, "source dynamique"
        action_code = int(action.get("action", 0) or 0)
        if action_code != 2:
            return None, "action source ASC autre que réglages"
        if int(action.get("inputMethod", 0) or 0) != 2:
            return None, "réglage source ASC non fourni en JSON fixe"
        raw = str(action.get("settings") or "").strip()
        try:
            settings = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None, "JSON de source ASC invalide"
        if not isinstance(settings, Mapping):
            return None, "réglages source ASC non objets"
        return {
            "type": "set_input_settings",
            "params": {
                "input": source,
                "settings": dict(settings),
                "overlay": True,
            },
            "enabled": True,
            "name": "Import ASC",
        }, ""

    if kind == "audio":
        source = _fixed_selection(action, "audioSource")
        if not source:
            return None, "source audio dynamique"
        action_code = int(action.get("action", 0) or 0)
        if action_code in {0, 1}:
            return {
                "type": "input_mute",
                "params": {
                    "input": source,
                    "muted": action_code == 0,
                },
                "enabled": True,
                "name": "Import ASC",
            }, ""
        if action_code == 2:
            if bool(action.get("fade", False)):
                return None, "fondu de volume ASC non représenté par SSR"
            if not bool(action.get("useDb", False)):
                return None, "volume ASC en pourcentage non importé automatiquement"
            value = _number_value(action.get("volumeDB"))
            if value is None:
                return None, "volume dB ASC dynamique/non numérique"
            return {
                "type": "input_volume_db",
                "params": {"input": source, "volume_db": value},
                "enabled": True,
                "name": "Import ASC",
            }, ""
        return None, "action audio ASC non prise en charge"

    return None, f"action ASC non prise en charge : {kind or '<vide>'}"


def convert_advanced_scene_switcher(
    payload: Mapping[str, Any],
) -> tuple[ImportedMacro, ...]:
    root = _asc_root(payload)
    macros = _items(root.get("macros"))
    out: list[ImportedMacro] = []
    for index, macro in enumerate(macros):
        name = str(macro.get("name") or f"Macro {index + 1}").strip()
        if bool(macro.get("group", False)):
            out.append(ImportedMacro(name, "unsupported", "groupe ASC"))
            continue
        if bool(macro.get("pause", False)):
            out.append(ImportedMacro(name, "unsupported", "macro ASC en pause"))
            continue
        if bool(macro.get("parallel", False)):
            out.append(
                ImportedMacro(
                    name,
                    "unsupported",
                    "exécution parallèle ASC non équivalente à SSR",
                )
            )
            continue
        if _items(macro.get("elseActions")):
            out.append(
                ImportedMacro(
                    name,
                    "unsupported",
                    "branche else ASC non représentée par une règle SSR",
                )
            )
            continue

        conditions = [
            item for item in _items(macro.get("conditions")) if _segment_enabled(item)
        ]
        if len(conditions) != 1:
            out.append(
                ImportedMacro(
                    name,
                    "unsupported",
                    "SSR importe automatiquement une seule condition ASC fixe",
                )
            )
            continue
        selector, reason = _condition_to_selector(conditions[0])
        if selector is None:
            out.append(ImportedMacro(name, "unsupported", reason))
            continue

        converted_actions: list[Mapping[str, Any]] = []
        failure = ""
        for action in _items(macro.get("actions")):
            converted, why = _convert_asc_action(action)
            if converted == {}:
                continue
            if converted is None:
                failure = why
                break
            converted_actions.append(converted)
        if failure:
            out.append(
                ImportedMacro(
                    name,
                    "partial",
                    failure,
                    selector=selector,
                    actions=tuple(converted_actions),
                )
            )
            continue
        if not converted_actions:
            out.append(
                ImportedMacro(
                    name,
                    "unsupported",
                    "aucune action ASC déterministe convertible",
                    selector=selector,
                )
            )
            continue
        out.append(
            ImportedMacro(
                name,
                "converted",
                selector=selector,
                actions=tuple(converted_actions),
                profile_name=_safe_name("ASC :: ", name),
            )
        )
    return tuple(out)


class OBSCollectionImporter:
    def __init__(
        self,
        client: OBSClientManager,
        *,
        obs_scenes_root: str | Path | None = None,
    ) -> None:
        self.client = client
        self.reader = OBSResourceCatalogReader(client)
        self.layout_manager = OBSLayoutManager(client)
        self.obs_scenes_root = obs_scenes_root

    def build_plan(self) -> CollectionImportPlan:
        catalog = self.reader.sync()
        warnings = list(catalog.warnings)
        actions: list[Mapping[str, Any]] = []

        for input_ref in catalog.inputs:
            try:
                details = self.reader.input_details(input_ref.name)
            except OBSRequestError as exc:
                warnings.append(
                    f"Réglages input '{input_ref.name}' non lisibles : {exc}"
                )
                continue
            if details.settings:
                actions.append(
                    {
                        "type": "set_input_settings",
                        "params": {
                            "input": input_ref.name,
                            "settings": dict(details.settings),
                            "overlay": False,
                        },
                        "enabled": True,
                        "name": "Import collection OBS",
                    }
                )

        sources = {
            *(item.name for item in catalog.inputs),
            *(item.name for item in catalog.scenes),
            *catalog.groups,
        }
        for source in sorted(sources, key=str.casefold):
            try:
                filters = self.reader.filters_for_source(source)
            except OBSRequestError:
                continue
            for filter_ref in filters:
                if filter_ref.enabled is not None:
                    actions.append(
                        {
                            "type": "source_filter_enabled",
                            "params": {
                                "source": source,
                                "filter": filter_ref.name,
                                "enabled": bool(filter_ref.enabled),
                            },
                            "enabled": True,
                            "name": "Import collection OBS",
                        }
                    )
                try:
                    details = self.reader.filter_details(source, filter_ref.name)
                except OBSRequestError as exc:
                    warnings.append(
                        f"Réglages filtre '{source}/{filter_ref.name}' non lisibles : {exc}"
                    )
                    continue
                if details.settings:
                    actions.append(
                        {
                            "type": "source_filter_settings",
                            "params": {
                                "source": source,
                                "filter": filter_ref.name,
                                "settings": dict(details.settings),
                                "overlay": False,
                            },
                            "enabled": True,
                            "name": "Import collection OBS",
                        }
                    )

        layouts: dict[str, Mapping[str, Any]] = {}
        for scene in catalog.scenes:
            try:
                captured = self.layout_manager.capture_profile_result(scene.name)
            except Exception as exc:
                warnings.append(f"Layout '{scene.name}' non capturé : {exc}")
                continue
            profile_name = _safe_name(
                f"Import {catalog.collection} :: ",
                scene.name,
            )
            layouts[profile_name] = captured.profile
            warnings.extend(captured.warnings)

        collection_file = find_obs_collection_file(
            catalog.collection,
            root=self.obs_scenes_root,
        )
        macros: tuple[ImportedMacro, ...] = ()
        if collection_file is None:
            warnings.append(
                "Fichier local de collection OBS introuvable : macros Advanced "
                "Scene Switcher non analysées."
            )
        else:
            try:
                raw = json.loads(collection_file.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                warnings.append(f"Collection OBS locale illisible : {exc}")
            else:
                if isinstance(raw, Mapping):
                    macros = convert_advanced_scene_switcher(raw)

        return CollectionImportPlan(
            collection=catalog.collection,
            collection_file=str(collection_file or ""),
            snapshot_profile_name=_safe_name(
                "Import Collection :: ",
                catalog.collection,
            ),
            snapshot_actions=tuple(actions),
            layout_profiles=layouts,
            macros=macros,
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _unique_name(existing: Mapping[str, object], wanted: str) -> str:
    if wanted not in existing:
        return wanted
    index = 2
    while f"{wanted} ({index})" in existing:
        index += 1
    return f"{wanted} ({index})"


def apply_collection_import_plan(
    config: Mapping[str, Any],
    plan: CollectionImportPlan,
    *,
    import_snapshot: bool = True,
    import_layouts: bool = True,
    import_macros: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    result = copy.deepcopy(dict(config))
    counts = {"snapshot_profiles": 0, "layout_profiles": 0, "macros": 0, "rules": 0}

    profiles = result.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles doit être un objet")
    capture_profiles = profiles.setdefault("capture", {})
    game_profiles = profiles.setdefault("game", {})
    if not isinstance(capture_profiles, dict) or not isinstance(game_profiles, dict):
        raise ValueError("profiles.capture/game doivent être des objets")

    if import_snapshot:
        name = _unique_name(capture_profiles, plan.snapshot_profile_name)
        capture_profiles[name] = {
            "extends": "",
            "conditions": {},
            "actions": copy.deepcopy(list(plan.snapshot_actions)),
        }
        counts["snapshot_profiles"] += 1

    if import_layouts:
        layout_profiles = result.setdefault("layout_profiles", {})
        if not isinstance(layout_profiles, dict):
            raise ValueError("layout_profiles doit être un objet")
        for wanted, profile in plan.layout_profiles.items():
            name = _unique_name(layout_profiles, wanted)
            layout_profiles[name] = copy.deepcopy(dict(profile))
            counts["layout_profiles"] += 1

    if import_macros:
        rules = result.setdefault("rules", [])
        if not isinstance(rules, list):
            raise ValueError("rules doit être une liste")
        fallback = _mapping(_mapping(result.get("router")).get("fallback_state"))
        existing_rule_names = {
            str(item.get("name") or "")
            for item in rules
            if isinstance(item, Mapping)
        }
        next_priority = max(
            (
                int(item.get("priority", 0) or 0)
                for item in rules
                if isinstance(item, Mapping)
            ),
            default=0,
        ) + 10
        for macro in plan.converted_macros:
            profile_name = _unique_name(game_profiles, macro.profile_name)
            game_profiles[profile_name] = {
                "extends": "",
                "conditions": {},
                "actions": copy.deepcopy(list(macro.actions)),
            }
            counts["macros"] += 1

            rule_name = _unique_name(
                {name: True for name in existing_rule_names},
                _safe_name("ASC :: ", macro.name),
            )
            existing_rule_names.add(rule_name)
            state = dict(fallback)
            state["Game"] = profile_name
            selector = dict(macro.selector)
            rules.append(
                {
                    "name": rule_name,
                    "behavior": "match",
                    "priority": next_priority,
                    "enabled": False,
                    "exe": selector.get("exe", ""),
                    "path": selector.get("path", ""),
                    "title_regex": selector.get("title_regex", ""),
                    "state": state,
                    "apply_delay_ms": 0,
                    "conditions": {},
                }
            )
            next_priority += 1
            counts["rules"] += 1

    return result, counts
