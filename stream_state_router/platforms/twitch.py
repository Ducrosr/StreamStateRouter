from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import threading
import time
from typing import Any, Mapping
from urllib.request import Request, urlopen

import websocket

from ..events import EventBus


@dataclass(frozen=True, slots=True)
class TwitchEventSubConfig:
    enabled: bool = False
    client_id: str = ""
    user_access_token: str = ""
    broadcaster_user_id: str = ""
    user_id: str = ""
    moderator_user_id: str = ""
    websocket_url: str = "wss://eventsub.wss.twitch.tv/ws"
    connect_timeout_seconds: float = 10.0
    subscriptions: tuple[str, ...] = (
        "channel.chat.message",
        "channel.follow",
        "channel.subscribe",
        "channel.subscription.gift",
        "channel.subscription.message",
        "channel.cheer",
        "channel.raid",
    )


_SUBSCRIPTION_VERSIONS = {
    "channel.chat.message": "1",
    "channel.follow": "2",
    "channel.subscribe": "1",
    "channel.subscription.gift": "1",
    "channel.subscription.message": "1",
    "channel.cheer": "1",
    "channel.raid": "1",
}


class TwitchEventSubAdapter:
    platform = "twitch"

    def __init__(
        self,
        config: TwitchEventSubConfig,
        event_bus: EventBus,
        *,
        websocket_app_factory=websocket.WebSocketApp,
        clock=time.monotonic,
    ):
        self.config = config
        self.event_bus = event_bus
        self._websocket_app_factory = websocket_app_factory
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._session_id = ""
        self._reconnect_url = ""
        self._last_error = ""
        self._last_message_at = 0.0
        self._dedupe_order: deque[str] = deque(maxlen=1000)
        self._dedupe: set[str] = set()
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _remember_message(self, message_id: str) -> bool:
        value = str(message_id or "").strip()
        if not value:
            return True
        with self._lock:
            if value in self._dedupe:
                return False
            if len(self._dedupe_order) == self._dedupe_order.maxlen:
                oldest = self._dedupe_order.popleft()
                self._dedupe.discard(oldest)
            self._dedupe_order.append(value)
            self._dedupe.add(value)
        return True

    def _condition_for(self, kind: str) -> dict[str, str]:
        broadcaster = self.config.broadcaster_user_id.strip()
        user_id = (self.config.user_id or broadcaster).strip()
        moderator = (
            self.config.moderator_user_id or broadcaster
        ).strip()
        if kind == "channel.chat.message":
            return {
                "broadcaster_user_id": broadcaster,
                "user_id": user_id,
            }
        if kind == "channel.follow":
            return {
                "broadcaster_user_id": broadcaster,
                "moderator_user_id": moderator,
            }
        if kind == "channel.raid":
            return {"to_broadcaster_user_id": broadcaster}
        return {"broadcaster_user_id": broadcaster}

    def _create_subscription(
        self,
        kind: str,
        session_id: str,
    ) -> None:
        version = _SUBSCRIPTION_VERSIONS.get(kind)
        if version is None:
            raise ValueError(f"Subscription Twitch inconnue : {kind}")
        payload = json.dumps(
            {
                "type": kind,
                "version": version,
                "condition": self._condition_for(kind),
                "transport": {
                    "method": "websocket",
                    "session_id": session_id,
                },
            }
        ).encode("utf-8")
        request = Request(
            "https://api.twitch.tv/helix/eventsub/subscriptions",
            data=payload,
            method="POST",
            headers={
                "Authorization": (
                    f"Bearer {self.config.user_access_token}"
                ),
                "Client-Id": self.config.client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=10.0) as response:
            response.read()

    def _subscribe_all(self, session_id: str) -> None:
        for kind in self.config.subscriptions:
            self._create_subscription(kind, session_id)

    @staticmethod
    def _user(event: Mapping[str, Any]) -> str:
        return str(
            event.get("user_name")
            or event.get("from_broadcaster_user_name")
            or ""
        )

    def _publish_notification(
        self,
        kind: str,
        event: Mapping[str, Any],
    ) -> None:
        user = self._user(event)
        if kind == "channel.chat.message":
            message = event.get("message")
            text = (
                str(message.get("text") or "")
                if isinstance(message, Mapping)
                else ""
            )
            self.event_bus.publish(
                channel="chat",
                type="message",
                platform=self.platform,
                payload={
                    "display_name": user,
                    "user_name": str(event.get("user_login") or ""),
                    "user_id": str(event.get("user_id") or ""),
                    "text": text,
                    "color": str(event.get("color") or ""),
                    "message_id": str(event.get("message_id") or ""),
                },
            )
            return

        payload: dict[str, object] = {
            "display_name": user,
            "user_name": str(event.get("user_login") or ""),
            "user_id": str(event.get("user_id") or ""),
        }
        event_type = kind.rsplit(".", 1)[-1]
        label = event_type
        alert_title = ""

        if kind == "channel.follow":
            event_type = "follow"
            label = "Nouveau follower"
        elif kind == "channel.subscribe":
            event_type = "subscription"
            label = "Nouvel abonnement"
            payload["tier"] = str(event.get("tier") or "")
            payload["is_gift"] = bool(event.get("is_gift"))
            alert_title = "NOUVEL ABONNÉ"
        elif kind == "channel.subscription.gift":
            event_type = "subscription_gift"
            label = "Sub gifts"
            payload["total"] = int(event.get("total") or 0)
            payload["tier"] = str(event.get("tier") or "")
            alert_title = "SUB GIFTS"
        elif kind == "channel.subscription.message":
            event_type = "resubscription"
            label = "Réabonnement"
            payload["tier"] = str(event.get("tier") or "")
            payload["cumulative_months"] = int(
                event.get("cumulative_months") or 0
            )
            message = event.get("message")
            if isinstance(message, Mapping):
                payload["text"] = str(message.get("text") or "")
            alert_title = "RÉABONNEMENT"
        elif kind == "channel.cheer":
            event_type = "cheer"
            label = "Cheer"
            payload["bits"] = int(event.get("bits") or 0)
            payload["text"] = str(event.get("message") or "")
            payload["is_anonymous"] = bool(
                event.get("is_anonymous")
            )
            alert_title = "CHEER"
        elif kind == "channel.raid":
            event_type = "raid"
            label = "Raid"
            payload["display_name"] = str(
                event.get("from_broadcaster_user_name") or ""
            )
            payload["viewer_count"] = int(
                event.get("viewers") or 0
            )
            alert_title = "RAID"

        payload["label"] = label
        self.event_bus.publish(
            channel="events",
            type=event_type,
            platform=self.platform,
            payload=payload,
        )
        if alert_title:
            alert_payload = dict(payload)
            alert_payload["title"] = alert_title
            alert_payload.setdefault(
                "text",
                str(alert_payload.get("display_name") or ""),
            )
            alert_payload["duration_ms"] = 5000
            self.event_bus.publish(
                channel="alerts",
                type=event_type,
                platform=self.platform,
                payload=alert_payload,
            )

    def _handle_message(self, raw: str) -> None:
        message = json.loads(raw)
        if not isinstance(message, Mapping):
            return
        metadata = message.get("metadata")
        payload = message.get("payload")
        if not isinstance(metadata, Mapping) or not isinstance(
            payload,
            Mapping,
        ):
            return
        message_id = str(metadata.get("message_id") or "")
        if not self._remember_message(message_id):
            return
        with self._lock:
            self._last_message_at = self._clock()

        message_type = str(metadata.get("message_type") or "")
        if message_type == "session_welcome":
            session = payload.get("session")
            if not isinstance(session, Mapping):
                return
            session_id = str(session.get("id") or "")
            with self._lock:
                self._session_id = session_id
            if session_id:
                self._subscribe_all(session_id)
            return
        if message_type == "session_reconnect":
            session = payload.get("session")
            if isinstance(session, Mapping):
                with self._lock:
                    self._reconnect_url = str(
                        session.get("reconnect_url") or ""
                    )
            return
        if message_type == "notification":
            subscription = payload.get("subscription")
            event = payload.get("event")
            if isinstance(subscription, Mapping) and isinstance(
                event,
                Mapping,
            ):
                self._publish_notification(
                    str(subscription.get("type") or ""),
                    event,
                )
            return
        if message_type == "revocation":
            subscription = payload.get("subscription")
            reason = (
                str(subscription.get("status") or "")
                if isinstance(subscription, Mapping)
                else "revoked"
            )
            with self._lock:
                self._last_error = f"Subscription Twitch révoquée : {reason}"

    def _run_socket(self, url: str) -> None:
        def on_message(_ws, message):
            try:
                self._handle_message(str(message))
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)

        def on_error(_ws, error):
            with self._lock:
                self._last_error = str(error)

        def on_close(_ws, _code, _reason):
            return

        app = self._websocket_app_factory(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        with self._lock:
            self._socket = app
        app.run_forever()

    def _run(self) -> None:
        url = self.config.websocket_url
        while not self._stop.is_set():
            with self._lock:
                reconnect = self._reconnect_url
                self._reconnect_url = ""
            if reconnect:
                url = reconnect
            try:
                self._run_socket(url)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
            if self._stop.wait(2.0):
                break
            # A normal network reconnect gets a fresh session and therefore
            # re-subscribes on the next welcome. A Twitch reconnect URL keeps
            # subscriptions attached to the migrated session.
            if not reconnect:
                url = self.config.websocket_url

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        for label, value in (
            ("client_id", self.config.client_id),
            ("user_access_token", self.config.user_access_token),
            ("broadcaster_user_id", self.config.broadcaster_user_id),
        ):
            if not str(value).strip():
                raise ValueError(f"Twitch {label} requis")
        self._stop.clear()
        thread = threading.Thread(
            target=self._run,
            name="SSR-TwitchEventSub",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            socket = self._socket
            self._socket = None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=3.0)
