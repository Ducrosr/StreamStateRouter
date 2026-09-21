from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .client import OBSClientManager, OBSRequestError


@dataclass(frozen=True, slots=True)
class SceneRef:
    name: str
    uuid: str = ""
    index: int = 0


@dataclass(frozen=True, slots=True)
class SceneItemRef:
    collection: str
    root_scene: str
    container: str
    container_kind: str
    path: tuple[str, ...]
    source: str
    source_uuid: str
    source_kind: str
    occurrence: int
    scene_item_id: int | None
    enabled: bool | None


@dataclass(frozen=True, slots=True)
class InputRef:
    name: str
    kind: str
    uuid: str = ""


@dataclass(frozen=True, slots=True)
class FilterRef:
    source: str
    name: str
    kind: str
    enabled: bool | None


@dataclass(frozen=True, slots=True)
class TransitionRef:
    name: str
    kind: str
    uuid: str = ""


@dataclass(frozen=True, slots=True)
class OBSResourceCatalog:
    collection: str
    current_program_scene: str
    current_program_scene_uuid: str
    canvas: tuple[int, int] | None
    scenes: tuple[SceneRef, ...]
    groups: tuple[str, ...]
    scene_items: tuple[SceneItemRef, ...]
    inputs: tuple[InputRef, ...]
    transitions: tuple[TransitionRef, ...]
    available_requests: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()
    unreadable_containers: frozenset[str] = frozenset()
    session_generation: int = 0

    def supports(self, request: str) -> bool | None:
        if not self.available_requests:
            return None
        return str(request) in self.available_requests

    def summary(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "current_program_scene": self.current_program_scene,
            "current_program_scene_uuid": self.current_program_scene_uuid,
            "canvas": list(self.canvas) if self.canvas else None,
            "scenes": len(self.scenes),
            "groups": len(self.groups),
            "scene_items": len(self.scene_items),
            "inputs": len(self.inputs),
            "transitions": len(self.transitions),
            "available_requests": len(self.available_requests),
            "warnings": list(self.warnings),
            "complete": not self.warnings and not self.unreadable_containers,
            "partial": bool(self.warnings or self.unreadable_containers),
            "unreadable_containers": sorted(self.unreadable_containers),
            "session_generation": self.session_generation,
        }


@dataclass(frozen=True, slots=True)
class InputDetails:
    input: InputRef
    settings: Mapping[str, Any]
    filters: tuple[FilterRef, ...]


@dataclass(frozen=True, slots=True)
class FilterDetails:
    filter: FilterRef
    settings: Mapping[str, Any]


