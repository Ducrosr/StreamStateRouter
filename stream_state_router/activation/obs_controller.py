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
        "kind": "activation_hide",
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
        self._scene_topology_cache: dict[str, tuple[float, frozenset[str], str]] = {}
        self._last_collection_probe = 0.0
        self._scene_collection: str | None = None
        self._pending_hides: dict[tuple[str, str, str, str, str], PendingHide] = {}
        self._cooperative_yield = None
        self._sync_visibility_owners()

    def set_cooperative_yield(self, callback) -> None:
        self._cooperative_yield = callback

    def _yield_runtime(self) -> None:
        callback = self._cooperative_yield
        if callback is not None:
            callback()

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
        collection = str(self._scene_collection or "").strip()
        owners.update(
            (pending.target.container, pending.target.source)
            for pending in self._pending_hides.values()
            if not collection or pending.collection == collection
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
        for raw in raw_items or ():
            if not isinstance(raw, Mapping):
                continue
            kind = str(raw.get("kind") or "activation_hide").strip().casefold()
            if kind not in {"activation_hide", "activation"}:
                continue
            pending = pending_hide_from_mapping(raw)
            if pending is None:
                continue
            # Retry deadlines are monotonic-process values. Never trust a
            # persisted/imported absolute deadline after a runtime/process
            # replacement; make the contextualized obligation immediately due.
            pending.next_retry_at = 0.0
            key = self._pending_key(pending.policy, pending.target, pending.collection)
            self._pending_hides[key] = pending
            imported += 1
        # Imported obligations retain their own Scene Collection context. They
        # must not redefine the collection of the new OBS session.
        self._sync_visibility_owners()
        return imported

    def invalidate_cache(self) -> None:
        # Activation visibility uses pair-local fresh ids. Do not flush the
        # LayoutProfile transform cache globally from this subsystem.
        self._scene_topology_cache.clear()

    def pending_hides(self, policy_name: str | None = None) -> tuple[PendingHide, ...]:
        values = tuple(self._pending_hides.values())
        if policy_name is None:
            return values
        wanted = str(policy_name)
        return tuple(item for item in values if item.policy == wanted)

    def pending_hides_for_current_collection(self) -> tuple[PendingHide, ...]:
        collection = str(self._scene_collection or "").strip()
        if not collection:
            return ()
        return tuple(
            item for item in self._pending_hides.values()
            if item.collection == collection
        )

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

        now = self._clock()
        cached = self._scene_topology_cache.get(scene)
        if cached is not None and now - cached[0] < self._eligibility_cache_seconds:
            sources = cached[1]
            scan_error = cached[2]
        else:
            try:
                if hasattr(self.layout_manager, "scan_scene_topology"):
                    topology = self.layout_manager.scan_scene_topology(scene, recursive=True)
                    sources = frozenset(str(item.source) for item in topology)
                else:  # compatibility for lightweight integrations/fakes
                    catalog = self.layout_manager.discover_scene(scene, recursive=True)
                    sources = frozenset(
                        str(element.source)
                        for elements in catalog.values()
                        for element in elements
                    )
                scan_error = ""
            except Exception as exc:
                sources = frozenset()
                scan_error = str(exc)
            self._scene_topology_cache[scene] = (now, sources, scan_error)

        if scan_error:
            return False, f"scan OBS impossible : {scan_error}"
        present = module_source in sources
        reason = f"module présent dans {scene}" if present else f"module absent de {scene}"
        return present, reason

    def is_eligible(self, policy_name: str, policy: TriggerPolicyConfig) -> bool:
        return self.eligibility(policy_name, policy)[0]

    def apply_event(self, event: ActivationEvent) -> str:
        if event.kind not in {"show", "hide"}:
            return ""
        policy = self._policy(event.policy)
        target = self._target_for_event(policy, event)
        if target is None:
            raise RuntimeError(
                f"Source d'activation introuvable ou ambiguë pour {event.policy}: "
                f"{event.container}/{event.source}"
            )

        if event.kind == "hide":
            # A hide belongs to the Scene Collection in which the temporary
            # visibility was created. Never reinterpret it against a collection
            # merely because another hide in the same batch observed a switch.
            origin_collection = (
                str(event.collection or "").strip()
                or str(self._scene_collection or "").strip()
                or "<unknown>"
            )
            self._ensure_pending_hide(event.policy, target, origin_collection)
            self._yield_runtime()
            current = self._current_scene_collection()
            if self._scene_collection != current:
                self._adopt_collection(current)
                self.invalidate_cache()

            if origin_collection == "<unknown>":
                self._ack_pending_hide(event.policy, target, origin_collection)
                origin_collection = current
                self._ensure_pending_hide(event.policy, target, origin_collection)
            elif current != origin_collection:
                raise ActivationCollectionChanged(
                    "Masquage suspendu : Scene Collection d'origine "
                    f"{origin_collection} != collection active {current}"
                )

            status, error = self._mutate_visibility(target, False)
            if status in {"applied", "missing"}:
                self._ack_pending_hide(event.policy, target, origin_collection)
                return origin_collection
            self._record_pending_hide_failure(
                event.policy,
                target,
                origin_collection,
                error,
            )
            raise ActivationVisibilityUncertain(
                f"Masquage non acquitté pour {target.container}/{target.source}: {error}"
            )

        collection = self._operation_collection()

        blocked, reason = self.policy_cleanup_status(event.policy)
        if blocked:
            raise ActivationBlocked(reason)

        if policy.exclusive:
            for candidate in policy.targets:
                if candidate.identity == target.identity:
                    continue
                self._ensure_pending_hide(event.policy, candidate, collection)
                status, error = self._mutate_visibility(candidate, False)
                if status in {"applied", "missing"}:
                    self._ack_pending_hide(event.policy, candidate, collection)
                    continue
                self._record_pending_hide_failure(
                    event.policy,
                    candidate,
                    collection,
                    error,
                )
                raise ActivationVisibilityUncertain(
                    "Activation exclusive bloquée : masquage concurrent non acquitté "
                    f"pour {candidate.container}/{candidate.source}: {error}"
                )

            # Force a clean false->true edge for sources whose media/browser
            # animation needs to restart. Failure to acknowledge this hide blocks
            # the show rather than allowing two potentially visible targets.
            self._ensure_pending_hide(event.policy, target, collection)
            status, error = self._mutate_visibility(target, False)
            if status == "missing":
                self._ack_pending_hide(event.policy, target, collection)
                raise ActivationTargetMissing(
                    f"Source absente : {target.container}/{target.source}"
                )
            if status != "applied":
                self._record_pending_hide_failure(
                    event.policy,
                    target,
                    collection,
                    error,
                )
                raise ActivationVisibilityUncertain(
                    f"Pré-masquage non acquitté pour {target.container}/{target.source}: {error}"
                )
            self._ack_pending_hide(event.policy, target, collection)

        # Pre-arm a compensating hide before the show. It is explicitly
        # acknowledged only after OBS confirms the show result (or confirms the
        # target is absent). An uncertain response therefore leaves a durable
        # cleanup obligation with the original Scene Collection context.
        self._ensure_pending_hide(event.policy, target, collection)
        status, error = self._mutate_visibility(target, True)
        if status == "applied":
            self._ack_pending_hide(event.policy, target, collection)
            return collection
        if status == "missing":
            self._ack_pending_hide(event.policy, target, collection)
            raise ActivationTargetMissing(
                f"Source absente : {target.container}/{target.source}"
            )

        self._record_pending_hide_failure(event.policy, target, collection, error)
        raise ActivationVisibilityUncertain(
            f"Affichage au résultat incertain pour {target.container}/{target.source}: {error}"
        )

    def reconcile(self) -> tuple[str, ...]:
        """Hide every owned target in the current collection.

        A warning means cleanup is not complete. Unacknowledged hides remain
        structured in _pending_hides and keep their policies blocked.
        """
        warnings: list[str] = []
        self._yield_runtime()
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
                self._yield_runtime()
                key = (
                    policy_name,
                    target.container,
                    target.container_kind,
                    target.source,
                )
                if key in seen:
                    continue
                seen.add(key)
                self._ensure_pending_hide(policy_name, target, collection_name)
                status, error = self._mutate_visibility(target, False)
                if status in {"applied", "missing"}:
                    self._ack_pending_hide(policy_name, target, collection_name)
                    continue
                self._record_pending_hide_failure(
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

        self._yield_runtime()
        try:
            current = self._current_scene_collection()
        except Exception:
            return ()

        messages: list[str] = []
        if self._scene_collection != current:
            previous = self._scene_collection
            self._adopt_collection(current)
            self.invalidate_cache()
            if previous:
                messages.append(
                    f"Scene Collection modifiée : obligations de {previous} conservées et suspendues"
                )

        for key, pending in tuple(self._pending_hides.items()):
            # A cleanup obligation is valid only in the collection where the
            # potentially-visible item/filter was created. Keep, but never
            # replay, obligations belonging to another collection.
            if pending.collection != current or timestamp < pending.next_retry_at:
                continue
            self._yield_runtime()
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
        self._yield_runtime()
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
        # Pending hides keep the collection in which they were created. A
        # collection switch suspends old obligations; it must never erase or
        # replay them against the new collection.
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

    def register_hide_obligation(self, event: ActivationEvent) -> None:
        """Arm a hide obligation without performing the visibility mutation.

        Shutdown uses this before resetting scheduler state so a visible target
        is already represented as cleanup work before its transient state is
        discarded.
        """
        if event.kind != "hide":
            return
        policy = self._policy(event.policy)
        target = self._target_for_event(policy, event)
        if target is None:
            raise RuntimeError(
                f"Source d'activation introuvable ou ambiguë pour {event.policy}: "
                f"{event.container}/{event.source}"
            )
        # Never perform network I/O while snapshotting shutdown state.
        # A normally-visible activation already has its originating collection
        # from apply_event(show). If that context is unexpectedly unavailable,
        # preserve a non-replayable obligation rather than guessing a collection.
        collection = (
            str(event.collection or "").strip()
            or str(self._scene_collection or "").strip()
            or "<unknown>"
        )
        self._ensure_pending_hide(event.policy, target, collection)

    def _ensure_pending_hide(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
    ) -> PendingHide:
        now = self._clock()
        key = self._pending_key(policy_name, target, collection)
        pending = self._pending_hides.get(key)
        if pending is None:
            pending = PendingHide(
                policy=policy_name,
                target=target,
                collection=collection,
                created_at=now,
                next_retry_at=now,
            )
            self._pending_hides[key] = pending
            self._sync_visibility_owners()
        return pending

    def _record_pending_hide_failure(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
        error: str,
    ) -> None:
        now = self._clock()
        pending = self._ensure_pending_hide(policy_name, target, collection)
        pending.attempts += 1
        pending.last_error = str(error)
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** min(pending.attempts - 1, 8)),
        )
        pending.next_retry_at = now + delay

    def _ack_pending_hide(
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

    # Compatibility for older internal callers/tests.
    def _record_pending_hide(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
        error: str,
    ) -> None:
        self._record_pending_hide_failure(policy_name, target, collection, error)

    def _clear_pending_hide(
        self,
        policy_name: str,
        target: TriggerTargetConfig,
        collection: str,
    ) -> None:
        self._ack_pending_hide(policy_name, target, collection)

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
