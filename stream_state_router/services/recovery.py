from __future__ import annotations

import json
from datetime import datetime, timezone
from .paths import user_data_dir


class RuntimeMarker:
    def __init__(self) -> None:
        self.path = user_data_dir() / "runtime.json"
        self.previous_unclean = False

    def start(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.previous_unclean = data.get("clean_shutdown") is False
            except Exception:
                self.previous_unclean = True
        self._write(False)

    def clean_shutdown(self) -> None:
        self._write(True)

    def _write(self, clean: bool) -> None:
        payload = {
            "clean_shutdown": clean,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
