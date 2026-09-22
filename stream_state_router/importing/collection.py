from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

from ..obs.catalog import OBSResourceCatalog, OBSResourceCatalogReader
from ..obs.layouts import OBSLayoutManager


_INPUT_VOLUME_DB_MIN = -100.0
_INPUT_VOLUME_DB_MAX = 26.0


@dataclass(frozen=True, slots=True)
class ImportFinding:
    scope: str
    name: str
    reason: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "name": self.name,
            "reason": self.reason,
            "raw": copy.deepcopy(dict(self.raw)),
        }


@dataclass(frozen=True, slots=True)
class ConvertedTrigger:
    exe: str = ""
    title_regex: str = ""

    def key(self) -> tuple[str, str]:
        return (self.exe.casefold(), self.title_regex)

    def as_rule_fields(self) -> dict[str, str]:
        return {
            "exe": self.exe,
            "path": "",
            "title_regex": self.title_regex,
        }


@dataclass(frozen=True, slots=True)
class ConvertedMacro:
    name: str
    trigger: ConvertedTrigger
    actions: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "trigger": self.trigger.as_rule_fields(),
            "actions": [copy.deepcopy(dict(item)) for item in self.actions],
        }


@dataclass(frozen=True, slots=True)
class CollectionImportPreview:
    collection: str
    baseline_profile_name: str
    baseline_actions: tuple[Mapping[str, Any], ...]
    layouts: Mapping[str, Mapping[str, Any]]
    converted_macros: tuple[ConvertedMacro, ...]
    findings: tuple[ImportFinding, ...]
    asc_source: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "baseline_profile_name": self.baseline_profile_name,
            "baseline_actions": [
                copy.deepcopy(dict(item)) for item in self.baseline_actions
            ],
            "layouts": copy.deepcopy(dict(self.layouts)),
            "converted_macros": [
                item.as_mapping() for item in self.converted_macros
            ],
            "findings": [item.as_mapping() for item in self.findings],
            "asc_source": self.asc_source,
            "summary": {
                "baseline_actions": len(self.baseline_actions),
                "layouts": len(self.layouts),
                "converted_macros": len(self.converted_macros),
                "findings": len(self.findings),
            },
        }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _segment_enabled(segment: Mapping[str, Any]) -> bool:
    settings = _mapping(segment.get("segmentSettings"))
    return bool(settings.get("enabled", True))


def _selector_name(
    value: object,
    *,
    expected_type: int = 0,
) -> str:
    row = _mapping(value)
    if int(row.get("type", expected_type) or 0) != expected_type:
        return ""
    return str(row.get("name") or "").strip()


def _json_object(text: object) -> dict[str, Any] | None:
    if isinstance(text, Mapping):
        return copy.deepcopy(dict(text))
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None


def _duration_is_zero(value: object) -> bool:
    row = _mapping(value)
    raw = row.get("value", 0)
    if isinstance(raw, Mapping):
        raw = raw.get("value", 0)
    try:
        return math.isfinite(float(raw)) and float(raw) == 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _qt_regex_to_python(pattern: str, config: Mapping[str, Any]) -> str:
    options = int(config.get("options", 0) or 0)
    unsupported = options & ~(0x1 | 0x2 | 0x4 | 0x8)
    if unsupported:
        raise ValueError(
            f"options QRegularExpression non prises en charge: 0x{unsupported:x}"
        )
    flags = ""
    if options & 0x1:
        flags += "i"
    if options & 0x2:
        flags += "s"
    if options & 0x4:
        flags += "m"
    if options & 0x8:
        flags += "x"
    prefix = f"(?{flags})" if flags else ""
    if bool(config.get("partial", False)):
        return prefix + pattern
    return prefix + f"^(?:{pattern})$"


