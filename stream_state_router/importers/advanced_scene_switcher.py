from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .scene_collection import SceneCollectionSnapshot


@dataclass(frozen=True, slots=True)
class RejectedAdvancedSceneSwitcherMacro:
    name: str
    reason: str
    raw: Mapping[str, Any]

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "reason": self.reason,
            "raw": copy.deepcopy(dict(self.raw)),
        }


@dataclass(frozen=True, slots=True)
class AdvancedSceneSwitcherImportReport:
    macros_total: int
    macros_converted: int
    actions_converted: int
    rules_created: int
    profiles_created: int
    attached_to_existing_rules: int
    skipped: tuple[str, ...]
    rejected_raw: tuple[RejectedAdvancedSceneSwitcherMacro, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "macros_total": self.macros_total,
            "macros_converted": self.macros_converted,
            "actions_converted": self.actions_converted,
            "rules_created": self.rules_created,
            "profiles_created": self.profiles_created,
            "attached_to_existing_rules": self.attached_to_existing_rules,
            "skipped": list(self.skipped),
            "rejected_raw": [
                item.as_mapping() for item in self.rejected_raw
            ],
        }

    def summary(self) -> str:
        lines = [
            f"Macros ASC analysées : {self.macros_total}",
            f"Macros converties : {self.macros_converted}",
            f"Actions converties : {self.actions_converted}",
            f"Règles SSR créées : {self.rules_created}",
            f"Profils SSR créés : {self.profiles_created}",
            (
                "Macros rattachées à une règle SSR existante : "
                f"{self.attached_to_existing_rules}"
            ),
            f"Éléments non convertis : {len(self.skipped)}",
        ]
        if self.skipped:
            lines.append("")
            lines.extend(f"- {item}" for item in self.skipped)
        return "\n".join(lines)


def _enabled(segment: Mapping[str, Any]) -> bool:
    settings = segment.get("segmentSettings")
    if not isinstance(settings, Mapping):
        return True
    return bool(settings.get("enabled", True))


