from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


class OBSReadClient(Protocol):
    """Minimal read-only contract used by the OBS catalogue."""

    @property
    def request_count(self) -> int: ...

    def send(self, request: str, data: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OBSSceneRef:
    name: str
    uuid: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {"name": self.name, "uuid": self.uuid}


@dataclass(frozen=True, slots=True)
class OBSSceneItemRef:
    root_scene: str
    container: str
    container_kind: str
    path: tuple[str, ...]
    source_name: str
    source_kind: str
    input_kind: str = ""
    source_uuid: str = ""
    scene_item_id: int = 0
    occurrence: int = 1
    enabled: bool | None = None
    locked: bool | None = None

    @property
    def identity(self) -> tuple[str, str, int]:
        """Physical item identity without persisting ephemeral scene-item ids.

        root_scene/path are navigation context only. A group or nested scene may
        be referenced from several parents while its internal item remains the
        same OBS occurrence.
        """
        return (self.container, self.source_name, self.occurrence)

    def as_mapping(self) -> dict[str, object]:
        return {
            "root_scene": self.root_scene,
            "container": self.container,
            "container_kind": self.container_kind,
            "path": list(self.path),
            "source_name": self.source_name,
            "source_kind": self.source_kind,
            "input_kind": self.input_kind,
            "source_uuid": self.source_uuid,
            "scene_item_id": self.scene_item_id,
            "occurrence": self.occurrence,
            "enabled": self.enabled,
            "locked": self.locked,
        }


@dataclass(frozen=True, slots=True)
class OBSInputRef:
    name: str
    kind: str = ""
    uuid: str = ""
    settings: Mapping[str, object] | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "uuid": self.uuid,
            "settings": dict(self.settings) if self.settings is not None else None,
        }


@dataclass(frozen=True, slots=True)
class OBSFilterRef:
    source_name: str
    name: str
    kind: str = ""
    enabled: bool | None = None
    index: int | None = None
    settings: Mapping[str, object] | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.source_name, self.name)

    def as_mapping(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "index": self.index,
            "settings": dict(self.settings) if self.settings is not None else None,
        }


@dataclass(frozen=True, slots=True)
class OBSTransitionRef:
    name: str
    kind: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class OBSResourceCatalog:
    scene_collection: str = ""
    current_program_scene: str = ""
    canvas_width: int = 0
    canvas_height: int = 0
    scenes: tuple[OBSSceneRef, ...] = ()
    scene_items: tuple[OBSSceneItemRef, ...] = ()
    inputs: tuple[OBSInputRef, ...] = ()
    filters: tuple[OBSFilterRef, ...] = ()
    transitions: tuple[OBSTransitionRef, ...] = ()
    warnings: tuple[str, ...] = ()
    requests_used: int = 0

    def as_mapping(self) -> dict[str, object]:
        return {
            "scene_collection": self.scene_collection,
            "current_program_scene": self.current_program_scene,
            "canvas": {"width": self.canvas_width, "height": self.canvas_height},
            "scenes": [item.as_mapping() for item in self.scenes],
            "scene_items": [item.as_mapping() for item in self.scene_items],
            "inputs": [item.as_mapping() for item in self.inputs],
            "filters": [item.as_mapping() for item in self.filters],
            "transitions": [item.as_mapping() for item in self.transitions],
            "warnings": list(self.warnings),
            "requests_used": self.requests_used,
        }

    def summary(self) -> dict[str, object]:
        return {
            "scene_collection": self.scene_collection,
            "current_program_scene": self.current_program_scene,
            "canvas": {"width": self.canvas_width, "height": self.canvas_height},
            "scene_count": len(self.scenes),
            "scene_item_count": len(self.scene_items),
            "input_count": len(self.inputs),
            "filter_count": len(self.filters),
            "transition_count": len(self.transitions),
            "warnings": list(self.warnings),
            "requests_used": self.requests_used,
        }


@dataclass(slots=True)
class _CatalogBuildState:
    warnings: list[str] = field(default_factory=list)
    group_cache: dict[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)


