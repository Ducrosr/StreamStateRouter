from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from ..obs.catalog import OBSResourceCatalog, OBSResourceCatalogReader
from ..obs.client import OBSClientManager, OBSRequestError
from ..obs.observed import observe_desired_state
from ..planning import (
    DesiredState,
    ExecutionPlan,
    PlanDiagnostic,
    PropertyKey,
    build_execution_plan,
)


@dataclass(frozen=True, slots=True)
class ControlScope:
    """Generic property scope; it deliberately contains no game-specific logic."""

    excluded_containers: frozenset[str] = frozenset()
    excluded_sources: frozenset[str] = frozenset()
    excluded_items: frozenset[tuple[str, str]] = frozenset()

    def block_reason(self, key: PropertyKey) -> str:
        if key.container and key.container in self.excluded_containers:
            return f"Container '{key.container}' is delegated to another owner"
        if key.source and key.source in self.excluded_sources:
            return f"Source '{key.source}' is delegated to another owner"
        if (
            key.container
            and key.source
            and (key.container, key.source) in self.excluded_items
        ):
            return (
                f"Resource '{key.container}/{key.source}' is delegated "
                "to another owner"
            )
        return ""


def _catalog_preflight(
    desired: DesiredState,
    catalog: OBSResourceCatalog,
    *,
    scope: ControlScope,
    filter_index: Mapping[str, frozenset[str]] | None = None,
    unreadable_filter_sources: frozenset[str] = frozenset(),
) -> dict[PropertyKey, PlanDiagnostic]:
    """Validate references that the lightweight catalog can prove or disprove."""

    blocked: dict[PropertyKey, PlanDiagnostic] = {}
    scene_names = {item.name for item in catalog.scenes}
    input_names = {item.name for item in catalog.inputs}
    source_names = {
        *scene_names,
        *catalog.groups,
        *input_names,
        *(item.source for item in catalog.scene_items),
    }
    item_by_key = {
        (item.container, item.source, item.occurrence): item
        for item in catalog.scene_items
    }
    item_keys = set(item_by_key)

    def reject(key: PropertyKey, code: str, message: str) -> None:
        blocked[key] = PlanDiagnostic("error", code, message, key)

    for assignment in desired.assignments:
        key = assignment.key

        if not catalog.collection:
            reject(
                key,
                "scene_collection_unknown",
                "Active OBS Scene Collection could not be established",
            )
            continue

        if (
            key.collection
            and catalog.collection
            and key.collection != catalog.collection
        ):
            reject(
                key,
                "scene_collection_mismatch",
                (
                    f"Property belongs to Scene Collection '{key.collection}', "
                    f"active collection is '{catalog.collection}'"
                ),
            )
            continue

        scope_reason = scope.block_reason(key)
        if scope_reason:
            reject(key, "resource_out_of_scope", scope_reason)
            continue

        if key.kind == "program_scene":
            if catalog.supports("GetCurrentProgramScene") is False:
                reject(
                    key,
                    "capability_missing",
                    "OBS does not advertise required request GetCurrentProgramScene",
                )
                continue
            target = str(assignment.value or "")
            if target not in scene_names:
                reject(
                    key,
                    "scene_missing",
                    f"Scene '{target}' is not present in the OBS catalog",
                )
            continue

        if key.kind == "scene_item_visibility":
            identity = (key.container, key.source, key.occurrence)
            reference = item_by_key.get(identity)
            list_request = (
                "GetGroupSceneItemList"
                if reference is not None and reference.container_kind == "group"
                else "GetSceneItemList"
            )
            required = (list_request, "GetSceneItemEnabled")
            missing = [
                request
                for request in required
                if catalog.supports(request) is False
            ]
            if missing:
                reject(
                    key,
                    "capability_missing",
                    "OBS does not advertise required request(s): "
                    + ", ".join(missing),
                )
                continue
            if key.container in catalog.unreadable_containers:
                reject(
                    key,
                    "scene_item_unverified",
                    (
                        f"Scene-item container '{key.container}' could not be "
                        "verified in the OBS catalog"
                    ),
                )
                continue
            if identity not in item_keys:
                reject(
                    key,
                    "scene_item_missing",
                    (
                        f"Scene item occurrence {key.occurrence} "
                        f"'{key.container}/{key.source}' is not present "
                        "in the OBS catalog"
                    ),
                )
            continue

        if key.kind in {"input_setting", "input_mute", "input_volume_db"}:
            if key.source not in input_names:
                reject(
                    key,
                    "input_missing",
                    f"Input '{key.source}' is not present in the OBS catalog",
                )
                continue
            request = {
                "input_setting": "GetInputSettings",
                "input_mute": "GetInputMute",
                "input_volume_db": "GetInputVolume",
            }[key.kind]
            if catalog.supports(request) is False:
                reject(
                    key,
                    "capability_missing",
                    f"OBS does not advertise required request {request}",
                )
            continue

        if key.kind in {"filter_enabled", "filter_setting"}:
            if key.source not in source_names:
                reject(
                    key,
                    "filter_source_missing",
                    f"Filter source '{key.source}' is not present in the OBS catalog",
                )
                continue
            if catalog.supports("GetSourceFilter") is False:
                reject(
                    key,
                    "capability_missing",
                    "OBS does not advertise required request GetSourceFilter",
                )
                continue
            if catalog.supports("GetSourceFilterList") is False:
                reject(
                    key,
                    "capability_missing",
                    "OBS does not advertise required request GetSourceFilterList",
                )
                continue
            if key.source in unreadable_filter_sources:
                reject(
                    key,
                    "filter_catalog_unreadable",
                    f"Filters for source '{key.source}' could not be verified",
                )
                continue
            if (
                filter_index is not None
                and key.source in filter_index
                and key.filter_name not in filter_index[key.source]
            ):
                reject(
                    key,
                    "filter_missing",
                    (
                        f"Filter '{key.filter_name}' is not present on "
                        f"source '{key.source}'"
                    ),
                )
            continue

        if key.kind == "layout_profile":
            if key.container and key.container not in scene_names:
                reject(
                    key,
                    "layout_scene_missing",
                    f"Layout scene '{key.container}' is not present in the OBS catalog",
                )

    return blocked


