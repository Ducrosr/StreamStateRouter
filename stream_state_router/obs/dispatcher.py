from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Iterable, Mapping

from ..router.engine import StateChange
from ..router.models import StreamState
from .client import OBSClientManager
from .layouts import OBSLayoutManager, resolve_layout_profile
from .models import OBSAction, OBSProfile


ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")
PROFILE_DOMAINS = ACTION_PROFILE_DOMAINS
STATE_DOMAINS = ACTION_PROFILE_DOMAINS + ("layout",)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    executed: int
    skipped: int
    changed_domains: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class OBSDispatcher:
    """Translate logical stream states into explicit obs-websocket actions.

    It owns no game-detection logic. Logical profiles can inherit from a base
    profile and may declare optional OBS-side conditions.
    """

    def __init__(
        self,
        client: OBSClientManager,
        profiles: Mapping[str, Mapping[str, OBSProfile]] | None = None,
        layout_profiles: Mapping[str, Mapping[str, object]] | None = None,
    ):
        self.client = client
        self._profiles = {domain: dict(values) for domain, values in (profiles or {}).items()}
        self._layout_profiles = {
            str(name): dict(value) for name, value in (layout_profiles or {}).items()
        }
        self._layout_manager = OBSLayoutManager(client)
        self._last_state: StreamState | None = None
        self._scene_item_cache: dict[tuple[str, str], int] = {}
        self._context_cache: tuple[float, dict[str, Any]] | None = None

    @property
    def layout_manager(self) -> OBSLayoutManager:
        return self._layout_manager

    def configure_profiles(
        self,
        profiles: Mapping[str, Mapping[str, OBSProfile]],
    ) -> None:
        self._profiles = {domain: dict(values) for domain, values in profiles.items()}
        self._last_state = None
        self._scene_item_cache.clear()
        self._layout_manager.reset_cache()
        self._context_cache = None

    def configure_layouts(self, profiles: Mapping[str, Mapping[str, object]]) -> None:
        self._layout_profiles = {str(name): dict(value) for name, value in profiles.items()}
        self._layout_manager.reset_cache()
        self._last_state = None
        self._context_cache = None

    def reset(self) -> None:
        self._last_state = None
        self._scene_item_cache.clear()
        self._layout_manager.reset_cache()
        self._context_cache = None

    def obs_context(self) -> dict[str, Any]:
        """Return a small current OBS context for conditional rules/profiles.

        Context is cached briefly so a foreground polling loop never turns into
        a high-frequency obs-websocket polling loop.
        """
        now = time.monotonic()
        if self._context_cache is not None and now - self._context_cache[0] < 0.5:
            return dict(self._context_cache[1])
        if not self.client.config.enabled:
            return {
                "obs_enabled": False,
                "streaming": False,
                "recording": False,
                "program_scene": "",
            }
        context: dict[str, Any] = {"obs_enabled": True}
        try:
            stream = self.client.send("GetStreamStatus")
            context["streaming"] = bool(stream.get("outputActive", False))
        except Exception:
            context["streaming"] = None
        try:
            record = self.client.send("GetRecordStatus")
            context["recording"] = bool(record.get("outputActive", False))
        except Exception:
            context["recording"] = None
        try:
            scene = self.client.send("GetCurrentProgramScene")
            context["program_scene"] = str(scene.get("currentProgramSceneName") or "")
        except Exception:
            context["program_scene"] = ""
        self._context_cache = (now, dict(context))
        return context

    def conditions_match(self, conditions: Mapping[str, Any] | None) -> bool:
        if not conditions:
            return True
        context = self.obs_context()
        if "streaming" in conditions:
            wanted = bool(conditions.get("streaming"))
            if context.get("streaming") is None or bool(context.get("streaming")) != wanted:
                return False
        if "recording" in conditions:
            wanted = bool(conditions.get("recording"))
            if context.get("recording") is None or bool(context.get("recording")) != wanted:
                return False
        if str(conditions.get("program_scene") or "").strip():
            if str(context.get("program_scene") or "") != str(conditions.get("program_scene") or ""):
                return False
        if bool(conditions.get("obs_enabled", False)) and not bool(context.get("obs_enabled")):
            return False
        return True

    def dispatch_change(self, change: StateChange) -> DispatchResult:
        return self.dispatch_state(change.current, previous=change.previous)

    def dispatch_state(
        self,
        state: StreamState,
        *,
        previous: StreamState | None = None,
        force: bool = False,
    ) -> DispatchResult:
        baseline = self._last_state if previous is None else previous
        changed = []
        for domain in STATE_DOMAINS:
            if force or baseline is None or state.profile_name(domain) != baseline.profile_name(domain):
                changed.append(domain)

        executed = 0
        skipped = 0
        warnings: list[str] = []
        for domain in changed:
            profile_name = state.profile_name(domain)
            if domain == "layout":
                if profile_name not in self._layout_profiles:
                    skipped += 1
                    continue
                try:
                    layout = resolve_layout_profile(profile_name, self._layout_profiles)
                except Exception as exc:
                    skipped += 1
                    warnings.append(str(exc))
                    continue
                conditions = layout.get("conditions")
                if isinstance(conditions, Mapping) and not self.conditions_match(conditions):
                    skipped += 1
                    continue
                result = self._layout_manager.apply_profile(layout)
                executed += result.elements_applied
                skipped += result.elements_skipped
                warnings.extend(result.warnings)
                continue

            try:
                profile = self._resolve_action_profile(domain, profile_name)
            except Exception as exc:
                skipped += 1
                warnings.append(str(exc))
                continue
            if profile is None:
                skipped += 1
                continue
            if not self.conditions_match(profile.conditions):
                skipped += len(profile.actions) or 1
                continue
            for action in profile.actions:
                if not action.enabled:
                    skipped += 1
                    continue
                self.execute_action(action)
                executed += 1

        self._last_state = state
        return DispatchResult(executed, skipped, tuple(changed), tuple(warnings))

    def execute_profile(self, domain: str, profile_name: str) -> DispatchResult:
        if domain not in ACTION_PROFILE_DOMAINS:
            raise ValueError(f"Domaine inconnu : {domain}")
        profile = self._resolve_action_profile(domain, profile_name)
        if profile is None:
            raise ValueError(f"Profil introuvable : {domain}/{profile_name}")
        if not self.conditions_match(profile.conditions):
            return DispatchResult(0, len(profile.actions) or 1, (domain,))
        executed = 0
        skipped = 0
        for action in profile.actions:
            if not action.enabled:
                skipped += 1
                continue
            self.execute_action(action)
            executed += 1
        return DispatchResult(executed, skipped, (domain,))

    def execute_layout_profile(self, profile_name: str, *, preview: bool = False) -> DispatchResult:
        if profile_name not in self._layout_profiles:
            raise ValueError(f"Layout introuvable : {profile_name}")
        profile = resolve_layout_profile(profile_name, self._layout_profiles)
        if isinstance(profile.get("conditions"), Mapping) and not self.conditions_match(profile["conditions"]):
            return DispatchResult(0, 1, ("layout",))
        result = (
            self._layout_manager.preview_profile(profile)
            if preview
            else self._layout_manager.apply_profile(profile)
        )
        return DispatchResult(
            result.elements_applied,
            result.elements_skipped,
            ("layout",),
            result.warnings,
        )

    def _resolve_action_profile(self, domain: str, name: str) -> OBSProfile | None:
        domain_profiles = self._profiles.get(domain, {})
        if name not in domain_profiles:
            return None
        return self._resolve_action_profile_inner(domain_profiles, name, ())

    def _resolve_action_profile_inner(
        self,
        profiles: Mapping[str, OBSProfile],
        name: str,
        stack: tuple[str, ...],
    ) -> OBSProfile:
        if name in stack:
            raise ValueError("Héritage circulaire de profil OBS : " + " -> ".join((*stack, name)))
        profile = profiles[name]
        if not profile.extends:
            return profile
        if profile.extends not in profiles:
            raise ValueError(f"Profil parent introuvable : {profile.extends}")
        parent = self._resolve_action_profile_inner(profiles, profile.extends, (*stack, name))
        conditions = dict(parent.conditions)
        conditions.update(dict(profile.conditions))
        return OBSProfile(
            name=profile.name,
            actions=(*parent.actions, *profile.actions),
            extends=profile.extends,
            conditions=conditions,
        )

    def execute_action(self, action: OBSAction) -> None:
        kind = action.type.strip().casefold()
        p = dict(action.params)
        if kind == "set_program_scene":
            self.client.send(
                "SetCurrentProgramScene",
                {"sceneName": self._need(p, "scene")},
            )
            return
        if kind == "scene_item_enabled":
            scene = self._need(p, "scene")
            source = self._need(p, "source")
            item_id = self._scene_item_id(scene, source)
            self.client.send(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene,
                    "sceneItemId": item_id,
                    "sceneItemEnabled": bool(p.get("enabled", True)),
                },
            )
            return
        if kind == "source_filter_enabled":
            self.client.send(
                "SetSourceFilterEnabled",
                {
                    "sourceName": self._need(p, "source"),
                    "filterName": self._need(p, "filter"),
                    "filterEnabled": bool(p.get("enabled", True)),
                },
            )
            return
        if kind == "input_mute":
            self.client.send(
                "SetInputMute",
                {
                    "inputName": self._need(p, "input"),
                    "inputMuted": bool(p.get("muted", True)),
                },
            )
            return
        if kind == "input_volume_db":
            self.client.send(
                "SetInputVolume",
                {
                    "inputName": self._need(p, "input"),
                    "inputVolumeDb": float(p.get("volume_db", 0.0)),
                },
            )
            return
        if kind == "set_input_settings":
            settings = p.get("settings")
            if not isinstance(settings, Mapping):
                raise ValueError("set_input_settings requiert params.settings")
            self.client.send(
                "SetInputSettings",
                {
                    "inputName": self._need(p, "input"),
                    "inputSettings": dict(settings),
                    "overlay": bool(p.get("overlay", True)),
                },
            )
            return
        raise ValueError(f"Type d'action OBS inconnu : {action.type}")

    def _scene_item_id(self, scene: str, source: str) -> int:
        key = (scene, source)
        if key in self._scene_item_cache:
            return self._scene_item_cache[key]
        response = self.client.send(
            "GetSceneItemId",
            {"sceneName": scene, "sourceName": source},
        )
        item_id = int(response.get("sceneItemId") or 0)
        if not item_id:
            raise RuntimeError(f"Source '{source}' introuvable dans la scène '{scene}'")
        self._scene_item_cache[key] = item_id
        return item_id

    @staticmethod
    def _need(params: Mapping[str, object], key: str) -> str:
        value = str(params.get(key) or "").strip()
        if not value:
            raise ValueError(f"Paramètre OBS manquant : {key}")
        return value


def profile_map_from_raw(
    raw: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, dict[str, OBSProfile]]:
    result: dict[str, dict[str, OBSProfile]] = {domain: {} for domain in ACTION_PROFILE_DOMAINS}
    for domain, profiles in raw.items():
        if domain not in ACTION_PROFILE_DOMAINS or not isinstance(profiles, Mapping):
            continue
        for name, profile_raw in profiles.items():
            if not isinstance(profile_raw, Mapping):
                continue
            actions_raw = profile_raw.get("actions", [])
            actions = tuple(
                OBSAction.from_mapping(item)
                for item in actions_raw
                if isinstance(item, Mapping)
            )
            conditions = profile_raw.get("conditions")
            result[domain][str(name)] = OBSProfile(
                str(name),
                actions,
                str(profile_raw.get("extends") or ""),
                dict(conditions) if isinstance(conditions, Mapping) else {},
            )
    return result
