from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping

from ..router.engine import StateChange
from ..router.models import StreamState
from .client import OBSClientManager
from .layouts import OBSLayoutManager, resolve_layout_profile
from .models import OBSAction, OBSProfile


ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")
PROFILE_DOMAINS = ACTION_PROFILE_DOMAINS
STATE_DOMAINS = ACTION_PROFILE_DOMAINS + ("layout",)


@dataclass(frozen=True, slots=True)
class DomainDispatchStatus:
    domain: str
    desired_profile: str
    applied_profile: str
    status: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    executed: int
    skipped: int
    changed_domains: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    domain_statuses: tuple[DomainDispatchStatus, ...] = ()


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
        self._desired_state: StreamState | None = None
        self._applied_profiles: dict[str, str] = {}
        self._context_cache: tuple[float, dict[str, Any]] | None = None
        self._cooperative_yield = None

    @property
    def layout_manager(self) -> OBSLayoutManager:
        return self._layout_manager

    def set_cooperative_yield(self, callback) -> None:
        """Install the runtime shutdown checkpoint for long OBS batches."""
        self._cooperative_yield = callback
        self._layout_manager.set_cooperative_yield(callback)

    def _yield_runtime(self) -> None:
        callback = self._cooperative_yield
        if callback is not None:
            callback()

    def configure_profiles(
        self,
        profiles: Mapping[str, Mapping[str, OBSProfile]],
    ) -> None:
        self._profiles = {domain: dict(values) for domain, values in profiles.items()}
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._layout_manager.reset_cache()
        self._context_cache = None

    def configure_layouts(self, profiles: Mapping[str, Mapping[str, object]]) -> None:
        self._layout_profiles = {str(name): dict(value) for name, value in profiles.items()}
        self._layout_manager.reset_cache()
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._context_cache = None

    def reset(self) -> None:
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._layout_manager.reset_cache()
        self._context_cache = None

    def invalidate_applied_state(self) -> None:
        """Forget OBS-side convergence knowledge after reconnect/session changes."""
        self._applied_profiles.clear()
        self._context_cache = None

    @property
    def desired_state(self) -> StreamState | None:
        return self._desired_state

    def applied_profiles(self) -> dict[str, str]:
        return dict(self._applied_profiles)

    def pending_domains(self, state: StreamState | None = None) -> tuple[str, ...]:
        wanted = state or self._desired_state
        if wanted is None:
            return ()
        return tuple(
            domain
            for domain in STATE_DOMAINS
            if self._applied_profiles.get(domain) != wanted.profile_name(domain)
        )

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
        self._yield_runtime()
        try:
            stream = self.client.send("GetStreamStatus")
            context["streaming"] = bool(stream.get("outputActive", False))
        except Exception:
            context["streaming"] = None
        self._yield_runtime()
        try:
            record = self.client.send("GetRecordStatus")
            context["recording"] = bool(record.get("outputActive", False))
        except Exception:
            context["recording"] = None
        self._yield_runtime()
        try:
            scene = self.client.send("GetCurrentProgramScene")
            context["program_scene"] = str(scene.get("currentProgramSceneName") or "")
        except Exception:
            context["program_scene"] = ""
        self._context_cache = (now, dict(context))
        return context

    def cached_obs_context(self) -> dict[str, Any]:
        """Return the last OBS context snapshot without issuing any request."""
        if self._context_cache is not None:
            return dict(self._context_cache[1])
        enabled = bool(getattr(getattr(self.client, "config", None), "enabled", False))
        return {
            "obs_enabled": enabled,
            "streaming": None if enabled else False,
            "recording": None if enabled else False,
            "program_scene": "",
            "snapshot_available": False,
        }

    @staticmethod
    def conditions_match_context(
        conditions: Mapping[str, Any] | None,
        context: Mapping[str, Any],
    ) -> bool:
        if not conditions:
            return True
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

    def conditions_match(self, conditions: Mapping[str, Any] | None) -> bool:
        return self.conditions_match_context(conditions, self.obs_context())

    @staticmethod
    def _describe_action(action: OBSAction) -> dict[str, object]:
        params = dict(action.params)
        kind = action.type.strip().casefold()
        description: dict[str, object] = {
            "type": action.type,
            "enabled": bool(action.enabled),
        }
        if kind == "set_program_scene":
            description["target"] = str(params.get("scene") or "")
        elif kind == "scene_item_enabled":
            description["target"] = (
                f"{params.get('scene', '')}/{params.get('source', '')}"
            )
            description["value"] = bool(params.get("enabled", True))
        elif kind == "source_filter_enabled":
            description["target"] = (
                f"{params.get('source', '')}/{params.get('filter', '')}"
            )
            description["value"] = bool(params.get("enabled", True))
        elif kind == "input_mute":
            description["target"] = str(params.get("input") or "")
            description["value"] = bool(params.get("muted", True))
        elif kind == "input_volume_db":
            description["target"] = str(params.get("input") or "")
            description["value"] = params.get("volume_db", 0.0)
        elif kind == "set_input_settings":
            # Do not expose arbitrary settings content in diagnostics.
            description["target"] = str(params.get("input") or "")
            settings = params.get("settings")
            description["setting_keys"] = sorted(str(key) for key in settings) if isinstance(settings, Mapping) else []
        return description

    def plan_state(
        self,
        state: StreamState,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        """Describe what a dispatch would do without mutating OBS or dispatcher state."""
        frozen_context = dict(context) if context is not None else self.cached_obs_context()
        domains: list[dict[str, object]] = []
        for domain in STATE_DOMAINS:
            desired = state.profile_name(domain)
            applied = self._applied_profiles.get(domain, "")
            row: dict[str, object] = {
                "domain": domain,
                "desired_profile": desired,
                "applied_profile": applied,
                "needs_apply": applied != desired,
                "status": "noop" if applied == desired else "planned",
                "operations": [],
            }
            if domain == "layout":
                if desired not in self._layout_profiles:
                    row.update(status="missing", message="LayoutProfile introuvable")
                    domains.append(row)
                    continue
                try:
                    profile = resolve_layout_profile(desired, self._layout_profiles)
                except Exception as exc:
                    row.update(status="failed", message=str(exc))
                    domains.append(row)
                    continue
                conditions = profile.get("conditions")
                condition_match = not isinstance(conditions, Mapping) or self.conditions_match_context(
                    conditions, frozen_context
                )
                row["condition_match"] = condition_match
                if row["needs_apply"] and not condition_match:
                    row.update(status="blocked", message="conditions OBS non satisfaites")
                modules = profile.get("modules")
                support = profile.get("support_items")
                transition = profile.get("transition")
                row["operations"] = [{
                    "type": "layout",
                    "scene": str(profile.get("scene") or ""),
                    "modules": len(modules) if isinstance(modules, Mapping) else 0,
                    "support_items": len(support) if isinstance(support, list) else 0,
                    "transition": str(transition.get("mode") or "instant") if isinstance(transition, Mapping) else "instant",
                }]
                row["extends"] = str(profile.get("extends") or "")
                domains.append(row)
                continue

            try:
                profile = self._resolve_action_profile(domain, desired)
            except Exception as exc:
                row.update(status="failed", message=str(exc))
                domains.append(row)
                continue
            if profile is None:
                row.update(status="missing", message="Profil OBS introuvable")
                domains.append(row)
                continue
            condition_match = self.conditions_match_context(profile.conditions, frozen_context)
            row["condition_match"] = condition_match
            if row["needs_apply"] and not condition_match:
                row.update(status="blocked", message="conditions OBS non satisfaites")
            row["operations"] = [self._describe_action(action) for action in profile.actions]
            row["extends"] = profile.extends
            domains.append(row)

        return {
            "state": state.as_variables(),
            "context": frozen_context,
            "domains": domains,
        }

    def dispatch_change(self, change: StateChange) -> DispatchResult:
        # change.previous is the router's previous *decision*, not necessarily
        # what OBS actually acknowledged. Always diff against applied state.
        return self.dispatch_state(change.current)

    def dispatch_state(
        self,
        state: StreamState,
        *,
        previous: StreamState | None = None,
        force: bool = False,
    ) -> DispatchResult:
        del previous  # compatibility only: router history is not OBS applied state
        self._desired_state = state
        self._last_state = state
        changed = [
            domain
            for domain in STATE_DOMAINS
            if force or self._applied_profiles.get(domain) != state.profile_name(domain)
        ]

        executed = 0
        skipped = 0
        warnings: list[str] = []
        statuses: list[DomainDispatchStatus] = []

        def status(domain: str, desired: str, value: str, message: str = "") -> None:
            statuses.append(
                DomainDispatchStatus(
                    domain=domain,
                    desired_profile=desired,
                    applied_profile=self._applied_profiles.get(domain, ""),
                    status=value,
                    message=message,
                )
            )

        for domain in changed:
            self._yield_runtime()
            profile_name = state.profile_name(domain)
            if domain == "layout":
                if profile_name not in self._layout_profiles:
                    skipped += 1
                    status(domain, profile_name, "missing", "LayoutProfile introuvable")
                    continue
                try:
                    layout = resolve_layout_profile(profile_name, self._layout_profiles)
                except Exception as exc:
                    skipped += 1
                    warnings.append(str(exc))
                    status(domain, profile_name, "failed", str(exc))
                    continue
                conditions = layout.get("conditions")
                if isinstance(conditions, Mapping) and not self.conditions_match(conditions):
                    skipped += 1
                    status(domain, profile_name, "blocked", "conditions OBS non satisfaites")
                    continue
                try:
                    result = self._layout_manager.apply_profile(layout)
                except Exception as exc:
                    skipped += 1
                    warnings.append(str(exc))
                    status(domain, profile_name, "failed", str(exc))
                    continue
                executed += result.elements_applied
                skipped += result.elements_skipped
                warnings.extend(result.warnings)
                if result.missing_sources or result.warnings:
                    message = "; ".join(
                        [
                            *(f"source manquante: {name}" for name in result.missing_sources),
                            *result.warnings,
                        ]
                    )
                    status(domain, profile_name, "partial", message)
                    continue
                self._applied_profiles[domain] = profile_name
                status(domain, profile_name, "applied")
                continue

            try:
                profile = self._resolve_action_profile(domain, profile_name)
            except Exception as exc:
                skipped += 1
                warnings.append(str(exc))
                status(domain, profile_name, "failed", str(exc))
                continue
            if profile is None:
                skipped += 1
                status(domain, profile_name, "missing", "Profil OBS introuvable")
                continue
            if not self.conditions_match(profile.conditions):
                skipped += len(profile.actions) or 1
                status(domain, profile_name, "blocked", "conditions OBS non satisfaites")
                continue

            domain_executed = 0
            domain_skipped = 0
            failed = ""
            for action in profile.actions:
                self._yield_runtime()
                if not action.enabled:
                    skipped += 1
                    domain_skipped += 1
                    continue
                try:
                    self.execute_action(action)
                except Exception as exc:
                    failed = str(exc)
                    warnings.append(f"{domain}/{profile_name}: {exc}")
                    break
                executed += 1
                domain_executed += 1
            if failed:
                status(domain, profile_name, "failed", failed)
                continue
            self._applied_profiles[domain] = profile_name
            status(domain, profile_name, "applied")

        return DispatchResult(
            executed,
            skipped,
            tuple(changed),
            tuple(warnings),
            tuple(statuses),
        )

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
            self._yield_runtime()
            if not action.enabled:
                skipped += 1
                continue
            self.execute_action(action)
            executed += 1
        return DispatchResult(executed, skipped, (domain,))

    def execute_layout_profile(self, profile_name: str, *, preview: bool = False) -> DispatchResult:
        self._yield_runtime()
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
        self._yield_runtime()
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
        # sceneItemId is ephemeral OBS state. Resolve the logical
        # (scene, source) identity for every visibility operation so an ID
        # reused after an OBS scene edit can never target another item.
        response = self.client.send(
            "GetSceneItemId",
            {"sceneName": scene, "sourceName": source},
        )
        item_id = int(response.get("sceneItemId") or 0)
        if not item_id:
            raise RuntimeError(f"Source '{source}' introuvable dans la scène '{scene}'")
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
