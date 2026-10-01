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


class MediaArtworkStore:
    """Thread-safe in-memory artwork cache populated only by MediaRuntime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revision = 0
        self._content = b""
        self._content_type = ""
        self._identity = ""

    def update(
        self,
        content: bytes,
        *,
        content_type: str,
        identity: str,
    ) -> int:
        body = bytes(content)
        mime = str(content_type or "").split(";", 1)[0].strip().casefold()
        if mime not in {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
        }:
            raise ValueError("Type de pochette média non autorisé")
        if not body:
            raise ValueError("Pochette média vide")
        with self._lock:
            self._revision += 1
            self._content = body
            self._content_type = mime
            self._identity = str(identity or "")
            return self._revision

    def clear(self) -> int:
        with self._lock:
            if not self._content and not self._content_type:
                return self._revision
            self._revision += 1
            self._content = b""
            self._content_type = ""
            self._identity = ""
            return self._revision

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "revision": self._revision,
                "available": bool(self._content),
                "content": bytes(self._content),
                "content_type": self._content_type,
                "identity": self._identity,
            }
