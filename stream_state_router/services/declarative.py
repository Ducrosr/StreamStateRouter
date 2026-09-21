from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
    item_keys = {
        (item.container, item.source, item.occurrence)
        for item in catalog.scene_items
    }

    def reject(key: PropertyKey, code: str, message: str) -> None:
        blocked[key] = PlanDiagnostic("error", code, message, key)

    for assignment in desired.assignments:
        key = assignment.key

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
        self._catalog_reader = OBSResourceCatalogReader(client)
        self._catalog: OBSResourceCatalog | None = None

    @property
    def catalog(self) -> OBSResourceCatalog | None:
        return self._catalog

    def invalidate_catalog(self) -> None:
        self._catalog = None

    def sync_catalog(self) -> OBSResourceCatalog:
        catalog = self._catalog_reader.sync()
        self._catalog = catalog
        return catalog

    def plan_state(
        self,
        desired: DesiredState,
        *,
        refresh_catalog: bool = False,
        blocked_provenance: Mapping[str, str] | None = None,
    ) -> ExecutionPlan:
        catalog = self._catalog
        if refresh_catalog or catalog is None:
            catalog = self.sync_catalog()

        concrete_desired = desired.bind_collection(catalog.collection)

        filter_sources = {
            assignment.key.source
            for assignment in concrete_desired.assignments
            if assignment.key.kind in {"filter_enabled", "filter_setting"}
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
        for assignment in concrete_desired.assignments:
            for provenance in assignment.provenance:
                reason = (blocked_provenance or {}).get(provenance)
                if not reason:
                    continue
                preflight.setdefault(
                    assignment.key,
                    PlanDiagnostic(
                        "warning",
                        "condition_blocked",
                        reason,
                        assignment.key,
                    ),
                )
                break
        observable = DesiredState.build(
            assignment
            for assignment in concrete_desired.assignments
            if assignment.key not in preflight
        )
        observed = observe_desired_state(self.client, catalog, observable)
        return build_execution_plan(
            concrete_desired,
            observed,
            preflight=preflight,
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
