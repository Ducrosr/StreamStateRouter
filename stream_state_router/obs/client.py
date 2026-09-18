from __future__ import annotations

import threading
import time
from typing import Any

from .models import OBSConnectionConfig

try:
    import obsws_python as _obs
    from obsws_python.error import OBSSDKRequestError as _OBSSDKRequestError
    _OBS_REQUEST_ERRORS: tuple[type[BaseException], ...] = (_OBSSDKRequestError,)
except Exception:  # pragma: no cover - optional dependency / runtime guard
    _obs = None
    _OBS_REQUEST_ERRORS = ()


class OBSUnavailableError(RuntimeError):
    pass


class OBSRequestError(RuntimeError):
    """OBS is connected, but rejected a valid WebSocket request.

    Resource-not-found / invalid-request responses are application-level errors,
    not transport failures. Keeping that distinction prevents one deleted scene
    item from putting the whole OBS client into reconnect backoff.
    """

    pass


class OBSClientManager:
    """Small reconnecting obs-websocket v5 client wrapper."""

    def __init__(self, config: OBSConnectionConfig):
        self._config = config
        self._client = None
        self._lock = threading.RLock()
        self._last_failure = 0.0
        self._connected = False
        self.last_error = ""

    @property
    def config(self) -> OBSConnectionConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._connected

    def configure(self, config: OBSConnectionConfig) -> None:
        with self._lock:
            if config == self._config:
                return
            self._config = config
            self._client = None
            self._connected = False
            self.last_error = ""
            self._last_failure = 0.0

    def probe(self) -> tuple[bool, str]:
        try:
            response = self.send("GetVersion")
            version = str(response.get("obsVersion") or response.get("obs_version") or "?")
            return True, f"OBS WebSocket connecté — OBS {version}"
        except Exception as exc:
            return False, str(exc)

    def send(self, request: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._config.enabled:
            raise OBSUnavailableError("L'intégration OBS est désactivée")
        with self._lock:
            client = self._ensure_client()
            try:
                if data is None:
                    response = client.send(request, raw=True)
                else:
                    response = client.send(request, data, raw=True)
                self._connected = True
                self.last_error = ""
                return response if isinstance(response, dict) else {}
            except _OBS_REQUEST_ERRORS as exc:
                # OBS answered the request, so the WebSocket connection is still
                # healthy. Do NOT start reconnect backoff for request-level
                # errors such as "scene item not found". Callers can handle the
                # missing resource and continue applying the rest of the layout.
                self._connected = True
                self.last_error = str(exc)
                raise OBSRequestError(f"OBS WebSocket request {request}: {exc}") from exc
            except Exception as exc:
                # Transport/session failures really do invalidate the ReqClient.
                self._client = None
                self._connected = False
                self.last_error = str(exc)
                self._last_failure = time.monotonic()
                raise OBSUnavailableError(f"OBS WebSocket : {exc}") from exc

    def _ensure_client(self):
        if _obs is None:
            raise OBSUnavailableError(
                "obsws-python n'est pas installé. Installez les dépendances OBS de l'application."
            )
        if self._client is not None:
            return self._client
        elapsed = time.monotonic() - self._last_failure
        if self._last_failure and elapsed < self._config.reconnect_seconds:
            remaining = self._config.reconnect_seconds - elapsed
            raise OBSUnavailableError(f"Reconnexion OBS dans {remaining:.1f} s")
        try:
            self._client = _obs.ReqClient(
                host=self._config.host,
                port=self._config.port,
                password=self._config.password,
                timeout=self._config.timeout_seconds,
            )
            self._connected = True
            return self._client
        except Exception as exc:
            self._last_failure = time.monotonic()
            self._connected = False
            self.last_error = str(exc)
            raise OBSUnavailableError(f"Connexion OBS impossible : {exc}") from exc
