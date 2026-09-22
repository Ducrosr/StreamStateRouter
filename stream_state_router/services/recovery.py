from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

from .paths import user_data_dir


CLEANUP_SCHEMA_VERSION = 2


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
        if not str(item.get("source") or "").strip():
            return None
        if not str(item.get("collection") or "").strip():
            return None
        item["kind"] = "layout_fade"
        return item
    return None


class RuntimeMarker:
    def __init__(self) -> None:
        self.path = user_data_dir() / "runtime.json"
        self.previous_unclean = False
        self.previous_cleanup_incomplete = False
        self.previous_pending_cleanup: tuple[dict[str, object], ...] = ()
        self.finalized = False

    def start(self) -> None:
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
        self._write(False, cleanup_complete=False, pending_cleanup=self.previous_pending_cleanup)

    def finish(
        self,
        *,
        clean_shutdown: bool,
        cleanup_complete: bool,
        pending_cleanup=(),
    ) -> None:
        self._write(
            bool(clean_shutdown),
            cleanup_complete=bool(cleanup_complete),
            pending_cleanup=pending_cleanup,
        )
        self.finalized = True

    def clean_shutdown(self) -> None:
        self.finish(clean_shutdown=True, cleanup_complete=True, pending_cleanup=())

    def _write(self, clean: bool, *, cleanup_complete: bool, pending_cleanup) -> None:
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
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.path)