def _convert_condition(
    condition: Mapping[str, Any],
) -> ConvertedTrigger:
    kind = str(condition.get("id") or "").strip()
    if kind == "process":
        process = str(condition.get("process") or "").strip()
        if not process:
            raise ValueError("processus vide")
        if not bool(condition.get("focus", False)):
            raise ValueError(
                "la condition processus n'est pas limitée au processus au premier plan"
            )
        if bool(condition.get("checkPath", False)):
            raise ValueError("la vérification du chemin processus n'est pas importée")
        regex = _mapping(condition.get("regexConfig"))
        if bool(regex.get("enable", False)):
            raise ValueError("les regex de nom de processus ne sont pas supportées")
        return ConvertedTrigger(exe=process)

    if kind == "window":
        if not bool(condition.get("checkTitle", True)):
            raise ValueError("la condition fenêtre ne vérifie pas le titre")
        if not bool(condition.get("focus", False)):
            raise ValueError("la fenêtre n'est pas limitée au premier plan")
        for key in ("fullscreen", "maximized", "windowFocusChanged", "checkWindowText"):
            if bool(condition.get(key, False)):
                raise ValueError(
                    f"la condition fenêtre utilise {key}, sans équivalent SSR exact"
                )
        window = str(condition.get("window") or "").strip()
        if not window:
            raise ValueError("titre de fenêtre vide")
        regex = _mapping(condition.get("windowRegexConfig"))
        if bool(regex.get("enable", False)):
            title_regex = _qt_regex_to_python(window, regex)
        else:
            title_regex = f"^{re.escape(window)}$"
        return ConvertedTrigger(title_regex=title_regex)

    raise ValueError(f"condition ASC non supportée: {kind or '<vide>'}")


def _catalog_input_names(catalog: OBSResourceCatalog | None) -> set[str]:
    return {item.name for item in catalog.inputs} if catalog is not None else set()


def _catalog_filter_names(
    catalog: OBSResourceCatalog | None,
) -> set[tuple[str, str]]:
    # The lightweight catalog intentionally does not enumerate filters.
    # Exact filter existence is verified during live import when settings are read.
    return set()


def _scene_source_occurrences(
    catalog: OBSResourceCatalog | None,
    scene: str,
    source: str,
) -> int | None:
    if catalog is None:
        return None
    return sum(
        1
        for item in catalog.scene_items
        if item.container == scene and item.source == source
    )


