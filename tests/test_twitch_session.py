from __future__ import annotations

import io
import json
import unittest

from stream_state_router.events import EventBus
from stream_state_router.platforms import (
    TwitchEventSubMessageProcessor,
    TwitchEventSubSessionCoordinator,
    TwitchHelixClient,
    build_default_subscriptions,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class _Response:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._data


class TwitchSessionTests(unittest.TestCase):
    def _coordinator(self):
        bus = EventBus()
        processor = TwitchEventSubMessageProcessor(bus)
        clock = _Clock()
        specs = build_default_subscriptions(
            broadcaster_user_id="123",
            user_id="456",
        )
        coordinator = TwitchEventSubSessionCoordinator(
            processor,
            specs,
            clock=clock,
        )
        return coordinator, clock, specs

    def test_initial_welcome_requests_all_subscriptions(self) -> None:
        coordinator, _clock, specs = self._coordinator()

        instruction = coordinator.handle(
            {
                "metadata": {
                    "message_id": "welcome-1",
                    "message_type": "session_welcome",
                },
                "payload": {
                    "session": {
                        "id": "session-1",
                        "keepalive_timeout_seconds": 10,
                    }
                },
            }
        )

        self.assertEqual(instruction.result.kind, "welcome")
        self.assertEqual(len(instruction.subscribe), len(specs))
        self.assertEqual(
            instruction.subscribe[0]["transport"],
            {
                "method": "websocket",
                "session_id": "session-1",
            },
        )
        self.assertTrue(coordinator.connected)

    def test_reconnect_welcome_preserves_subscriptions_without_recreating(self) -> None:
        coordinator, _clock, _specs = self._coordinator()
        coordinator.handle(
            {
                "metadata": {
                    "message_id": "welcome-1",
                    "message_type": "session_welcome",
                },
                "payload": {
                    "session": {
                        "id": "old",
                        "keepalive_timeout_seconds": 10,
                    }
                },
            }
        )

        reconnect = coordinator.handle(
            {
                "metadata": {
                    "message_id": "reconnect-1",
                    "message_type": "session_reconnect",
                },
                "payload": {
                    "session": {
                        "id": "old",
                        "reconnect_url": "wss://example.test/new",
                    }
                },
            }
        )
        self.assertEqual(
            reconnect.reconnect_url,
            "wss://example.test/new",
        )

        welcome = coordinator.handle(
            {
                "metadata": {
                    "message_id": "welcome-2",
                    "message_type": "session_welcome",
                },
                "payload": {
                    "session": {
                        "id": "new",
                        "keepalive_timeout_seconds": 10,
                    }
                },
            }
        )

        self.assertEqual(welcome.subscribe, ())
        self.assertTrue(welcome.close_old_after_welcome)
        self.assertEqual(coordinator.session_id, "new")
        self.assertFalse(coordinator.awaiting_reconnect_welcome)

    def test_keepalive_expiry_detects_silent_connection(self) -> None:
        coordinator, clock, _specs = self._coordinator()
        coordinator.handle(
            {
                "metadata": {
                    "message_id": "welcome",
                    "message_type": "session_welcome",
                },
                "payload": {
                    "session": {
                        "id": "session",
                        "keepalive_timeout_seconds": 10,
                    }
                },
            }
        )

        clock.value = 110.5
        self.assertFalse(
            coordinator.keepalive_expired(grace_seconds=1.0)
        )
        clock.value = 111.1
        self.assertTrue(
            coordinator.keepalive_expired(grace_seconds=1.0)
        )

        coordinator.connection_lost()
        self.assertFalse(coordinator.keepalive_expired())

    def test_helix_validate_and_create_subscription_do_not_persist_token(self) -> None:
        requests = []

        def opener(request, *, timeout):
            requests.append((request, timeout))
            if request.full_url.endswith("/validate"):
                return _Response(
                    {
                        "client_id": "client",
                        "user_id": "456",
                        "login": "remy",
                        "scopes": ["user:read:chat"],
                        "expires_in": 3600,
                    }
                )
            return _Response(
                {
                    "data": [
                        {
                            "id": "subscription-id",
                            "status": "enabled",
                        }
                    ]
                }
            )

        client = TwitchHelixClient(
            opener=opener,
            timeout_seconds=3,
        )
        validation = client.validate_token("secret-token")
        self.assertEqual(validation.client_id, "client")
        self.assertEqual(validation.user_id, "456")

        payload = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": "123",
                "user_id": "456",
            },
            "transport": {
                "method": "websocket",
                "session_id": "session",
            },
        }
        result = client.create_subscription(
            client_id="client",
            access_token="secret-token",
            payload=payload,
        )

        self.assertEqual(
            result["data"][0]["id"],
            "subscription-id",
        )
        validate_request = requests[0][0]
        create_request = requests[1][0]
        self.assertEqual(
            validate_request.get_header("Authorization"),
            "OAuth secret-token",
        )
        self.assertEqual(
            create_request.get_header("Authorization"),
            "Bearer secret-token",
        )
        self.assertEqual(
            create_request.get_header("Client-id"),
            "client",
        )
        self.assertNotIn(
            b"secret-token",
            create_request.data or b"",
        )


if __name__ == "__main__":
    unittest.main()
