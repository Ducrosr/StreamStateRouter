from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Mapping

from .paths import user_data_dir


CLEANUP_SCHEMA_VERSION = 3


def _normalize_cleanup_item(raw: Mapping[str, object]) -> dict[str, object] | None:
    item = dict(raw)
    kind = str(item.get("kind") or "").strip().casefold()
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
        source = str(item.get("source") or "").strip()
        collection = str(item.get("collection") or "").strip()
        if not source or not collection:
            return None
        item["kind"] = "layout_fade"

        helper_id = str(item.get("helper_id") or "").strip()
        if not helper_id:
            # Schema v2 only knew collection + source. Preserve it for
            # diagnostics/transfer, but never promote it into helper ownership.
            item["legacy"] = True
            return item

        connection = item.get("connection")
        if not isinstance(connection, Mapping):
            return None
        host = str(connection.get("host") or "").strip()
        try:
            port = int(connection.get("port", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        required = (
            str(item.get("source_uuid") or "").strip(),
            str(item.get("source_kind") or "").strip(),
            str(item.get("filter_name") or "").strip(),
            str(item.get("filter_kind") or "").strip(),
            str(item.get("cleanup_action") or "").strip(),
        )
        if not host or port <= 0 or not all(required):
            return None
        item["connection"] = {"host": host.casefold(), "port": port}
        item["legacy"] = False
        return item
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
                    self.previous_unclean = data.get("clean_shutdown") is False
                    self.previous_cleanup_incomplete = data.get("cleanup_complete") is False
                    raw_pending = data.get("pending_cleanup", [])
                    if isinstance(raw_pending, list):
                        normalized: list[dict[str, object]] = []
                        for item in raw_pending:
                            if not isinstance(item, Mapping):
                                continue
                            parsed = _normalize_cleanup_item(item)
                            if parsed is not None:
                                normalized.append(parsed)
                        self.previous_pending_cleanup = tuple(normalized)
                except Exception:
                    self.previous_unclean = True
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
                return
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
                    continue
                parsed = _normalize_cleanup_item(item)
                if parsed is not None:
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
