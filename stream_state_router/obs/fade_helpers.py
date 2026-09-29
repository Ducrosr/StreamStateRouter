from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .. import __version__
from ..services.paths import user_data_dir


HELPER_MANIFEST_SCHEMA_VERSION = 1
LAYOUT_FADE_PURPOSE = "layout_fade"
LAYOUT_FADE_FILTER_PREFIX = "[SSR] Layout Fade::"
LEGACY_LAYOUT_FADE_FILTER = "[SSR] Layout Fade"
LAYOUT_FADE_FILTER_KIND = "color_filter_v2"
_RESERVED_LAYOUT_FADE_SETTINGS = frozenset({"opacity"})


class FadeHelperManifestError(RuntimeError):
    """The durable helper manifest cannot be trusted or persisted."""


@dataclass(frozen=True, slots=True)
class FadeHelperIdentity:
    helper_id: str
    purpose: str
    connection_host: str
    connection_port: int
    collection: str
    source_uuid: str
    source_alias: str
    source_kind: str
    filter_name: str
    filter_kind: str
    created_at: str
    created_with_version: str
    creation_session_generation: int
    state: str = "prepared"
    non_temporary_settings: Mapping[str, Any] | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "helper_id": self.helper_id,
            "purpose": self.purpose,
            "connection": {
                "host": self.connection_host,
                "port": int(self.connection_port),
            },
            "collection": self.collection,
            "source": {
                "uuid": self.source_uuid,
                "alias": self.source_alias,
                "kind": self.source_kind,
            },
            "filter": {
                "name": self.filter_name,
                "kind": self.filter_kind,
            },
            "created_at": self.created_at,
            "created_with_version": self.created_with_version,
            "creation_session_generation": int(self.creation_session_generation),
            "state": self.state,
            "non_temporary_settings": copy.deepcopy(
                dict(self.non_temporary_settings or {})
            ),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FadeHelperIdentity":
        connection = raw.get("connection")
        source = raw.get("source")
        filter_ref = raw.get("filter")
        if not isinstance(connection, Mapping):
            raise FadeHelperManifestError("helper connection context is invalid")
        if not isinstance(source, Mapping):
            raise FadeHelperManifestError("helper source identity is invalid")
        if not isinstance(filter_ref, Mapping):
            raise FadeHelperManifestError("helper filter identity is invalid")

        helper_id = _text(raw.get("helper_id"))
        purpose = _text(raw.get("purpose"))
        host = _text(connection.get("host")).casefold()
        port = _positive_int(connection.get("port"))
        collection = _text(raw.get("collection"))
        source_uuid = _text(source.get("uuid"))
        source_alias = _text(source.get("alias"))
        source_kind = _text(source.get("kind"))
        filter_name = _text(filter_ref.get("name"))
        filter_kind = _text(filter_ref.get("kind"))
        created_at = _text(raw.get("created_at"))
        version = _text(raw.get("created_with_version"))
        session_generation = _non_negative_int(raw.get("creation_session_generation"))
        state = _text(raw.get("state")) or "prepared"
        non_temporary = raw.get("non_temporary_settings")
        if non_temporary is None:
            non_temporary = {}
        if not isinstance(non_temporary, Mapping):
            raise FadeHelperManifestError("helper non-temporary settings are invalid")

        required = {
            "helper_id": helper_id,
            "purpose": purpose,
            "connection.host": host,
            "collection": collection,
            "source.uuid": source_uuid,
            "source.alias": source_alias,
            "source.kind": source_kind,
            "filter.name": filter_name,
            "filter.kind": filter_kind,
            "created_at": created_at,
            "created_with_version": version,
        }
        missing = [name for name, value in required.items() if not value]
        if port <= 0:
            missing.append("connection.port")
        if missing:
            raise FadeHelperManifestError(
                "helper manifest entry missing: " + ", ".join(missing)
            )
        if state not in {"prepared", "observed"}:
            raise FadeHelperManifestError(f"unknown helper state: {state}")
        try:
            parsed_helper_id = uuid.UUID(hex=helper_id)
        except (ValueError, AttributeError) as exc:
            raise FadeHelperManifestError("helper_id is not a generated UUID") from exc
        if (
            parsed_helper_id.version != 4
            or parsed_helper_id.hex != helper_id
        ):
            raise FadeHelperManifestError("helper_id is not a canonical UUID4 hex value")
        if purpose != LAYOUT_FADE_PURPOSE:
            raise FadeHelperManifestError(f"unsupported helper purpose: {purpose}")
        expected_filter_name = f"{LAYOUT_FADE_FILTER_PREFIX}{helper_id}"
        if filter_name != expected_filter_name:
            raise FadeHelperManifestError(
                "helper filter name contradicts helper_id"
            )
        if filter_kind != LAYOUT_FADE_FILTER_KIND:
            raise FadeHelperManifestError(
                f"unsupported layout fade filter kind: {filter_kind}"
            )
        return cls(
            helper_id=helper_id,
            purpose=purpose,
            connection_host=host,
            connection_port=port,
            collection=collection,
            source_uuid=source_uuid,
            source_alias=source_alias,
            source_kind=source_kind,
            filter_name=filter_name,
            filter_kind=filter_kind,
            created_at=created_at,
            created_with_version=version,
            creation_session_generation=session_generation,
            state=state,
            non_temporary_settings=copy.deepcopy(dict(non_temporary)),
        )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: object) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return result if result > 0 else 0


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalize_host(value: object) -> str:
    return _text(value).casefold()


