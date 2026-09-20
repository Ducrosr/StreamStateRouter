from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from ..obs.client import (
    OBSRequestError,
    OBSResourceNotFoundError,
    OBSUnavailableError,
)
from .models import ActivationEvent, TriggerPolicyConfig, TriggerTargetConfig


class ActivationVisibilityUncertain(RuntimeError):
    """OBS visibility may have changed, but the result was not acknowledged."""


class ActivationTargetMissing(RuntimeError):
    """OBS confirmed that the selected activation target is absent."""


class ActivationBlocked(RuntimeError):
    """A policy cannot activate while cleanup is still uncertain."""


class ActivationCollectionChanged(RuntimeError):
    """The active OBS collection changed before an operation could be applied."""


@dataclass(slots=True)
class PendingHide:
    policy: str
    target: TriggerTargetConfig
    collection: str
    created_at: float
    attempts: int = 0
    next_retry_at: float = 0.0
    last_error: str = ""


def pending_hide_to_mapping(pending: PendingHide) -> dict[str, object]:
    return {
        "policy": pending.policy,
        "collection": pending.collection,
        "target": pending.target.to_mapping(),
        "created_at": float(pending.created_at),
        "attempts": int(pending.attempts),
        "next_retry_at": float(pending.next_retry_at),
        "last_error": pending.last_error,
    }


def pending_hide_from_mapping(raw: Mapping[str, object]) -> PendingHide | None:
    target_raw = raw.get("target")
    if not isinstance(target_raw, Mapping):
        return None
    policy = str(raw.get("policy") or "").strip()
    collection = str(raw.get("collection") or "").strip()
    if not policy or not collection:
        return None
    try:
        target = TriggerTargetConfig.from_mapping(target_raw)
        return PendingHide(
            policy=policy,
            target=target,
            collection=collection,
            created_at=float(raw.get("created_at", 0.0) or 0.0),
            attempts=max(0, int(raw.get("attempts", 0) or 0)),
            next_retry_at=float(raw.get("next_retry_at", 0.0) or 0.0),
            last_error=str(raw.get("last_error") or ""),
        )
    except (TypeError, ValueError, OverflowError):
        return None


