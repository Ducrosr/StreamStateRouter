from __future__ import annotations

from typing import Any, Mapping


def wire_windows_hdr_capture_profiles(
    config: dict[str, Any],
    *,
    hdr_profile: str = "HDR",
    sdr_profile: str = "SDR",
    display: str = "primary",
) -> tuple[str, ...]:
    """Wire named capture profiles to explicit Windows HDR on/off actions."""
    profiles = config.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("config.profiles doit être un objet.")
    capture = profiles.setdefault("capture", {})
    if not isinstance(capture, dict):
        raise ValueError("config.profiles.capture doit être un objet.")

    changed: list[str] = []
    for profile_name, enabled in (
        (str(hdr_profile), True),
        (str(sdr_profile), False),
    ):
        profile = capture.get(profile_name)
        if profile is None:
            profile = {
                "actions": [],
                "conditions": {},
                "extends": "",
            }
            capture[profile_name] = profile
        if not isinstance(profile, dict):
            raise ValueError(
                f"CaptureProfile '{profile_name}' doit être un objet."
            )
        actions = profile.setdefault("actions", [])
        if not isinstance(actions, list):
            raise ValueError(
                f"CaptureProfile '{profile_name}'.actions doit être une liste."
            )

        matches = [
            action
            for action in actions
            if isinstance(action, Mapping)
            and str(action.get("type") or "").strip().casefold()
            == "windows_hdr"
        ]
        desired_params = {
            "enabled": enabled,
            "display": str(display),
        }
        if matches:
            if len(matches) > 1:
                raise ValueError(
                    f"CaptureProfile '{profile_name}' contient plusieurs actions "
                    "windows_hdr."
                )
            params = matches[0].get("params")
            current = dict(params) if isinstance(params, Mapping) else {}
            normalized = {
                "enabled": bool(current.get("enabled", False)),
                "display": str(current.get("display") or "primary"),
            }
            if normalized != desired_params:
                raise ValueError(
                    f"CaptureProfile '{profile_name}' contient déjà une action "
                    "windows_hdr contradictoire."
                )
            continue

        actions.append(
            {
                "type": "windows_hdr",
                "name": "Windows HDR ON" if enabled else "Windows HDR OFF",
                "enabled": True,
                "params": desired_params,
            }
        )
        changed.append(profile_name)

    return tuple(changed)

def neutralize_referenced_test_layout_profiles(
    config: dict[str, Any],
    *,
    marker: str = "[Module] TEST SSR",
) -> tuple[str, ...]:
    """Turn referenced legacy test layouts into explicit no-op profiles."""
    layout_profiles = config.get("layout_profiles")
    if not isinstance(layout_profiles, dict):
        raise ValueError("config.layout_profiles doit être un objet.")

    referenced: set[str] = set()
    router = config.get("router")
    if isinstance(router, Mapping):
        fallback = router.get("fallback_state")
        if isinstance(fallback, Mapping):
            name = str(fallback.get("LayoutProfile") or "").strip()
            if name:
                referenced.add(name)

    rules = config.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            state = rule.get("state")
            if not isinstance(state, Mapping):
                continue
            name = str(state.get("LayoutProfile") or "").strip()
            if name:
                referenced.add(name)

    changed: list[str] = []
    wanted_marker = str(marker or "").strip().casefold()
    for name in sorted(referenced, key=str.casefold):
        profile = layout_profiles.get(name)
        if not isinstance(profile, dict):
            continue
        scene = str(profile.get("scene") or "").strip()
        if not wanted_marker or wanted_marker not in scene.casefold():
            continue
        layout_profiles[name] = {
            "scene": "",
            "coordinate_mode": "normalized",
            "modules": {},
            "conditions": {},
            "extends": "",
            "transition": {
                "mode": "instant",
                "duration_ms": 0,
                "steps": 8,
            },
        }
        changed.append(name)

    return tuple(changed)


