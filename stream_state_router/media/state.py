from __future__ import annotations

import threading
import time

from .models import MediaState


class MediaStateStore:
    """Thread-safe latest-state bridge for widgets/API consumers."""

    def __init__(
        self,
        provider: str = "",
        *,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._revision = 0
        self._updated_at = 0.0
        self._stale_after_seconds = 3.0
        self._state = MediaState(
            provider=str(provider or ""),
            connected=False,
        )

    def update(self, state: MediaState) -> dict[str, object]:
        with self._lock:
            self._revision += 1
            self._state = state
            self._updated_at = float(self._clock())
            return self._mapping_locked()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._mapping_locked()

    def set_expected_poll(self, poll_seconds: float) -> None:
        value = max(0.1, float(poll_seconds))
        with self._lock:
            self._stale_after_seconds = max(1.0, value * 3.0)

    def public_snapshot(
        self,
        *,
        stale_after_seconds: float | None = None,
    ) -> dict[str, object]:
        with self._lock:
            payload = self._state.as_public_mapping()
            payload["revision"] = self._revision
            now = float(self._clock())
            age = (
                max(0.0, now - self._updated_at)
                if self._updated_at > 0.0
                else 0.0
            )
            payload["age_seconds"] = age
            threshold = (
                self._stale_after_seconds
                if stale_after_seconds is None
                else max(0.1, float(stale_after_seconds))
            )
            payload["stale"] = bool(
                self._revision == 0
                or age > threshold
            )
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
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Pochette média trop volumineuse")
        signatures = {
            "image/jpeg": body.startswith(b"\xff\xd8\xff"),
            "image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/gif": body.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": (
                len(body) >= 12
                and body.startswith(b"RIFF")
                and body[8:12] == b"WEBP"
            ),
        }
        if not signatures.get(mime, False):
            raise ValueError(
                "Contenu de pochette incompatible avec son type MIME"
            )
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



class MediaCommandStore:
    """Thread-safe bounded lifecycle registry for accepted media commands."""

    _TERMINAL = frozenset({"completed", "failed", "uncertain"})

    def __init__(
        self,
        *,
        max_records: int = 250,
        max_expired: int = 250,
    ) -> None:
        self._lock = threading.RLock()
        self._max_records = max(1, int(max_records))
        self._max_expired = max(1, int(max_expired))
        self._records: dict[str, dict[str, object]] = {}
        self._expired: dict[str, None] = {}

    def reserve(
        self,
        request_id: str,
        action: str,
        *,
        state: dict[str, object] | None = None,
    ) -> None:
        key = str(request_id or "")
        if not key:
            raise ValueError("request_id média requis")
        with self._lock:
            self._compact_locked()
            if len(self._records) >= self._max_records:
                raise RuntimeError("Historique des commandes média saturé")
            self._expired.pop(key, None)
            self._records[key] = {
                "request_id": key,
                "action": str(action or ""),
                "status": "queued",
                "success": None,
                "error": "",
                "state": dict(state or {}),
            }

    def discard(self, request_id: str) -> None:
        key = str(request_id or "")
        with self._lock:
            self._records.pop(key, None)
            self._expired.pop(key, None)

    def mark_running(self, request_id: str) -> None:
        key = str(request_id or "")
        with self._lock:
            row = self._records.get(key)
            if row is None:
                return
            if str(row.get("status") or "") != "queued":
                return
            row["status"] = "running"

    def finish(
        self,
        request_id: str,
        *,
        success: bool,
        error: str = "",
        state: dict[str, object] | None = None,
        status: str = "",
    ) -> None:
        key = str(request_id or "")
        with self._lock:
            row = self._records.get(key)
            if row is None:
                return
            terminal_status = str(status or "").strip().casefold()
            if terminal_status not in self._TERMINAL:
                terminal_status = "completed" if success else "failed"
            row["status"] = terminal_status
            row["success"] = bool(success)
            row["error"] = str(error or "")
            row["state"] = dict(state or {})

    def get(self, request_id: str) -> dict[str, object] | None:
        key = str(request_id or "")
        with self._lock:
            row = self._records.get(key)
            if row is not None:
                result = dict(row)
                state = row.get("state")
                result["state"] = (
                    dict(state) if isinstance(state, dict) else {}
                )
                return result
            if key in self._expired:
                return {
                    "request_id": key,
                    "status": "expired",
                    "success": False,
                    "error": "Résultat de commande média expiré",
                    "state": {},
                }
            return None

    def _compact_locked(self) -> None:
        if len(self._records) < self._max_records:
            return
        for key in tuple(self._records):
            row = self._records.get(key)
            if row is None:
                continue
            if str(row.get("status") or "") not in self._TERMINAL:
                continue
            self._records.pop(key, None)
            self._expired[key] = None
            while len(self._expired) > self._max_expired:
                first = next(iter(self._expired))
                self._expired.pop(first, None)
            if len(self._records) < self._max_records:
                return

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
