from __future__ import annotations

import threading

from .models import MediaState


class MediaStateStore:
    """Thread-safe latest-state bridge for widgets/API consumers."""

    def __init__(self, provider: str = "") -> None:
        self._lock = threading.RLock()
        self._revision = 0
        self._state = MediaState(
            provider=str(provider or ""),
            connected=False,
        )

    def update(self, state: MediaState) -> dict[str, object]:
        with self._lock:
            self._revision += 1
            self._state = state
            return self._mapping_locked()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._mapping_locked()

    def public_snapshot(self) -> dict[str, object]:
        with self._lock:
            payload = self._state.as_public_mapping()
            payload["revision"] = self._revision
            return payload

    def _mapping_locked(self) -> dict[str, object]:
        payload = self._state.as_mapping()
        payload["revision"] = self._revision
        return payload