def _convert_action(
    action: Mapping[str, Any],
    *,
    catalog: OBSResourceCatalog | None,
) -> dict[str, Any]:
    kind = str(action.get("id") or "").strip()

    if kind == "scene_switch":
        if int(action.get("action", 0) or 0) != 0:
            raise ValueError("seul le changement vers une scène nommée est importable")
        if int(action.get("sceneType", 0) or 0) != 0:
            raise ValueError("les changements de scène Preview ne sont pas importables")
        selection = _mapping(action.get("sceneSelection"))
        scene = _selector_name(selection)
        if not scene:
            raise ValueError("la scène cible n'est pas une scène nommée fixe")
        canvas = str(selection.get("canvasSelection") or "").strip()
        if canvas and canvas.casefold() != "main":
            raise ValueError("seul le canvas Main est actuellement supporté")
        transition_type = int(action.get("transitionType", 1) or 0)
        if transition_type != 1:
            raise ValueError(
                "la macro impose une transition spécifique non représentée par SSR"
            )
        if "duration" in action and not _duration_is_zero(action.get("duration")):
            raise ValueError(
                "la macro impose une durée de transition spécifique"
            )
        return {
            "type": "set_program_scene",
            "params": {"scene": scene},
            "enabled": True,
        }

    if kind == "scene_visibility":
        if bool(action.get("updateTransition", False)) or bool(
            action.get("updateDuration", False)
        ):
            raise ValueError(
                "la macro modifie la transition/durée de visibilité"
            )
        mode = int(action.get("action", 0) or 0)
        if mode not in {0, 1}:
            raise ValueError("les actions Toggle de visibilité sont état-dépendantes")
        scene_sel = _mapping(action.get("sceneSelection"))
        scene = _selector_name(scene_sel)
        if not scene:
            raise ValueError("la scène de visibilité n'est pas fixe")
        item_sel = _mapping(action.get("sceneItemSelection"))
        if int(item_sel.get("type", -1) or 0) != 0:
            raise ValueError("la sélection d'élément n'est pas un nom de source fixe")
        source = str(item_sel.get("item") or "").strip()
        if not source:
            raise ValueError("source de scène vide")
        count = _scene_source_occurrences(catalog, scene, source)
        if count is not None and count != 1:
            raise ValueError(
                f"la source '{source}' a {count} occurrence(s) dans '{scene}'"
            )
        return {
            "type": "scene_item_enabled",
            "params": {
                "scene": scene,
                "source": source,
                "enabled": mode == 0,
            },
            "enabled": True,
        }

    if kind == "filter":
        source = _selector_name(action.get("source"))
        filter_name = _selector_name(action.get("filter"))
        if not source or not filter_name:
            raise ValueError("source ou filtre dynamique/non nommé")
        mode = int(action.get("action", 0) or 0)
        if mode in {0, 1}:
            return {
                "type": "source_filter_enabled",
                "params": {
                    "source": source,
                    "filter": filter_name,
                    "enabled": mode == 0,
                },
                "enabled": True,
            }
        if mode == 2:
            raise ValueError("Toggle filtre est état-dépendant")
        if mode == 3:
            if int(action.get("inputMethod", 0) or 0) != 2:
                raise ValueError(
                    "seuls les settings de filtre fournis comme JSON fixe sont importables"
                )
            settings = _json_object(action.get("settings"))
            if settings is None:
                raise ValueError("settings JSON du filtre invalides")
            return {
                "type": "source_filter_settings",
                "params": {
                    "source": source,
                    "filter": filter_name,
                    "settings": settings,
                    "overlay": True,
                },
                "enabled": True,
            }
        raise ValueError("action filtre sans équivalent SSR sûr")

    if kind == "source":
        source = _selector_name(action.get("source"))
        if not source:
            raise ValueError("source dynamique/non nommée")
        mode = int(action.get("action", 0) or 0)
        if mode != 2:
            raise ValueError(
                "seule l'action Source Settings en JSON fixe est importable"
            )
        if int(action.get("inputMethod", 0) or 0) != 2:
            raise ValueError(
                "seuls les settings source fournis comme JSON fixe sont importables"
            )
        if catalog is not None and source not in _catalog_input_names(catalog):
            raise ValueError(
                f"'{source}' n'est pas un input OBS catalogué"
            )
        settings = _json_object(action.get("settings"))
        if settings is None:
            raise ValueError("settings JSON de la source invalides")
        return {
            "type": "set_input_settings",
            "params": {
                "input": source,
                "settings": settings,
                "overlay": True,
            },
            "enabled": True,
        }

    raise ValueError(f"action ASC non supportée: {kind or '<vide>'}")


