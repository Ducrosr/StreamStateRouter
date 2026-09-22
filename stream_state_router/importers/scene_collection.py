from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from ..obs.catalog import OBSResourceCatalogReader
from ..obs.client import OBSClientManager, OBSRequestError


@dataclass(frozen=True, slots=True)
class ImportedInput:
    name: str
    kind: str
    uuid: str
    settings: Mapping[str, Any]
    muted: bool | None
    volume_db: float | None


@dataclass(frozen=True, slots=True)
class ImportedFilter:
    source: str
    name: str
    kind: str
    enabled: bool | None
    settings: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ImportedSceneItem:
    scene: str
    source: str
    occurrence: int
    enabled: bool | None


@dataclass(frozen=True, slots=True)
class SceneCollectionSnapshot:
    collection: str
    current_program_scene: str
    inputs: tuple[ImportedInput, ...]
    filters: tuple[ImportedFilter, ...]
    scene_items: tuple[ImportedSceneItem, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionImportReport:
    collection: str
    target_domain: str
    target_profile: str
    added_actions: int
    replaced_actions: int
    skipped: tuple[str, ...]

    def summary(self) -> str:
        lines = [
            f"Collection : {self.collection}",
            f"Cible : {self.target_domain}/{self.target_profile}",
            f"Actions ajoutées : {self.added_actions}",
            f"Actions remplacées : {self.replaced_actions}",
            f"Éléments ignorés : {len(self.skipped)}",
        ]
        if self.skipped:
            lines.append("")
            lines.extend(f"- {item}" for item in self.skipped)
        return "\n".join(lines)


def _action_identity(action: Mapping[str, Any]) -> tuple[str, ...]:
    kind = str(action.get("type") or "")
    params = action.get("params")
    values = params if isinstance(params, Mapping) else {}
    if kind == "set_input_settings":
        return (kind, str(values.get("input") or ""))
    if kind in {"input_mute", "input_volume_db"}:
        return (kind, str(values.get("input") or ""))
    if kind in {"source_filter_enabled", "source_filter_settings"}:
        return (
            kind,
            str(values.get("source") or ""),
            str(values.get("filter") or ""),
        )
    if kind == "scene_item_enabled":
        return (
            kind,
            str(values.get("scene") or ""),
            str(values.get("source") or ""),
        )
    return (kind,)


class SceneCollectionImporter:
    """Snapshot the current OBS collection and project stable state into SSR.

    Layout geometry remains owned by the existing LayoutProfile capture path.
    This importer focuses on settings/state that can be represented by profile
    actions without inventing ambiguous scene-item identities.
    """

    def __init__(self, client: OBSClientManager) -> None:
        self.client = client
        self.reader = OBSResourceCatalogReader(client)

    def snapshot(self) -> SceneCollectionSnapshot:
        catalog = self.reader.sync()
        warnings = list(catalog.warnings)
        inputs: list[ImportedInput] = []
        filters: dict[tuple[str, str], ImportedFilter] = {}

        for input_ref in catalog.inputs:
            settings: Mapping[str, Any] = {}
            kind = input_ref.kind
            try:
                details = self.reader.input_details(input_ref.name)
                settings = dict(details.settings)
                kind = details.input.kind or kind
            except OBSRequestError as exc:
                warnings.append(
                    f"Input '{input_ref.name}' settings unreadable: {exc}"
                )

            muted: bool | None = None
            try:
                response = self.client.send(
                    "GetInputMute",
                    {"inputUuid": input_ref.uuid}
                    if input_ref.uuid
                    else {"inputName": input_ref.name},
                )
                if isinstance(response.get("inputMuted"), bool):
                    muted = bool(response["inputMuted"])
            except OBSRequestError as exc:
                warnings.append(
                    f"Input '{input_ref.name}' mute unreadable: {exc}"
                )

            volume_db: float | None = None
            try:
                response = self.client.send(
                    "GetInputVolume",
                    {"inputUuid": input_ref.uuid}
                    if input_ref.uuid
                    else {"inputName": input_ref.name},
                )
                raw = response.get("inputVolumeDb")
                if (
                    not isinstance(raw, bool)
                    and isinstance(raw, (int, float))
                    and math.isfinite(float(raw))
                ):
                    volume_db = float(raw)
            except OBSRequestError as exc:
                warnings.append(
                    f"Input '{input_ref.name}' volume unreadable: {exc}"
                )

            inputs.append(
                ImportedInput(
                    name=input_ref.name,
                    kind=kind,
                    uuid=input_ref.uuid,
                    settings=dict(settings),
                    muted=muted,
                    volume_db=volume_db,
                )
            )

        candidate_sources = {
            *(item.name for item in catalog.inputs),
            *(item.name for item in catalog.scenes),
            *catalog.groups,
            *(item.source for item in catalog.scene_items),
        }
        for source in sorted(candidate_sources, key=str.casefold):
            try:
                refs = self.reader.filters_for_source(source)
            except OBSRequestError:
                continue
            for ref in refs:
                identity = (source, ref.name)
                if identity in filters:
                    continue
                try:
                    details = self.reader.filter_details(source, ref.name)
                    settings = dict(details.settings)
                    filter_ref = details.filter
                except OBSRequestError as exc:
                    warnings.append(
                        f"Filter '{source}/{ref.name}' settings unreadable: {exc}"
                    )
                    settings = {}
                    filter_ref = ref
                filters[identity] = ImportedFilter(
                    source=source,
                    name=ref.name,
                    kind=filter_ref.kind,
                    enabled=filter_ref.enabled,
                    settings=settings,
                )

        scene_items = tuple(
            ImportedSceneItem(
                scene=item.container,
                source=item.source,
                occurrence=item.occurrence,
                enabled=item.enabled,
            )
            for item in catalog.scene_items
            if item.container_kind == "scene"
        )

        final_collection = self.reader.current_collection()
        if (
            catalog.collection
            and final_collection
            and final_collection != catalog.collection
        ):
            raise RuntimeError(
                "OBS Scene Collection changed during import: "
                f"{catalog.collection} -> {final_collection}"
            )

        return SceneCollectionSnapshot(
            collection=catalog.collection,
            current_program_scene=catalog.current_program_scene,
            inputs=tuple(inputs),
            filters=tuple(
                filters[key]
                for key in sorted(
                    filters,
                    key=lambda value: (
                        value[0].casefold(),
                        value[1].casefold(),
                    ),
                )
            ),
            scene_items=scene_items,
            warnings=tuple(warnings),
        )

    @staticmethod
    def actions_from_snapshot(
        snapshot: SceneCollectionSnapshot,
        *,
        include_input_settings: bool = True,
        include_audio_state: bool = True,
        include_filters: bool = True,
        include_visibility: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        actions: list[dict[str, Any]] = []
        skipped: list[str] = []
        prefix = f"Import {snapshot.collection}".strip()

        for item in snapshot.inputs:
            if include_input_settings and item.settings:
                actions.append(
                    {
                        "type": "set_input_settings",
                        "name": f"{prefix} · settings · {item.name}",
                        "enabled": True,
                        "params": {
                            "input": item.name,
                            "settings": dict(item.settings),
                            "overlay": True,
                        },
                    }
                )
            if include_audio_state and item.muted is not None:
                actions.append(
                    {
                        "type": "input_mute",
                        "name": f"{prefix} · mute · {item.name}",
                        "enabled": True,
                        "params": {
                            "input": item.name,
                            "muted": item.muted,
                        },
                    }
                )
            if include_audio_state and item.volume_db is not None:
                if -100.0 <= item.volume_db <= 26.0:
                    actions.append(
                        {
                            "type": "input_volume_db",
                            "name": f"{prefix} · volume · {item.name}",
                            "enabled": True,
                            "params": {
                                "input": item.name,
                                "volume_db": item.volume_db,
                            },
                        }
                    )
                else:
                    skipped.append(
                        f"Volume {item.name}={item.volume_db:.4f} dB hors plage "
                        "écrivable SSR [-100,+26]"
                    )

        if include_filters:
            for item in snapshot.filters:
                if item.enabled is not None:
                    actions.append(
                        {
                            "type": "source_filter_enabled",
                            "name": (
                                f"{prefix} · filtre actif · "
                                f"{item.source}/{item.name}"
                            ),
                            "enabled": True,
                            "params": {
                                "source": item.source,
                                "filter": item.name,
                                "enabled": item.enabled,
                            },
                        }
                    )
                if item.settings:
                    actions.append(
                        {
                            "type": "source_filter_settings",
                            "name": (
                                f"{prefix} · filtre settings · "
                                f"{item.source}/{item.name}"
                            ),
                            "enabled": True,
                            "params": {
                                "source": item.source,
                                "filter": item.name,
                                "settings": dict(item.settings),
                                "overlay": True,
                            },
                        }
                    )

        if include_visibility:
            counts: dict[tuple[str, str], int] = {}
            for item in snapshot.scene_items:
                identity = (item.scene, item.source)
                counts[identity] = counts.get(identity, 0) + 1
            for item in snapshot.scene_items:
                identity = (item.scene, item.source)
                if counts[identity] != 1:
                    skipped.append(
                        "Visibilité ambiguë ignorée : "
                        f"{item.scene}/{item.source} apparaît {counts[identity]} fois"
                    )
                    continue
                if item.enabled is None:
                    skipped.append(
                        "Visibilité inconnue ignorée : "
                        f"{item.scene}/{item.source}"
                    )
                    continue
                actions.append(
                    {
                        "type": "scene_item_enabled",
                        "name": (
                            f"{prefix} · visibilité · "
                            f"{item.scene}/{item.source}"
                        ),
                        "enabled": True,
                        "params": {
                            "scene": item.scene,
                            "source": item.source,
                            "enabled": item.enabled,
                        },
                    }
                )

        return actions, skipped

    @staticmethod
    def merge_actions_into_profile(
        config: dict[str, Any],
        *,
        domain: str,
        profile_name: str,
        snapshot: SceneCollectionSnapshot,
        include_input_settings: bool = True,
        include_audio_state: bool = True,
        include_filters: bool = True,
        include_visibility: bool = False,
    ) -> CollectionImportReport:
        profiles = config.setdefault("profiles", {})
        domain_profiles = profiles.setdefault(domain, {})
        profile = domain_profiles.get(profile_name)
        if not isinstance(profile, dict):
            raise ValueError(f"Profil introuvable : {domain}/{profile_name}")
        existing = profile.setdefault("actions", [])
        if not isinstance(existing, list):
            raise ValueError(f"Actions invalides : {domain}/{profile_name}")

        imported, skipped = SceneCollectionImporter.actions_from_snapshot(
            snapshot,
            include_input_settings=include_input_settings,
            include_audio_state=include_audio_state,
            include_filters=include_filters,
            include_visibility=include_visibility,
        )
        positions = {
            _action_identity(action): index
            for index, action in enumerate(existing)
            if isinstance(action, Mapping)
        }
        added = 0
        replaced = 0
        for action in imported:
            identity = _action_identity(action)
            if identity in positions:
                existing[positions[identity]] = action
                replaced += 1
            else:
                positions[identity] = len(existing)
                existing.append(action)
                added += 1

        skipped.extend(snapshot.warnings)
        return CollectionImportReport(
            collection=snapshot.collection,
            target_domain=domain,
            target_profile=profile_name,
            added_actions=added,
            replaced_actions=replaced,
            skipped=tuple(skipped),
        )
