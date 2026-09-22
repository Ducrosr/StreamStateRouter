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
