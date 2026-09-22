from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from ..planning.models import DesiredState, ObservedState, ObservedValue, PropertyKey
from .catalog import OBSResourceCatalog
from .client import OBSClientManager, OBSRequestError



@dataclass(frozen=True, slots=True)
class SceneItemBinding:
    key: PropertyKey
    container_uuid: str
    source_uuid: str
    scene_item_id: int
    occurrence_fingerprint: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class InputBinding:
    key: PropertyKey
    input_uuid: str


ExecutionBinding = SceneItemBinding | InputBinding


def build_execution_bindings(
    catalog: OBSResourceCatalog,
    desired: DesiredState,
) -> tuple[ExecutionBinding, ...]:
    """Bind the MVP executable properties to verified physical OBS identities.

    These bindings are snapshots, not durable permissions.  The executor must
    revalidate them immediately before every mutation and acknowledgement.
    """

    scenes = {item.name: item for item in catalog.scenes}
    inputs = {item.name: item for item in catalog.inputs}
    bindings: list[ExecutionBinding] = []

    for assignment in desired.assignments:
        key = assignment.key
        if key.kind == "scene_item_visibility":
            if key.container in catalog.groups:
                raise ValueError(
                    "Group scene-item containers are not executable in the MVP"
                )
            scene = scenes.get(key.container)
            if scene is None or not scene.uuid:
                raise ValueError(
                    f"Scene container '{key.container}' has no stable UUID binding"
                )
            refs = sorted(
                (
                    item
                    for item in catalog.scene_items
                    if item.container == key.container and item.source == key.source
                ),
                key=lambda item: item.occurrence,
            )
            if key.occurrence >= len(refs):
                raise ValueError(
                    f"Scene-item occurrence {key.occurrence} is not bound"
                )
            expected_occurrences = list(range(len(refs)))
            actual_occurrences = [item.occurrence for item in refs]
            if actual_occurrences != expected_occurrences:
                raise ValueError("Scene-item occurrence set is incomplete or ambiguous")
            if any(item.scene_item_id is None or not item.source_uuid for item in refs):
                raise ValueError("Scene-item physical identity is incomplete")
            reference = refs[key.occurrence]
            bindings.append(
                SceneItemBinding(
                    key=key,
                    container_uuid=scene.uuid,
                    source_uuid=reference.source_uuid,
                    scene_item_id=int(reference.scene_item_id),
                    occurrence_fingerprint=tuple(
                        (int(item.scene_item_id), item.source_uuid)
                        for item in refs
                    ),
                )
            )
            continue

        if key.kind in {"input_mute", "input_volume_db"}:
            input_ref = inputs.get(key.source)
            if input_ref is None or not input_ref.uuid:
                raise ValueError(
                    f"Input '{key.source}' has no stable UUID binding"
                )
            bindings.append(InputBinding(key=key, input_uuid=input_ref.uuid))
            continue

        raise ValueError(
            f"Property kind '{key.kind}' is outside the declarative executor MVP allowlist"
        )

    return tuple(bindings)


