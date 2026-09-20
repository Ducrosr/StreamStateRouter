from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True, order=True)
class PropertyKey:
    """Stable logical identity of one property managed by SSR.

    Scene-item ids are intentionally not persisted here: OBS may recycle them
    after structural edits.  Scene-item occurrences are identified by their
    collection/container/source tuple plus an occurrence index and must be
    freshly resolved before a future executor mutates OBS.
    """

    kind: str
    collection: str = ""
    container: str = ""
    source: str = ""
    occurrence: int = 0
    filter_name: str = ""
    setting: str = ""

    def __post_init__(self) -> None:
        if not str(self.kind).strip():
            raise ValueError("PropertyKey.kind is required")
        if int(self.occurrence) < 0:
            raise ValueError("PropertyKey.occurrence must be >= 0")

    @classmethod
    def scene_item_visibility(
        cls,
        *,
        collection: str,
        container: str,
        source: str,
        occurrence: int = 0,
    ) -> "PropertyKey":
        return cls(
            "scene_item_visibility",
            str(collection),
            str(container),
            str(source),
            int(occurrence),
        )

    @classmethod
    def program_scene(
        cls,
        *,
        collection: str,
    ) -> "PropertyKey":
        return cls("program_scene", str(collection))

    @classmethod
    def input_mute(
        cls,
        *,
        collection: str,
        input_name: str,
    ) -> "PropertyKey":
        return cls("input_mute", str(collection), source=str(input_name))

    @classmethod
    def input_volume_db(
        cls,
        *,
        collection: str,
        input_name: str,
    ) -> "PropertyKey":
        return cls("input_volume_db", str(collection), source=str(input_name))

    @classmethod
    def input_setting(
        cls,
        *,
        collection: str,
        input_name: str,
        setting: str,
    ) -> "PropertyKey":
        return cls(
            "input_setting",
            str(collection),
            source=str(input_name),
            setting=str(setting),
        )

    @classmethod
    def filter_enabled(
        cls,
        *,
        collection: str,
        source: str,
        filter_name: str,
    ) -> "PropertyKey":
        return cls(
            "filter_enabled",
            str(collection),
            source=str(source),
            filter_name=str(filter_name),
        )

    @classmethod
    def filter_setting(
        cls,
        *,
        collection: str,
        source: str,
        filter_name: str,
        setting: str,
    ) -> "PropertyKey":
        return cls(
            "filter_setting",
            str(collection),
            source=str(source),
            filter_name=str(filter_name),
            setting=str(setting),
        )

    @classmethod
    def layout_profile(
        cls,
        *,
        collection: str,
        scene: str = "",
    ) -> "PropertyKey":
        # Layout application stays delegated to OBSLayoutManager.  This key only
        # lets the declarative planner express the selected layout target.
        return cls("layout_profile", str(collection), container=str(scene))

    def as_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "collection": self.collection,
            "container": self.container,
            "source": self.source,
            "occurrence": self.occurrence,
            "filter": self.filter_name,
            "setting": self.setting,
        }


@dataclass(frozen=True, slots=True)
class DesiredAssignment:
    key: PropertyKey
    value: Any
    provenance: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        key: PropertyKey,
        value: Any,
        *,
        provenance: str | Iterable[str] = (),
    ) -> "DesiredAssignment":
        if isinstance(provenance, str):
            values = (provenance,) if provenance else ()
        else:
            values = tuple(str(item) for item in provenance if str(item))
        return cls(key=key, value=value, provenance=values)

    def as_mapping(self, *, diagnostic: bool = False) -> dict[str, object]:
        value: Any = self.value
        if diagnostic and self.key.kind == "input_setting":
            # Arbitrary OBS input settings may contain URLs/tokens.  The legacy
            # dispatcher intentionally avoids exposing their values in status
            # diagnostics; declarative diagnostics preserve that boundary.
            value = "<redacted>"
        return {
            "property": self.key.as_mapping(),
            "value": value,
            "provenance": list(self.provenance),
        }


class DesiredStateConflict(ValueError):
    def __init__(
        self,
        key: PropertyKey,
        left: DesiredAssignment,
        right: DesiredAssignment,
    ):
        left_origin = ", ".join(left.provenance) or "unknown"
        right_origin = ", ".join(right.provenance) or "unknown"
        super().__init__(
            "Conflicting desired values for "
            f"{key.kind}: {left_origin} vs {right_origin}"
        )
        self.key = key
        self.left = left
        self.right = right

    def diagnostic_message(self) -> str:
        left = ", ".join(self.left.provenance) or "unknown"
        right = ", ".join(self.right.provenance) or "unknown"
        return (
            f"Conflicting desired values for {self.key.kind}: "
            f"{left} vs {right}"
        )


@dataclass(frozen=True, slots=True)
class DesiredState:
    assignments: tuple[DesiredAssignment, ...] = ()

    @classmethod
    def empty(cls) -> "DesiredState":
        return cls()

    @classmethod
    def build(cls, values: Iterable[DesiredAssignment]) -> "DesiredState":
        merged: dict[PropertyKey, DesiredAssignment] = {}
        for assignment in values:
            previous = merged.get(assignment.key)
            if previous is None:
                merged[assignment.key] = assignment
                continue
            if previous.value != assignment.value:
                raise DesiredStateConflict(assignment.key, previous, assignment)
            provenance = tuple(
                dict.fromkeys((*previous.provenance, *assignment.provenance))
            )
            merged[assignment.key] = DesiredAssignment(
                key=assignment.key,
                value=assignment.value,
                provenance=provenance,
            )
        return cls(
            tuple(
                merged[key]
                for key in sorted(merged)
            )
        )

    def by_key(self) -> dict[PropertyKey, DesiredAssignment]:
        return {item.key: item for item in self.assignments}

    def as_mapping(self, *, diagnostic: bool = False) -> dict[str, object]:
        return {
            "properties": [
                item.as_mapping(diagnostic=diagnostic)
                for item in self.assignments
            ],
        }

    def target_signature(self) -> str:
        """Stable fingerprint of managed target properties and values.

        Provenance is intentionally excluded: two profile resolutions that
        produce the same physical target share the same target signature.
        """

        payload = [
            {
                "property": item.key.as_mapping(),
                "value": item.value,
            }
            for item in self.assignments
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def bind_collection(self, collection: str) -> "DesiredState":
        """Bind collection-agnostic intent to one concrete OBS collection.

        Profile resolution is intentionally pure and may not know the current
        Scene Collection. The read-only planning service binds blank collection
        keys once it has a frozen catalog snapshot. Explicit collection keys are
        preserved so mismatches can still be diagnosed.
        """

        name = str(collection or "").strip()
        if not name:
            return self
        return DesiredState.build(
            DesiredAssignment(
                key=(
                    replace(item.key, collection=name)
                    if not item.key.collection
                    else item.key
                ),
                value=item.value,
                provenance=item.provenance,
            )
            for item in self.assignments
        )


@dataclass(frozen=True, slots=True)
class ObservedValue:
    known: bool
    value: Any = None

    @classmethod
    def unknown(cls) -> "ObservedValue":
        return cls(False, None)

    @classmethod
    def known_value(cls, value: Any) -> "ObservedValue":
        return cls(True, value)


@dataclass(frozen=True, slots=True)
class ObservedState:
    values: Mapping[PropertyKey, ObservedValue]

    @classmethod
    def empty(cls) -> "ObservedState":
        return cls({})

    def get(self, key: PropertyKey) -> ObservedValue:
        return self.values.get(key, ObservedValue.unknown())
