from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

from ..events import EventBus
from .twitch import (
    TwitchEventSubMessageProcessor,
    build_default_subscriptions,
    required_scopes,
)
from .twitch_session import (
    TWITCH_EVENTSUB_WS_URL,
    TwitchEventSubSessionCoordinator,
    TwitchHelixClient,
    TwitchTokenValidation,
)


class TwitchQtEventSubService(QObject):
    """Qt EventSub WebSocket service.

    Network events feed only the platform EventBus. This service never calls
    OBS or the SSR routing worker.
    """

    status_changed = Signal(object)
    error = Signal(str)
    subscription_result = Signal(str, bool, str)
    _validation_ready = Signal(object, str)
    _subscription_ready = Signal(str, bool, str)

    def __init__(
        self,
        event_bus: EventBus,
        *,
        client_id: str,
        access_token_provider: Callable[[], str],
        broadcaster_user_id: str = "",
        user_id: str = "",
        moderator_user_id: str = "",
        helix: TwitchHelixClient | None = None,
        socket_factory: Callable[[QObject], QWebSocket] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.event_bus = event_bus
        self.client_id = str(client_id or "").strip()
        self._access_token_provider = access_token_provider
        self._configured_broadcaster_id = str(
            broadcaster_user_id or ""
        ).strip()
        self._configured_user_id = str(user_id or "").strip()
        self._configured_moderator_id = str(
            moderator_user_id or ""
        ).strip()
        self._helix = helix or TwitchHelixClient()
        self._socket_factory = (
            socket_factory
            if socket_factory is not None
            else lambda owner: QWebSocket("", parent=owner)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="SSR-Twitch",
        )
        self._processor = TwitchEventSubMessageProcessor(event_bus)
        self._coordinator: TwitchEventSubSessionCoordinator | None = None
        self._validation: TwitchTokenValidation | None = None
        self._active: QWebSocket | None = None
        self._candidate: QWebSocket | None = None
        self._stopping = False
        self._retry_attempt = 0
        self._token = ""

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_keepalive)

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._connect_primary)

        self._validation_ready.connect(self._on_validation_ready)
        self._subscription_ready.connect(
            self._on_subscription_ready
        )

    @property
    def running(self) -> bool:
        return bool(
            not self._stopping
            and (
                self._active is not None
                or self._candidate is not None
            )
        )

    def start(self) -> None:
        if not self.client_id:
            self.error.emit("Client ID Twitch requis")
            return
        self._stopping = False
        self._retry_attempt = 0
        token = str(self._access_token_provider() or "").strip()
        if not token:
            self.error.emit(
                "Aucun token Twitch disponible ; reconnectez le compte."
            )
            return
        self._token = token
        self._emit_status("validation")
        future = self._executor.submit(
            self._helix.validate_token,
            token,
        )
        future.add_done_callback(self._validation_done)

    def stop(self) -> None:
        self._stopping = True
        self._retry.stop()
        self._watchdog.stop()
        for socket in (self._candidate, self._active):
            if socket is not None:
                socket.abort()
                socket.deleteLater()
        self._candidate = None
        self._active = None
        if self._coordinator is not None:
            self._coordinator.connection_lost()
        self._emit_status("stopped")

    def close(self) -> None:
        self.stop()
        self._executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

    def _validation_done(self, future: Future) -> None:
        try:
            validation = future.result()
        except Exception as exc:
            self._validation_ready.emit(None, str(exc))
        else:
            self._validation_ready.emit(validation, "")

    def _on_validation_ready(
        self,
        validation: TwitchTokenValidation | None,
        error: str,
    ) -> None:
        if self._stopping:
            return
        if error or validation is None:
            self.error.emit(
                error or "Validation Twitch impossible"
            )
            self._emit_status("error")
            return
        if (
            validation.client_id
            and validation.client_id != self.client_id
        ):
            self.error.emit(
                "Le token Twitch appartient à un autre Client ID."
            )
            self._emit_status("error")
            return

        broadcaster = (
            self._configured_broadcaster_id
            or validation.user_id
        )
        user_id = self._configured_user_id or validation.user_id
        moderator = (
            self._configured_moderator_id
            or broadcaster
        )
        try:
            subscriptions = build_default_subscriptions(
                broadcaster_user_id=broadcaster,
                user_id=user_id,
                moderator_user_id=moderator,
            )
        except Exception as exc:
            self.error.emit(str(exc))
            self._emit_status("error")
            return

        missing = sorted(
            set(required_scopes(subscriptions))
            - set(validation.scopes),
            key=str.casefold,
        )
        if missing:
            self.error.emit(
                "Scopes Twitch manquants : "
                + ", ".join(missing)
            )
            self._emit_status(
                "authorization_required",
                missing_scopes=missing,
            )
            return

        self._validation = validation
        self._coordinator = TwitchEventSubSessionCoordinator(
            self._processor,
            subscriptions,
        )
        self._connect_primary()

    def _connect_primary(self) -> None:
        if self._stopping or self._coordinator is None:
            return
        if self._active is not None:
            self._active.abort()
            self._active.deleteLater()
            self._active = None
        self._coordinator.connection_lost()
        self._emit_status("connecting")
        self._active = self._open_socket(
            TWITCH_EVENTSUB_WS_URL,
            candidate=False,
        )

    def _open_socket(
        self,
        url: str,
        *,
        candidate: bool,
    ) -> QWebSocket:
        socket = self._socket_factory(self)
        socket.textMessageReceived.connect(
            lambda message, s=socket: self._on_text(s, message)
        )
        socket.connected.connect(
            lambda s=socket: self._on_connected(s)
        )
        socket.disconnected.connect(
            lambda s=socket: self._on_disconnected(s)
        )
        socket.errorOccurred.connect(
            lambda _error, s=socket: self._on_socket_error(s)
        )
        socket.open(QUrl(url))
        if candidate:
            self._candidate = socket
        return socket

    def _on_connected(self, socket: QWebSocket) -> None:
        if self._stopping:
            return
        role = (
            "reconnecting"
            if socket is self._candidate
            else "connected_transport"
        )
        self._emit_status(role)

    def _on_text(
        self,
        socket: QWebSocket,
        message: str,
    ) -> None:
        if self._stopping or self._coordinator is None:
            return
        try:
            frame = json.loads(str(message))
            if not isinstance(frame, dict):
                raise ValueError("Frame EventSub non objet")
            instruction = self._coordinator.handle(frame)
        except Exception as exc:
            self.error.emit(f"EventSub invalide : {exc}")
            return

        result = instruction.result
        if result.duplicate:
            return

        if result.kind == "welcome":
            self._watchdog.start()
            self._retry_attempt = 0
            if instruction.close_old_after_welcome:
                if socket is not self._candidate:
                    self.error.emit(
                        "Welcome de reconnexion reçu sur une socket inattendue."
                    )
                    return
                old = self._active
                self._active = self._candidate
                self._candidate = None
                if old is not None:
                    old.close()
                    old.deleteLater()
                self._emit_status(
                    "connected",
                    session_id=result.session_id,
                )
                return

            if socket is self._active:
                self._emit_status(
                    "subscribing",
                    session_id=result.session_id,
                )
                for payload in instruction.subscribe:
                    self._submit_subscription(payload)
            return

        if result.kind == "reconnect":
            url = instruction.reconnect_url
            if not url:
                self.error.emit(
                    "Twitch a demandé une reconnexion sans URL."
                )
                return
            if self._candidate is not None:
                self._candidate.abort()
                self._candidate.deleteLater()
            self._open_socket(url, candidate=True)
            self._emit_status("reconnecting")
            return

        if result.kind == "revocation":
            self.error.emit(
                "Subscription Twitch révoquée : "
                f"{result.subscription_type} "
                f"({result.subscription_status})"
            )
            self._emit_status(
                "degraded",
                subscription=result.subscription_type,
                subscription_status=result.subscription_status,
            )
            return

    def _submit_subscription(
        self,
        payload: dict[str, object],
    ) -> None:
        subscription_type = str(
            payload.get("type") or ""
        )
        future = self._executor.submit(
            self._helix.create_subscription,
            client_id=self.client_id,
            access_token=self._token,
            payload=payload,
        )
        future.add_done_callback(
            lambda fut, kind=subscription_type:
            self._subscription_done(kind, fut)
        )

    def _subscription_done(
        self,
        subscription_type: str,
        future: Future,
    ) -> None:
        try:
            future.result()
        except Exception as exc:
            self._subscription_ready.emit(
                subscription_type,
                False,
                str(exc),
            )
        else:
            self._subscription_ready.emit(
                subscription_type,
                True,
                "",
            )

    def _on_subscription_ready(
        self,
        subscription_type: str,
        success: bool,
        error: str,
    ) -> None:
        if self._stopping:
            return
        self.subscription_result.emit(
            subscription_type,
            success,
            error,
        )
        if not success:
            self.error.emit(
                f"Subscription {subscription_type} : {error}"
            )
            self._emit_status("degraded")
            return
        self._emit_status("connected")

    def _on_disconnected(self, socket: QWebSocket) -> None:
        if socket is self._candidate:
            self._candidate = None
            if not self._stopping:
                self.error.emit(
                    "Connexion de remplacement Twitch interrompue."
                )
            return
        if socket is not self._active:
            return
        self._active = None
        if self._stopping:
            return
        if self._coordinator is not None:
            self._coordinator.connection_lost()
        self._watchdog.stop()
        self._schedule_retry("disconnected")

    def _on_socket_error(self, socket: QWebSocket) -> None:
        if self._stopping:
            return
        self.error.emit(
            "Twitch WebSocket : " + socket.errorString()
        )

    def _check_keepalive(self) -> None:
        coordinator = self._coordinator
        if (
            self._stopping
            or coordinator is None
            or not coordinator.keepalive_expired(
                grace_seconds=2.0
            )
        ):
            return
        self.error.emit(
            "Twitch EventSub silencieux au-delà du keepalive."
        )
        socket = self._active
        if socket is not None:
            socket.abort()
        else:
            self._schedule_retry("keepalive_timeout")

    def _schedule_retry(self, reason: str) -> None:
        if self._stopping:
            return
        self._retry_attempt = min(
            self._retry_attempt + 1,
            8,
        )
        delay_ms = min(
            30000,
            1000 * (2 ** (self._retry_attempt - 1)),
        )
        self._emit_status(
            "retry_wait",
            reason=reason,
            retry_in_ms=delay_ms,
        )
        self._retry.start(delay_ms)

    def _emit_status(
        self,
        state: str,
        **extra: object,
    ) -> None:
        validation = self._validation
        coordinator = self._coordinator
        payload: dict[str, object] = {
            "state": str(state),
            "client_id": self.client_id,
            "login": (
                validation.login if validation is not None else ""
            ),
            "user_id": (
                validation.user_id if validation is not None else ""
            ),
            "session_id": (
                coordinator.session_id
                if coordinator is not None
                else ""
            ),
            "connected": bool(
                coordinator is not None
                and coordinator.connected
            ),
        }
        payload.update(extra)
        self.status_changed.emit(payload)