def _selection_name(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    if int(value.get("type", -1) or 0) != 0:
        return ""
    return str(value.get("name") or "").strip()


def _scene_name(action: Mapping[str, Any]) -> str:
    selection = action.get("sceneSelection")
    return _selection_name(selection)


def _duration_is_zero(raw: Any) -> bool:
    if raw in (None, {}):
        return True
    if not isinstance(raw, Mapping):
        return False
    value = raw.get("value", 0)
    if isinstance(value, Mapping):
        value = value.get("value", 0)
    try:
        return math.isfinite(float(value)) and float(value) == 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _action_identity(action: Mapping[str, Any]) -> tuple[str, ...]:
    kind = str(action.get("type") or "")
    raw = action.get("params")
    params = raw if isinstance(raw, Mapping) else {}
    if kind == "set_program_scene":
        return (kind,)
    if kind == "scene_item_enabled":
        return (
            kind,
            str(params.get("scene") or ""),
            str(params.get("source") or ""),
        )
    if kind in {"source_filter_enabled", "source_filter_settings"}:
        return (
            kind,
            str(params.get("source") or ""),
            str(params.get("filter") or ""),
        )
    if kind in {"set_input_settings", "input_mute", "input_volume_db"}:
        return (kind, str(params.get("input") or ""))
    return (kind,)


def _asc_game_assignment(action: Mapping[str, Any]) -> str:
    if str(action.get("id") or "").strip() != "variable":
        return ""
    try:
        action_kind = int(action.get("condition", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if action_kind != 0:
        return ""
    if str(action.get("variableName") or "").strip().casefold() != "game":
        return ""
    return str(action.get("strValue") or "").strip()


def _asc_game_condition(condition: Mapping[str, Any]) -> str:
    if str(condition.get("id") or "").strip() != "variable":
        return ""
    try:
        logic = int(condition.get("logic", 0) or 0)
        kind = int(condition.get("condition", -1))
    except (TypeError, ValueError, OverflowError):
        return ""
    if logic != 0 or kind != 0:
        return ""
    if str(condition.get("variableName") or "").strip().casefold() != "game":
        return ""
    regex = condition.get("regexConfig")
    if isinstance(regex, Mapping) and bool(regex.get("enable", False)):
        return ""
    return str(condition.get("strValue") or "").strip()


_TEMPLATE_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _asc_control_variable_defaults(data: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_variables = data.get("variables")
    if not isinstance(raw_variables, list):
        return result
    reserved = {
        "Game",
        "OverlayProfile",
        "CaptureProfile",
        "AudioProfile",
        "LayoutProfile",
    }
    for item in raw_variables:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if (
            not name
            or name in reserved
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            continue
        value = item.get("value")
        if not isinstance(value, str):
            value = item.get("defaultValue")
        if isinstance(value, str):
            result[name] = value
    return result


def _asc_changed_variables(
    conditions: list[Mapping[str, Any]],
) -> tuple[str, ...] | None:
    if not conditions:
        return None
    names: list[str] = []
    for index, condition in enumerate(conditions):
        if str(condition.get("id") or "").strip() != "variable":
            return None
        try:
            logic = int(condition.get("logic", 0) or 0)
            kind = int(condition.get("condition", -1))
        except (TypeError, ValueError, OverflowError):
            return None
        if kind != 5:  # MacroConditionVariable::VALUE_CHANGED
            return None
        expected_logic = 0 if index == 0 else 102  # ROOT_NONE, then OR
        if logic != expected_logic:
            return None
        name = str(condition.get("variableName") or "").strip()
        if not name:
            return None
        names.append(name)
    return tuple(names)


def _asc_wait_ms(action: Mapping[str, Any]) -> int | None:
    if str(action.get("id") or "").strip() != "wait":
        return None
    try:
        wait_type = int(action.get("waitType", -1))
    except (TypeError, ValueError, OverflowError):
        return None
    if wait_type != 0:
        return None
    raw = action.get("duration")
    if not isinstance(raw, Mapping):
        return None
    try:
        unit = int(raw.get("unit", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if unit != 0:
        return None
    value = raw.get("value")
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or not 0.0 <= seconds <= 10.0:
        return None
    return int(round(seconds * 1000.0))


def _replace_template_variable(
    value: Any,
    *,
    name: str,
    replacement: str,
) -> Any:
    token = "${" + name + "}"
    if isinstance(value, str):
        return value.replace(token, replacement)
    if isinstance(value, Mapping):
        return {
            key: _replace_template_variable(
                item,
                name=name,
                replacement=replacement,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_template_variable(
                item,
                name=name,
                replacement=replacement,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_template_variable(
                item,
                name=name,
                replacement=replacement,
            )
            for item in value
        )
    return value


def _append_exact_sequence(
    profile: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    existing = profile.setdefault("actions", [])
    if not isinstance(existing, list):
        raise ValueError("Le profil SSR cible contient une liste d'actions invalide.")
    if not actions:
        return False
    width = len(actions)
    for index in range(0, len(existing) - width + 1):
        if existing[index : index + width] == actions:
            return False
    existing.extend(copy.deepcopy(actions))
    return True


def _coalesce_input_settings_actions(
    macro_name: str,
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for action in actions:
        if str(action.get("type") or "") != "set_input_settings":
            merged.append(action)
            continue
        params = action.get("params")
        if not isinstance(params, Mapping):
            return None, f"{macro_name}: set_input_settings importé invalide"
        input_name = str(params.get("input") or "").strip()
        settings = params.get("settings")
        if not input_name or not isinstance(settings, Mapping):
            return None, f"{macro_name}: settings input importés invalides"
        key = input_name.casefold()
        if key not in positions:
            positions[key] = len(merged)
            merged.append(copy.deepcopy(action))
            continue
        previous = merged[positions[key]]
        previous_params = previous.get("params")
        previous_settings = (
            previous_params.get("settings")
            if isinstance(previous_params, dict)
            else None
        )
        if not isinstance(previous_settings, dict):
            return None, f"{macro_name}: fusion settings input impossible"
        conflicts = [
            setting
            for setting, value in settings.items()
            if setting in previous_settings
            and previous_settings[setting] != value
        ]
        if conflicts:
            return None, (
                f"{macro_name}: settings successifs contradictoires pour "
                f"{input_name}: {', '.join(sorted(map(str, conflicts)))}"
            )
        previous_settings.update(copy.deepcopy(dict(settings)))
    return merged, ""


def _extract_advanced_scene_switcher_payload(
    data: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    nested = data.get("advanced-scene-switcher")
    if isinstance(nested, Mapping):
        return nested

    modules = data.get("modules")
    if isinstance(modules, Mapping):
        nested = modules.get("advanced-scene-switcher")
        if isinstance(nested, Mapping):
            return nested

    return None


class AdvancedSceneSwitcherImporter:
    """Conservative importer for Advanced Scene Switcher JSON exports.

    Only macros whose semantics can be reproduced by SSR are converted.
    Unsupported conditions/actions are reported instead of approximated.
    """

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("Le fichier Advanced Scene Switcher doit être un objet JSON.")
        nested = _extract_advanced_scene_switcher_payload(data)
        if nested is not None:
            return copy.deepcopy(dict(nested))
        return copy.deepcopy(dict(data))

    @staticmethod
    def find_scene_collection_file(
        collection_name: str,
        *,
        scenes_dir: str | Path | None = None,
    ) -> Path | None:
        wanted = str(collection_name or "").strip()
        if not wanted:
            return None
        if scenes_dir is None:
            appdata = os.environ.get("APPDATA", "").strip()
            if not appdata:
                return None
            root = Path(appdata) / "obs-studio" / "basic" / "scenes"
        else:
            root = Path(scenes_dir)
        if not root.is_dir():
            return None

        for candidate in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("name") or "").strip() != wanted:
                continue
            nested = _extract_advanced_scene_switcher_payload(raw)
            if nested is not None:
                return candidate
        return None

    @staticmethod
    def _translate_window_regex(
        value: str,
        raw_config: Any,
    ) -> str | None:
        config = raw_config if isinstance(raw_config, Mapping) else {}
        if not bool(config.get("enable", False)):
            return "^" + re.escape(value) + "$"
        try:
            options = int(config.get("options", 0) or 0)
        except (TypeError, ValueError):
            return None
        # ASC uses QRegularExpression. The only options converted here are
        # CaseInsensitiveOption (0x1) and DotMatchesEverythingOption (0x2).
        if options & ~0x3:
            return None
        flags = ""
        if options & 0x1:
            flags += "i"
        if options & 0x2:
            flags += "s"
        expression = value
        if not bool(config.get("partial", False)):
            expression = f"^(?:{expression})$"
        return f"(?{flags}){expression}" if flags else expression

    @classmethod
    def _trigger_selector(
        cls,
        macro_name: str,
        condition: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            logic = int(condition.get("logic", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None, f"{macro_name}: logique de condition ASC invalide"
        if logic != 0:
            return None, (
                f"{macro_name}: logique de condition ASC {logic} non convertible "
                "(la négation/logique composée n'est pas approximée)"
            )

        duration = condition.get("durationModifier")
        if isinstance(duration, Mapping):
            try:
                constraint = int(duration.get("time_constraint", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return None, (
                    f"{macro_name}: contrainte de durée ASC invalide"
                )
            if constraint != 0:
                return None, (
                    f"{macro_name}: condition ASC avec contrainte de durée "
                    "non convertible exactement"
                )

        condition_id = str(condition.get("id") or "").strip()

        if condition_id == "process":
            regex = condition.get("regexConfig")
            if isinstance(regex, Mapping) and bool(regex.get("enable", False)):
                return None, (
                    f"{macro_name}: regex de processus ASC non convertie automatiquement"
                )
            process = str(condition.get("process") or "").strip()
            if not process:
                return None, f"{macro_name}: processus ASC vide"

            focused = bool(condition.get("focus", False))
            if not focused:
                if bool(condition.get("checkPath", False)):
                    return None, (
                        f"{macro_name}: chemin process sans focus non représenté "
                        "par process_running"
                    )
                return {
                    "exe": "",
                    "path": "",
                    "title_regex": "",
                    "conditions": {"process_running": process},
                    "process": process,
                }, ""

            path = ""
            if bool(condition.get("checkPath", False)):
                path_regex = condition.get("pathRegex")
                if (
                    isinstance(path_regex, Mapping)
                    and bool(path_regex.get("enable", False))
                ):
                    return None, (
                        f"{macro_name}: regex de chemin process ASC non convertible"
                    )
                path = str(condition.get("processPath") or "").strip()
                if not path:
                    return None, f"{macro_name}: chemin process ASC vide"
            return {
                "exe": process,
                "path": path,
                "title_regex": "",
                "conditions": {},
                "process": process,
            }, ""

        if condition_id == "window":
            if not bool(condition.get("focus", False)):
                return None, (
                    f"{macro_name}: condition fenêtre sans focus actif non représentable"
                )
            if not bool(condition.get("checkTitle", False)):
                return None, (
                    f"{macro_name}: condition fenêtre sans titre non représentable"
                )
            for key in (
                "fullscreen",
                "maximized",
                "windowFocusChanged",
                "checkWindowText",
            ):
                if bool(condition.get(key, False)):
                    return None, (
                        f"{macro_name}: contrainte fenêtre ASC '{key}' non convertible"
                    )
            window = str(condition.get("window") or "").strip()
            if not window:
                return None, f"{macro_name}: titre de fenêtre ASC vide"
            title_regex = cls._translate_window_regex(
                window,
                condition.get("windowRegexConfig"),
            )
            if not title_regex:
                return None, (
                    f"{macro_name}: options regex fenêtre ASC non convertibles"
                )
            return {
                "exe": "",
                "path": "",
                "title_regex": title_regex,
                "conditions": {},
                "process": "",
            }, ""

        return None, (
            f"{macro_name}: condition '{condition_id or '?'}' non convertible "
            "(process/window foreground seulement)"
        )

    @staticmethod
    def _convert_action(
        macro_name: str,
        action: Mapping[str, Any],
        *,
        snapshot: SceneCollectionSnapshot | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        action_id = str(action.get("id") or "").strip()
        if action_id == "scene_switch":
            if int(action.get("action", 0) or 0) != 0:
                return None, f"{macro_name}: variante scene_switch non prise en charge"
            if int(action.get("sceneType", 0) or 0) != 0:
                return None, f"{macro_name}: scène Preview non convertible"
            scene_selection = action.get("sceneSelection")
            scene = _scene_name(action)
            if not scene:
                return None, (
                    f"{macro_name}: scene_switch dynamique/courante non convertible"
                )
            if isinstance(scene_selection, Mapping):
                canvas = str(
                    scene_selection.get("canvasSelection") or ""
                ).strip()
                if canvas and canvas.casefold() != "main":
                    return None, (
                        f"{macro_name}: canvas '{canvas}' non pris en charge"
                    )
            if "transitionType" in action and int(
                action.get("transitionType", 1) or 0
            ) != 1:
                return None, (
                    f"{macro_name}: transition spécifique non représentée par SSR"
                )
            if "duration" in action and not _duration_is_zero(
                action.get("duration")
            ):
                return None, (
                    f"{macro_name}: durée de transition spécifique non représentée"
                )
            return (
                {
                    "type": "set_program_scene",
                    "name": f"Import ASC · {macro_name} · scène",
                    "enabled": True,
                    "params": {"scene": scene},
                },
                "",
            )

        if action_id == "scene_visibility":
            if bool(action.get("updateTransition", False)) or bool(
                action.get("updateDuration", False)
            ):
                return None, (
                    f"{macro_name}: visibilité avec transition/durée personnalisée "
                    "non convertible exactement"
                )
            scene = _scene_name(action)
            item = action.get("sceneItemSelection")
            source = (
                str(item.get("item") or "").strip()
                if isinstance(item, Mapping)
                and int(item.get("type", -1) or 0) == 0
                and int(item.get("idxType", 0) or 0) == 0
                and int(item.get("idx", 0) or 0) == 0
                else ""
            )
            mode = int(action.get("action", -1) or 0)
            if not scene or not source or mode not in {0, 1}:
                return None, (
                    f"{macro_name}: visibilité de scène dynamique, occurrence "
                    "spécifique ou toggle non convertible"
                )
            if snapshot is not None:
                count = snapshot.scene_item_count(scene, source)
                if count != 1:
                    return None, (
                        f"{macro_name}: {scene}/{source} possède "
                        f"{count} occurrence(s) dans OBS"
                    )
            return (
                {
                    "type": "scene_item_enabled",
                    "name": f"Import ASC · {macro_name} · visibilité",
                    "enabled": True,
                    "params": {
                        "scene": scene,
                        "source": source,
                        "enabled": mode == 0,
                    },
                },
                "",
            )

        if action_id == "filter":
            source = _selection_name(action.get("source"))
            filter_name = _selection_name(action.get("filter"))
            mode = int(action.get("action", -1) or 0)
            if not source or not filter_name:
                return None, (
                    f"{macro_name}: source/filtre ASC dynamique non convertible"
                )
            if mode in {0, 1}:
                return (
                    {
                        "type": "source_filter_enabled",
                        "name": f"Import ASC · {macro_name} · filtre",
                        "enabled": True,
                        "params": {
                            "source": source,
                            "filter": filter_name,
                            "enabled": mode == 0,
                        },
                    },
                    "",
                )
            if mode == 3:
                if int(action.get("inputMethod", -1) or 0) != 2:
                    return None, (
                        f"{macro_name}: settings filtre ASC non-JSON "
                        "à migrer manuellement"
                    )
                raw_settings = str(action.get("settings") or "").strip()
                try:
                    settings = json.loads(raw_settings)
                except json.JSONDecodeError:
                    return None, (
                        f"{macro_name}: JSON settings filtre ASC invalide"
                    )
                if not isinstance(settings, Mapping):
                    return None, (
                        f"{macro_name}: settings filtre ASC ne sont pas un objet"
                    )
                return (
                    {
                        "type": "source_filter_settings",
                        "name": f"Import ASC · {macro_name} · filtre settings",
                        "enabled": True,
                        "params": {
                            "source": source,
                            "filter": filter_name,
                            "settings": dict(settings),
                            "overlay": True,
                        },
                    },
                    "",
                )
            return None, f"{macro_name}: action filtre ASC {mode} non convertible"

        if action_id == "source":
            source = _selection_name(action.get("source"))
            mode = int(action.get("action", -1) or 0)
            if mode != 2 or not source:
                return None, (
                    f"{macro_name}: action source ASC {mode} non convertible"
                )
            if snapshot is not None and source not in snapshot.input_names:
                return None, (
                    f"{macro_name}: '{source}' n'est pas un input OBS de la collection"
                )

            input_method = int(action.get("inputMethod", -1) or 0)
            settings: Mapping[str, Any]
            if input_method == 2:
                raw_settings = str(action.get("settings") or "").strip()
                try:
                    parsed = json.loads(raw_settings)
                except json.JSONDecodeError:
                    return None, f"{macro_name}: JSON settings source ASC invalide"
                if not isinstance(parsed, Mapping):
                    return None, (
                        f"{macro_name}: settings source ASC ne sont pas un objet"
                    )
                settings = dict(parsed)
            elif input_method == 0:
                setting = action.get("sourceSetting")
                if not isinstance(setting, Mapping):
                    return None, (
                        f"{macro_name}: propriété source ASC manuelle invalide"
                    )
                setting_id = str(setting.get("id") or "").strip()
                if not setting_id:
                    return None, (
                        f"{macro_name}: propriété source ASC manuelle sans identifiant"
                    )
                manual = str(action.get("manualSettingValue") or "")
                if "${" in manual:
                    rebuilt = _TEMPLATE_VARIABLE_RE.sub(
                        lambda match: "${" + match.group(1) + "}",
                        manual,
                    )
                    if rebuilt != manual:
                        return None, (
                            f"{macro_name}: template source ASC invalide"
                        )
                try:
                    setting_type = int(setting.get("type", -1))
                except (TypeError, ValueError, OverflowError):
                    setting_type = -1
                if setting_type in {5, 6}:
                    value: Any = manual
                elif setting_type == 2:
                    try:
                        value = int(manual)
                    except (TypeError, ValueError, OverflowError):
                        return None, (
                            f"{macro_name}: valeur entière invalide pour {setting_id}"
                        )
                else:
                    return None, (
                        f"{macro_name}: type de propriété source ASC "
                        f"{setting_type} non convertible en mode manuel"
                    )
                settings = {setting_id: value}
            else:
                return None, (
                    f"{macro_name}: méthode settings source ASC "
                    f"{input_method} non convertible"
                )

            return (
                {
                    "type": "set_input_settings",
                    "name": f"Import ASC · {macro_name} · source settings",
                    "enabled": True,
                    "params": {
                        "input": source,
                        "settings": dict(settings),
                        "overlay": True,
                    },
                },
                "",
            )

        if action_id == "wait":
            duration_ms = _asc_wait_ms(action)
            if duration_ms is None:
                return None, (
                    f"{macro_name}: action wait ASC non convertible exactement"
                )
            return (
                {
                    "type": "wait_ms",
                    "name": f"Import ASC · {macro_name} · attente",
                    "enabled": True,
                    "params": {"duration_ms": duration_ms},
                },
                "",
            )

        if action_id == "run":
            process_config = action.get("processConfig")
            if not isinstance(process_config, Mapping):
                return None, f"{macro_name}: configuration run ASC invalide"
            if bool(action.get("wait", False)):
                return None, (
                    f"{macro_name}: run ASC bloquant non convertible"
                )
            executable = str(process_config.get("path") or "").strip()
            executable_name = (
                executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            )
            args_raw = process_config.get("args")
            args = [
                str(item.get("arg") or "")
                for item in args_raw
                if isinstance(item, Mapping)
            ] if isinstance(args_raw, list) else []
            if (
                executable_name != "soundvolumeview.exe"
                or len(args) != 4
                or args[0].casefold() != "/setappdefault"
                or args[2].casefold() not in {"0", "1", "2", "all"}
                or not args[1].strip()
                or not args[3].strip()
            ):
                return None, (
                    f"{macro_name}: action run générique non convertible "
                    "(seul SoundVolumeView /SetAppDefault est reconnu)"
                )
            return (
                {
                    "type": "app_audio_output",
                    "name": f"Import ASC · {macro_name} · routage audio",
                    "enabled": True,
                    "params": {
                        "device": args[1].strip(),
                        "roles": args[2].strip().casefold(),
                        "process": args[3].strip(),
                    },
                    "_soundvolumeview_path": executable,
                },
                "",
            )

        if action_id == "audio":
            source = _selection_name(action.get("audioSource"))
            if not source:
                return None, (
                    f"{macro_name}: source audio ASC dynamique non convertible"
                )
            if snapshot is not None and source not in snapshot.input_names:
                return None, (
                    f"{macro_name}: '{source}' n'est pas un input OBS de la collection"
                )
            mode = int(action.get("action", -1) or 0)
            if mode == 0:
                return (
                    {
                        "type": "input_mute",
                        "name": f"Import ASC · {macro_name} · mute",
                        "enabled": True,
                        "params": {"input": source, "muted": True},
                    },
                    "",
                )
            if mode == 1:
                return (
                    {
                        "type": "input_mute",
                        "name": f"Import ASC · {macro_name} · unmute",
                        "enabled": True,
                        "params": {"input": source, "muted": False},
                    },
                    "",
                )
            if mode == 2:
                if bool(action.get("fade", False)):
                    return None, f"{macro_name}: fade audio ASC non convertible"
                if not bool(action.get("useDb", False)):
                    return None, (
                        f"{macro_name}: volume ASC en pourcentage non converti "
                        "automatiquement"
                    )
                raw = action.get("volumeDB")
                if not isinstance(raw, Mapping):
                    return None, f"{macro_name}: volume dB ASC invalide"
                if int(raw.get("type", 0) or 0) != 0:
                    return None, (
                        f"{macro_name}: volume dB ASC dépend d'une variable"
                    )
                value = raw.get("value")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not -100.0 <= float(value) <= 26.0
                ):
                    return None, f"{macro_name}: volume dB ASC invalide/hors plage"
                return (
                    {
                        "type": "input_volume_db",
                        "name": f"Import ASC · {macro_name} · volume",
                        "enabled": True,
                        "params": {
                            "input": source,
                            "volume_db": float(value),
                        },
                    },
                    "",
                )
            return None, f"{macro_name}: action audio ASC {mode} non convertible"

        return None, f"{macro_name}: action ASC '{action_id}' non convertible"

    @staticmethod
    def _unique_name(existing: set[str], wanted: str) -> str:
        base = wanted.strip() or "Import ASC"
        if base not in existing:
            existing.add(base)
            return base
        index = 2
        while f"{base} ({index})" in existing:
            index += 1
        name = f"{base} ({index})"
        existing.add(name)
        return name

    @staticmethod
    def _merge_actions(
        profile: dict[str, Any],
        actions: list[dict[str, Any]],
        *,
        reject_conflicts: bool = False,
    ) -> tuple[int, tuple[str, ...]]:
        existing = profile.setdefault("actions", [])
        if not isinstance(existing, list):
            raise ValueError("Le profil SSR cible contient une liste d'actions invalide.")
        positions = {
            _action_identity(item): index
            for index, item in enumerate(existing)
            if isinstance(item, Mapping)
        }
        if reject_conflicts:
            conflicts = tuple(
                repr(identity)
                for action in actions
                for identity in (_action_identity(action),)
                if identity in positions
                and existing[positions[identity]] != action
            )
            if conflicts:
                return 0, conflicts

        changed = 0
        for action in actions:
            identity = _action_identity(action)
            if identity in positions:
                previous = existing[positions[identity]]
                if previous == action:
                    continue
                existing[positions[identity]] = action
            else:
                positions[identity] = len(existing)
                existing.append(action)
            changed += 1
        return changed, ()

    @classmethod
    def apply_to_config(
        cls,
        data: Mapping[str, Any],
        config: dict[str, Any],
        *,
        snapshot: SceneCollectionSnapshot | None = None,
        enable_created_rules: bool = False,
    ) -> AdvancedSceneSwitcherImportReport:
        macros_raw = data.get("macros", [])
        macros = [
            item
            for item in macros_raw
            if isinstance(item, Mapping) and not bool(item.get("group", False))
        ] if isinstance(macros_raw, list) else []

        control_variables = config.setdefault("control_variables", {})
        if not isinstance(control_variables, dict):
            raise ValueError("config.control_variables doit être un objet.")
        for variable_name, variable_value in _asc_control_variable_defaults(
            data
        ).items():
            control_variables.setdefault(variable_name, variable_value)

        rules = config.setdefault("rules", [])
        if not isinstance(rules, list):
            raise ValueError("config.rules doit être une liste.")
        profiles = config.setdefault("profiles", {})
        game_profiles = profiles.setdefault("game", {})
        if not isinstance(game_profiles, dict):
            raise ValueError("config.profiles.game doit être un objet.")

        fallback_raw = config.get("router", {}).get("fallback_state", {})
        fallback = dict(fallback_raw) if isinstance(fallback_raw, Mapping) else {}

        def existing_process_state(process: str) -> dict[str, Any] | None:
            candidates: list[dict[str, Any]] = []
            wanted = process.casefold()
            for rule in rules:
                if not isinstance(rule, Mapping):
                    continue
                if str(rule.get("behavior", "match")).casefold() != "match":
                    continue
                if str(rule.get("exe") or "").strip().casefold() != wanted:
                    continue
                if dict(rule.get("conditions") or {}):
                    continue
                state = rule.get("state")
                if isinstance(state, Mapping):
                    candidates.append(copy.deepcopy(dict(state)))
            if len(candidates) != 1:
                return None
            return candidates[0]

        process_game_targets: dict[str, str] = {}
        process_asc_game_values: dict[str, str] = {}
        process_baseline_states: dict[str, dict[str, Any]] = {}
        ambiguous_process_targets: set[str] = set()
        for macro in macros:
            conditions_raw = macro.get("conditions", [])
            actions_raw = macro.get("actions", [])
            conditions = [
                item
                for item in conditions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(conditions_raw, list) else []
            actions = [
                item
                for item in actions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(actions_raw, list) else []
            if len(conditions) != 1:
                continue
            condition = conditions[0]
            if str(condition.get("id") or "").strip() != "process":
                continue
            process = str(condition.get("process") or "").strip()
            targets = {
                value
                for item in actions
                for value in (_asc_game_assignment(item),)
                if value
            }
            if not process or len(targets) != 1:
                continue
            key = process.casefold()
            asc_target = next(iter(targets))
            previous = process_asc_game_values.get(key)
            if (
                previous is not None
                and previous.casefold() != asc_target.casefold()
            ):
                ambiguous_process_targets.add(key)
                process_game_targets.pop(key, None)
                process_asc_game_values.pop(key, None)
                process_baseline_states.pop(key, None)
                continue
            if key in ambiguous_process_targets:
                continue

            baseline = existing_process_state(process)
            resolved_target = asc_target
            if baseline is not None:
                existing_target = str(baseline.get("Game") or "").strip()
                if existing_target:
                    resolved_target = existing_target
                    process_baseline_states[key] = baseline

            process_asc_game_values[key] = asc_target
            process_game_targets[key] = resolved_target
        existing_rule_names = {
            str(rule.get("name") or "")
            for rule in rules
            if isinstance(rule, Mapping)
        }
        existing_profile_names = set(str(name) for name in game_profiles)

        converted_macros = 0
        actions_converted = 0
        rules_created = 0
        profiles_created = 0
        attached = 0
        skipped: list[str] = []
        rejected_raw: list[RejectedAdvancedSceneSwitcherMacro] = []

        def reject(
            macro_name: str,
            reason: str,
            raw_macro: Mapping[str, Any],
        ) -> None:
            skipped.append(reason)
            rejected_raw.append(
                RejectedAdvancedSceneSwitcherMacro(
                    name=macro_name,
                    reason=reason,
                    raw=copy.deepcopy(dict(raw_macro)),
                )
            )

        for index, macro in enumerate(macros):
            name = str(macro.get("name") or f"Macro {index + 1}").strip()
            if bool(macro.get("pause", False)):
                reject(name, f"{name}: macro en pause", macro)
                continue
            if bool(macro.get("parallel", False)):
                reject(name, f"{name}: exécution parallèle non convertible", macro)
                continue
            if bool(macro.get("skipExecOnStart", False)):
                reject(name, f"{name}: skipExecOnStart non convertible exactement", macro)
                continue
            else_actions = macro.get("elseActions", [])
            if isinstance(else_actions, list) and any(
                isinstance(item, Mapping) and _enabled(item)
                for item in else_actions
            ):
                reject(name, f"{name}: elseActions non converties", macro)
                continue

            conditions_raw = macro.get("conditions", [])
            conditions = [
                item
                for item in conditions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(conditions_raw, list) else []
            active_actions_raw = macro.get("actions", [])
            active_actions_for_special = [
                item
                for item in active_actions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(active_actions_raw, list) else []

            changed_variables = _asc_changed_variables(conditions)
            if changed_variables is not None and set(changed_variables) == {
                "Mood",
                "Game",
            }:
                converted_sequence: list[dict[str, Any]] = []
                sequence_reasons: list[str] = []
                for action in active_actions_for_special:
                    mapped, action_reason = cls._convert_action(
                        name,
                        action,
                        snapshot=snapshot,
                    )
                    if mapped is None:
                        sequence_reasons.append(action_reason)
                    else:
                        converted_sequence.append(mapped)
                if sequence_reasons:
                    reject(name, "; ".join(sequence_reasons), macro)
                    continue
                if not converted_sequence:
                    reject(name, f"{name}: aucune action convertible", macro)
                    continue
                fallback_profile = (
                    str(fallback.get("Game") or "Vanilla").strip()
                    or "Vanilla"
                )
                profile_game_values: dict[str, str] = {
                    fallback_profile: fallback_profile,
                }
                for process_key, profile_name in process_game_targets.items():
                    if not str(profile_name).strip():
                        continue
                    asc_value = process_asc_game_values.get(
                        process_key,
                        profile_name,
                    )
                    previous_value = profile_game_values.get(profile_name)
                    if (
                        previous_value is not None
                        and previous_value.casefold() != asc_value.casefold()
                    ):
                        reject(
                            name,
                            (
                                f"{name}: plusieurs valeurs ASC Game ciblent "
                                f"le même profil SSR '{profile_name}'"
                            ),
                            macro,
                        )
                        break
                    profile_game_values[profile_name] = asc_value
                else:
                    for profile_name in sorted(profile_game_values):
                        profile = game_profiles.get(profile_name)
                        if profile is None:
                            game_profiles[profile_name] = {
                                "actions": [],
                                "extends": "",
                                "conditions": {},
                            }
                            existing_profile_names.add(profile_name)
                            profiles_created += 1
                            profile = game_profiles[profile_name]
                        if not isinstance(profile, dict):
                            reject(
                                name,
                                f"{name}: Game profile '{profile_name}' invalide",
                                macro,
                            )
                            break
                        bound_sequence = _replace_template_variable(
                            converted_sequence,
                            name="Game",
                            replacement=profile_game_values[profile_name],
                        )
                        _append_exact_sequence(profile, bound_sequence)
                    else:
                        converted_macros += 1
                        actions_converted += len(converted_sequence)
                        continue
                    continue
                continue
            if len(conditions) == 1:
                condition = conditions[0]
                if str(condition.get("id") or "").strip() == "streamdeck":
                    assignments = [
                        item
                        for item in active_actions_for_special
                        if str(item.get("id") or "").strip() == "variable"
                        and int(item.get("condition", 0) or 0) == 0
                    ]
                    if len(assignments) == 1:
                        variable_name = str(
                            assignments[0].get("variableName") or ""
                        ).strip()
                        value = str(assignments[0].get("strValue") or "")
                        if (
                            variable_name
                            and variable_name.casefold() != "game"
                            and variable_name in control_variables
                        ):
                            pattern = condition.get("pattern")
                            data_key = (
                                str(pattern.get("data") or "").strip()
                                if isinstance(pattern, Mapping)
                                else ""
                            )
                            reject(
                                name,
                                (
                                    f"{name}: remplacer le bouton Advanced Scene "
                                    "Switcher par l'action Stream Deck SSR "
                                    f"Variable de contrôle ({variable_name}={value}"
                                    + (
                                        f", ancien data={data_key}"
                                        if data_key
                                        else ""
                                    )
                                    + ")"
                                ),
                                macro,
                            )
                            continue

            fallback_target = str(
                fallback.get("Game") or "Vanilla"
            ).strip() or "Vanilla"
            explicit_special_targets = {
                value
                for action in active_actions_for_special
                for value in (_asc_game_assignment(action),)
                if value
            }
            if (
                len(conditions) >= 1
                and len(explicit_special_targets) == 1
                and next(iter(explicit_special_targets)).casefold()
                == fallback_target.casefold()
                and all(
                    str(item.get("id") or "").strip() == "process"
                    for item in conditions
                )
                and all(
                    int(item.get("logic", -999))
                    == (1 if index == 0 else 103)
                    for index, item in enumerate(conditions)
                )
                and all(
                    str(item.get("process") or "").strip().casefold()
                    in process_game_targets
                    for item in conditions
                )
            ):
                converted_macros += 1
                continue

            if len(conditions) != 1:
                reject(
                    name,
                    f"{name}: {len(conditions)} conditions actives ; "
                    "SSR n'assemble pas automatiquement une logique ASC complexe",
                    macro,
                )
                continue

            direct_profile_target = _asc_game_condition(conditions[0])
            selector: dict[str, Any] | None
            if direct_profile_target:
                selector = None
            else:
                selector, reason = cls._trigger_selector(name, conditions[0])
                if selector is None:
                    reject(name, reason, macro)
                    continue

            actions_raw = macro.get("actions", [])
            active_actions = [
                item
                for item in actions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(actions_raw, list) else []
            game_targets = {
                value
                for action in active_actions
                for value in (_asc_game_assignment(action),)
                if value
            }
            if len(game_targets) > 1:
                reject(
                    name,
                    f"{name}: plusieurs affectations Game contradictoires",
                    macro,
                )
                continue
            explicit_game_target = (
                next(iter(game_targets)) if game_targets else ""
            )
            converted: list[dict[str, Any]] = []
            reasons: list[str] = []
            soundvolumeview_paths: set[str] = set()
            for action in active_actions:
                if _asc_game_assignment(action):
                    continue
                mapped, action_reason = cls._convert_action(
                    name,
                    action,
                    snapshot=snapshot,
                )
                if mapped is None:
                    reasons.append(action_reason)
                else:
                    path = str(
                        mapped.pop("_soundvolumeview_path", "") or ""
                    ).strip()
                    if path:
                        soundvolumeview_paths.add(path)
                    converted.append(mapped)
            if reasons:
                reject(name, "; ".join(reasons), macro)
                continue
            converted, coalesce_reason = _coalesce_input_settings_actions(
                name,
                converted,
            )
            if converted is None:
                reject(name, coalesce_reason, macro)
                continue

            if len(soundvolumeview_paths) > 1:
                reject(
                    name,
                    f"{name}: plusieurs chemins SoundVolumeView contradictoires",
                    macro,
                )
                continue
            if soundvolumeview_paths:
                imported_path = next(iter(soundvolumeview_paths))
                host = config.setdefault("host_control", {})
                if not isinstance(host, dict):
                    reject(
                        name,
                        f"{name}: host_control SSR invalide",
                        macro,
                    )
                    continue
                current_path = str(host.get("soundvolumeview_path") or "").strip()
                if (
                    current_path
                    and current_path.casefold() != imported_path.casefold()
                ):
                    reject(
                        name,
                        f"{name}: chemin SoundVolumeView en conflit avec SSR",
                        macro,
                    )
                    continue
                host["soundvolumeview_path"] = imported_path

            process_name = (
                str(selector.get("process") or "").strip()
                if selector is not None
                else ""
            )
            process_key = process_name.casefold() if process_name else ""
            inferred_game_target = (
                process_game_targets.get(process_key, "")
                if process_key
                else ""
            )
            asc_process_target = (
                process_asc_game_values.get(process_key, "")
                if process_key
                else ""
            )
            resolved_explicit_target = explicit_game_target
            if (
                inferred_game_target
                and explicit_game_target
                and asc_process_target
                and explicit_game_target.casefold()
                == asc_process_target.casefold()
            ):
                resolved_explicit_target = inferred_game_target
            profile_target = (
                direct_profile_target
                or resolved_explicit_target
                or inferred_game_target
            )
            if not converted and not (
                explicit_game_target and selector is not None
            ):
                reject(name, f"{name}: aucune action convertible", macro)
                continue

            identities = [_action_identity(action) for action in converted]
            duplicates = sorted(
                {
                    identity
                    for identity in identities
                    if identities.count(identity) > 1
                },
                key=repr,
            )
            if duplicates:
                reject(
                    name,
                    (
                        f"{name}: plusieurs actions successives ciblent la même "
                        "propriété SSR ; séquence non convertible en état final "
                        f"({duplicates!r})"
                    ),
                    macro,
                )
                continue

            if profile_target:
                profile_name = profile_target
                profile = game_profiles.get(profile_name)
                if profile is None:
                    game_profiles[profile_name] = {
                        "actions": [],
                        "extends": "",
                        "conditions": {},
                    }
                    existing_profile_names.add(profile_name)
                    profiles_created += 1
                    profile = game_profiles[profile_name]
                if not isinstance(profile, dict):
                    reject(
                        name,
                        f"{name}: Game profile '{profile_name}' invalide",
                        macro,
                    )
                    continue
            else:
                profile_name = ""

            if selector is None:
                assert profile_name
                _changed, conflicts = cls._merge_actions(
                    profile,
                    converted,
                    reject_conflicts=True,
                )
                if conflicts:
                    reject(
                        name,
                        (
                            f"{name}: conflit avec des actions SSR existantes "
                            f"sur {', '.join(conflicts)}"
                        ),
                        macro,
                    )
                    continue
                converted_macros += 1
                actions_converted += len(converted)
                continue

            exe = str(selector.get("exe") or "")
            path = str(selector.get("path") or "")
            title_regex = str(selector.get("title_regex") or "")
            selector_conditions = selector.get("conditions")
            conditions_mapping = (
                dict(selector_conditions)
                if isinstance(selector_conditions, Mapping)
                else {}
            )
            matching_rules = [
                rule
                for rule in rules
                if isinstance(rule, dict)
                and str(rule.get("behavior", "match")).casefold() == "match"
                and str(rule.get("exe") or "").casefold() == exe.casefold()
                and str(rule.get("path") or "").casefold() == path.casefold()
                and str(rule.get("title_regex") or "") == title_regex
                and dict(rule.get("conditions") or {}) == conditions_mapping
            ]
            if len(matching_rules) > 1:
                reject(
                    name,
                    f"{name}: plusieurs règles SSR correspondent déjà au même déclencheur",
                    macro,
                )
                continue

            inferred_only = bool(
                inferred_game_target
                and not explicit_game_target
                and not direct_profile_target
            )
            if inferred_only and profile_name:
                _changed, conflicts = cls._merge_actions(
                    profile,
                    converted,
                    reject_conflicts=True,
                )
                if conflicts:
                    reject(
                        name,
                        (
                            f"{name}: conflit avec des actions SSR existantes "
                            f"sur {', '.join(conflicts)}"
                        ),
                        macro,
                    )
                    continue
                attached += 1
            elif len(matching_rules) == 1:
                rule = matching_rules[0]
                state = rule.get("state")
                if not isinstance(state, Mapping):
                    reject(
                        name,
                        f"{name}: règle SSR existante {exe or conditions_mapping} "
                        "sans état exploitable",
                        macro,
                    )
                    continue
                existing_target = str(state.get("Game") or "").strip()
                if profile_name and existing_target.casefold() != profile_name.casefold():
                    reject(
                        name,
                        (
                            f"{name}: la règle SSR existante cible Game="
                            f"{existing_target}, attendu {profile_name}"
                        ),
                        macro,
                    )
                    continue
                if not profile_name:
                    profile_name = existing_target
                    profile = game_profiles.get(profile_name)
                if not profile_name or not isinstance(profile, dict):
                    reject(
                        name,
                        f"{name}: Game profile '{profile_name}' introuvable",
                        macro,
                    )
                    continue
                _changed, conflicts = cls._merge_actions(
                    profile,
                    converted,
                    reject_conflicts=True,
                )
                if conflicts:
                    reject(
                        name,
                        (
                            f"{name}: conflit avec des actions SSR existantes "
                            f"sur {', '.join(conflicts)}"
                        ),
                        macro,
                    )
                    continue
                attached += 1
            else:
                if not profile_name:
                    profile_name = cls._unique_name(
                        existing_profile_names,
                        f"ASC · {name}",
                    )
                    game_profiles[profile_name] = {
                        "actions": converted,
                        "extends": "",
                        "conditions": {},
                    }
                    profile = game_profiles[profile_name]
                    profiles_created += 1
                else:
                    _changed, conflicts = cls._merge_actions(
                        profile,
                        converted,
                        reject_conflicts=True,
                    )
                    if conflicts:
                        reject(
                            name,
                            (
                                f"{name}: conflit avec des actions SSR existantes "
                                f"sur {', '.join(conflicts)}"
                            ),
                            macro,
                        )
                        continue

                baseline_state = (
                    process_baseline_states.get(process_key)
                    if process_key
                    else None
                )
                state = (
                    copy.deepcopy(baseline_state)
                    if isinstance(baseline_state, Mapping)
                    else dict(fallback)
                )
                state["Game"] = profile_name
                rule_name = cls._unique_name(
                    existing_rule_names,
                    f"ASC · {name}",
                )
                rules.append(
                    {
                        "name": rule_name,
                        "behavior": "match",
                        "priority": 50 - index,
                        "enabled": bool(enable_created_rules),
                        "exe": exe,
                        "path": path,
                        "title_regex": title_regex,
                        "state": state,
                        "apply_delay_ms": 0,
                        "conditions": conditions_mapping,
                    }
                )
                rules_created += 1

            converted_macros += 1
            actions_converted += len(converted)

        return AdvancedSceneSwitcherImportReport(
            macros_total=len(macros),
            macros_converted=converted_macros,
            actions_converted=actions_converted,
            rules_created=rules_created,
            profiles_created=profiles_created,
            attached_to_existing_rules=attached,
            skipped=tuple(skipped),
            rejected_raw=tuple(rejected_raw),
        )
