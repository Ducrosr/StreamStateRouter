from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AdvancedSceneSwitcherImportReport:
    macros_total: int
    macros_converted: int
    actions_converted: int
    rules_created: int
    profiles_created: int
    attached_to_existing_rules: int
    skipped: tuple[str, ...]

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
    if kind == "set_input_settings":
        return (kind, str(params.get("input") or ""))
    return (kind,)


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
        nested = data.get("advanced-scene-switcher")
        if isinstance(nested, Mapping):
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
            nested = raw.get("advanced-scene-switcher")
            if isinstance(nested, Mapping):
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
        condition_id = str(condition.get("id") or "").strip()

        if condition_id == "process":
            if not bool(condition.get("focus", False)):
                return None, (
                    f"{macro_name}: condition process sans focus actif non représentable "
                    "par le routeur foreground SSR"
                )
            regex = condition.get("regexConfig")
            if isinstance(regex, Mapping) and bool(regex.get("enable", False)):
                return None, (
                    f"{macro_name}: regex de processus ASC non convertie automatiquement"
                )
            process = str(condition.get("process") or "").strip()
            if not process:
                return None, f"{macro_name}: processus ASC vide"
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
            }, ""

        return None, (
            f"{macro_name}: condition '{condition_id or '?'}' non convertible "
            "(process/window foreground seulement)"
        )

    @staticmethod
    def _convert_action(
        macro_name: str,
        action: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        action_id = str(action.get("id") or "").strip()
        if action_id == "scene_switch":
            if int(action.get("action", 0) or 0) != 0:
                return None, f"{macro_name}: variante scene_switch non prise en charge"
            scene = _scene_name(action)
            if not scene:
                return None, (
                    f"{macro_name}: scene_switch dynamique/courante non convertible"
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
            if int(action.get("inputMethod", -1) or 0) != 2:
                return None, (
                    f"{macro_name}: settings source ASC non-JSON "
                    "à migrer manuellement"
                )
            raw_settings = str(action.get("settings") or "").strip()
            try:
                settings = json.loads(raw_settings)
            except json.JSONDecodeError:
                return None, f"{macro_name}: JSON settings source ASC invalide"
            if not isinstance(settings, Mapping):
                return None, (
                    f"{macro_name}: settings source ASC ne sont pas un objet"
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

        if action_id == "audio":
            source = _selection_name(action.get("audioSource"))
            if not source:
                return None, (
                    f"{macro_name}: source audio ASC dynamique non convertible"
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
    def _merge_actions(profile: dict[str, Any], actions: list[dict[str, Any]]) -> int:
        existing = profile.setdefault("actions", [])
        if not isinstance(existing, list):
            raise ValueError("Le profil SSR cible contient une liste d'actions invalide.")
        positions = {
            _action_identity(item): index
            for index, item in enumerate(existing)
            if isinstance(item, Mapping)
        }
        changed = 0
        for action in actions:
            identity = _action_identity(action)
            if identity in positions:
                existing[positions[identity]] = action
            else:
                positions[identity] = len(existing)
                existing.append(action)
            changed += 1
        return changed

    @classmethod
    def apply_to_config(
        cls,
        data: Mapping[str, Any],
        config: dict[str, Any],
    ) -> AdvancedSceneSwitcherImportReport:
        macros_raw = data.get("macros", [])
        macros = [
            item
            for item in macros_raw
            if isinstance(item, Mapping) and not bool(item.get("group", False))
        ] if isinstance(macros_raw, list) else []

        rules = config.setdefault("rules", [])
        if not isinstance(rules, list):
            raise ValueError("config.rules doit être une liste.")
        profiles = config.setdefault("profiles", {})
        game_profiles = profiles.setdefault("game", {})
        if not isinstance(game_profiles, dict):
            raise ValueError("config.profiles.game doit être un objet.")

        fallback_raw = config.get("router", {}).get("fallback_state", {})
        fallback = dict(fallback_raw) if isinstance(fallback_raw, Mapping) else {}
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

        for index, macro in enumerate(macros):
            name = str(macro.get("name") or f"Macro {index + 1}").strip()
            if bool(macro.get("pause", False)):
                skipped.append(f"{name}: macro en pause")
                continue
            if bool(macro.get("parallel", False)):
                skipped.append(f"{name}: exécution parallèle non convertible")
                continue
            if bool(macro.get("skipExecOnStart", False)):
                skipped.append(f"{name}: skipExecOnStart non convertible exactement")
                continue
            else_actions = macro.get("elseActions", [])
            if isinstance(else_actions, list) and any(
                isinstance(item, Mapping) and _enabled(item)
                for item in else_actions
            ):
                skipped.append(f"{name}: elseActions non converties")
                continue

            conditions_raw = macro.get("conditions", [])
            conditions = [
                item
                for item in conditions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(conditions_raw, list) else []
            if len(conditions) != 1:
                skipped.append(
                    f"{name}: {len(conditions)} conditions actives ; "
                    "SSR n'assemble pas automatiquement une logique ASC complexe"
                )
                continue
            selector, reason = cls._trigger_selector(name, conditions[0])
            if selector is None:
                skipped.append(reason)
                continue

            actions_raw = macro.get("actions", [])
            active_actions = [
                item
                for item in actions_raw
                if isinstance(item, Mapping) and _enabled(item)
            ] if isinstance(actions_raw, list) else []
            converted: list[dict[str, Any]] = []
            reasons: list[str] = []
            for action in active_actions:
                mapped, action_reason = cls._convert_action(name, action)
                if mapped is None:
                    reasons.append(action_reason)
                else:
                    converted.append(mapped)
            if reasons:
                skipped.extend(reasons)
                continue
            if not converted:
                skipped.append(f"{name}: aucune action convertible")
                continue

            exe = str(selector.get("exe") or "")
            path = str(selector.get("path") or "")
            title_regex = str(selector.get("title_regex") or "")
            matching_rules = [
                rule
                for rule in rules
                if isinstance(rule, dict)
                and str(rule.get("behavior", "match")).casefold() == "match"
                and str(rule.get("exe") or "").casefold() == exe.casefold()
                and str(rule.get("path") or "").casefold() == path.casefold()
                and str(rule.get("title_regex") or "") == title_regex
            ]
            if len(matching_rules) > 1:
                skipped.append(
                    f"{name}: plusieurs règles SSR correspondent déjà au même déclencheur"
                )
                continue

            if len(matching_rules) == 1:
                rule = matching_rules[0]
                state = rule.get("state")
                if not isinstance(state, Mapping):
                    skipped.append(
                        f"{name}: règle SSR existante {exe} sans état exploitable"
                    )
                    continue
                profile_name = str(state.get("Game") or "").strip()
                profile = game_profiles.get(profile_name)
                if not profile_name or not isinstance(profile, dict):
                    skipped.append(
                        f"{name}: Game profile '{profile_name}' introuvable"
                    )
                    continue
                cls._merge_actions(profile, converted)
                attached += 1
            else:
                profile_name = cls._unique_name(
                    existing_profile_names,
                    f"ASC · {name}",
                )
                game_profiles[profile_name] = {
                    "actions": converted,
                    "extends": "",
                    "conditions": {},
                }
                profiles_created += 1

                state = dict(fallback)
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
                        "enabled": False,
                        "exe": exe,
                        "path": path,
                        "title_regex": title_regex,
                        "state": state,
                        "apply_delay_ms": 0,
                        "conditions": {},
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
        )