def is_layout_fade_name(name: object) -> bool:
    value = _text(name)
    return value == LEGACY_LAYOUT_FADE_FILTER or value.startswith(
        LAYOUT_FADE_FILTER_PREFIX
    )


def non_temporary_filter_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = settings if isinstance(settings, Mapping) else {}
    return {
        str(key): copy.deepcopy(value)
        for key, value in raw.items()
        if str(key) not in _RESERVED_LAYOUT_FADE_SETTINGS
    }


class FadeHelperManifestStore:
    """Durable ownership inventory for SSR-created layout fade helpers.

    The manifest is deliberately separate from runtime.json. A helper can remain
    reusable in OBS after a clean shutdown while no temporary cleanup obligation
    is active.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (user_data_dir() / "helper-manifest.json")
        self._lock = threading.RLock()

    def entries(self) -> tuple[FadeHelperIdentity, ...]:
        with self._lock:
            return tuple(self._load())

    def get(self, helper_id: str) -> FadeHelperIdentity | None:
        wanted = _text(helper_id)
        if not wanted:
            return None
        matches = [item for item in self.entries() if item.helper_id == wanted]
        if len(matches) > 1:
            raise FadeHelperManifestError(
                f"duplicate helper_id in manifest: {wanted}"
            )
        return matches[0] if matches else None

    def prepare_layout_fade(
        self,
        *,
        connection_host: str,
        connection_port: int,
        collection: str,
        source_uuid: str,
        source_alias: str,
        source_kind: str,
        session_generation: int,
    ) -> FadeHelperIdentity:
        host = _normalize_host(connection_host)
        port = _positive_int(connection_port)
        collection = _text(collection)
        source_uuid = _text(source_uuid)
        source_alias = _text(source_alias)
        source_kind = _text(source_kind)
        if not all((host, port, collection, source_uuid, source_alias, source_kind)):
            raise FadeHelperManifestError(
                "insufficient target identity for durable fade helper"
            )

        with self._lock:
            entries = self._load()
            matches = [
                item
                for item in entries
                if item.purpose == LAYOUT_FADE_PURPOSE
                and item.connection_host == host
                and item.connection_port == port
                and item.collection == collection
                and item.source_uuid == source_uuid
            ]
            if len(matches) > 1:
                raise FadeHelperManifestError(
                    "multiple owned fade helpers target the same qualified source"
                )
            if matches:
                current = matches[0]
                if current.source_kind != source_kind:
                    raise FadeHelperManifestError(
                        "owned fade helper source kind no longer matches"
                    )
                if current.filter_kind != LAYOUT_FADE_FILTER_KIND:
                    raise FadeHelperManifestError(
                        "owned fade helper filter kind is incompatible"
                    )
                if current.source_alias != source_alias:
                    updated = replace(current, source_alias=source_alias)
                    entries[entries.index(current)] = updated
                    self._write(entries)
                    return updated
                return current

            helper_id = uuid.uuid4().hex
            identity = FadeHelperIdentity(
                helper_id=helper_id,
                purpose=LAYOUT_FADE_PURPOSE,
                connection_host=host,
                connection_port=port,
                collection=collection,
                source_uuid=source_uuid,
                source_alias=source_alias,
                source_kind=source_kind,
                filter_name=f"{LAYOUT_FADE_FILTER_PREFIX}{helper_id}",
                filter_kind=LAYOUT_FADE_FILTER_KIND,
                created_at=datetime.now(timezone.utc).isoformat(),
                created_with_version=str(__version__),
                creation_session_generation=max(0, int(session_generation or 0)),
                state="prepared",
                non_temporary_settings={},
            )
            entries.append(identity)
            self._write(entries)
            return identity

    def mark_observed(
        self,
        helper_id: str,
        *,
        source_alias: str,
        non_temporary_settings: Mapping[str, Any] | None,
    ) -> FadeHelperIdentity:
        with self._lock:
            entries = self._load()
            matches = [
                (index, item)
                for index, item in enumerate(entries)
                if item.helper_id == _text(helper_id)
            ]
            if len(matches) != 1:
                raise FadeHelperManifestError(
                    f"helper identity is not uniquely present: {helper_id}"
                )
            index, current = matches[0]
            updated = FadeHelperIdentity(
                helper_id=current.helper_id,
                purpose=current.purpose,
                connection_host=current.connection_host,
                connection_port=current.connection_port,
                collection=current.collection,
                source_uuid=current.source_uuid,
                source_alias=_text(source_alias) or current.source_alias,
                source_kind=current.source_kind,
                filter_name=current.filter_name,
                filter_kind=current.filter_kind,
                created_at=current.created_at,
                created_with_version=current.created_with_version,
                creation_session_generation=current.creation_session_generation,
                state="observed",
                non_temporary_settings=non_temporary_filter_settings(
                    non_temporary_settings
                ),
            )
            entries[index] = updated
            self._write(entries)
            return updated

    def prove_filter(
        self,
        *,
        connection_host: str,
        connection_port: int,
        collection: str,
        source_uuid: str,
        source_alias: str,
        source_kind: str,
        filter_name: str,
        filter_kind: str,
    ) -> FadeHelperIdentity | None:
        host = _normalize_host(connection_host)
        port = _positive_int(connection_port)
        matches = [
            item
            for item in self.entries()
            if item.purpose == LAYOUT_FADE_PURPOSE
            and item.connection_host == host
            and item.connection_port == port
            and item.collection == _text(collection)
            and item.source_uuid == _text(source_uuid)
            and item.source_kind == _text(source_kind)
            and item.filter_name == _text(filter_name)
            and item.filter_kind == _text(filter_kind)
        ]
        if len(matches) > 1:
            raise FadeHelperManifestError(
                "manifest contains ambiguous ownership evidence"
            )
        if not matches:
            return None
        item = matches[0]
        # A write-ahead "prepared" entry proves only that SSR reserved an
        # identity. It does not prove that the matching OBS filter was created
        # by SSR. Ownership becomes reusable/exportable only after the normal
        # creation path has observed the exact helper and persisted that state.
        if item.state != "observed":
            return None
        # Alias is descriptive, not the stable identity. Same UUID with a rename
        # remains the same input, but the caller must update the alias before
        # the next mutation through prepare_layout_fade().
        return item

    @staticmethod
    def settings_compatible(
        identity: FadeHelperIdentity,
        current_settings: Mapping[str, Any] | None,
    ) -> bool:
        if identity.state != "observed":
            return True
        return non_temporary_filter_settings(current_settings) == dict(
            identity.non_temporary_settings or {}
        )

    def _load(self) -> list[FadeHelperIdentity]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FadeHelperManifestError(
                f"helper manifest unreadable: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise FadeHelperManifestError("helper manifest root is not an object")
        schema = raw.get("schema")
        if schema != HELPER_MANIFEST_SCHEMA_VERSION:
            raise FadeHelperManifestError(
                f"unsupported helper manifest schema: {schema!r}"
            )
        rows = raw.get("helpers")
        if not isinstance(rows, list):
            raise FadeHelperManifestError("helper manifest helpers is not a list")
        entries = [
            FadeHelperIdentity.from_mapping(item)
            for item in rows
            if isinstance(item, Mapping)
        ]
        if len(entries) != len(rows):
            raise FadeHelperManifestError(
                "helper manifest contains a non-object entry"
            )
        helper_ids = [item.helper_id for item in entries]
        if len(helper_ids) != len(set(helper_ids)):
            raise FadeHelperManifestError("helper manifest contains duplicate helper_id")

        ownership_keys = [
            (
                item.purpose,
                item.connection_host,
                item.connection_port,
                item.collection,
                item.source_uuid,
            )
            for item in entries
            if item.purpose == LAYOUT_FADE_PURPOSE
        ]
        if len(ownership_keys) != len(set(ownership_keys)):
            raise FadeHelperManifestError(
                "helper manifest contains multiple helpers for one qualified source"
            )
        return entries

    def _write(self, entries: list[FadeHelperIdentity]) -> None:
        payload = {
            "schema": HELPER_MANIFEST_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "helpers": [item.as_mapping() for item in entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                except OSError:
                    directory_fd = -1
                if directory_fd >= 0:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        except Exception as exc:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            raise FadeHelperManifestError(
                f"helper manifest persistence failed: {exc}"
            ) from exc


class MemoryFadeHelperManifestStore(FadeHelperManifestStore):
    """Non-persistent store for injected/fake OBS clients in unit tests."""

    def __init__(self) -> None:
        self.path = Path("<memory>")
        self._lock = threading.RLock()
        self._entries: list[FadeHelperIdentity] = []

    def _load(self) -> list[FadeHelperIdentity]:
        return [FadeHelperIdentity.from_mapping(item.as_mapping()) for item in self._entries]

    def _write(self, entries: list[FadeHelperIdentity]) -> None:
        self._entries = [
            FadeHelperIdentity.from_mapping(item.as_mapping()) for item in entries
        ]