class OBSActivationController:
    """Translate scheduler events into acknowledged OBS visibility mutations."""

    def __init__(
        self,
        dispatcher,
        policies: Mapping[str, TriggerPolicyConfig] | None = None,
        *,
        clock=time.monotonic,
        eligibility_cache_seconds: float = 0.5,
        collection_probe_seconds: float = 1.0,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 5.0,
    ) -> None:
        self.dispatcher = dispatcher
        self.client = dispatcher.client
        self.layout_manager = dispatcher.layout_manager
        self._policies = dict(policies or {})
        self._clock = clock
        self._eligibility_cache_seconds = max(0.05, float(eligibility_cache_seconds))
        self._collection_probe_seconds = max(0.1, float(collection_probe_seconds))
        self._retry_base_seconds = max(0.05, float(retry_base_seconds))
        self._retry_max_seconds = max(self._retry_base_seconds, float(retry_max_seconds))
        self._module_presence_cache: dict[tuple[str, str], tuple[float, bool, str]] = {}
        self._last_collection_probe = 0.0
        self._scene_collection: str | None = None
        self._pending_hides: dict[tuple[str, str, str, str, str], PendingHide] = {}
        self._sync_visibility_owners()

    def configure(self, policies: Mapping[str, TriggerPolicyConfig]) -> None:
        self._policies = dict(policies)
        self._sync_visibility_owners()
        self.invalidate_cache()

    def _sync_visibility_owners(self) -> None:
        owners = {
            (target.container, target.source)
            for policy in self._policies.values()
            for target in policy.targets
        }
        owners.update(
            (pending.target.container, pending.target.source)
            for pending in self._pending_hides.values()
        )
        self.layout_manager.set_runtime_visibility_owners(owners)

    def export_pending_hides(self) -> tuple[dict[str, object], ...]:
        return tuple(
            pending_hide_to_mapping(item)
            for item in sorted(
                self._pending_hides.values(),
                key=lambda value: (
                    value.collection,
                    value.policy,
                    value.target.container,
                    value.target.source,
                ),
            )
        )

    def import_pending_hides(self, raw_items) -> int:
        imported = 0
        collections: set[str] = set()
        for raw in raw_items or ():
            if not isinstance(raw, Mapping):
                continue
            pending = pending_hide_from_mapping(raw)
            if pending is None:
                continue
            key = self._pending_key(pending.policy, pending.target, pending.collection)
            self._pending_hides[key] = pending
            collections.add(pending.collection)
            imported += 1
        if len(collections) == 1 and self._scene_collection is None:
            # Remember the collection the obligation belongs to. The first real
            # OBS probe will keep it only if the same collection is active.
            self._scene_collection = next(iter(collections))
        self._sync_visibility_owners()
        return imported

    def invalidate_cache(self) -> None:
        # Activation visibility uses pair-local fresh ids. Do not flush the
        # LayoutProfile transform cache globally from this subsystem.
        self._module_presence_cache.clear()

    def pending_hides(self, policy_name: str | None = None) -> tuple[PendingHide, ...]:
        values = tuple(self._pending_hides.values())
        if policy_name is None:
            return values
        wanted = str(policy_name)
        return tuple(item for item in values if item.policy == wanted)

    def policy_cleanup_status(self, policy_name: str) -> tuple[bool, str]:
        collection = self._scene_collection
        pending = [
            item
            for item in self._pending_hides.values()
            if item.policy == policy_name
            and (collection is None or item.collection == collection)
        ]
        if not pending:
            return False, ""
        names = ", ".join(
            sorted(
                {
                    f"{item.target.container_kind}:{item.target.container}/{item.target.source}"
                    for item in pending
                }
            )
        )
        return True, f"nettoyage OBS en attente : {names}"

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
                f"Source d'activation introuvable ou ambiguë pour {event.policy}: "
                f"{event.container}/{event.source}"
            )

        collection = self._operation_collection()

        if event.kind == "hide":
            status, error = self._mutate_visibility(target, False)
            if status in {"applied", "missing"}:
                self._clear_pending_hide(event.policy, target, collection)
                return
            self._record_pending_hide(event.policy, target, collection, error)
            raise ActivationVisibilityUncertain(
                f"Masquage non acquitté pour {target.container}/{target.source}: {error}"
            )

        blocked, reason = self.policy_cleanup_status(event.policy)
        if blocked:
            raise ActivationBlocked(reason)

        if policy.exclusive:
            for candidate in policy.targets:
                if candidate.identity == target.identity:
                    continue
                status, error = self._mutate_visibility(candidate, False)
                if status in {"applied", "missing"}:
                    self._clear_pending_hide(event.policy, candidate, collection)
                    continue
                self._record_pending_hide(event.policy, candidate, collection, error)
                raise ActivationVisibilityUncertain(
                    "Activation exclusive bloquée : masquage concurrent non acquitté "
                    f"pour {candidate.container}/{candidate.source}: {error}"
                )

            # Force a clean false->true edge for sources whose media/browser
            # animation needs to restart. Failure to acknowledge this hide blocks
            # the show rather than allowing two potentially visible targets.
            status, error = self._mutate_visibility(target, False)
            if status == "missing":
                raise ActivationTargetMissing(
                    f"Source absente : {target.container}/{target.source}"
                )
            if status != "applied":
                self._record_pending_hide(event.policy, target, collection, error)
                raise ActivationVisibilityUncertain(
                    f"Pré-masquage non acquitté pour {target.container}/{target.source}: {error}"
                )

        status, error = self._mutate_visibility(target, True)
        if status == "applied":
            return
        if status == "missing":
            raise ActivationTargetMissing(
                f"Source absente : {target.container}/{target.source}"
            )

        # A lost/uncertain response after show may mean OBS applied the show.
        # Compensate by scheduling a hide and block this policy until it is acked.
        self._record_pending_hide(event.policy, target, collection, error)
        raise ActivationVisibilityUncertain(
            f"Affichage au résultat incertain pour {target.container}/{target.source}: {error}"
        )

    def reconcile(self) -> tuple[str, ...]:
        """Hide every owned target in the current collection.

        A warning means cleanup is not complete. Unacknowledged hides remain
        structured in _pending_hides and keep their policies blocked.
        """
        warnings: list[str] = []
        try:
            collection = self._current_scene_collection()
        except Exception as exc:
            self._last_collection_probe = self._clock()
            return (f"Scene Collection: {exc}",)

        self._adopt_collection(collection)
        self.invalidate_cache()
        warnings.extend(self.hide_all(collection=collection))
        self._last_collection_probe = self._clock()
        return tuple(warnings)

    def hide_all(self, *, collection: str | None = None) -> tuple[str, ...]:
        collection_name = collection or self._operation_collection()
        warnings: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for policy_name, policy in self._policies.items():
            for target in policy.targets:
                key = (
                    policy_name,
                    target.container,
                    target.container_kind,
                    target.source,
                )
                if key in seen:
                    continue
                seen.add(key)
                status, error = self._mutate_visibility(target, False)
                if status in {"applied", "missing"}:
                    self._clear_pending_hide(policy_name, target, collection_name)
                    continue
                self._record_pending_hide(
                    policy_name,
                    target,
                    collection_name,
                    error,
                )
                warnings.append(
                    f"{target.container}/{target.source}: masquage non acquitté ({error})"
                )
        return tuple(warnings)

    def retry_pending_hides(self, *, now: float | None = None) -> tuple[str, ...]:
        if not self._pending_hides:
            return ()
        timestamp = self._clock() if now is None else float(now)
        due = [
            pending
            for pending in self._pending_hides.values()
            if timestamp >= pending.next_retry_at
        ]
        if not due or not bool(getattr(self.client, "connected", False)):
            return ()

        try:
            current = self._current_scene_collection()
        except Exception:
            return ()

        if self._scene_collection != current:
            self._adopt_collection(current)
            self.invalidate_cache()
            return (f"Anciennes opérations abandonnées après changement vers {current}",)

        messages: list[str] = []
        for key, pending in tuple(self._pending_hides.items()):
            if pending.collection != current or timestamp < pending.next_retry_at:
                continue
            status, error = self._mutate_visibility(pending.target, False)
            if status in {"applied", "missing"}:
                self._pending_hides.pop(key, None)
                self._sync_visibility_owners()
                messages.append(
                    f"Masquage acquitté : {pending.target.container}/{pending.target.source}"
                )
                continue
            pending.attempts += 1
            pending.last_error = error
            delay = min(
                self._retry_max_seconds,
                self._retry_base_seconds * (2 ** min(pending.attempts, 8)),
            )
            pending.next_retry_at = timestamp + delay
        return tuple(messages)

    def scene_collection_changed(self) -> bool:
        if not bool(getattr(self.client, "connected", False)):
            return False
        now = self._clock()
        if (
            self._last_collection_probe
            and now - self._last_collection_probe < self._collection_probe_seconds
        ):
            return False
        self._last_collection_probe = now
        try:
            current = self._current_scene_collection()
        except Exception:
            return False
        if self._scene_collection is None:
            self._adopt_collection(current)
            return False
        if current == self._scene_collection:
            return False
        self._adopt_collection(current)
        self.invalidate_cache()
        return True

    def _operation_collection(self) -> str:
        current = self._current_scene_collection()
        if self._scene_collection is None:
            self._adopt_collection(current)
            return current
        if current != self._scene_collection:
            previous = self._scene_collection
            self._adopt_collection(current)
            self.invalidate_cache()
            raise ActivationCollectionChanged(
                f"Scene Collection modifiée : {previous} -> {current}"
            )
        return current

    def _adopt_collection(self, collection: str) -> None:
        current = str(collection or "").strip()
        if self._scene_collection == current:
            return
        # Never replay operations captured for another Scene Collection.
        self._pending_hides.clear()
        self._scene_collection = current
        self._sync_visibility_owners()

    def _current_scene_collection(self) -> str:
        response = self.client.send("GetSceneCollectionList")
        return str(response.get("currentSceneCollectionName") or "").strip()

    def _mutate_visibility(
        self,
        target: TriggerTargetConfig,
        enabled: bool,
    ) -> tuple[str, str]:
        try:
            self.layout_manager.set_activation_item_enabled(
                target.container,
                target.source,
                enabled,
                container_kind=target.container_kind,
            )
            return "applied", ""
        except OBSResourceNotFoundError as exc:
            return "missing", str(exc)
        except (OBSUnavailableError, OBSRequestError) as exc:
            return "uncertain", str(exc)
        except Exception as exc:
            return "uncertain", str(exc)

    def _record_pending_hide(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
        error: str,
    ) -> None:
        now = self._clock()
        key = self._pending_key(policy_name, target, collection)
        pending = self._pending_hides.get(key)
        if pending is None:
            pending = PendingHide(
                policy=policy_name,
                target=target,
                collection=collection,
                created_at=now,
            )
            self._pending_hides[key] = pending
        pending.attempts += 1
        pending.last_error = str(error)
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** min(pending.attempts - 1, 8)),
        )
        pending.next_retry_at = now + delay
        self._sync_visibility_owners()

    def _clear_pending_hide(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
    ) -> None:
        self._pending_hides.pop(
            self._pending_key(policy_name, target, collection),
            None,
        )
        self._sync_visibility_owners()

    @staticmethod
    def _pending_key(
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
    ) -> tuple[str, str, str, str, str]:
        return (
            str(policy_name),
            str(collection),
            target.container,
            target.container_kind,
            target.source,
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
        if event.container:
            matches = [
                target
                for target in policy.targets
                if target.source == event.source
                and target.container == event.container
                and target.container_kind == event.container_kind
            ]
            return matches[0] if len(matches) == 1 else None

        # Backward compatibility for old events that only carried source. This
        # remains valid only while source alone is unambiguous.
        matches = [
            target for target in policy.targets if target.source == event.source
        ]
        return matches[0] if len(matches) == 1 else None