def _advss_root(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("advanced-scene-switcher")
    if isinstance(nested, Mapping):
        return nested
    return payload


def convert_advss_payload(
    payload: Mapping[str, Any],
    *,
    catalog: OBSResourceCatalog | None = None,
) -> tuple[tuple[ConvertedMacro, ...], tuple[ImportFinding, ...]]:
    root = _advss_root(payload)
    macros = root.get("macros")
    if not isinstance(macros, list):
        return (), (
            ImportFinding(
                scope="advanced-scene-switcher",
                name="macros",
                reason="aucune liste 'macros' trouvée",
                raw=copy.deepcopy(dict(root)),
            ),
        )

    converted: list[ConvertedMacro] = []
    findings: list[ImportFinding] = []
    for index, raw_macro in enumerate(macros):
        if not isinstance(raw_macro, Mapping):
            findings.append(
                ImportFinding(
                    "macro",
                    f"#{index + 1}",
                    "entrée macro non objet",
                    {},
                )
            )
            continue
        macro = dict(raw_macro)
        name = str(macro.get("name") or f"Macro {index + 1}").strip()
        if bool(macro.get("group", False)):
            findings.append(
                ImportFinding("macro", name, "groupe ASC, non exécutable", macro)
            )
            continue
        if bool(macro.get("pause", False)):
            findings.append(
                ImportFinding("macro", name, "macro ASC en pause", macro)
            )
            continue
        else_actions = [
            item
            for item in (macro.get("elseActions") or [])
            if isinstance(item, Mapping) and _segment_enabled(item)
        ]
        if else_actions:
            findings.append(
                ImportFinding(
                    "macro",
                    name,
                    "Else Actions présentes, sémantique non représentable par une règle SSR",
                    macro,
                )
            )
            continue

        conditions = [
            item
            for item in (macro.get("conditions") or [])
            if isinstance(item, Mapping) and _segment_enabled(item)
        ]
        if len(conditions) != 1:
            findings.append(
                ImportFinding(
                    "macro",
                    name,
                    f"{len(conditions)} condition(s) actives; SSR exige une condition "
                    "foreground unique pour conversion automatique",
                    macro,
                )
            )
            continue
        try:
            trigger = _convert_condition(conditions[0])
        except ValueError as exc:
            findings.append(
                ImportFinding("macro", name, str(exc), macro)
            )
            continue

        actions: list[dict[str, Any]] = []
        rejected = ""
        for raw_action in macro.get("actions") or []:
            if not isinstance(raw_action, Mapping) or not _segment_enabled(raw_action):
                continue
            try:
                actions.append(
                    _convert_action(raw_action, catalog=catalog)
                )
            except ValueError as exc:
                rejected = str(exc)
                break
        if rejected:
            findings.append(ImportFinding("macro", name, rejected, macro))
            continue
        if not actions:
            findings.append(
                ImportFinding("macro", name, "aucune action active convertible", macro)
            )
            continue
        converted.append(
            ConvertedMacro(
                name=name,
                trigger=trigger,
                actions=tuple(actions),
                raw=macro,
            )
        )

    return tuple(converted), tuple(findings)


def find_obs_collection_json(
    collection_name: str,
    *,
    appdata: str | Path | None = None,
) -> Path | None:
    root = Path(
        appdata
        or os.environ.get("APPDATA")
        or ""
    )
    if not str(root):
        return None
    scenes = root / "obs-studio" / "basic" / "scenes"
    if not scenes.is_dir():
        return None
    wanted = str(collection_name or "").strip().casefold()
    for path in sorted(scenes.glob("*.json"), key=lambda item: item.name.casefold()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        name = str(payload.get("name") or "").strip().casefold()
        if name == wanted:
            return path
    return None


def _read_advss_file(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    root = _advss_root(payload)
    return payload if isinstance(root.get("macros"), list) else None


def _safe_profile_fragment(name: str) -> str:
    value = re.sub(r"\s+", " ", str(name or "").strip())
    return value or "Imported"


def _unique_name(existing: Mapping[str, Any], desired: str) -> str:
    if desired not in existing:
        return desired
    index = 2
    while f"{desired} ({index})" in existing:
        index += 1
    return f"{desired} ({index})"


def apply_collection_import(
    config: Mapping[str, Any],
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(config))
    profiles = result.setdefault("profiles", {})
    capture_profiles = profiles.setdefault("capture", {})
    game_profiles = profiles.setdefault("game", {})
    layouts = result.setdefault("layout_profiles", {})
    rules = result.setdefault("rules", [])

    baseline_actions = preview.get("baseline_actions")
    if isinstance(baseline_actions, list) and baseline_actions:
        requested = str(
            preview.get("baseline_profile_name") or "OBS Collection"
        ).strip()
        name = _unique_name(capture_profiles, requested)
        capture_profiles[name] = {
            "actions": copy.deepcopy(baseline_actions),
            "extends": "",
            "conditions": {},
        }

    raw_layouts = preview.get("layouts")
    if isinstance(raw_layouts, Mapping):
        for requested_name, profile in raw_layouts.items():
            if not isinstance(profile, Mapping):
                continue
            name = _unique_name(layouts, str(requested_name))
            layouts[name] = copy.deepcopy(dict(profile))

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in preview.get("converted_macros") or []:
        if not isinstance(raw, Mapping):
            continue
        trigger = _mapping(raw.get("trigger"))
        exe = str(trigger.get("exe") or "").strip()
        title_regex = str(trigger.get("title_regex") or "").strip()
        key = (exe.casefold(), title_regex)
        group = grouped.setdefault(
            key,
            {
                "names": [],
                "exe": exe,
                "title_regex": title_regex,
                "actions": [],
            },
        )
        group["names"].append(str(raw.get("name") or "ASC"))
        actions = raw.get("actions")
        if isinstance(actions, list):
            group["actions"].extend(copy.deepcopy(actions))

    fallback = _mapping(_mapping(result.get("router")).get("fallback_state"))
    for group_index, group in enumerate(grouped.values()):
        label = " + ".join(group["names"])
        requested_profile = f"ASC · {_safe_profile_fragment(label)}"
        profile_name = _unique_name(game_profiles, requested_profile)
        game_profiles[profile_name] = {
            "actions": copy.deepcopy(group["actions"]),
            "extends": "",
            "conditions": {},
        }

        matching = None
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if str(rule.get("behavior", "match")).casefold() != "match":
                continue
            if (
                str(rule.get("exe") or "").strip().casefold()
                == str(group["exe"]).casefold()
                and str(rule.get("path") or "").strip() == ""
                and str(rule.get("title_regex") or "").strip()
                == str(group["title_regex"])
            ):
                matching = rule
                break

        if matching is not None:
            state = matching.setdefault("state", {})
            state["Game"] = profile_name
            continue

        state = copy.deepcopy(dict(fallback))
        state["Game"] = profile_name
        rules.append(
            {
                "name": _unique_name(
                    {str(item.get("name") or ""): True for item in rules if isinstance(item, Mapping)},
                    f"ASC · {_safe_profile_fragment(label)}",
                ),
                "behavior": "match",
                "priority": max(1, 100 - group_index),
                "enabled": True,
                "exe": group["exe"],
                "path": "",
                "title_regex": group["title_regex"],
                "state": state,
                "apply_delay_ms": 0,
                "conditions": {},
            }
        )

    return result


class OBSCollectionImporter:
    def __init__(
        self,
        client,
        *,
        layout_manager: OBSLayoutManager | None = None,
        cooperative_yield=None,
    ) -> None:
        self.client = client
        self.reader = OBSResourceCatalogReader(
            client,
            cooperative_yield=cooperative_yield,
        )
        self.layout_manager = layout_manager or OBSLayoutManager(client)

    def _baseline_actions(
        self,
        catalog: OBSResourceCatalog,
        findings: list[ImportFinding],
    ) -> tuple[Mapping[str, Any], ...]:
        actions: list[Mapping[str, Any]] = []
        for input_ref in catalog.inputs:
            try:
                details = self.reader.input_details(input_ref.name)
            except Exception as exc:
                findings.append(
                    ImportFinding(
                        "input",
                        input_ref.name,
                        f"settings/filters illisibles: {exc}",
                        {},
                    )
                )
                continue

            if details.settings:
                actions.append(
                    {
                        "type": "set_input_settings",
                        "params": {
                            "input": input_ref.name,
                            "settings": copy.deepcopy(dict(details.settings)),
                            "overlay": True,
                        },
                        "enabled": True,
                    }
                )

            try:
                mute = self.client.send(
                    "GetInputMute",
                    {"inputUuid": input_ref.uuid},
                )
                if isinstance(mute.get("inputMuted"), bool):
                    actions.append(
                        {
                            "type": "input_mute",
                            "params": {
                                "input": input_ref.name,
                                "muted": bool(mute["inputMuted"]),
                            },
                            "enabled": True,
                        }
                    )
            except Exception:
                pass

            try:
                volume = self.client.send(
                    "GetInputVolume",
                    {"inputUuid": input_ref.uuid},
                )
                raw_db = volume.get("inputVolumeDb")
                if (
                    not isinstance(raw_db, bool)
                    and isinstance(raw_db, (int, float))
                    and math.isfinite(float(raw_db))
                    and _INPUT_VOLUME_DB_MIN
                    <= float(raw_db)
                    <= _INPUT_VOLUME_DB_MAX
                ):
                    actions.append(
                        {
                            "type": "input_volume_db",
                            "params": {
                                "input": input_ref.name,
                                "volume_db": float(raw_db),
                            },
                            "enabled": True,
                        }
                    )
            except Exception:
                pass

            for filter_ref in details.filters:
                actions.append(
                    {
                        "type": "source_filter_enabled",
                        "params": {
                            "source": input_ref.name,
                            "filter": filter_ref.name,
                            "enabled": bool(filter_ref.enabled),
                        },
                        "enabled": True,
                    }
                )
                try:
                    filter_details = self.reader.filter_details(
                        input_ref.name,
                        filter_ref.name,
                    )
                except Exception as exc:
                    findings.append(
                        ImportFinding(
                            "filter",
                            f"{input_ref.name}/{filter_ref.name}",
                            f"settings illisibles: {exc}",
                            {},
                        )
                    )
                    continue
                if filter_details.settings:
                    actions.append(
                        {
                            "type": "source_filter_settings",
                            "params": {
                                "source": input_ref.name,
                                "filter": filter_ref.name,
                                "settings": copy.deepcopy(
                                    dict(filter_details.settings)
                                ),
                                "overlay": True,
                            },
                            "enabled": True,
                        }
                    )
        return tuple(actions)

    def preview(
        self,
        *,
        asc_payload: Mapping[str, Any] | None = None,
        asc_source: str = "",
        auto_detect_asc: bool = True,
    ) -> CollectionImportPreview:
        findings: list[ImportFinding] = []
        catalog = self.reader.sync()
        collection = catalog.collection or "OBS"

        layouts: dict[str, Mapping[str, Any]] = {}
        for scene in catalog.scenes:
            try:
                capture = self.layout_manager.capture_profile_result(scene.name)
                layouts[f"OBS · {collection} · {scene.name}"] = capture.profile
                for warning in capture.warnings:
                    findings.append(
                        ImportFinding("layout", scene.name, str(warning), {})
                    )
            except Exception as exc:
                findings.append(
                    ImportFinding(
                        "layout",
                        scene.name,
                        f"capture impossible: {exc}",
                        {},
                    )
                )

        detected_source = asc_source
        payload = asc_payload
        if payload is None and auto_detect_asc:
            path = find_obs_collection_json(collection)
            if path is not None:
                candidate = _read_advss_file(path)
                if candidate is not None:
                    payload = candidate
                    detected_source = str(path)

        converted: tuple[ConvertedMacro, ...] = ()
        if payload is not None:
            converted, asc_findings = convert_advss_payload(
                payload,
                catalog=catalog,
            )
            findings.extend(asc_findings)

        baseline_name = f"OBS · {collection} · Baseline"
        return CollectionImportPreview(
            collection=collection,
            baseline_profile_name=baseline_name,
            baseline_actions=self._baseline_actions(catalog, findings),
            layouts=layouts,
            converted_macros=converted,
            findings=tuple(findings),
            asc_source=detected_source,
        )
