from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

from .paths import user_data_dir


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
                    self.previous_pending_cleanup = tuple(
                        dict(item) for item in raw_pending if isinstance(item, Mapping)
                    )
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
        payload = {
            "clean_shutdown": bool(clean),
            "cleanup_complete": bool(cleanup_complete),
            "pending_cleanup": [dict(item) for item in pending_cleanup],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.path)
