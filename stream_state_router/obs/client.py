from __future__ import annotations

import threading
import time
import uuid
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


class OBSSceneCollectionContextChangedError(OBSUnavailableError):
    """The Scene Collection context changed across a qualified request."""

    def __init__(
        self,
        detail: str,
        *,
        request_submitted: bool,
    ):
        super().__init__(detail)
        self.request_submitted = bool(request_submitted)


class OBSRequestError(RuntimeError):
    """OBS is connected, but rejected a valid WebSocket request."""

    def __init__(self, request: str, detail: str):
        super().__init__(f"OBS WebSocket request {request}: {detail}")
        self.request = str(request)
        self.detail = str(detail)


class OBSResourceNotFoundError(OBSRequestError):
    """OBS confirmed that the requested source/scene item does not exist."""


def _is_confirmed_missing_request_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in {600, 601}:
        return True
    text = str(exc).casefold()
    markers = (
        "no scene items were found",
        "scene item not found",
        "source not found",
        "no source was found",
        "does not exist",
    )
    return any(marker in text for marker in markers)


class OBSClientManager:
    """Small reconnecting obs-websocket v5 client wrapper.

    Scene Collection-sensitive callers can additionally bind a request sequence
    to a monotonic collection generation.  The generation is fed by OBS
    CurrentSceneCollectionChanging/Changed events.  A custom-event barrier is
    used after qualified requests so an A -> B -> A round trip cannot be hidden
    merely because the collection name is equal again at the end.
    """

    _BARRIER_KEY = "__ssr_scene_collection_barrier"

    def __init__(self, config: OBSConnectionConfig):
        self._config = config
        self._client = None
        self._lock = threading.RLock()
        self._last_failure = 0.0
        self._connected = False
        self._request_count = 0
        self._session_generation = 0
        self.last_error = ""

        self._event_client = None
        self._event_session_generation = 0
        self._event_condition = threading.Condition(threading.RLock())
        self._scene_collection_generation = 0
        self._event_barriers_seen: set[str] = set()

    @property
    def config(self) -> OBSConnectionConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def request_count(self) -> int:
        """Monotonic count of OBS requests actually submitted on this manager."""
        with self._lock:
            return int(self._request_count)

    @property
    def session_generation(self) -> int:
        """Monotonic identity of the current OBS transport/session context."""

        with self._lock:
            return int(self._session_generation)

    @property
    def scene_collection_generation(self) -> int:
        """Monotonic identity invalidated by every observed collection change."""

        with self._event_condition:
            return int(self._scene_collection_generation)

    def on_current_scene_collection_changing(self, _data) -> None:
        with self._event_condition:
            self._scene_collection_generation += 1
            self._event_condition.notify_all()

    def on_current_scene_collection_changed(self, _data) -> None:
        with self._event_condition:
            self._scene_collection_generation += 1
            self._event_condition.notify_all()

    def on_custom_event(self, data) -> None:
        payload = getattr(data, "event_data", None)
        if not isinstance(payload, dict):
            return
        token = payload.get(self._BARRIER_KEY)
        if not isinstance(token, str) or not token:
            return
        with self._event_condition:
            self._event_barriers_seen.add(token)
            self._event_condition.notify_all()

    def configure(self, config: OBSConnectionConfig) -> None:
        event_client = None
        request_client = None
        with self._lock:
            if config == self._config:
                return
            self._config = config
            request_client = self._client
            self._client = None
            event_client = self._detach_event_client_locked()
            self._connected = False
            self._session_generation += 1
            self.last_error = ""
            self._last_failure = 0.0
        self._disconnect_client(request_client)
        self._disconnect_client(event_client)

    def close(self) -> None:
        """Close owned request/event clients after runtime cleanup has finished."""
        event_client = None
        request_client = None
        with self._lock:
            request_client = self._client
            self._client = None
            event_client = self._detach_event_client_locked()
            self._connected = False
            self._session_generation += 1
            self.last_error = ""
        self._disconnect_client(request_client)
        self._disconnect_client(event_client)

    @staticmethod
    def _disconnect_client(client) -> None:
        if client is None:
            return
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass

    def _detach_event_client_locked(self):
        client = self._event_client
        self._event_client = None
        self._event_session_generation = 0
        with self._event_condition:
            self._scene_collection_generation += 1
            self._event_barriers_seen.clear()
            self._event_condition.notify_all()
        return client

    def probe(self) -> tuple[bool, str]:
        try:
            response = self.send("GetVersion")
            version = str(response.get("obsVersion") or response.get("obs_version") or "?")
            return True, f"OBS WebSocket connecté — OBS {version}"
        except OBSRequestError as exc:
            cause = exc.__cause__
            code = getattr(cause, "code", None)
            lowered = str(exc).casefold()
            if code == 207 or "code 207" in lowered or "obs is not ready" in lowered:
                return (
                    False,
                    "OBS WebSocket connecté — OBS est encore en cours d'initialisation",
                )
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

    def _guarded_client_locked(
        self,
        expected_session_generation: int | None,
    ):
        if expected_session_generation is None:
            return self._ensure_client()

        expected = int(expected_session_generation)
        if expected <= 0:
            raise OBSUnavailableError(
                "Invalid expected OBS session generation for guarded request"
            )
        if (
            self._client is None
            or not self._connected
            or self._session_generation != expected
        ):
            raise OBSUnavailableError(
                "OBS session changed before guarded request; refusing reconnect"
            )
        return self._client

    def _submit_locked(
        self,
        client,
        request: str,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._request_count += 1
        request_error: BaseException | None = None
        response: dict[str, Any] = {}
        try:
            if data is None:
                raw = client.send(request, raw=True)
            else:
                raw = client.send(request, data, raw=True)
            response = raw if isinstance(raw, dict) else {}
            self._connected = True
            self.last_error = ""
        except _OBS_REQUEST_ERRORS as exc:
            self._connected = True
            self.last_error = str(exc)
            request_error = exc
        except Exception as exc:
            event_client = self._detach_event_client_locked()
            self._client = None
            self._connected = False
            self._session_generation += 1
            self.last_error = str(exc)
            self._last_failure = time.monotonic()
            self._disconnect_client(event_client)
            raise OBSUnavailableError(f"OBS WebSocket : {exc}") from exc

        if request_error is not None:
            error_type = (
                OBSResourceNotFoundError
                if _is_confirmed_missing_request_error(request_error)
                else OBSRequestError
            )
            raise error_type(request, str(request_error)) from request_error
        return response

    def _ensure_event_client_locked(self):
        if _obs is None or not hasattr(_obs, "EventClient"):
            raise OBSUnavailableError(
                "obsws-python EventClient indisponible; "
                "qualification Scene Collection impossible"
            )

        current = self._event_client
        worker = getattr(current, "worker", None)
        if (
            current is not None
            and self._event_session_generation == self._session_generation
            and worker is not None
            and worker.is_alive()
        ):
            return current

        stale = self._detach_event_client_locked()
        self._disconnect_client(stale)

        try:
            event_client = _obs.EventClient(
                host=self._config.host,
                port=self._config.port,
                password=self._config.password,
                timeout=self._config.timeout_seconds,
            )
            event_client.callback.register(
                [
                    self.on_current_scene_collection_changing,
                    self.on_current_scene_collection_changed,
                    self.on_custom_event,
                ]
            )
        except Exception as exc:
            self.last_error = str(exc)
            raise OBSUnavailableError(
                f"Flux d'événements OBS indisponible : {exc}"
            ) from exc

        self._event_client = event_client
        self._event_session_generation = self._session_generation
        with self._event_condition:
            # Replacing the observer invalidates every token captured from the
            # previous event stream even if OBS stayed on the same collection.
            self._scene_collection_generation += 1
            self._event_barriers_seen.clear()
            self._event_condition.notify_all()
        return event_client

    def _collection_event_barrier_locked(
        self,
        request_client,
        *,
        expected_session_generation: int,
    ) -> int:
        self._ensure_event_client_locked()
        if self._session_generation != int(expected_session_generation):
            raise OBSUnavailableError(
                "OBS session changed before Scene Collection barrier"
            )

        token = (
            f"{self._session_generation}:"
            f"{self._request_count}:"
            f"{uuid.uuid4().hex}"
        )
        self._submit_locked(
            request_client,
            "BroadcastCustomEvent",
            {"eventData": {self._BARRIER_KEY: token}},
        )

        timeout = max(0.25, float(self._config.timeout_seconds or 2.0))
        deadline = time.monotonic() + timeout
        with self._event_condition:
            while token not in self._event_barriers_seen:
                event_client = self._event_client
                worker = getattr(event_client, "worker", None)
                if (
                    event_client is None
                    or worker is None
                    or not worker.is_alive()
                    or self._event_session_generation != self._session_generation
                ):
                    raise OBSUnavailableError(
                        "Flux d'événements OBS perdu pendant la qualification "
                        "Scene Collection"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OBSUnavailableError(
                        "Timeout de synchronisation des événements Scene Collection"
                    )
                self._event_condition.wait(min(0.05, remaining))
            self._event_barriers_seen.discard(token)
            return int(self._scene_collection_generation)

    def scene_collection_context(
        self,
        *,
        expected_session_generation: int | None = None,
    ) -> tuple[str, int]:
        """Return a stable collection name plus a monotonic event generation."""

        with self._lock:
            client = self._guarded_client_locked(expected_session_generation)
            session_generation = int(self._session_generation)
            self._ensure_event_client_locked()

            # A stable sample requires no collection event between the barrier
            # preceding GetSceneCollectionList and the barrier following it.
            for _attempt in range(4):
                before = self._collection_event_barrier_locked(
                    client,
                    expected_session_generation=session_generation,
                )
                response = self._submit_locked(
                    client,
                    "GetSceneCollectionList",
                    None,
                )
                name = str(
                    response.get("currentSceneCollectionName") or ""
                ).strip()
                after = self._collection_event_barrier_locked(
                    client,
                    expected_session_generation=session_generation,
                )
                if before == after and name:
                    return name, after

            raise OBSSceneCollectionContextChangedError(
                "Scene Collection modifiée pendant sa qualification",
                request_submitted=False,
            )

    def send(
        self,
        request: str,
        data: dict[str, Any] | None = None,
        *,
        expected_session_generation: int | None = None,
        expected_scene_collection_generation: int | None = None,
    ) -> dict[str, Any]:
        if not self._config.enabled:
            raise OBSUnavailableError("L'intégration OBS est désactivée")

        with self._lock:
            client = self._guarded_client_locked(expected_session_generation)
            session_generation = int(self._session_generation)

            expected_collection_generation = (
                None
                if expected_scene_collection_generation is None
                else int(expected_scene_collection_generation)
            )

            if expected_collection_generation is not None:
                pre_generation = self._collection_event_barrier_locked(
                    client,
                    expected_session_generation=session_generation,
                )
                if pre_generation != expected_collection_generation:
                    raise OBSSceneCollectionContextChangedError(
                        "Scene Collection modifiée avant la requête OBS qualifiée",
                        request_submitted=False,
                    )

            request_error: BaseException | None = None
            response: dict[str, Any] = {}
            try:
                response = self._submit_locked(client, request, data)
            except (OBSRequestError, OBSResourceNotFoundError) as exc:
                request_error = exc

            if expected_collection_generation is not None:
                post_generation = self._collection_event_barrier_locked(
                    client,
                    expected_session_generation=session_generation,
                )
                if post_generation != expected_collection_generation:
                    raise OBSSceneCollectionContextChangedError(
                        f"Scene Collection modifiée pendant {request}",
                        request_submitted=True,
                    )

            if request_error is not None:
                raise request_error
            return response

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
            self._session_generation += 1
            return self._client
        except Exception as exc:
            self._last_failure = time.monotonic()
            self._connected = False
            self.last_error = str(exc)
            raise OBSUnavailableError(f"Connexion OBS impossible : {exc}") from exc
