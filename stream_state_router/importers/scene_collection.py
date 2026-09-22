from __future__ import annotations

from dataclasses import dataclass
import copy
import math
from typing import Any, Callable, Mapping

from ..obs.catalog import OBSResourceCatalogReader
from ..obs.client import OBSClientManager, OBSRequestError
from ..obs.layouts import OBSLayoutManager


@dataclass(frozen=True, slots=True)
class ImportedInput:
    name: str
    kind: str
    uuid: str
    settings: Mapping[str, Any]
    muted: bool | None
    volume_db: float | None

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "uuid": self.uuid,
            "settings": copy.deepcopy(dict(self.settings)),
            "muted": self.muted,
            "volume_db": self.volume_db,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ImportedInput":
        settings = raw.get("settings")
        return cls(
            name=str(raw.get("name") or ""),
            kind=str(raw.get("kind") or ""),
            uuid=str(raw.get("uuid") or ""),
            settings=(
                copy.deepcopy(dict(settings))
                if isinstance(settings, Mapping)
                else {}
            ),
            muted=(
                bool(raw.get("muted"))
                if isinstance(raw.get("muted"), bool)
                else None
            ),
            volume_db=(
                float(raw["volume_db"])
                if (
                    not isinstance(raw.get("volume_db"), bool)
                    and isinstance(raw.get("volume_db"), (int, float))
                    and math.isfinite(float(raw["volume_db"]))
                )
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ImportedFilter:
    source: str
    name: str
    kind: str
    enabled: bool | None
    settings: Mapping[str, Any]

    def as_mapping(self) -> dict[str, object]:
        return {
            "source": self.source,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "settings": copy.deepcopy(dict(self.settings)),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ImportedFilter":
        settings = raw.get("settings")
        return cls(
            source=str(raw.get("source") or ""),
            name=str(raw.get("name") or ""),
            kind=str(raw.get("kind") or ""),
            enabled=(
                bool(raw.get("enabled"))
                if isinstance(raw.get("enabled"), bool)
                else None
            ),
            settings=(
                copy.deepcopy(dict(settings))
                if isinstance(settings, Mapping)
                else {}
            ),
        )


@dataclass(frozen=True, slots=True)
class ImportedSceneItem:
    scene: str
    source: str
    occurrence: int
    enabled: bool | None

    def as_mapping(self) -> dict[str, object]:
        return {
            "scene": self.scene,
            "source": self.source,
            "occurrence": self.occurrence,
            "enabled": self.enabled,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ImportedSceneItem":
        try:
            occurrence = int(raw.get("occurrence", 0))
        except (TypeError, ValueError, OverflowError):
            occurrence = 0
        return cls(
            scene=str(raw.get("scene") or ""),
            source=str(raw.get("source") or ""),
            occurrence=occurrence,
            enabled=(
                bool(raw.get("enabled"))
                if isinstance(raw.get("enabled"), bool)
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class SceneCollectionSnapshot:
    collection: str
    current_program_scene: str
    inputs: tuple[ImportedInput, ...]
    filters: tuple[ImportedFilter, ...]
    scene_items: tuple[ImportedSceneItem, ...]
    scenes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def input_names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.inputs)

    def scene_item_count(self, scene: str, source: str) -> int:
        return sum(
            1
            for item in self.scene_items
            if item.scene == scene and item.source == source
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "collection": self.collection,
            "current_program_scene": self.current_program_scene,
            "inputs": [item.as_mapping() for item in self.inputs],
            "filters": [item.as_mapping() for item in self.filters],
            "scene_items": [item.as_mapping() for item in self.scene_items],
            "scenes": list(self.scenes),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SceneCollectionSnapshot":
        def rows(name: str) -> list[Mapping[str, Any]]:
            value = raw.get(name)
            return [
                item
                for item in value
                if isinstance(item, Mapping)
            ] if isinstance(value, list) else []

        return cls(
            collection=str(raw.get("collection") or ""),
            current_program_scene=str(
                raw.get("current_program_scene") or ""
            ),
            inputs=tuple(
                ImportedInput.from_mapping(item)
                for item in rows("inputs")
            ),
            filters=tuple(
                ImportedFilter.from_mapping(item)
                for item in rows("filters")
            ),
            scene_items=tuple(
                ImportedSceneItem.from_mapping(item)
                for item in rows("scene_items")
            ),
            scenes=tuple(
                str(item)
                for item in (raw.get("scenes") or [])
                if str(item).strip()
            ),
            warnings=tuple(
                str(item)
                for item in (raw.get("warnings") or [])
                if str(item).strip()
            ),
        )


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


@dataclass(frozen=True, slots=True)
class LayoutImportReport:
    collection: str
    added: int
    refreshed: int
    skipped: tuple[str, ...]

    def summary(self) -> str:
        lines = [
            f"LayoutProfiles ajoutés : {self.added}",
            f"LayoutProfiles rafraîchis : {self.refreshed}",
            f"Layouts ignorés : {len(self.skipped)}",
        ]
        if self.skipped:
            lines.append("")
            lines.extend(f"- {item}" for item in self.skipped)
        return "\n".join(lines)


def _non_audio_request_error(exc: OBSRequestError) -> bool:
    text = str(exc).casefold()
    return "code 604" in text or "does not support audio" in text


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
    """Read current OBS state and project only stable, representable state.

    OBS I/O and configuration mutation are deliberately separate.  The runtime
    worker can call snapshot()/capture_layout_profiles(), serialize the result,
    then Qt can merge the returned data into a draft configuration without
    issuing any OBS request.
    """

    def __init__(
        self,
        client: OBSClientManager,
        *,
        layout_manager: OBSLayoutManager | None = None,
        cooperative_yield: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self._cooperative_yield = cooperative_yield
        self.reader = OBSResourceCatalogReader(
            client,
            cooperative_yield=cooperative_yield,
        )
        self.layout_manager = layout_manager or OBSLayoutManager(client)
        if (
            cooperative_yield is not None
            and hasattr(self.layout_manager, "set_cooperative_yield")
        ):
            self.layout_manager.set_cooperative_yield(cooperative_yield)

    def _send(
        self,
        request: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._cooperative_yield is not None:
            self._cooperative_yield()
        return self.client.send(request, data)

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
                response = self._send(
                    "GetInputMute",
                    {"inputUuid": input_ref.uuid}
                    if input_ref.uuid
                    else {"inputName": input_ref.name},
                )
                if isinstance(response.get("inputMuted"), bool):
                    muted = bool(response["inputMuted"])
            except OBSRequestError as exc:
                if not _non_audio_request_error(exc):
                    warnings.append(
                        f"Input '{input_ref.name}' mute unreadable: {exc}"
                    )

            volume_db: float | None = None
            try:
                response = self._send(
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
                if not _non_audio_request_error(exc):
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
            scenes=tuple(scene.name for scene in catalog.scenes),
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
            emitted_ambiguous: set[tuple[str, str]] = set()
            for item in snapshot.scene_items:
                identity = (item.scene, item.source)
                if counts[identity] != 1:
                    if identity not in emitted_ambiguous:
                        skipped.append(
                            "Visibilité ambiguë ignorée : "
                            f"{item.scene}/{item.source} apparaît "
                            f"{counts[identity]} fois"
                        )
                        emitted_ambiguous.add(identity)
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

    def capture_layout_profiles(
        self,
        *,
        snapshot: SceneCollectionSnapshot,
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
        captured: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []
        for scene in snapshot.scenes:
            profile_name = f"Import {snapshot.collection} · {scene}".strip()
            try:
                capture = self.layout_manager.capture_profile_result(scene)
            except Exception as exc:
                skipped.append(f"{scene}: capture layout impossible : {exc}")
                continue
            captured[profile_name] = copy.deepcopy(dict(capture.profile))
            skipped.extend(
                f"{scene}: {warning}" for warning in capture.warnings
            )
        return captured, tuple(skipped)

    @staticmethod
    def apply_layout_profiles(
        config: dict[str, Any],
        *,
        collection: str,
        profiles: Mapping[str, Mapping[str, Any]],
        skipped: tuple[str, ...] = (),
    ) -> LayoutImportReport:
        layout_profiles = config.setdefault("layout_profiles", {})
        if not isinstance(layout_profiles, dict):
            raise ValueError("config.layout_profiles doit être un objet.")

        added = 0
        refreshed = 0
        for profile_name, profile in profiles.items():
            if profile_name in layout_profiles:
                refreshed += 1
            else:
                added += 1
            layout_profiles[str(profile_name)] = copy.deepcopy(dict(profile))

        return LayoutImportReport(
            collection=str(collection or ""),
            added=added,
            refreshed=refreshed,
            skipped=tuple(str(item) for item in skipped),
        )

    def import_layout_profiles(
        self,
        config: dict[str, Any],
        *,
        snapshot: SceneCollectionSnapshot,
    ) -> LayoutImportReport:
        profiles, skipped = self.capture_layout_profiles(snapshot=snapshot)
        return self.apply_layout_profiles(
            config,
            collection=snapshot.collection,
            profiles=profiles,
            skipped=skipped,
        )

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
