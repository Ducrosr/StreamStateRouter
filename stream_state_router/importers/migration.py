from __future__ import annotations

from typing import Any, Mapping


def wire_windows_hdr_capture_profiles(
    config: dict[str, Any],
    *,
    hdr_profile: str = "HDR",
    sdr_profile: str = "Default",
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
