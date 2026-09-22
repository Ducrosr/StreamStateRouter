from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..activation.models import TriggerPolicyConfig
from ..host import HostControlConfig, HostControlController
from ..obs.dispatcher import PROFILE_DOMAINS, profile_map_from_raw
from ..obs.models import OBSConnectionConfig
from ..obs.layouts import anchor_factors, parse_module_source, transform_bbox
from ..router.models import DEFAULT_PROFILE_NAMES, StreamState
from ..router.rules import AppRule, ResolutionKind, RuleSet
from .paths import backups_dir, config_path, default_config_path

SCHEMA_VERSION = 6
SUPPORTED_ACTION_TYPES = {
    "set_program_scene",
    "scene_item_enabled",
    "source_filter_enabled",
    "source_filter_settings",
    "input_mute",
    "input_volume_db",
    "set_input_settings",
    "app_audio_output",
    "windows_hdr",
}
LAYOUT_ANCHORS = {
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
}
LAYOUT_TRANSITIONS = {"instant", "move", "fade", "move_fade"}


class ConfigError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration introuvable : {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON invalide ({path.name}) : {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("La racine de la configuration doit être un objet JSON")
    return data


def _layout_box(value: Any, fallback: Mapping[str, Any] | None = None) -> dict[str, float]:
    raw = value if isinstance(value, Mapping) else (fallback or {})
    return {
        "x": float(raw.get("x", 0.0) or 0.0),
        "y": float(raw.get("y", 0.0) or 0.0),
        "width": max(0.0001, abs(float(raw.get("width", 1.0) or 1.0))),
        "height": max(0.0001, abs(float(raw.get("height", 1.0) or 1.0))),
    }


def _split_legacy_grouped_layout_modules(profile: dict[str, Any]) -> None:
    """Migrate old ``[prefix] element`` grouping to one OBS source per module.

    Schema v3 interpreted the bracket prefix as the module identity. In the
    real scene collection the convention is ``[Type de module] Nom du module``:
    the prefix is only a category. Re-key every stored module by its full OBS
    source name and preserve the effective geometry of each former child.
    """
    modules = profile.get("modules")
    if not isinstance(modules, dict):
        return

