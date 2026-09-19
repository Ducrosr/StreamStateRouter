from __future__ import annotations

import time
from typing import Mapping

from .models import ActivationEvent, TriggerPolicyConfig, TriggerTargetConfig


class OBSActivationController:
    """Translate scheduler events into temporary OBS source visibility."""

    def __init__(
        self,
        dispatcher,
        policies: Mapping[str, TriggerPolicyConfig] | None = None,
        *,
        clock=time.monotonic,
        eligibility_cache_seconds: float = 0.5,
        collection_probe_seconds: float = 1.0,
    ) -> None:
        self.dispatcher = dispatcher
        self.client = dispatcher.client
        self.layout_manager = dispatcher.layout_manager
        self._policies = dict(policies or {})
        self._clock = clock
        self._eligibility_cache_seconds = max(0.05, float(eligibility_cache_seconds))
        self._collection_probe_seconds = max(0.1, float(collection_probe_seconds))
        self._module_presence_cache: dict[tuple[str, str], tuple[float, bool, str]] = {}
        self._last_collection_probe = 0.0
        self._scene_collection: str | None = None
        self._sync_visibility_owners()

    def configure(self, policies: Mapping[str, TriggerPolicyConfig]) -> None:
        self._policies = dict(policies)
        self._sync_visibility_owners()
        self.invalidate_cache()

    def _sync_visibility_owners(self) -> None:
        self.layout_manager.set_runtime_visibility_owners(
            (target.container, target.source)
            for policy in self._policies.values()
            for target in policy.targets
        )

    def invalidate_cache(self) -> None:
        self._module_presence_cache.clear()
        self.layout_manager.reset_cache()

    def eligibility(
        self,
        policy_name: str,
        policy: TriggerPolicyConfig,
    ) -> tuple[bool, str]:
        config = getattr(self.client, "config", None)
        if config is None or not bool(getattr(config, "enabled", False)):
            return False, "pilotage OBS désactivé"
        if not bool(getattr(self.client, "connected", False)):
            return False, "OBS déconnecté"

        mode = str(policy.active_when or "module_in_program_scene").casefold()
        if mode == "always":
            return True, "condition Toujours satisfaite"

        context = self.dispatcher.obs_context()
        if mode == "streaming":
            streaming = bool(context.get("streaming"))
            return (True, "stream actif") if streaming else (False, "stream inactif")
        if mode != "module_in_program_scene":
            return False, f"condition inconnue : {mode}"

        scene = str(context.get("program_scene") or "").strip()
        module_source = str(policy.module_source or policy_name).strip()
        if not scene:
            return False, "aucune scène programme détectée"
        if not module_source:
            return False, "module OBS non défini"

        key = (scene, module_source)
        now = self._clock()
        cached = self._module_presence_cache.get(key)
        if cached is not None and now - cached[0] < self._eligibility_cache_seconds:
            return cached[1], cached[2]

        try:
            catalog = self.layout_manager.discover_scene(scene, recursive=True)
            present = any(
                element.source == module_source
                for elements in catalog.values()
                for element in elements
            )
            reason = f"module présent dans {scene}" if present else f"module absent de {scene}"
        except Exception as exc:
            present = False
            reason = f"scan OBS impossible : {exc}"
        self._module_presence_cache[key] = (now, present, reason)
        return present, reason

    def is_eligible(self, policy_name: str, policy: TriggerPolicyConfig) -> bool:
        return self.eligibility(policy_name, policy)[0]

    def apply_event(self, event: ActivationEvent) -> None:
        if event.kind not in {"show", "hide"}:
            return
        policy = self._policy(event.policy)
        target = self._target_for_event(policy, event)
        if target is None:
            raise RuntimeError(
                f"Source d'activation introuvable pour {event.policy}: "
                f"{event.container}/{event.source}"
            )

        if event.kind == "show":
            if policy.exclusive:
                # A stale/missing alternate target must not prevent a valid
                # selected target from being shown. The selected target itself
                # still raises normally so the scheduler can fail safe/reset.
                for candidate in policy.targets:
                    if candidate == target:
                        continue
                    try:
                        self._set_target_enabled(candidate, False)
                    except Exception:
                        continue
                self._set_target_enabled(target, False)
            self._set_target_enabled(target, True)
            return

        self._set_target_enabled(target, False)

    def reconcile(self) -> tuple[str, ...]:
        """Hide every owned target after startup/reconnect/collection change."""
        self.invalidate_cache()
        warnings = self.hide_all()
        try:
            self._scene_collection = self._current_scene_collection()
        except Exception as exc:
            warnings = (*warnings, f"Scene Collection: {exc}")
        self._last_collection_probe = self._clock()
        return warnings

    def hide_all(self) -> tuple[str, ...]:
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        for policy in self._policies.values():
            for target in policy.targets:
                key = (target.container, target.source)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    self._set_target_enabled(target, False)
                except Exception as exc:
                    warnings.append(f"{target.container}/{target.source}: {exc}")
        return tuple(warnings)

    def scene_collection_changed(self) -> bool:
        if not bool(getattr(self.client, "connected", False)):
            return False
        now = self._clock()
        if self._last_collection_probe and now - self._last_collection_probe < self._collection_probe_seconds:
            return False
        self._last_collection_probe = now
        try:
            current = self._current_scene_collection()
        except Exception:
            return False
        if self._scene_collection is None:
            self._scene_collection = current
            return False
        if current == self._scene_collection:
            return False
        self._scene_collection = current
        self.invalidate_cache()
        return True

    def _current_scene_collection(self) -> str:
        response = self.client.send("GetSceneCollectionList")
        return str(response.get("currentSceneCollectionName") or "").strip()

    def _set_target_enabled(self, target: TriggerTargetConfig, enabled: bool) -> None:
        self.layout_manager.set_item_enabled(
            target.container,
            target.source,
            enabled,
            container_kind=target.container_kind,
        )

    def _policy(self, name: str) -> TriggerPolicyConfig:
        try:
            return self._policies[name]
        except KeyError as exc:
            raise KeyError(f"Politique d'activation introuvable : {name}") from exc

    @staticmethod
    def _target_for_event(
        policy: TriggerPolicyConfig,
        event: ActivationEvent,
    ) -> TriggerTargetConfig | None:
        exact = [
            target
            for target in policy.targets
            if target.source == event.source
            and (not event.container or target.container == event.container)
        ]
        if len(exact) == 1:
            return exact[0]
        if event.container:
            for target in exact:
                if target.container_kind == event.container_kind:
                    return target
        for target in policy.targets:
            if target.source == event.source:
                return target
        return None
