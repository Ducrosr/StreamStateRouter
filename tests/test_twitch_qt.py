from __future__ import annotations

import json
import time
import unittest

from PySide6.QtCore import QCoreApplication, QObject, Signal

from stream_state_router.events import EventBus
from stream_state_router.platforms.twitch_qt import (
    TwitchQtEventSubService,
)
from stream_state_router.platforms.twitch_session import (
    TwitchTokenValidation,
)


class _FakeSocket(QObject):
    textMessageReceived = Signal(str)
    connected = Signal()
    disconnected = Signal()
    errorOccurred = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.opened_url = ""
        self.closed = False
        self.aborted = False

    def open(self, url) -> None:
        self.opened_url = url.toString()
        self.connected.emit()

    def close(self) -> None:
        self.closed = True
        self.disconnected.emit()

    def abort(self) -> None:
        self.aborted = True
        self.disconnected.emit()

    def errorString(self) -> str:
        return "fake-error"


class _FakeHelix:
    def __init__(self):
        self.created: list[dict] = []

    def validate_token(self, _token: str):
        return TwitchTokenValidation(
            client_id="client",
            user_id="123",
            login="remy",
            scopes=(
                "bits:read",
                "channel:read:redemptions",
                "channel:read:subscriptions",
                "moderator:read:followers",
                "user:read:chat",
            ),
            expires_in=3600,
        )

    def create_subscription(
        self,
        *,
        client_id: str,
        access_token: str,
        payload,
    ):
        self.created.append(dict(payload))
        return {"data": [{"status": "enabled"}]}


class TwitchQtTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _service(self):
        sockets: list[_FakeSocket] = []
        helix = _FakeHelix()
        bus = EventBus()

        def factory(parent):
            socket = _FakeSocket(parent)
            sockets.append(socket)
            return socket

        service = TwitchQtEventSubService(
            bus,
            client_id="client",
            access_token_provider=lambda: "access-token",
            helix=helix,
            socket_factory=factory,
        )
        self.addCleanup(service.close)
        service._token = "access-token"
        service._on_validation_ready(
            TwitchTokenValidation(
                client_id="client",
                user_id="123",
                login="remy",
                scopes=(
                    "bits:read",
                    "channel:read:redemptions",
                    "channel:read:subscriptions",
                    "moderator:read:followers",
                    "user:read:chat",
                ),
                expires_in=3600,
            ),
            "",
        )
        return service, bus, helix, sockets

    def _wait_until(self, predicate, timeout=2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Condition asynchrone non satisfaite")

    @staticmethod
    def _welcome(message_id: str, session_id: str) -> str:
        return json.dumps(
            {
                "metadata": {
                    "message_id": message_id,
                    "message_type": "session_welcome",
                },
                "payload": {
                    "session": {
                        "id": session_id,
                        "keepalive_timeout_seconds": 10,
                    }
                },
            }
        )

    def test_welcome_creates_subscriptions_and_notifications_reach_bus(self):
        service, bus, helix, sockets = self._service()
        self.assertEqual(len(sockets), 1)
        primary = sockets[0]

        primary.textMessageReceived.emit(
            self._welcome("welcome-1", "session-1")
        )
        self._wait_until(lambda: len(helix.created) == 8)

        self.assertTrue(
            all(
                row["transport"]["session_id"] == "session-1"
                for row in helix.created
            )
        )

        primary.textMessageReceived.emit(
            json.dumps(
                {
                    "metadata": {
                        "message_id": "follow-1",
                        "message_type": "notification",
                        "subscription_type": "channel.follow",
                    },
                    "payload": {
                        "subscription": {
                            "type": "channel.follow",
                            "status": "enabled",
                        },
                        "event": {
                            "user_id": "9",
                            "user_login": "cloud",
                            "user_name": "Cloud",
                        },
                    },
                }
            )
        )
        self.assertEqual(
            [event.type for event in bus.events("alerts")],
            ["follow"],
        )

    def test_reconnect_keeps_old_socket_until_candidate_welcome(self):
        service, _bus, helix, sockets = self._service()
        primary = sockets[0]
        primary.textMessageReceived.emit(
            self._welcome("welcome-1", "session-old")
        )
        self._wait_until(lambda: len(helix.created) == 8)
        initial_count = len(helix.created)

        primary.textMessageReceived.emit(
            json.dumps(
                {
                    "metadata": {
                        "message_id": "reconnect-1",
                        "message_type": "session_reconnect",
                    },
                    "payload": {
                        "session": {
                            "id": "session-old",
                            "reconnect_url": (
                                "wss://eventsub.wss.twitch.tv/ws?reconnect=1"
                            ),
                        }
                    },
                }
            )
        )

        self.assertEqual(len(sockets), 2)
        candidate = sockets[1]
        self.assertFalse(primary.closed)
        self.assertIn("reconnect=1", candidate.opened_url)

        candidate.textMessageReceived.emit(
            self._welcome("welcome-2", "session-new")
        )
        self.app.processEvents()

        self.assertTrue(primary.closed)
        self.assertIs(service._active, candidate)
        self.assertIsNone(service._candidate)
        self.assertEqual(len(helix.created), initial_count)


if __name__ == "__main__":
    unittest.main()
