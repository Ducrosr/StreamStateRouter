from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping


def _typed_values_equal(left: Any, right: Any) -> bool:
    """Strict recursive equality for managed JSON-like values.

    Booleans are never interchangeable with numbers and integers are compared
    without lossy float conversion. Numeric tolerance belongs to the specific
    physical property that needs it, not to arbitrary settings.
    """

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, float) and not math.isfinite(left):
            return False
        if isinstance(right, float) and not math.isfinite(right):
            return False
        return left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_typed_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _typed_values_equal(l_item, r_item)
            for l_item, r_item in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return left == right


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return deepcopy(value)


def _materialize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _materialize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_materialize_value(item) for item in value]
    return deepcopy(value)


def _canonical_signature_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$invalid_float": str(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_signature_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_signature_value(item) for item in value]
    return {
        "$unsupported_type": (
            f"{type(value).__module__}.{type(value).__qualname__}"
        )
    }


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
        kind = str(self.kind).strip()
        if not kind:
            raise ValueError("PropertyKey.kind is required")
        try:
            occurrence = int(self.occurrence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("PropertyKey.occurrence must be an integer") from exc
        if occurrence < 0:
            raise ValueError("PropertyKey.occurrence must be >= 0")

        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "occurrence", occurrence)
        for field_name in (
            "collection",
            "container",
            "source",
            "filter_name",
            "setting",
        ):
            value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                "" if value is None else str(value),
            )

        required: dict[str, tuple[str, ...]] = {
            "scene_item_visibility": ("container", "source"),
            "input_mute": ("source",),
            "input_volume_db": ("source",),
            "input_setting": ("source", "setting"),
            "filter_enabled": ("source", "filter_name"),
            "filter_setting": ("source", "filter_name", "setting"),
        }
        allowed: dict[str, frozenset[str]] = {
            "program_scene": frozenset({"collection"}),
            "scene_item_visibility": frozenset(
                {"collection", "container", "source", "occurrence"}
            ),
            "input_mute": frozenset({"collection", "source"}),
            "input_volume_db": frozenset({"collection", "source"}),
            "input_setting": frozenset({"collection", "source", "setting"}),
            "filter_enabled": frozenset({"collection", "source", "filter_name"}),
            "filter_setting": frozenset(
                {"collection", "source", "filter_name", "setting"}
            ),
            "layout_profile": frozenset({"collection", "container"}),
        }

        if kind in allowed:
            values = {
                "collection": self.collection,
                "container": self.container,
                "source": self.source,
                "occurrence": self.occurrence,
                "filter_name": self.filter_name,
                "setting": self.setting,
            }
            for field_name, value in values.items():
                if field_name in allowed[kind]:
                    continue
                if value not in {"", 0}:
                    raise ValueError(
                        f"PropertyKey.{field_name} is not valid for {kind}"
                    )
            for field_name in required.get(kind, ()):
                if not str(getattr(self, field_name) or "").strip():
                    raise ValueError(
                        f"PropertyKey.{field_name} is required for {kind}"
                    )

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_value(self.value))
        object.__setattr__(
            self,
            "provenance",
            tuple(str(item) for item in self.provenance if str(item)),
        )

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
        value: Any = _materialize_value(self.value)
        if diagnostic and self.key.kind in {"input_setting", "filter_setting"}:
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

    def __post_init__(self) -> None:
        merged: dict[PropertyKey, DesiredAssignment] = {}
        for assignment in tuple(self.assignments):
            if not isinstance(assignment, DesiredAssignment):
                raise TypeError("DesiredState assignments must be DesiredAssignment")
            previous = merged.get(assignment.key)
            if previous is None:
                merged[assignment.key] = assignment
                continue
            if not _typed_values_equal(previous.value, assignment.value):
                raise DesiredStateConflict(assignment.key, previous, assignment)
            provenance = tuple(
                dict.fromkeys((*previous.provenance, *assignment.provenance))
            )
            merged[assignment.key] = DesiredAssignment(
                key=assignment.key,
                value=assignment.value,
                provenance=provenance,
            )
        object.__setattr__(
            self,
            "assignments",
            tuple(merged[key] for key in sorted(merged)),
        )

    @classmethod
    def empty(cls) -> "DesiredState":
        return cls()

    @classmethod
    def build(cls, values: Iterable[DesiredAssignment]) -> "DesiredState":
        return cls(tuple(values))

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
                "value": _canonical_signature_value(item.value),
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
    code: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_value(self.value))

    @classmethod
    def unknown(
        cls,
        *,
        code: str = "observed_value_unknown",
        reason: str = "",
    ) -> "ObservedValue":
        return cls(False, None, str(code), str(reason))

    @classmethod
    def known_value(cls, value: Any) -> "ObservedValue":
        return cls(True, value)


@dataclass(frozen=True, slots=True)
class ObservedState:
    values: Mapping[PropertyKey, ObservedValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @classmethod
    def empty(cls) -> "ObservedState":
        return cls({})

    def get(self, key: PropertyKey) -> ObservedValue:
        return self.values.get(key, ObservedValue.unknown())
