from __future__ import annotations

import math
from typing import Mapping


class LayoutFadeCleanupFormatError(ValueError):
    """A persisted/imported layout-fade obligation is unsafe to trust."""


def _strict_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finite_number(
    raw: Mapping[str, object],
    key: str,
    *,
    default: float,
    required: bool,
) -> float:
    if key not in raw:
        if required:
            raise LayoutFadeCleanupFormatError(
                f"obligation layout_fade sans {key}"
            )
        return float(default)
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LayoutFadeCleanupFormatError(
            f"obligation layout_fade {key} invalide"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise LayoutFadeCleanupFormatError(
            f"obligation layout_fade {key} invalide"
        )
    return result


def _nonnegative_int(
    raw: Mapping[str, object],
    key: str,
    *,
    default: int,
    required: bool,
) -> int:
    if key not in raw:
        if required:
            raise LayoutFadeCleanupFormatError(
                f"obligation layout_fade sans {key}"
            )
        return int(default)
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LayoutFadeCleanupFormatError(
            f"obligation layout_fade {key} invalide"
        )
    return int(value)


def normalize_layout_fade_cleanup(
    raw: Mapping[str, object],
    *,
    schema: int | None,
    strict_current: bool,
) -> dict[str, object]:
    """Return one canonical layout-fade cleanup obligation."""
    if not isinstance(raw, Mapping):
        raise LayoutFadeCleanupFormatError("obligation layout_fade non-objet")

    item = dict(raw)
    if _strict_text(item.get("kind")).casefold() != "layout_fade":
        raise LayoutFadeCleanupFormatError(
            "obligation cleanup n'est pas layout_fade"
        )

    source = _strict_text(item.get("source"))
    collection = _strict_text(item.get("collection"))
    if not source or not collection:
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade sans source/collection"
        )
    item["kind"] = "layout_fade"
    item["source"] = source
    item["collection"] = collection

    if "helper_id" in item and not isinstance(item.get("helper_id"), str):
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade helper_id invalide"
        )
    helper_id = _strict_text(item.get("helper_id"))
    if schema in {None, 2} and helper_id:
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade legacy contient une identité helper non prouvable"
        )

    if schema == 3 and strict_current:
        raw_legacy = item.get("legacy")
        if not isinstance(raw_legacy, bool) or raw_legacy == bool(helper_id):
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade contradictoire dans le schéma courant"
            )
        legacy = raw_legacy
    else:
        raw_legacy = item.get("legacy", not bool(helper_id))
        if not isinstance(raw_legacy, bool):
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade legacy invalide"
            )
        legacy = bool(raw_legacy or not helper_id)
        if helper_id and legacy:
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade helper marquée legacy"
            )

    required_metadata = bool(schema == 3 and strict_current)
    item["created_at"] = _finite_number(
        item, "created_at", default=0.0, required=required_metadata
    )
    item["attempts"] = _nonnegative_int(
        item, "attempts", default=0, required=required_metadata
    )
    if "last_error" not in item:
        if required_metadata:
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade sans last_error"
            )
        last_error = ""
    else:
        raw_last_error = item.get("last_error")
        if not isinstance(raw_last_error, str):
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade last_error invalide"
            )
        last_error = raw_last_error
    item["last_error"] = last_error

    if not helper_id:
        item["legacy"] = True
        raw_ambiguous = item.get("ambiguous", False)
        if not isinstance(raw_ambiguous, bool):
            raise LayoutFadeCleanupFormatError(
                "obligation layout_fade ambiguous invalide"
            )
        item["ambiguous"] = raw_ambiguous
        return item

    raw_ambiguous = item.get("ambiguous")
    if raw_ambiguous is None and not strict_current:
        raw_ambiguous = False
    if not isinstance(raw_ambiguous, bool):
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade ambiguous invalide"
        )

    connection = item.get("connection")
    if not isinstance(connection, Mapping):
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade connexion invalide"
        )
    host = _strict_text(connection.get("host"))
    raw_port = connection.get("port")
    if isinstance(raw_port, bool) or not isinstance(raw_port, int):
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade port invalide"
        )
    port = int(raw_port)
    source_uuid = _strict_text(item.get("source_uuid"))
    source_kind = _strict_text(item.get("source_kind"))
    filter_name = _strict_text(item.get("filter_name"))
    filter_kind = _strict_text(item.get("filter_kind"))
    cleanup_action = _strict_text(item.get("cleanup_action"))
    if (
        not host
        or port <= 0
        or not source_uuid
        or not source_kind
        or not filter_name
        or not filter_kind
        or not cleanup_action
    ):
        raise LayoutFadeCleanupFormatError(
            "obligation layout_fade identité helper incomplète"
        )

    item["helper_id"] = helper_id
    item["source_uuid"] = source_uuid
    item["source_kind"] = source_kind
    item["connection"] = {"host": host.casefold(), "port": port}
    item["filter_name"] = filter_name
    item["filter_kind"] = filter_kind
    item["cleanup_action"] = cleanup_action
    item["legacy"] = False
    item["ambiguous"] = raw_ambiguous
    return item