class OBSResourceCatalogReader:
    """Build an explicit, read-only snapshot of resources exposed by OBS.

    Synchronisation is deliberately explicit. This class never schedules itself,
    never mutates OBS, and never scans on the foreground-routing tick.
    """

    def __init__(
        self,
        client: OBSReadClient,
        *,
        cooperative_yield: Callable[[], None] | None = None,
    ):
        self.client = client
        self._cooperative_yield = cooperative_yield

    def _send(self, request: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._cooperative_yield is not None:
            self._cooperative_yield()
        return self._send(request, data)

    def sync(self, *, include_settings: bool = False) -> OBSResourceCatalog:
        before = self._request_count()
        state = _CatalogBuildState()

        scene_collection = self._scene_collection(state)
        scenes, current_program = self._scenes(state)
        scene_items = self._scene_items(scenes, state)
        inputs = self._inputs(state, include_settings=include_settings)
        filters = self._filters(
            inputs,
            scenes,
            scene_items,
            state,
            include_settings=include_settings,
        )
        transitions = self._transitions(state)
        canvas_width, canvas_height = self._video_settings(state)

        after = self._request_count()
        requests_used = max(0, after - before) if before >= 0 and after >= 0 else 0
        return OBSResourceCatalog(
            scene_collection=scene_collection,
            current_program_scene=current_program,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            scenes=tuple(sorted(scenes, key=lambda item: (item.name.casefold(), item.uuid))),
            scene_items=tuple(
                sorted(
                    scene_items,
                    key=lambda item: (
                        item.root_scene.casefold(),
                        item.path,
                        item.container.casefold(),
                        item.source_name.casefold(),
                        item.occurrence,
                    ),
                )
            ),
            inputs=tuple(sorted(inputs, key=lambda item: (item.name.casefold(), item.kind))),
            filters=tuple(
                sorted(
                    filters,
                    key=lambda item: (item.source_name.casefold(), item.name.casefold()),
                )
            ),
            transitions=tuple(sorted(transitions, key=lambda item: item.name.casefold())),
            warnings=tuple(state.warnings),
            requests_used=requests_used,
        )

    def _request_count(self) -> int:
        try:
            return int(getattr(self.client, "request_count"))
        except (AttributeError, TypeError, ValueError):
            return -1

    def _scene_collection(self, state: _CatalogBuildState) -> str:
        try:
            response = self._send("GetSceneCollectionList")
        except Exception as exc:
            state.warnings.append(f"Scene Collection non lisible : {exc}")
            return ""
        return str(response.get("currentSceneCollectionName") or "")

    def _scenes(self, state: _CatalogBuildState) -> tuple[list[OBSSceneRef], str]:
        try:
            response = self._send("GetSceneList")
        except Exception as exc:
            state.warnings.append(f"Liste des scènes non lisible : {exc}")
            return [], ""
        scenes: list[OBSSceneRef] = []
        for raw in response.get("scenes", []) or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("sceneName") or "").strip()
            if not name:
                continue
            scenes.append(OBSSceneRef(name=name, uuid=str(raw.get("sceneUuid") or "")))
        return scenes, str(response.get("currentProgramSceneName") or "")

    def _scene_items(
        self,
        scenes: list[OBSSceneRef],
        state: _CatalogBuildState,
    ) -> list[OBSSceneItemRef]:
        out: list[OBSSceneItemRef] = []
        for scene in scenes:
            try:
                response = self._send("GetSceneItemList", {"sceneName": scene.name})
            except Exception as exc:
                state.warnings.append(f"Scène '{scene.name}' non lisible : {exc}")
                continue
            raw_items = tuple(
                raw for raw in response.get("sceneItems", []) or [] if isinstance(raw, Mapping)
            )
            self._walk_container(
                root_scene=scene.name,
                container=scene.name,
                container_kind="scene",
                path=(scene.name,),
                raw_items=raw_items,
                out=out,
                state=state,
                depth=0,
            )
        return out

    def _walk_container(
        self,
        *,
        root_scene: str,
        container: str,
        container_kind: str,
        path: tuple[str, ...],
        raw_items: tuple[Mapping[str, Any], ...],
        out: list[OBSSceneItemRef],
        state: _CatalogBuildState,
        depth: int,
    ) -> None:
        if depth > 8:
            state.warnings.append(f"Profondeur maximale atteinte sous '{container}'")
            return
        occurrences: dict[str, int] = {}
        for raw in raw_items:
            if self._is_backup_item(raw):
                continue
            source_name = str(raw.get("sourceName") or "").strip()
            if not source_name:
                continue
            occurrences[source_name] = occurrences.get(source_name, 0) + 1
            is_group = bool(raw.get("isGroup", False))
            source_type = str(raw.get("sourceType") or "")
            input_kind = str(raw.get("inputKind") or "")
            is_scene = not is_group and (
                source_type == "OBS_SOURCE_TYPE_SCENE" or input_kind == "scene"
            )
            source_kind = "group" if is_group else ("scene" if is_scene else "input")
            out.append(
                OBSSceneItemRef(
                    root_scene=root_scene,
                    container=container,
                    container_kind=container_kind,
                    path=path,
                    source_name=source_name,
                    source_kind=source_kind,
                    input_kind=input_kind,
                    source_uuid=str(raw.get("sourceUuid") or ""),
                    scene_item_id=self._int(raw.get("sceneItemId")),
                    occurrence=occurrences[source_name],
                    enabled=self._optional_bool(raw, "sceneItemEnabled"),
                    locked=self._optional_bool(raw, "sceneItemLocked"),
                )
            )
            if not is_group:
                # Nested scenes are represented as their own scene resources and
                # are scanned from GetSceneList. Recursing here would duplicate
                # their children once per parent occurrence.
                continue
            children = state.group_cache.get(source_name)
            if children is None:
                try:
                    response = self._send(
                        "GetGroupSceneItemList",
                        {"sceneName": source_name},
                    )
                    children = tuple(
                        item
                        for item in response.get("sceneItems", []) or []
                        if isinstance(item, Mapping)
                    )
                    state.group_cache[source_name] = children
                except Exception as exc:
                    state.warnings.append(
                        f"Groupe '{source_name}' non lisible depuis '{container}' : {exc}"
                    )
                    continue
            self._walk_container(
                root_scene=root_scene,
                container=source_name,
                container_kind="group",
                path=(*path, source_name),
                raw_items=children,
                out=out,
                state=state,
                depth=depth + 1,
            )

    def _inputs(
        self,
        state: _CatalogBuildState,
        *,
        include_settings: bool,
    ) -> list[OBSInputRef]:
        try:
            response = self._send("GetInputList")
        except Exception as exc:
            state.warnings.append(f"Liste des inputs non lisible : {exc}")
            return []
        out: list[OBSInputRef] = []
        for raw in response.get("inputs", []) or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("inputName") or "").strip()
            if not name:
                continue
            settings: Mapping[str, object] | None = None
            if include_settings:
                try:
                    details = self._send("GetInputSettings", {"inputName": name})
                    raw_settings = details.get("inputSettings")
                    if isinstance(raw_settings, Mapping):
                        settings = dict(raw_settings)
                except Exception as exc:
                    state.warnings.append(f"Settings input '{name}' non lisibles : {exc}")
            out.append(
                OBSInputRef(
                    name=name,
                    kind=str(raw.get("inputKind") or ""),
                    uuid=str(raw.get("inputUuid") or ""),
                    settings=settings,
                )
            )
        return out

    def _filters(
        self,
        inputs: list[OBSInputRef],
        scenes: list[OBSSceneRef],
        scene_items: list[OBSSceneItemRef],
        state: _CatalogBuildState,
        *,
        include_settings: bool,
    ) -> list[OBSFilterRef]:
        # OBS filters may be attached to input sources, scenes or groups. The
        # Midgar collection notably keeps its layout MOVE filters on a scene,
        # so limiting discovery to GetInputList would silently miss them.
        source_names = {item.name for item in inputs}
        source_names.update(scene.name for scene in scenes)
        source_names.update(
            item.source_name for item in scene_items if item.source_kind == "group"
        )

        out: list[OBSFilterRef] = []
        for source_name in sorted(source_names, key=str.casefold):
            try:
                response = self._send(
                    "GetSourceFilterList",
                    {"sourceName": source_name},
                )
            except Exception as exc:
                state.warnings.append(f"Filtres de '{source_name}' non lisibles : {exc}")
                continue
            for raw in response.get("filters", []) or []:
                if not isinstance(raw, Mapping):
                    continue
                name = str(raw.get("filterName") or "").strip()
                if not name:
                    continue
                settings: Mapping[str, object] | None = None
                raw_settings = raw.get("filterSettings")
                if include_settings and isinstance(raw_settings, Mapping):
                    settings = dict(raw_settings)
                if include_settings and settings is None:
                    try:
                        details = self._send(
                            "GetSourceFilter",
                            {"sourceName": source_name, "filterName": name},
                        )
                        detailed_settings = details.get("filterSettings")
                        if isinstance(detailed_settings, Mapping):
                            settings = dict(detailed_settings)
                    except Exception as exc:
                        state.warnings.append(
                            f"Settings filtre '{source_name}/{name}' non lisibles : {exc}"
                        )
                out.append(
                    OBSFilterRef(
                        source_name=source_name,
                        name=name,
                        kind=str(raw.get("filterKind") or ""),
                        enabled=self._optional_bool(raw, "filterEnabled"),
                        index=self._optional_int(raw.get("filterIndex")),
                        settings=settings,
                    )
                )
        return out

    def _transitions(self, state: _CatalogBuildState) -> list[OBSTransitionRef]:
        try:
            response = self._send("GetSceneTransitionList")
        except Exception as exc:
            state.warnings.append(f"Transitions non lisibles : {exc}")
            return []
        out: list[OBSTransitionRef] = []
        for raw in response.get("transitions", []) or []:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("transitionName") or "").strip()
            if name:
                out.append(OBSTransitionRef(name=name, kind=str(raw.get("transitionKind") or "")))
        return out

    def _video_settings(self, state: _CatalogBuildState) -> tuple[int, int]:
        try:
            response = self._send("GetVideoSettings")
        except Exception as exc:
            state.warnings.append(f"Canvas OBS non lisible : {exc}")
            return 0, 0
        return self._int(response.get("baseWidth")), self._int(response.get("baseHeight"))

    @staticmethod
    def _is_backup_item(raw: Mapping[str, Any]) -> bool:
        return bool(raw.get("groupItemBackup", raw.get("group_item_backup", False)))

    @staticmethod
    def _int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _optional_int(cls, value: object) -> int | None:
        if value is None:
            return None
        return cls._int(value)

    @staticmethod
    def _optional_bool(raw: Mapping[str, Any], key: str) -> bool | None:
        if key not in raw:
            return None
        return bool(raw.get(key))