    canvas_raw = profile.get("canvas") if isinstance(profile.get("canvas"), Mapping) else {}
    try:
        canvas_w = max(0.0, float(canvas_raw.get("width", 0) or 0))
        canvas_h = max(0.0, float(canvas_raw.get("height", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        canvas_w = canvas_h = 0.0

    migrated: dict[str, Any] = {}
    for _legacy_key, raw_module in modules.items():
        if not isinstance(raw_module, Mapping):
            continue
        elements = raw_module.get("elements")
        if not isinstance(elements, list):
            continue

        old_base = _layout_box(raw_module.get("base_bounds"))
        old_geometry = _layout_box(raw_module.get("geometry"), old_base)
        scale_x = old_geometry["width"] / old_base["width"]
        scale_y = old_geometry["height"] / old_base["height"]
        anchor = str(raw_module.get("anchor") or "top_left")
        ax, ay = anchor_factors(anchor)

        for raw_element in elements:
            if not isinstance(raw_element, Mapping) or not bool(raw_element.get("included", True)):
                continue
            source = str(raw_element.get("source") or "").strip()
            transform = raw_element.get("transform")
            if not source or not isinstance(transform, Mapping):
                continue

            parsed = parse_module_source(source)
            module_type = parsed.module if parsed is not None else "Autre"
            display_name = parsed.element if parsed is not None else source
            elem_left, elem_top, elem_width, elem_height = transform_bbox(transform)
            elem_base = {
                "x": elem_left,
                "y": elem_top,
                "width": max(1.0, elem_width),
                "height": max(1.0, elem_height),
            }
            elem_geometry = {
                "x": old_geometry["x"] + (elem_base["x"] - old_base["x"]) * scale_x,
                "y": old_geometry["y"] + (elem_base["y"] - old_base["y"]) * scale_y,
                "width": max(1.0, elem_base["width"] * scale_x),
                "height": max(1.0, elem_base["height"] * scale_y),
            }

            container = str(raw_element.get("container") or raw_module.get("container") or profile.get("scene") or "")
            key = source
            if key in migrated:
                key = f"{source} @ {container or 'scene'}"
                suffix = 2
                while key in migrated:
                    key = f"{source} @ {container or 'scene'} #{suffix}"
                    suffix += 1

            element = copy.deepcopy(dict(raw_element))
            element["element"] = display_name
            element["included"] = True
            element["container"] = container

            module = copy.deepcopy(dict(raw_module))
            module.update(
                {
                    "display_name": display_name,
                    "module_type": module_type,
                    "source_name": source,
                    "container": container,
                    "visible": bool(raw_module.get("visible", True)) and bool(raw_element.get("enabled", True)),
                    "base_bounds": elem_base,
                    "geometry": elem_geometry,
                    "elements": [element],
                }
            )
            if canvas_w > 0 and canvas_h > 0:
                module["normalized_geometry"] = {
                    "x": elem_geometry["x"] / canvas_w,
                    "y": elem_geometry["y"] / canvas_h,
                    "width": elem_geometry["width"] / canvas_w,
                    "height": elem_geometry["height"] / canvas_h,
                }
                module["anchor_offsets"] = {
                    "x": elem_geometry["x"] + elem_geometry["width"] * ax - canvas_w * ax,
                    "y": elem_geometry["y"] + elem_geometry["height"] * ay - canvas_h * ay,
                }
            migrated[key] = module

    profile["modules"] = migrated


def migrate_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate known older configuration schemas without losing user rules."""
    migrated = copy.deepcopy(dict(data))
    try:
        version = int(migrated.get("schema_version", 0))
    except (TypeError, ValueError):
        return migrated

    if version == SCHEMA_VERSION:
        return migrated

    if version == 1:
        router = migrated.setdefault("router", {})
        fallback = router.setdefault("fallback_state", {})
        if isinstance(fallback, dict):
            fallback.setdefault("LayoutProfile", fallback.get("OverlayProfile", "Vanilla"))
        layout_names: set[str] = set()
        if isinstance(fallback, dict):
            layout_names.add(str(fallback.get("LayoutProfile") or "Vanilla"))
        for raw in migrated.get("rules", []):
            if not isinstance(raw, dict) or str(raw.get("behavior", "match")).casefold() != "match":
                continue
            state = raw.get("state")
            if not isinstance(state, dict):
                continue
            state.setdefault("LayoutProfile", state.get("OverlayProfile", "Vanilla"))
            layout_names.add(str(state.get("LayoutProfile") or "Vanilla"))
        existing = migrated.get("layout_profiles")
        if not isinstance(existing, dict):
            existing = {}
        for name in layout_names or {"Vanilla"}:
            existing.setdefault(name, {"scene": "", "modules": {}})
        migrated["layout_profiles"] = existing
        version = 2

    if version == 2:
        migrated.setdefault("layout_history", {})
        migrated.setdefault(
            "api",
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8765,
                "token": "",
            },
        )
        ui = migrated.setdefault("ui", {})
        if isinstance(ui, dict):
            ui.setdefault("auto_detect_modules", True)
            ui.setdefault("module_scan_seconds", 5)
        for raw in migrated.get("rules", []):
            if isinstance(raw, dict):
                raw.setdefault("apply_delay_ms", 0)
                raw.setdefault("conditions", {})
        profiles = migrated.get("profiles", {})
        if isinstance(profiles, dict):
            for domain_profiles in profiles.values():
                if not isinstance(domain_profiles, dict):
                    continue
                for profile in domain_profiles.values():
                    if isinstance(profile, dict):
                        profile.setdefault("extends", "")
                        profile.setdefault("conditions", {})
        layouts = migrated.get("layout_profiles", {})
        if isinstance(layouts, dict):
            for profile in layouts.values():
                if not isinstance(profile, dict):
                    continue
                profile.setdefault("extends", "")
                profile.setdefault("coordinate_mode", "normalized")
                profile.setdefault("conditions", {})
                profile.setdefault("transition", {"mode": "instant", "duration_ms": 0, "steps": 8})
                modules = profile.get("modules", {})
                if not isinstance(modules, dict):
                    continue
                for module in modules.values():
                    if not isinstance(module, dict):
                        continue
                    module.setdefault("managed", True)
                    module.setdefault("locked", False)
                    module.setdefault("anchor_mode", "relative")
                    for element in module.get("elements", []) if isinstance(module.get("elements"), list) else []:
                        if not isinstance(element, dict):
                            continue
                        element.setdefault("follow_position", True)
                        element.setdefault("follow_size", True)
                        element.setdefault("follow_visibility", True)
                        element.setdefault("locked", False)
                        element.setdefault("flags", [])
        version = 3

    if version == 3:
        layouts = migrated.get("layout_profiles", {})
        if isinstance(layouts, dict):
            for profile in layouts.values():
                if isinstance(profile, dict):
                    _split_legacy_grouped_layout_modules(profile)
        history = migrated.get("layout_history", {})
        if isinstance(history, dict):
            for entries in history.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    profile = entry.get("profile")
                    if isinstance(profile, dict):
                        _split_legacy_grouped_layout_modules(profile)
        version = 4

    if version == 4:
        migrated.setdefault("activation_policies", {})
        version = 5

    if version == 5:
        migrated.setdefault(
            "host_control",
            {
                "soundvolumeview_path": "",
                "audio_timeout_seconds": 5.0,
            },
        )
        version = 6

    migrated["schema_version"] = version
    return migrated


def ensure_user_config() -> Path:
    target = config_path()
    if not target.exists():
        source = default_config_path()
        if not source.exists():
            raise ConfigError(f"Configuration par défaut introuvable : {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else ensure_user_config()
    data = migrate_config(_read_json(target))
    errors = validate_config(data)
    if errors:
        raise ConfigError("Configuration invalide :\n- " + "\n- ".join(errors))
    return data


def _validated_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = migrate_config(data)
    errors = validate_config(payload)
    if errors:
        raise ConfigError("Configuration invalide :\n- " + "\n- ".join(errors))
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Configuration non sérialisable strictement : {exc}") from exc
    return payload


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temp.replace(target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def _prune_backups(*, keep: int = 20) -> None:
    directory = backups_dir()
    if not directory.exists():
        return
    files = sorted(
        directory.glob("config-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in files[max(1, int(keep)) :]:
        try:
            stale.unlink()
        except OSError:
            pass


def save_config(
    data: Mapping[str, Any],
    path: str | Path | None = None,
    *,
    backup_limit: int = 20,
) -> Path:
    payload = _validated_payload(data)
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        directory = backups_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = directory / f"config-{stamp}-{uuid.uuid4().hex[:8]}.json"
        shutil.copy2(target, backup)
        _prune_backups(keep=backup_limit)
    _atomic_write_json(target, payload)
    return target


def _redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    obs = result.get("obs")
    if isinstance(obs, dict):
        obs["password"] = ""
    api = result.get("api")
    if isinstance(api, dict):
        api["token"] = ""
    host = result.get("host_control")
    if isinstance(host, dict):
        host["soundvolumeview_path"] = ""

    profiles = result.get("profiles")
    if isinstance(profiles, Mapping):
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
                    if not isinstance(action, dict):
                        continue
                    if str(action.get("type") or "") not in {
                        "set_input_settings",
                        "source_filter_settings",
                    }:
                        continue
                    params = action.get("params")
                    if isinstance(params, dict):
                        params["settings"] = {}
                    # Arbitrary OBS settings may contain URLs, cookies, API
                    # tokens or credentials. A shareable export must never
                    # replay the redacted placeholder as an intentional write.
                    action["enabled"] = False
    return result


def export_config(
    data: Mapping[str, Any],
    destination: str | Path,
    *,
    include_secrets: bool = True,
) -> Path:
    payload = _validated_payload(data)
    if not include_secrets:
        payload = _redact_secrets(payload)
    target = Path(destination)
    _atomic_write_json(target, payload)
    return target


def import_config(source: str | Path) -> dict[str, Any]:
    return load_config(Path(source))


def latest_valid_backup() -> tuple[dict[str, Any], Path] | None:
    directory = backups_dir()
    if not directory.exists():
        return None
    candidates = sorted(
        directory.glob("config-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            return load_config(candidate), candidate
        except ConfigError:
            continue
    return None


def push_layout_history(data: dict[str, Any], name: str, profile: Mapping[str, Any], *, limit: int = 10) -> None:
    history = data.setdefault("layout_history", {})
    entries = history.setdefault(str(name), [])
    if not isinstance(entries, list):
        entries = []
        history[str(name)] = entries
    entries.append(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "profile": copy.deepcopy(dict(profile)),
        }
    )
    del entries[:-max(1, int(limit))]


def pop_layout_history(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    history = data.get("layout_history", {})
    if not isinstance(history, dict):
        return None
    entries = history.get(str(name))
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries.pop()
    profile = entry.get("profile") if isinstance(entry, Mapping) else None
    return copy.deepcopy(dict(profile)) if isinstance(profile, Mapping) else None


def _valid_number(value: Any, *, positive: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(parsed):
        return False
    return parsed > 0 if positive else True


def _valid_int(
    value: Any,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if minimum is not None and parsed < minimum:
        return False
    if maximum is not None and parsed > maximum:
        return False
    return True


def _validate_conditions(raw: Any, prefix: str, errors: list[str]) -> None:
    if raw in (None, {}):
        return
    if not isinstance(raw, Mapping):
        errors.append(f"{prefix} doit être un objet")
        return
    allowed = {"streaming", "recording", "program_scene", "obs_enabled"}
    unknown = set(raw) - allowed
    if unknown:
        errors.append(f"{prefix} contient des conditions inconnues : {', '.join(sorted(unknown))}")
    for key in ("streaming", "recording", "obs_enabled"):
        if key in raw and not isinstance(raw.get(key), bool):
            errors.append(f"{prefix}.{key} doit être booléen")
    if "program_scene" in raw and not isinstance(raw.get("program_scene"), str):
        errors.append(f"{prefix}.program_scene doit être une chaîne")


def _check_inheritance_cycles(mapping: Mapping[str, Any], prefix: str, errors: list[str]) -> None:
    def visit(name: str, stack: tuple[str, ...]) -> None:
        raw = mapping.get(name)
        if not isinstance(raw, Mapping):
            return
        parent = str(raw.get("extends") or "").strip()
        if not parent:
            return
        if parent not in mapping:
            errors.append(f"{prefix}.{name}.extends référence un profil inexistant : {parent}")
            return
        if parent in stack or parent == name:
            errors.append(f"Héritage circulaire dans {prefix} : {' -> '.join((*stack, name, parent))}")
            return
        visit(parent, (*stack, name))

    for name in mapping:
        visit(str(name), ())


def config_revision(data: Mapping[str, Any]) -> str:
    """Stable short revision for draft/saved/applied UI state."""
    payload = json.dumps(
        dict(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def validate_config(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        schema = int(data.get("schema_version", 0))
    except (TypeError, ValueError):
        schema = 0
    if schema != SCHEMA_VERSION:
        errors.append(f"schema_version doit valoir {SCHEMA_VERSION}")

    router = data.get("router")
    if not isinstance(router, Mapping):
        errors.append("router doit être un objet")
        router = {}
    else:
        for key in ("poll_ms", "debounce_ms", "fallback_debounce_ms"):
            if not _valid_int(router.get(key, 0), minimum=0):
                errors.append(f"router.{key} doit être un entier >= 0")

    fallback = router.get("fallback_state") if isinstance(router, Mapping) else None
    if not isinstance(fallback, Mapping):
        errors.append("router.fallback_state doit être un objet")

    rules = data.get("rules")
    if not isinstance(rules, list):
        errors.append("rules doit être une liste")
        rules = []
    seen_names: set[str] = set()
    for index, raw in enumerate(rules):
        prefix = f"rules[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{prefix} doit être un objet")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            errors.append(f"{prefix}.name est requis")
        elif name.casefold() in seen_names:
            errors.append(f"Nom de règle dupliqué : {name}")
        else:
            seen_names.add(name.casefold())
        behavior = str(raw.get("behavior", "match")).casefold()
        if behavior not in {"match", "ignore"}:
            errors.append(f"{prefix}.behavior doit être match ou ignore")
        if not any(str(raw.get(key) or "").strip() for key in ("exe", "path", "title_regex")):
            errors.append(f"{prefix} doit définir exe, path ou title_regex")
        title_regex = str(raw.get("title_regex") or "").strip()
        if title_regex:
            try:
                re.compile(title_regex)
            except re.error as exc:
                errors.append(f"{prefix}.title_regex est invalide : {exc}")
        if behavior == "match" and not isinstance(raw.get("state"), Mapping):
            errors.append(f"{prefix}.state est requis pour une règle match")
        if not _valid_int(raw.get("apply_delay_ms", 0), minimum=0):
            errors.append(f"{prefix}.apply_delay_ms doit être un entier >= 0")
        _validate_conditions(raw.get("conditions", {}), f"{prefix}.conditions", errors)

    obs = data.get("obs")
    if not isinstance(obs, Mapping):
        errors.append("obs doit être un objet")
    else:
        host = str(obs.get("host") or "127.0.0.1").strip().casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            errors.append("obs.host doit rester local (127.0.0.1, localhost ou ::1)")
        if not _valid_int(obs.get("port", 4455), minimum=1, maximum=65535):
            errors.append("obs.port doit être compris entre 1 et 65535")
        if not _valid_number(obs.get("timeout_seconds", 2.0), positive=True):
            errors.append("obs.timeout_seconds doit être un nombre fini > 0")
        if not _valid_number(obs.get("reconnect_seconds", 3.0)) or float(obs.get("reconnect_seconds", 3.0)) < 0:
            errors.append("obs.reconnect_seconds doit être un nombre fini >= 0")

    api = data.get("api", {})
    if not isinstance(api, Mapping):
        errors.append("api doit être un objet")
    else:
        if str(api.get("host") or "127.0.0.1").strip() not in {"127.0.0.1", "localhost", "::1"}:
            errors.append("api.host doit rester local")
        if not _valid_int(api.get("port", 8765), minimum=1, maximum=65535):
            errors.append("api.port doit être compris entre 1 et 65535")

    activation_policies = data.get("activation_policies", {})
    if not isinstance(activation_policies, Mapping):
        errors.append("activation_policies doit être un objet")
        activation_policies = {}
    for policy_name, policy in activation_policies.items():
        prefix = f"activation_policies.{policy_name}"
        if not str(policy_name).strip() or not isinstance(policy, Mapping):
            errors.append(f"{prefix} doit être un objet nommé")
            continue
        policy_type = str(policy.get("type") or "random").casefold()
        if policy_type != "random":
            errors.append(f"{prefix}.type inconnu : {policy_type}")
        active_when = str(policy.get("active_when") or "module_in_program_scene").casefold()
        if active_when not in {"always", "streaming", "module_in_program_scene"}:
            errors.append(f"{prefix}.active_when inconnu : {active_when}")
        try:
            chance = float(policy.get("chance", 0.01))
            if not math.isfinite(chance) or not 0.0 <= chance <= 1.0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{prefix}.chance doit être compris entre 0 et 1")
        for key, default, allow_zero in (
            ("interval_seconds", 60.0, False),
            ("cooldown_seconds", 600.0, True),
            ("default_duration_seconds", 10.0, False),
        ):
            try:
                value = float(policy.get(key, default))
                valid = math.isfinite(value) and (
                    value >= 0.0 if allow_zero else value > 0.0
                )
                if not valid:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                qualifier = ">= 0" if allow_zero else "> 0"
                errors.append(f"{prefix}.{key} doit être {qualifier}")
        targets = policy.get("targets", [])
        if not isinstance(targets, list):
            errors.append(f"{prefix}.targets doit être une liste")
            continue
        identities: dict[tuple[str, str, str], int] = {}
        target_nodes: list[tuple[int, tuple[str, ...]]] = []
        for target_index, target in enumerate(targets):
            tprefix = f"{prefix}.targets[{target_index}]"
            if not isinstance(target, Mapping):
                errors.append(f"{tprefix} doit être un objet")
                continue
            if not str(target.get("container") or "").strip():
                errors.append(f"{tprefix}.container est requis")
            source = str(target.get("source") or "").strip()
            container = str(target.get("container") or "").strip()
            container_kind = str(target.get("container_kind") or "scene").strip() or "scene"
            if not source:
                errors.append(f"{tprefix}.source est requis")
            if container_kind not in {"scene", "group"}:
                errors.append(f"{tprefix}.container_kind doit être scene ou group")
            if container and source:
                identity = (container, container_kind, source)
                previous = identities.get(identity)
                if previous is not None:
                    errors.append(
                        f"{tprefix} duplique exactement {prefix}.targets[{previous}]"
                    )
                else:
                    identities[identity] = target_index
                path_raw = target.get("path")
                path = tuple(
                    str(item).strip()
                    for item in path_raw
                    if str(item).strip()
                ) if isinstance(path_raw, (list, tuple)) else ()
                if path:
                    target_nodes.append((target_index, (*path, source)))
            try:
                weight = float(target.get("weight", 1.0))
                if not math.isfinite(weight) or weight < 0.0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{tprefix}.weight doit être >= 0")
            if target.get("duration_seconds") not in (None, ""):
                try:
                    duration = float(target.get("duration_seconds"))
                    if not math.isfinite(duration) or duration <= 0.0:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"{tprefix}.duration_seconds doit être > 0")

        for left_pos, (left_index, left_path) in enumerate(target_nodes):
            for right_index, right_path in target_nodes[left_pos + 1 :]:
                shortest = min(len(left_path), len(right_path))
                if left_path[:shortest] != right_path[:shortest]:
                    continue
                if len(left_path) == len(right_path):
                    continue
                parent_index, child_index = (
                    (left_index, right_index)
                    if len(left_path) < len(right_path)
                    else (right_index, left_index)
                )
                errors.append(
                    f"{prefix}.targets[{parent_index}] et targets[{child_index}] "
                    "sont en conflit ancêtre/descendant"
                )

    activation_owners: dict[tuple[str, str, str], str] = {}
    if isinstance(activation_policies, Mapping):
        for policy_name, policy in activation_policies.items():
            if not isinstance(policy, Mapping):
                continue
            targets = policy.get("targets", [])
            if not isinstance(targets, list):
                continue
            for target_index, target in enumerate(targets):
                if not isinstance(target, Mapping):
                    continue
                container = str(target.get("container") or "").strip()
                source = str(target.get("source") or "").strip()
                kind = str(target.get("container_kind") or "scene").strip() or "scene"
                if not container or not source:
                    continue
                identity = (container, kind, source)
                previous = activation_owners.get(identity)
                if previous is not None and previous != str(policy_name):
                    errors.append(
                        f"activation_policies.{policy_name}.targets[{target_index}] partage la cible "
                        f"{kind}:{container}/{source} avec activation_policies.{previous}"
                    )
                else:
                    activation_owners[identity] = str(policy_name)

    profiles = data.get("profiles")
    if not isinstance(profiles, Mapping):
        errors.append("profiles doit être un objet")
        profiles = {}
    for domain in PROFILE_DOMAINS:
        values = profiles.get(domain, {}) if isinstance(profiles, Mapping) else {}
        if not isinstance(values, Mapping):
            errors.append(f"profiles.{domain} doit être un objet")
            continue
        _check_inheritance_cycles(values, f"profiles.{domain}", errors)
        for name, profile in values.items():
            if not str(name).strip():
                errors.append(f"profiles.{domain} contient un nom vide")
            if not isinstance(profile, Mapping):
                errors.append(f"profiles.{domain}.{name} doit être un objet")
                continue
            _validate_conditions(profile.get("conditions", {}), f"profiles.{domain}.{name}.conditions", errors)
            actions = profile.get("actions", [])
            if not isinstance(actions, list):
                errors.append(f"profiles.{domain}.{name}.actions doit être une liste")
                continue
            for action_index, action in enumerate(actions):
                if not isinstance(action, Mapping):
                    errors.append(f"profiles.{domain}.{name}.actions[{action_index}] doit être un objet")
                    continue
                action_type = str(action.get("type") or "").strip()
                if not action_type:
                    errors.append(f"profiles.{domain}.{name}.actions[{action_index}].type est requis")
                elif action_type not in SUPPORTED_ACTION_TYPES:
                    errors.append(f"profiles.{domain}.{name}.actions[{action_index}].type inconnu : {action_type}")
                    continue
                aprefix = f"profiles.{domain}.{name}.actions[{action_index}]"
                params = action.get("params", {})
                if not isinstance(params, Mapping):
                    errors.append(f"{aprefix}.params doit être un objet")
                    continue
                if "enabled" in action and not isinstance(action.get("enabled"), bool):
                    errors.append(f"{aprefix}.enabled doit être booléen")

                def required_text(
                    key: str,
                    *,
                    params: Mapping[str, Any] = params,
                    aprefix: str = aprefix,
                ) -> None:
                    if not str(params.get(key) or "").strip():
                        errors.append(f"{aprefix}.params.{key} est requis")

                if action_type == "set_program_scene":
                    required_text("scene")
                elif action_type == "scene_item_enabled":
                    required_text("scene")
                    required_text("source")
                    if "enabled" in params and not isinstance(params.get("enabled"), bool):
                        errors.append(f"{aprefix}.params.enabled doit être booléen")
                elif action_type == "source_filter_enabled":
                    required_text("source")
                    required_text("filter")
                    if "enabled" in params and not isinstance(params.get("enabled"), bool):
                        errors.append(f"{aprefix}.params.enabled doit être booléen")
                elif action_type == "source_filter_settings":
                    required_text("source")
                    required_text("filter")
                    if not isinstance(params.get("settings"), Mapping):
                        errors.append(f"{aprefix}.params.settings doit être un objet")
                    if "overlay" in params and not isinstance(params.get("overlay"), bool):
                        errors.append(f"{aprefix}.params.overlay doit être booléen")
                elif action_type == "input_mute":
                    required_text("input")
                    if "muted" in params and not isinstance(params.get("muted"), bool):
                        errors.append(f"{aprefix}.params.muted doit être booléen")
                elif action_type == "input_volume_db":
                    required_text("input")
                    if "volume_db" not in params:
                        errors.append(
                            f"{aprefix}.params.volume_db est obligatoire"
                        )
                    else:
                        raw_volume = params.get("volume_db")
                        if (
                            isinstance(raw_volume, bool)
                            or not isinstance(raw_volume, (int, float))
                            or not math.isfinite(float(raw_volume))
                        ):
                            errors.append(
                                f"{aprefix}.params.volume_db doit être un nombre fini"
                            )
                        elif not -100.0 <= float(raw_volume) <= 26.0:
                            errors.append(
                                f"{aprefix}.params.volume_db doit être compris entre -100 et 26 dB"
                            )
                elif action_type == "set_input_settings":
                    required_text("input")
                    if not isinstance(params.get("settings"), Mapping):
                        errors.append(f"{aprefix}.params.settings doit être un objet")
                    if "overlay" in params and not isinstance(params.get("overlay"), bool):
                        errors.append(f"{aprefix}.params.overlay doit être booléen")
                elif action_type == "app_audio_output":
                    required_text("device")
                    required_text("process")
                    roles = str(params.get("roles") or "all").strip().casefold()
                    if roles not in {"0", "1", "2", "all"}:
                        errors.append(
                            f"{aprefix}.params.roles doit être 0, 1, 2 ou all"
                        )
                elif action_type == "windows_hdr":
                    if "enabled" not in params or not isinstance(params.get("enabled"), bool):
                        errors.append(f"{aprefix}.params.enabled doit être booléen")
                    display = str(params.get("display") or "primary").strip().casefold()
                    if display not in {"primary", "all"}:
                        errors.append(
                            f"{aprefix}.params.display doit être primary ou all"
                        )

    host_control = data.get("host_control", {})
    if not isinstance(host_control, Mapping):
        errors.append("host_control doit être un objet")
    else:
        path = host_control.get("soundvolumeview_path", "")
        if not isinstance(path, str):
            errors.append("host_control.soundvolumeview_path doit être une chaîne")
        timeout = host_control.get("audio_timeout_seconds", 5.0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            errors.append(
                "host_control.audio_timeout_seconds doit être un nombre fini > 0"
            )

    layout_profiles = data.get("layout_profiles")
    if not isinstance(layout_profiles, Mapping):
        errors.append("layout_profiles doit être un objet")
        layout_profiles = {}
    _check_inheritance_cycles(layout_profiles, "layout_profiles", errors)
    for profile_name, profile in layout_profiles.items():
        prefix = f"layout_profiles.{profile_name}"
        if not str(profile_name).strip():
            errors.append("layout_profiles contient un nom vide")
        if not isinstance(profile, Mapping):
            errors.append(f"{prefix} doit être un objet")
            continue
        _validate_conditions(profile.get("conditions", {}), f"{prefix}.conditions", errors)
        transition = profile.get("transition", {})
        if not isinstance(transition, Mapping):
            errors.append(f"{prefix}.transition doit être un objet")
        else:
            mode = str(transition.get("mode") or "instant")
            if mode not in LAYOUT_TRANSITIONS:
                errors.append(f"{prefix}.transition.mode inconnu : {mode}")
            if not _valid_int(transition.get("duration_ms", 0), minimum=0):
                errors.append(f"{prefix}.transition.duration_ms doit être un entier >= 0")
            if not _valid_int(transition.get("steps", 8), minimum=1, maximum=60):
                errors.append(f"{prefix}.transition.steps doit être compris entre 1 et 60")
        modules = profile.get("modules", {})
        if not isinstance(modules, Mapping):
            errors.append(f"{prefix}.modules doit être un objet")
            continue
        for module_name, module in modules.items():
            mprefix = f"{prefix}.modules.{module_name}"
            if not str(module_name).strip() or not isinstance(module, Mapping):
                errors.append(f"{mprefix} doit être un objet nommé")
                continue
            for box_key in ("base_bounds", "geometry"):
                box = module.get(box_key)
                if not isinstance(box, Mapping):
                    errors.append(f"{mprefix}.{box_key} doit être un objet")
                    continue
                for key in ("x", "y"):
                    if not _valid_number(box.get(key, 0)):
                        errors.append(f"{mprefix}.{box_key}.{key} doit être numérique")
                for key in ("width", "height"):
                    if not _valid_number(box.get(key, 0), positive=True):
                        errors.append(f"{mprefix}.{box_key}.{key} doit être > 0")
            anchor = str(module.get("anchor") or "top_left")
            if anchor not in LAYOUT_ANCHORS:
                errors.append(f"{mprefix}.anchor inconnu : {anchor}")
            elements = module.get("elements", [])
            if not isinstance(elements, list):
                errors.append(f"{mprefix}.elements doit être une liste")
                continue
            for element_index, element in enumerate(elements):
                eprefix = f"{mprefix}.elements[{element_index}]"
                if not isinstance(element, Mapping):
                    errors.append(f"{eprefix} doit être un objet")
                    continue
                if not str(element.get("source") or "").strip():
                    errors.append(f"{eprefix}.source est requis")
                transform = element.get("transform")
                if not isinstance(transform, Mapping):
                    errors.append(f"{eprefix}.transform doit être un objet")
                    continue
                for key in (
                    "positionX", "positionY", "scaleX", "scaleY", "rotation",
                    "boundsWidth", "boundsHeight", "width", "height",
                    "sourceWidth", "sourceHeight",
                ):
                    if key in transform and not _valid_number(transform.get(key)):
                        errors.append(f"{eprefix}.transform.{key} doit être un nombre fini")
                for key in ("included", "enabled", "follow_position", "follow_size", "follow_visibility", "locked"):
                    if key in element and not isinstance(element.get(key), bool):
                        errors.append(f"{eprefix}.{key} doit être booléen")

    profile_keys = {
        "game": "Game",
        "overlay": "OverlayProfile",
        "capture": "CaptureProfile",
        "audio": "AudioProfile",
        "layout": "LayoutProfile",
    }
    profile_sets = {
        domain: set((profiles.get(domain) or {}).keys())
        if isinstance(profiles.get(domain, {}), Mapping)
        else set()
        for domain in PROFILE_DOMAINS
    }
    profile_sets["layout"] = set(layout_profiles.keys()) if isinstance(layout_profiles, Mapping) else set()

    def check_state_refs(state, where: str) -> None:
        if not isinstance(state, Mapping):
            return
        parsed = StreamState.from_mapping(state)
        for domain, key in profile_keys.items():
            value = parsed.profile_name(domain)
            if value in profile_sets[domain]:
                continue
            # OBSDispatcher already treats a completely unconfigured domain
            # targeting its canonical default as intentionally unmanaged.
            # Configuration validation must accept the same state or an
            # otherwise valid read-only/default domain becomes impossible to
            # represent (notably the executor lab with no LayoutProfile).
            if (
                not profile_sets[domain]
                and value == DEFAULT_PROFILE_NAMES[domain]
            ):
                continue
            errors.append(f"{where}.{key} référence un profil inexistant : {value}")

    check_state_refs(fallback, "router.fallback_state")
    for index, raw in enumerate(rules):
        if not isinstance(raw, Mapping) or str(raw.get("behavior", "match")).casefold() != "match":
            continue
        check_state_refs(raw.get("state"), f"rules[{index}].state")

    return errors


def release_runtime_visibility_ownership(
    data: dict[str, Any],
    *,
    container: str,
    source: str,
) -> int:
    """Explicitly return one orphaned visibility marker to LayoutProfile ownership."""
    wanted = (str(container).strip(), str(source).strip())
    if not all(wanted):
        return 0
    changed = 0

    def release_profile(profile: Any) -> None:
        nonlocal changed
        if not isinstance(profile, Mapping):
            return
        modules = profile.get("modules")
        if isinstance(modules, Mapping):
            for module in modules.values():
                if not isinstance(module, Mapping):
                    continue
                elements = module.get("elements")
                if not isinstance(elements, list):
                    continue
                for element in elements:
                    if not isinstance(element, dict):
                        continue
                    identity = (
                        str(element.get("container") or "").strip(),
                        str(element.get("source") or "").strip(),
                    )
                    if identity != wanted:
                        continue
                    if str(element.get("visibility_owner") or "").casefold() == "runtime":
                        element["visibility_owner"] = ""
                        element["follow_visibility"] = True
                        changed += 1
        support = profile.get("support_items")
        if isinstance(support, list):
            for item in support:
                if not isinstance(item, dict):
                    continue
                identity = (
                    str(item.get("container") or "").strip(),
                    str(item.get("source") or "").strip(),
                )
                if identity == wanted and str(item.get("visibility_owner") or "").casefold() == "runtime":
                    item["visibility_owner"] = ""
                    changed += 1

    layouts = data.get("layout_profiles")
    if isinstance(layouts, Mapping):
        for profile in layouts.values():
            release_profile(profile)
    history = data.get("layout_history")
    if isinstance(history, Mapping):
        for entries in history.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, Mapping):
                    release_profile(entry.get("profile"))
    return changed


def build_activation_policies(data: Mapping[str, Any]) -> dict[str, TriggerPolicyConfig]:
    raw = data.get("activation_policies", {})
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): TriggerPolicyConfig.from_mapping(str(name), policy)
        for name, policy in raw.items()
        if isinstance(policy, Mapping)
    }


def build_ruleset(data: Mapping[str, Any]) -> tuple[RuleSet, int, int, int]:
    router = data.get("router", {})
    fallback = StreamState.from_mapping(router.get("fallback_state", {}))
    rules: list[AppRule] = []
    for raw in data.get("rules", []):
        if not isinstance(raw, Mapping):
            continue
        behavior_text = str(raw.get("behavior", "match")).casefold()
        behavior = ResolutionKind.IGNORE if behavior_text == "ignore" else ResolutionKind.MATCH
        conditions = raw.get("conditions")
        rules.append(
            AppRule(
                name=str(raw.get("name") or "Unnamed rule"),
                priority=int(raw.get("priority", 0)),
                exe=str(raw.get("exe") or ""),
                path=str(raw.get("path") or ""),
                title_regex=str(raw.get("title_regex") or ""),
                enabled=bool(raw.get("enabled", True)),
                behavior=behavior,
                state=(
                    StreamState.from_mapping(raw.get("state", {}))
                    if behavior is ResolutionKind.MATCH
                    else None
                ),
                conditions=dict(conditions) if isinstance(conditions, Mapping) else {},
                apply_delay_ms=max(0, int(raw.get("apply_delay_ms", 0))),
            )
        )
    return (
        RuleSet(rules, fallback=fallback),
        int(router.get("poll_ms", 50)),
        int(router.get("debounce_ms", 150)),
        int(router.get("fallback_debounce_ms", 350)),
    )


def build_host_controller(data: Mapping[str, Any]) -> HostControlController:
    raw = data.get("host_control", {})
    host = raw if isinstance(raw, Mapping) else {}
    timeout_raw = host.get("audio_timeout_seconds", 5.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = 5.0
    return HostControlController(
        HostControlConfig(
            soundvolumeview_path=str(host.get("soundvolumeview_path") or ""),
            audio_timeout_seconds=max(0.1, timeout),
        )
    )


def build_obs_config(data: Mapping[str, Any]) -> OBSConnectionConfig:
    obs = data.get("obs", {})
    return OBSConnectionConfig(
        enabled=bool(obs.get("enabled", False)),
        host=str(obs.get("host") or "127.0.0.1"),
        port=int(obs.get("port", 4455)),
        password=str(obs.get("password") or ""),
        timeout_seconds=float(obs.get("timeout_seconds", 2.0)),
        reconnect_seconds=float(obs.get("reconnect_seconds", 3.0)),
    )


def build_profiles(data: Mapping[str, Any]):
    profiles = data.get("profiles", {})
    return profile_map_from_raw(profiles if isinstance(profiles, Mapping) else {})


def build_layout_profiles(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = data.get("layout_profiles", {})
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): copy.deepcopy(dict(profile))
        for name, profile in raw.items()
        if isinstance(profile, Mapping)
    }
