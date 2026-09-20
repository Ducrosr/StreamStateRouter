from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable, Mapping


class DesiredStateConflict(ValueError):
    pass


class ObservedStateConflict(ValueError):
    pass


@dataclass(frozen=True, order=True, slots=True)
class ResourceKey:
    """Stable property identity used for conflict detection and planning."""

    kind: str
    scope: tuple[str, ...]
    property_name: str

    @classmethod
    def scene_item_visibility(
        cls,
        *,
        collection: str,
        container: str,
        source: str,
        occurrence: int = 1,
    ) -> "ResourceKey":
        return cls(
            "scene_item",
            (str(collection), str(container), str(source), str(max(1, int(occurrence)))),
            "visible",
        )

    @classmethod
    def input_setting(
        cls,
        input_name: str,
        setting: str,
        *,
        collection: str = "",
    ) -> "ResourceKey":
        return cls(
            "input",
            (str(collection), str(input_name)),
            f"settings.{str(setting)}",
        )

    @classmethod
    def filter_enabled(
        cls,
        source: str,
        filter_name: str,
        *,
        collection: str = "",
    ) -> "ResourceKey":
        return cls(
            "filter",
            (str(collection), str(source), str(filter_name)),
            "enabled",
        )

    @classmethod
    def filter_setting(
        cls,
        source: str,
        filter_name: str,
        setting: str,
        *,
        collection: str = "",
    ) -> "ResourceKey":
        return cls(
            "filter",
            (str(collection), str(source), str(filter_name)),
            f"settings.{str(setting)}",
        )

    @classmethod
    def layout_profile(cls) -> "ResourceKey":
        return cls("layout", (), "profile")

    def label(self) -> str:
        target = "/".join(part for part in self.scope if part)
        if target:
            return f"{self.kind}:{target}:{self.property_name}"
        return f"{self.kind}:{self.property_name}"

    def as_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "scope": list(self.scope),
            "property": self.property_name,
            "label": self.label(),
        }


@dataclass(frozen=True, slots=True)
class DesiredProperty:
    key: ResourceKey
    value: object
    provenance: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        key: ResourceKey,
        value: object,
        *,
        provenance: str | Iterable[str] = (),
    ) -> "DesiredProperty":
        if isinstance(provenance, str):
            origins = (provenance,) if provenance else ()
        else:
            origins = tuple(str(item) for item in provenance if str(item))
        return cls(key=key, value=copy.deepcopy(value), provenance=origins)

    def as_mapping(self) -> dict[str, object]:
        return {
            "resource": self.key.as_mapping(),
            "value": copy.deepcopy(self.value),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class DesiredState:
    properties: tuple[DesiredProperty, ...] = ()

    @classmethod
    def build(cls, properties: Iterable[DesiredProperty]) -> "DesiredState":
        merged: dict[ResourceKey, DesiredProperty] = {}
        for item in properties:
            current = merged.get(item.key)
            if current is None:
                merged[item.key] = item
                continue
            if not values_equal(current.value, item.value):
                raise DesiredStateConflict(
                    f"Conflit pour {item.key.label()} : {current.value!r} != {item.value!r}"
                )
            provenance = tuple(dict.fromkeys((*current.provenance, *item.provenance)))
            merged[item.key] = DesiredProperty.create(
                item.key,
                current.value,
                provenance=provenance,
            )
        return cls(tuple(merged[key] for key in sorted(merged)))

    def as_mapping(self) -> dict[str, object]:
        return {"properties": [item.as_mapping() for item in self.properties]}


@dataclass(frozen=True, slots=True)
class ObservedProperty:
    key: ResourceKey
    known: bool
    value: object = None
    detail: str = ""

    @classmethod
    def known_value(cls, key: ResourceKey, value: object) -> "ObservedProperty":
        return cls(key=key, known=True, value=copy.deepcopy(value))

    @classmethod
    def unknown(cls, key: ResourceKey, detail: str = "not_observed") -> "ObservedProperty":
        return cls(key=key, known=False, value=None, detail=str(detail))

    def as_mapping(self) -> dict[str, object]:
        return {
            "resource": self.key.as_mapping(),
            "known": self.known,
            "value": copy.deepcopy(self.value),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ObservedState:
    properties: tuple[ObservedProperty, ...] = ()

    @classmethod
    def build(cls, properties: Iterable[ObservedProperty]) -> "ObservedState":
        values: dict[ResourceKey, ObservedProperty] = {}
        for item in properties:
            current = values.get(item.key)
            if current is None:
                values[item.key] = item
                continue
            if current.known and item.known:
                if not values_equal(current.value, item.value):
                    raise ObservedStateConflict(
                        f"Observations contradictoires pour {item.key.label()} : "
                        f"{current.value!r} != {item.value!r}"
                    )
                continue
            if item.known:
                values[item.key] = item
        return cls(tuple(values[key] for key in sorted(values)))

    @classmethod
    def from_values(cls, values: Mapping[ResourceKey, object]) -> "ObservedState":
        return cls.build(ObservedProperty.known_value(key, value) for key, value in values.items())

    def lookup(self, key: ResourceKey) -> ObservedProperty:
        for item in self.properties:
            if item.key == key:
                return item
        return ObservedProperty.unknown(key)

    def as_mapping(self) -> dict[str, object]:
        return {"properties": [item.as_mapping() for item in self.properties]}


def values_equal(left: object, right: object) -> bool:
    return _canonical_value(left) == _canonical_value(right)


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        items = [
            (_canonical_key(key), _canonical_value(item))
            for key, item in value.items()
        ]
        return (
            "mapping",
            tuple(sorted(items, key=repr)),
        )
    if isinstance(value, (list, tuple)):
        return ("sequence", tuple(_canonical_value(item) for item in value))
    if isinstance(value, set):
        return ("set", tuple(sorted((_canonical_value(item) for item in value), key=repr)))
    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("none", None)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("string", value)
    return (type(value).__qualname__, repr(value))


def _canonical_key(value: object) -> tuple[str, str]:
    return (type(value).__qualname__, repr(value))
