from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Mapping

from ..recovery_schema import (
    LayoutFadeCleanupFormatError,
    normalize_layout_fade_cleanup,
)
from .paths import user_data_dir


CLEANUP_SCHEMA_VERSION = 3


class RuntimeMarkerFormatError(RuntimeError):
    """runtime.json cannot be understood safely by this SSR version."""


def _strict_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_cleanup_item(
    raw: Mapping[str, object],
    *,
    schema: int | None = None,
    strict_current: bool = False,
) -> dict[str, object] | None:
    item = dict(raw)
    kind = _strict_text(item.get("kind")).casefold()
    if not kind:
        # Legacy runtime markers only persisted activation hides.
        if isinstance(item.get("target"), Mapping):
            kind = "activation_hide"
        else:
            return None
    if kind in {"activation", "activation_hide"}:
        if not isinstance(item.get("target"), Mapping):
            return None
        item["kind"] = "activation_hide"
        return item
    if kind == "layout_fade":
        try:
            return normalize_layout_fade_cleanup(
                item,
                schema=schema,
                strict_current=strict_current,
            )
        except LayoutFadeCleanupFormatError:
            return None
    return None


class RuntimeMarker:
    def __init__(self) -> None:
        self.path = user_data_dir() / "runtime.json"
        self.previous_unclean = False
        self.previous_cleanup_incomplete = False
        self.previous_pending_cleanup: tuple[dict[str, object], ...] = ()
        self.finalized = False
        self._write_lock = threading.RLock()

    def start(self) -> None:
        with self._write_lock:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise RuntimeMarkerFormatError(
                        f"runtime.json illisible; recovery préservé sans réécriture: {exc}"
                    ) from exc
                if not isinstance(data, Mapping):
                    raise RuntimeMarkerFormatError(
                        "runtime.json doit contenir un objet JSON; fichier préservé"
                    )

                raw_schema = data.get("cleanup_schema")
                schema: int | None
                if raw_schema is None:
                    schema = None
                elif isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
                    raise RuntimeMarkerFormatError(
                        "cleanup_schema invalide; runtime.json préservé"
                    )
                else:
                    schema = int(raw_schema)
                if schema is not None and schema not in {2, CLEANUP_SCHEMA_VERSION}:
                    direction = "futur" if schema > CLEANUP_SCHEMA_VERSION else "inconnu"
                    raise RuntimeMarkerFormatError(
                        f"cleanup_schema {direction} {schema}; "
                        "réécriture refusée pour préserver le recovery"
                    )

                strict_current_schema = (
                    schema is not None and schema >= CLEANUP_SCHEMA_VERSION
                )
                if not isinstance(data.get("clean_shutdown"), bool):
                    raise RuntimeMarkerFormatError(
                        "clean_shutdown invalide; runtime.json préservé"
                    )
                if (
                    schema is not None
                    and not isinstance(data.get("cleanup_complete"), bool)
                ) or (
                    schema is None
                    and "cleanup_complete" in data
                    and not isinstance(data.get("cleanup_complete"), bool)
                ):
                    raise RuntimeMarkerFormatError(
                        "cleanup_complete invalide; runtime.json préservé"
                    )

                self.previous_unclean = data.get("clean_shutdown") is False
                self.previous_cleanup_incomplete = data.get("cleanup_complete") is False
                if strict_current_schema and "pending_cleanup" not in data:
                    raise RuntimeMarkerFormatError(
                        "pending_cleanup absent du schéma courant; runtime.json préservé"
                    )
                raw_pending = data.get("pending_cleanup", [])
                if not isinstance(raw_pending, list):
                    raise RuntimeMarkerFormatError(
                        "pending_cleanup invalide; runtime.json préservé"
                    )

                normalized: list[dict[str, object]] = []
                for item in raw_pending:
                    if not isinstance(item, Mapping):
                        raise RuntimeMarkerFormatError(
                            "obligation cleanup non-objet; runtime.json préservé"
                        )
                    item_kind = _strict_text(item.get("kind")).casefold()
                    if item_kind == "layout_fade":
                        try:
                            parsed = normalize_layout_fade_cleanup(
                                item,
                                schema=schema,
                                strict_current=strict_current_schema,
                            )
                        except LayoutFadeCleanupFormatError as exc:
                            raise RuntimeMarkerFormatError(
                                f"{exc}; runtime.json préservé"
                            ) from exc
                    else:
                        parsed = _normalize_cleanup_item(
                            item,
                            schema=schema,
                            strict_current=strict_current_schema,
                        )
                        if parsed is None:
                            raise RuntimeMarkerFormatError(
                                "obligation cleanup inconnue ou incomplète; "
                                "runtime.json préservé"
                            )
                    normalized.append(parsed)
                self.previous_pending_cleanup = tuple(normalized)

            self.finalized = False
            self._write(
                False,
                cleanup_complete=False,
                pending_cleanup=self.previous_pending_cleanup,
            )

    def finish(
        self,
        *,
        clean_shutdown: bool,
        cleanup_complete: bool,
        pending_cleanup=(),
    ) -> None:
        with self._write_lock:
            self._write(
                bool(clean_shutdown),
                cleanup_complete=bool(cleanup_complete),
                pending_cleanup=pending_cleanup,
            )
            # Commit finalization under the same lock as the durable write.
            # Late worker callbacks must not dirty the marker after shutdown.
            self.finalized = True

    def checkpoint_pending_cleanup(self, pending_cleanup) -> None:
        """Durably journal live cleanup obligations without finalizing the session."""
        with self._write_lock:
            if self.finalized:
                raise RuntimeError(
                    "runtime.json déjà finalisé; checkpoint de cleanup refusé"
                )
            self._write(
                False,
                cleanup_complete=False,
                pending_cleanup=pending_cleanup,
            )

    def clean_shutdown(self) -> None:
        self.finish(clean_shutdown=True, cleanup_complete=True, pending_cleanup=())

    def _write(self, clean: bool, *, cleanup_complete: bool, pending_cleanup) -> None:
        with self._write_lock:
            normalized: list[dict[str, object]] = []
            for item in pending_cleanup:
                if not isinstance(item, Mapping):
                    raise RuntimeMarkerFormatError(
                        "obligation cleanup runtime non-objet; écriture refusée"
                    )
                item_kind = _strict_text(item.get("kind")).casefold()
                if item_kind == "layout_fade":
                    try:
                        parsed = normalize_layout_fade_cleanup(
                            item,
                            schema=CLEANUP_SCHEMA_VERSION,
                            strict_current=False,
                        )
                    except LayoutFadeCleanupFormatError as exc:
                        raise RuntimeMarkerFormatError(
                            f"{exc}; écriture refusée"
                        ) from exc
                else:
                    parsed = _normalize_cleanup_item(item)
                    if parsed is None:
                        raise RuntimeMarkerFormatError(
                            "obligation cleanup runtime inconnue ou incomplète; "
                            "écriture refusée"
                        )
                normalized.append(parsed)
            payload = {
                "clean_shutdown": bool(clean),
                "cleanup_complete": bool(cleanup_complete),
                "cleanup_schema": CLEANUP_SCHEMA_VERSION,
                "pending_cleanup": normalized,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temp.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, indent=2)
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
            except Exception:
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