def observe_desired_state(
    client: OBSClientManager,
    catalog: OBSResourceCatalog,
    desired: DesiredState,
    *,
    cooperative_yield: Callable[[], None] | None = None,
) -> ObservedState:
    """Read only the physical values needed by one desired state.

    The planner itself remains pure.  This adapter is deliberately targeted and
    never scans unrelated input/filter settings.
    """
    values: dict[PropertyKey, ObservedValue] = {}
    input_cache: dict[str, Mapping[str, Any] | None] = {}
    filter_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}

    def send(
        request: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if cooperative_yield is not None:
            cooperative_yield()
        return client.send(request, data)
    for assignment in desired.assignments:
        key = assignment.key
        if key.collection and catalog.collection and key.collection != catalog.collection:
            values[key] = ObservedValue.unknown()
            continue

        if key.kind == "program_scene":
            try:
                response = send("GetCurrentProgramScene")
            except OBSRequestError:
                values[key] = ObservedValue.unknown()
            else:
                scene = str(
                    response.get("sceneName")
                    or response.get("currentProgramSceneName")
                    or ""
                ).strip()
                values[key] = (
                    ObservedValue.known_value(scene)
                    if scene
                    else ObservedValue.unknown()
                )
            continue

        if key.kind == "scene_item_visibility":
            catalog_refs = [
                item
                for item in catalog.scene_items
                if item.container == key.container and item.source == key.source
            ]
            reference = next(
                (
                    item
                    for item in catalog_refs
                    if item.occurrence == key.occurrence
                ),
                None,
            )
            if reference is None:
                values[key] = ObservedValue.unknown(
                    code="scene_item_reference_missing",
                    reason=(
                        "Scene-item reference is not present in the current "
                        "catalog snapshot."
                    ),
                )
                continue
            request = (
                "GetGroupSceneItemList"
                if reference.container_kind == "group"
                else "GetSceneItemList"
            )
            try:
                listing = send(request, {"sceneName": key.container})
                rows = [
                    row
                    for row in listing.get("sceneItems", []) or []
                    if isinstance(row, Mapping)
                    and not bool(
                        row.get("groupItemBackup", False)
                        or row.get("group_item_backup", False)
                    )
                    and str(row.get("sourceName") or "").strip() == key.source
                ]
            except OBSRequestError:
                values[key] = ObservedValue.unknown(
                    code="scene_item_unreadable",
                    reason="Scene-item container could not be read from OBS.",
                )
                continue

            if len(rows) != len(catalog_refs) or key.occurrence >= len(rows):
                values[key] = ObservedValue.unknown(
                    code="stale_reference",
                    reason=(
                        "Scene-item duplicate cardinality changed since catalog "
                        "synchronization; occurrence binding is stale."
                    ),
                )
                continue

            row = rows[key.occurrence]
            item_id: int | None = None
            if "sceneItemId" in row:
                try:
                    item_id = int(row.get("sceneItemId"))
                except (TypeError, ValueError, OverflowError):
                    item_id = None
            candidate_uuid = str(row.get("sourceUuid") or "").strip()
            if (
                reference.source_uuid
                and candidate_uuid
                and candidate_uuid != reference.source_uuid
            ):
                values[key] = ObservedValue.unknown(
                    code="stale_reference",
                    reason="Scene-item source identity changed since catalog synchronization.",
                )
                continue
            if (
                reference.scene_item_id is not None
                and item_id is not None
                and item_id != reference.scene_item_id
            ):
                values[key] = ObservedValue.unknown(
                    code="stale_reference",
                    reason=(
                        "Scene-item occurrence order or physical identity changed "
                        "since catalog synchronization."
                    ),
                )
                continue
            if item_id is None:
                values[key] = ObservedValue.unknown(
                    code="scene_item_id_unknown",
                    reason="OBS did not provide a scene-item id for the bound occurrence.",
                )
                continue

            try:
                response = send(
                    "GetSceneItemEnabled",
                    {
                        "sceneName": key.container,
                        "sceneItemId": item_id,
                    },
                )
            except OBSRequestError:
                values[key] = ObservedValue.unknown(
                    code="scene_item_unreadable",
                    reason="Scene-item visibility could not be read from OBS.",
                )
            else:
                raw_enabled = response.get("sceneItemEnabled")
                if isinstance(raw_enabled, bool):
                    values[key] = ObservedValue.known_value(raw_enabled)
                else:
                    values[key] = ObservedValue.unknown(
                        code="scene_item_visibility_unknown",
                        reason=(
                            "OBS omitted sceneItemEnabled or returned a non-boolean "
                            "value for the bound occurrence."
                        ),
                    )
            continue

        if key.kind == "input_mute":
            try:
                response = send(
                    "GetInputMute",
                    {"inputName": key.source},
                )
            except OBSRequestError:
                values[key] = ObservedValue.unknown()
            else:
                raw_muted = response.get("inputMuted")
                if isinstance(raw_muted, bool):
                    values[key] = ObservedValue.known_value(raw_muted)
                else:
                    values[key] = ObservedValue.unknown(
                        code="input_mute_unknown",
                        reason="OBS omitted inputMuted or returned a non-boolean value.",
                    )
            continue

        if key.kind == "input_volume_db":
            try:
                response = send(
                    "GetInputVolume",
                    {"inputName": key.source},
                )
            except OBSRequestError:
                values[key] = ObservedValue.unknown(
                    code="input_volume_unknown",
                    reason="OBS rejected GetInputVolume.",
                )
            else:
                raw_volume = response.get("inputVolumeDb")
                if (
                    isinstance(raw_volume, bool)
                    or not isinstance(raw_volume, (int, float))
                    or not math.isfinite(float(raw_volume))
                ):
                    values[key] = ObservedValue.unknown(
                        code="input_volume_unknown",
                        reason=(
                            "OBS omitted inputVolumeDb or returned a non-finite "
                            "numeric value."
                        ),
                    )
                else:
                    values[key] = ObservedValue.known_value(float(raw_volume))
            continue

        if key.kind == "input_setting":
            settings = input_cache.get(key.source)
            if key.source not in input_cache:
                try:
                    response = send(
                        "GetInputSettings",
                        {"inputName": key.source},
                    )
                    raw = response.get("inputSettings") or {}
                    settings = dict(raw) if isinstance(raw, Mapping) else None
                except OBSRequestError:
                    settings = None
                input_cache[key.source] = settings
            if settings is not None and key.setting in settings:
                values[key] = ObservedValue.known_value(settings[key.setting])
            else:
                values[key] = ObservedValue.unknown()
            continue

        if key.kind in {"filter_enabled", "filter_setting"}:
            identity = (key.source, key.filter_name)
            response = filter_cache.get(identity)
            if identity not in filter_cache:
                try:
                    raw_response = send(
                        "GetSourceFilter",
                        {
                            "sourceName": key.source,
                            "filterName": key.filter_name,
                        },
                    )
                    response = dict(raw_response)
                except OBSRequestError:
                    response = None
                filter_cache[identity] = response

            if response is None:
                values[key] = ObservedValue.unknown()
                continue
            if key.kind == "filter_enabled":
                raw_enabled = response.get("filterEnabled")
                if isinstance(raw_enabled, bool):
                    values[key] = ObservedValue.known_value(raw_enabled)
                else:
                    values[key] = ObservedValue.unknown()
                continue
            settings = response.get("filterSettings") or {}
            if isinstance(settings, Mapping) and key.setting in settings:
                values[key] = ObservedValue.known_value(settings[key.setting])
            else:
                values[key] = ObservedValue.unknown()
            continue

        # Layout convergence is intentionally not inferred from geometry here.
        # OBSLayoutManager / future AppliedState integration owns that evidence.
        values[key] = ObservedValue.unknown()

    return ObservedState(values)