def set_capture_profile_for_process(
    config: dict[str, Any],
    *,
    process: str,
    capture_profile: str,
) -> tuple[str, ...]:
    """Update matching foreground/background process rules to one CaptureProfile."""
    wanted_process = str(process or "").strip()
    wanted_profile = str(capture_profile or "").strip()
    if not wanted_process or not wanted_profile:
        raise ValueError("process et capture_profile sont requis.")

    profiles = config.get("profiles")
    capture_profiles = (
        profiles.get("capture")
        if isinstance(profiles, Mapping)
        else None
    )
    if not isinstance(capture_profiles, Mapping) or wanted_profile not in capture_profiles:
        raise ValueError(
            f"CaptureProfile '{wanted_profile}' introuvable."
        )

    rules = config.get("rules")
    if not isinstance(rules, list):
        raise ValueError("config.rules doit être une liste.")

    changed: list[str] = []
    wanted_key = wanted_process.casefold()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("behavior", "match")).casefold() != "match":
            continue
        exe = str(rule.get("exe") or "").strip().casefold()
        raw_conditions = rule.get("conditions")
        conditions = (
            dict(raw_conditions)
            if isinstance(raw_conditions, Mapping)
            else {}
        )
        running = str(conditions.get("process_running") or "").strip().casefold()
        if exe != wanted_key and running != wanted_key:
            continue
        state = rule.get("state")
        if not isinstance(state, dict):
            raise ValueError(
                f"Règle '{rule.get('name', '')}' sans état modifiable."
            )
        if str(state.get("CaptureProfile") or "") == wanted_profile:
            continue
        state["CaptureProfile"] = wanted_profile
        changed.append(str(rule.get("name") or wanted_process))

    if not changed:
        matches = [
            rule
            for rule in rules
            if isinstance(rule, Mapping)
            and (
                str(rule.get("exe") or "").strip().casefold() == wanted_key
                or str(
                    (rule.get("conditions") or {}).get("process_running")
                    if isinstance(rule.get("conditions"), Mapping)
                    else ""
                ).strip().casefold() == wanted_key
            )
        ]
        if not matches:
            raise ValueError(
                f"Aucune règle SSR ne cible le processus '{wanted_process}'."
            )
    return tuple(changed)


def set_game_profile_input_setting(
    config: dict[str, Any],
    *,
    game_profile: str,
    input_name: str,
    setting: str,
    value: Any,
) -> int:
    """Replace one imported set_input_settings key in a named Game profile."""
    profiles = config.get("profiles")
    games = profiles.get("game") if isinstance(profiles, Mapping) else None
    if not isinstance(games, Mapping):
        raise ValueError("config.profiles.game doit être un objet.")

    profile = games.get(str(game_profile))
    if not isinstance(profile, dict):
        raise ValueError(f"Game profile '{game_profile}' introuvable.")
    actions = profile.get("actions")
    if not isinstance(actions, list):
        raise ValueError(
            f"Game profile '{game_profile}'.actions doit être une liste."
        )

    wanted_input = str(input_name or "").strip().casefold()
    wanted_setting = str(setting or "").strip()
    matches: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("type") or "").strip() != "set_input_settings":
            continue
        params = action.get("params")
        if not isinstance(params, dict):
            continue
        if str(params.get("input") or "").strip().casefold() != wanted_input:
            continue
        settings = params.get("settings")
        if isinstance(settings, dict) and wanted_setting in settings:
            matches.append(settings)

    if len(matches) != 1:
        raise ValueError(
            f"Réglage '{wanted_setting}' pour input '{input_name}' dans "
            f"Game profile '{game_profile}' : {len(matches)} correspondance(s)."
        )
    matches[0][wanted_setting] = value
    return 1


def set_fallback_capture_profile(
    config: dict[str, Any],
    *,
    capture_profile: str,
) -> str:
    """Set the router fallback CaptureProfile after validating the target."""
    wanted_profile = str(capture_profile or "").strip()
    if not wanted_profile:
        raise ValueError("capture_profile est requis.")

    profiles = config.get("profiles")
    capture_profiles = (
        profiles.get("capture")
        if isinstance(profiles, Mapping)
        else None
    )
    if (
        not isinstance(capture_profiles, Mapping)
        or wanted_profile not in capture_profiles
    ):
        raise ValueError(
            f"CaptureProfile '{wanted_profile}' introuvable."
        )

    router = config.setdefault("router", {})
    if not isinstance(router, dict):
        raise ValueError("config.router doit être un objet.")
    fallback = router.setdefault("fallback_state", {})
    if not isinstance(fallback, dict):
        raise ValueError("config.router.fallback_state doit être un objet.")
    previous = str(fallback.get("CaptureProfile") or "")
    fallback["CaptureProfile"] = wanted_profile
    return previous