class OBSResourceCatalogReader:
    """Read-only OBS resource discovery.

    Only Get* requests are issued.  The reader deliberately keeps the main sync
    lightweight: input settings and filter settings are loaded on demand.
    """

    def __init__(
        self,
        client: OBSClientManager,
        *,
        cooperative_yield: Callable[[], None] | None = None,
    ):
        self.client = client
        self._cooperative_yield = cooperative_yield

    def set_cooperative_yield(self, callback: Callable[[], None] | None) -> None:
        self._cooperative_yield = callback

    def _send(
        self,
        request: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._cooperative_yield is not None:
            self._cooperative_yield()
        return self._send(request, data)

    def _session_generation(self) -> int:
        try:
            return int(getattr(self.client, "session_generation"))
        except (AttributeError, TypeError, ValueError):
            return 0

    def current_collection(self) -> str:
        response = self._send("GetSceneCollectionList")
        return str(response.get("currentSceneCollectionName") or "").strip()

    @staticmethod
    def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
        return [item for item in (value or []) if isinstance(item, Mapping)]

    def sync(self) -> OBSResourceCatalog:
        warnings: list[str] = []
        unreadable_containers: set[str] = set()

        version_response = self._send("GetVersion")
        available_requests = frozenset(
            str(name).strip()
            for name in version_response.get("availableRequests", []) or []
            if str(name).strip()
        )

        collection_response = self._send("GetSceneCollectionList")
        collection = str(
            collection_response.get("currentSceneCollectionName") or ""
        ).strip()
        session_generation = self._session_generation()

        scene_response = self._send("GetSceneList")
        scene_rows = self._mapping_list(scene_response.get("scenes"))
        scenes = tuple(
            SceneRef(
                name=str(row.get("sceneName") or "").strip(),
                uuid=str(row.get("sceneUuid") or "").strip(),
                index=int(row.get("sceneIndex", index) or 0),
            )
            for index, row in enumerate(scene_rows)
            if str(row.get("sceneName") or "").strip()
        )
        current_program_scene = str(
            scene_response.get("currentProgramSceneName") or ""
        ).strip()
        current_program_scene_uuid = str(
            scene_response.get("currentProgramSceneUuid") or ""
        ).strip()

        group_names: set[str] = set()
        try:
            if available_requests and "GetGroupList" not in available_requests:
                warnings.append("Group list unavailable: request not advertised by OBS")
            else:
                group_response = self._send("GetGroupList")
                group_names.update(
                    str(name).strip()
                    for name in group_response.get("groups", []) or []
                    if str(name).strip()
                )
        except OBSRequestError as exc:
            warnings.append(f"Group list unavailable: {exc}")

        scene_items: list[SceneItemRef] = []
        for scene in scenes:
            try:
                response = self._send(
                    "GetSceneItemList",
                    {"sceneName": scene.name},
                )
            except OBSRequestError as exc:
                warnings.append(f"Scene '{scene.name}' unreadable: {exc}")
                unreadable_containers.add(scene.name)
                continue
            rows = self._mapping_list(response.get("sceneItems"))
            group_names.update(
                str(row.get("sourceName") or "").strip()
                for row in rows
                if bool(row.get("isGroup", False))
                and str(row.get("sourceName") or "").strip()
            )
            self._append_container_items(
                scene_items,
                collection=collection,
                root_scene=scene.name,
                container=scene.name,
                container_kind="scene",
                path=(scene.name,),
                rows=rows,
            )

        visited_groups: set[str] = set()
        pending_groups = sorted(group_names, key=str.casefold)
        while pending_groups:
            group = pending_groups.pop(0)
            if group in visited_groups:
                continue
            visited_groups.add(group)
            try:
                response = self._send(
                    "GetGroupSceneItemList",
                    {"sceneName": group},
                )
            except OBSRequestError as exc:
                warnings.append(f"Group '{group}' unreadable: {exc}")
                unreadable_containers.add(group)
                continue
            rows = self._mapping_list(response.get("sceneItems"))
            discovered_groups = {
                str(row.get("sourceName") or "").strip()
                for row in rows
                if bool(row.get("isGroup", False))
                and str(row.get("sourceName") or "").strip()
            }
            for child in sorted(discovered_groups, key=str.casefold):
                group_names.add(child)
                if child not in visited_groups:
                    pending_groups.append(child)
            # A group can be referenced from several root scenes. Its internal
            # membership is one OBS container and is catalogued exactly once.
            self._append_container_items(
                scene_items,
                collection=collection,
                root_scene="",
                container=group,
                container_kind="group",
                path=(group,),
                rows=rows,
            )
        groups = tuple(sorted(group_names, key=str.casefold))

        input_response = self._send("GetInputList")
        inputs = tuple(
            sorted(
                (
                    InputRef(
                        name=str(row.get("inputName") or "").strip(),
                        kind=str(row.get("inputKind") or "").strip(),
                        uuid=str(row.get("inputUuid") or "").strip(),
                    )
                    for row in self._mapping_list(input_response.get("inputs"))
                    if str(row.get("inputName") or "").strip()
                ),
                key=lambda item: item.name.casefold(),
            )
        )

        transition_response = self._send("GetSceneTransitionList")
        transitions = tuple(
            TransitionRef(
                name=str(row.get("transitionName") or "").strip(),
                kind=str(row.get("transitionKind") or "").strip(),
                uuid=str(row.get("transitionUuid") or "").strip(),
            )
            for row in self._mapping_list(transition_response.get("transitions"))
            if str(row.get("transitionName") or "").strip()
        )

        canvas: tuple[int, int] | None = None
        try:
            video = self._send("GetVideoSettings")
            width = int(video.get("baseWidth") or 0)
            height = int(video.get("baseHeight") or 0)
            if width > 0 and height > 0:
                canvas = (width, height)
        except OBSRequestError as exc:
            warnings.append(f"Video settings unavailable: {exc}")

        final_collection = self.current_collection()
        if collection and final_collection and final_collection != collection:
            raise RuntimeError(
                "OBS Scene Collection changed during catalog synchronization: "
                f"{collection} -> {final_collection}"
            )
        if self._session_generation() != session_generation:
            raise RuntimeError("OBS session changed during catalog synchronization")

        return OBSResourceCatalog(
            collection=collection,
            current_program_scene=current_program_scene,
            current_program_scene_uuid=current_program_scene_uuid,
            canvas=canvas,
            scenes=scenes,
            groups=groups,
            scene_items=tuple(
                sorted(
                    scene_items,
                    key=lambda item: (
                        item.container.casefold(),
                        item.source.casefold(),
                        item.occurrence,
                    ),
                )
            ),
            inputs=inputs,
            transitions=transitions,
            available_requests=available_requests,
            warnings=tuple(warnings),
            unreadable_containers=frozenset(unreadable_containers),
            session_generation=session_generation,
        )

    def input_details(self, input_name: str) -> InputDetails:
        name = str(input_name or "").strip()
        if not name:
            raise ValueError("input_name is required")

        settings_response = self._send(
            "GetInputSettings",
            {"inputName": name},
        )
        settings = settings_response.get("inputSettings") or {}
        if not isinstance(settings, Mapping):
            settings = {}
        kind = str(settings_response.get("inputKind") or "").strip()

        filters = self.filters_for_source(name)
        return InputDetails(
            input=InputRef(
                name=name,
                kind=kind,
                uuid=str(settings_response.get("inputUuid") or "").strip(),
            ),
            settings=dict(settings),
            filters=filters,
        )

    def filters_for_source(self, source_name: str) -> tuple[FilterRef, ...]:
        source = str(source_name or "").strip()
        if not source:
            raise ValueError("source_name is required")
        response = self._send(
            "GetSourceFilterList",
            {"sourceName": source},
        )
        return tuple(
            FilterRef(
                source=source,
                name=str(row.get("filterName") or "").strip(),
                kind=str(row.get("filterKind") or "").strip(),
                enabled=(
                    bool(row.get("filterEnabled"))
                    if "filterEnabled" in row
                    else None
                ),
            )
            for row in self._mapping_list(response.get("filters"))
            if str(row.get("filterName") or "").strip()
        )

    def filter_details(self, source_name: str, filter_name: str) -> FilterDetails:
        source = str(source_name or "").strip()
        name = str(filter_name or "").strip()
        if not source or not name:
            raise ValueError("source_name and filter_name are required")
        response = self._send(
            "GetSourceFilter",
            {"sourceName": source, "filterName": name},
        )
        settings = response.get("filterSettings") or {}
        if not isinstance(settings, Mapping):
            settings = {}
        ref = FilterRef(
            source=source,
            name=name,
            kind=str(response.get("filterKind") or "").strip(),
            enabled=(
                bool(response.get("filterEnabled"))
                if "filterEnabled" in response
                else None
            ),
        )
        return FilterDetails(filter=ref, settings=dict(settings))

    @staticmethod
    def _append_container_items(
        out: list[SceneItemRef],
        *,
        collection: str,
        root_scene: str,
        container: str,
        container_kind: str,
        path: tuple[str, ...],
        rows: list[Mapping[str, Any]],
    ) -> None:
        occurrences: dict[str, int] = {}
        for row in rows:
            # Export-only backup rows are not independently controllable items.
            if bool(
                row.get("groupItemBackup", False)
                or row.get("group_item_backup", False)
            ):
                continue
            source = str(row.get("sourceName") or "").strip()
            if not source:
                continue
            occurrence = occurrences.get(source, 0)
            occurrences[source] = occurrence + 1
            is_group = bool(row.get("isGroup", False))
            source_type = str(row.get("sourceType") or "")
            input_kind = str(row.get("inputKind") or "")
            is_scene = (
                not is_group
                and (
                    source_type == "OBS_SOURCE_TYPE_SCENE"
                    or input_kind == "scene"
                )
            )
            source_kind = "group" if is_group else ("scene" if is_scene else "input")
            scene_item_id: int | None = None
            if "sceneItemId" in row:
                try:
                    scene_item_id = int(row.get("sceneItemId"))
                except (TypeError, ValueError, OverflowError):
                    scene_item_id = None
            out.append(
                SceneItemRef(
                    collection=collection,
                    root_scene=root_scene,
                    container=container,
                    container_kind=container_kind,
                    path=path,
                    source=source,
                    source_uuid=str(row.get("sourceUuid") or "").strip(),
                    source_kind=source_kind,
                    occurrence=occurrence,
                    scene_item_id=scene_item_id,
                    enabled=(
                        bool(row.get("sceneItemEnabled"))
                        if "sceneItemEnabled" in row
                        else None
                    ),
                )
            )