class DeclarativePlanningService:
    """Read-only bridge from live OBS discovery to the pure planner.

    This service intentionally has no executor. It reuses the runtime's
    OBSClientManager and therefore creates neither a second connection nor a
    second OBS writer.
    """

    def __init__(
        self,
        client: OBSClientManager,
        *,
        scope: ControlScope | None = None,
    ):
        self.client = client
        self.scope = scope or ControlScope()
        self._cooperative_yield: Callable[[], None] | None = None
        self._catalog_reader = OBSResourceCatalogReader(client)
        self._catalog: OBSResourceCatalog | None = None
        self._catalog_stale_reason = ""

    def set_cooperative_yield(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        self._cooperative_yield = callback
        self._catalog_reader.set_cooperative_yield(callback)

    @property
    def catalog(self) -> OBSResourceCatalog | None:
        return self._catalog

    def invalidate_catalog(self, reason: str = "invalidated") -> None:
        if self._catalog is not None:
            self._catalog_stale_reason = str(reason or "invalidated")

    def catalog_status(self) -> dict[str, object]:
        catalog = self._catalog
        if catalog is None:
            return {"available": False, "stale": False}
        return {
            "available": True,
            "stale": bool(self._catalog_stale_reason),
            "stale_reason": self._catalog_stale_reason,
            **catalog.summary(),
        }

    def catalog_snapshot(self) -> dict[str, object]:
        """Return the cached structural snapshot without issuing OBS I/O."""

        catalog = self._catalog
        if catalog is None:
            return {"available": False, "stale": False}
        return {
            **self.catalog_status(),
            "scene_refs": [
                {"name": item.name, "uuid": item.uuid, "index": item.index}
                for item in catalog.scenes
            ],
            "groups_list": list(catalog.groups),
            "scene_item_refs": [
                {
                    "collection": item.collection,
                    "root_scene": item.root_scene,
                    "container": item.container,
                    "container_kind": item.container_kind,
                    "path": list(item.path),
                    "source": item.source,
                    "source_uuid": item.source_uuid,
                    "source_kind": item.source_kind,
                    "occurrence": item.occurrence,
                    "scene_item_id": item.scene_item_id,
                    "enabled": item.enabled,
                }
                for item in catalog.scene_items
            ],
            "input_refs": [
                {"name": item.name, "kind": item.kind, "uuid": item.uuid}
                for item in catalog.inputs
            ],
            "transition_refs": [
                {"name": item.name, "kind": item.kind, "uuid": item.uuid}
                for item in catalog.transitions
            ],
        }

    def _session_generation(self) -> int:
        try:
            return int(self.client.session_generation)
        except (AttributeError, TypeError, ValueError):
            return 0

    def context_identity(self) -> tuple[str, int]:
        """Return the live Scene Collection/session boundary for planner resolution."""

        generation_before = self._session_generation()
        collection = self._catalog_reader.current_collection()
        generation_after = self._session_generation()
        if (
            generation_before
            and generation_after
            and generation_before != generation_after
        ):
            raise RuntimeError(
                "OBS session changed while reading declarative context identity"
            )
        generation = generation_after or generation_before
        if not collection:
            raise RuntimeError("Active OBS Scene Collection could not be established")
        return collection, generation

    def _context_error(self, catalog: OBSResourceCatalog) -> str:
        current_collection = self._catalog_reader.current_collection()
        current_generation = self._session_generation()
        if (
            catalog.session_generation
            and current_generation
            and current_generation != catalog.session_generation
        ):
            return "OBS session changed since catalog synchronization"
        if (
            catalog.collection
            and current_collection
            and current_collection != catalog.collection
        ):
            return (
                "OBS Scene Collection changed since catalog synchronization: "
                f"{catalog.collection} -> {current_collection}"
            )
        if not current_collection:
            return "Active OBS Scene Collection could not be established"
        return ""

    def sync_catalog(self) -> OBSResourceCatalog:
        catalog = self._catalog_reader.sync()
        self._catalog = catalog
        self._catalog_stale_reason = ""
        return catalog

    def plan_state(
        self,
        desired: DesiredState,
        *,
        refresh_catalog: bool = False,
        blocked_provenance: Mapping[str, str] | None = None,
    ) -> ExecutionPlan:
        catalog = self._catalog
        if refresh_catalog or catalog is None or self._catalog_stale_reason:
            catalog = self.sync_catalog()
        else:
            context_error = self._context_error(catalog)
            if context_error:
                self.invalidate_catalog(context_error)
                catalog = self.sync_catalog()

        concrete_desired = desired.bind_collection(catalog.collection)

        condition_preflight: dict[PropertyKey, PlanDiagnostic] = {}
        blocked_provenance = blocked_provenance or {}
        matched_blocked_provenance: set[str] = set()
        for assignment in concrete_desired.assignments:
            for provenance in assignment.provenance:
                reason = blocked_provenance.get(provenance)
                if not reason:
                    continue
                matched_blocked_provenance.add(provenance)
                condition_preflight[assignment.key] = PlanDiagnostic(
                    "warning",
                    "condition_blocked",
                    reason,
                    assignment.key,
                )
                break

        filter_sources = {
            assignment.key.source
            for assignment in concrete_desired.assignments
            if assignment.key not in condition_preflight
            and assignment.key.kind in {"filter_enabled", "filter_setting"}
            and assignment.key.source
        }
        filter_index: dict[str, frozenset[str]] = {}
        unreadable_filter_sources: set[str] = set()
        if catalog.supports("GetSourceFilterList") is not False:
            known_sources = {
                *(item.name for item in catalog.scenes),
                *catalog.groups,
                *(item.name for item in catalog.inputs),
                *(item.source for item in catalog.scene_items),
            }
            for source in sorted(filter_sources, key=str.casefold):
                if source not in known_sources:
                    continue
                try:
                    filters = self._catalog_reader.filters_for_source(source)
                except OBSRequestError:
                    unreadable_filter_sources.add(source)
                    continue
                filter_index[source] = frozenset(item.name for item in filters)

        preflight = _catalog_preflight(
            concrete_desired,
            catalog,
            scope=self.scope,
            filter_index=filter_index,
            unreadable_filter_sources=frozenset(unreadable_filter_sources),
        )
        for key, diagnostic in condition_preflight.items():
            preflight.setdefault(key, diagnostic)
        observable = DesiredState.build(
            assignment
            for assignment in concrete_desired.assignments
            if assignment.key not in preflight
        )
        observed = observe_desired_state(
            self.client,
            catalog,
            observable,
            cooperative_yield=self._cooperative_yield,
        )
        context_error = self._context_error(catalog)
        if context_error:
            self.invalidate_catalog(context_error)
            raise RuntimeError(
                "Declarative observation context changed before publication: "
                + context_error
            )
        plan = build_execution_plan(
            concrete_desired,
            observed,
            preflight=preflight,
        )
        global_blocks = tuple(
            PlanDiagnostic(
                "error",
                "resolution_blocked",
                f"{provenance}: {reason}",
            )
            for provenance, reason in sorted(blocked_provenance.items())
            if provenance not in matched_blocked_provenance
        )
        if not global_blocks:
            return plan
        return ExecutionPlan(
            target_signature=plan.target_signature,
            diff=plan.diff,
            operations=plan.operations,
            diagnostics=(*plan.diagnostics, *global_blocks),
        )

    def dry_run(
        self,
        desired: DesiredState,
        *,
        refresh_catalog: bool = False,
        blocked_provenance: Mapping[str, str] | None = None,
    ) -> ExecutionPlan:
        """Compatibility/readability alias for the canonical planning path."""
        return self.plan_state(
            desired,
            refresh_catalog=refresh_catalog,
            blocked_provenance=blocked_provenance,
        )
