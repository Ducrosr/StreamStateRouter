from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from stream_state_router.events import EventBus
from stream_state_router.platforms import (
    TwitchEventSubAdapter,
    TwitchEventSubConfig,
)


class TwitchEventSubAdapterTests(unittest.TestCase):
    def _adapter(self) -> tuple[TwitchEventSubAdapter, EventBus]:
        bus = EventBus()
        adapter = TwitchEventSubAdapter(
            TwitchEventSubConfig(
                enabled=True,
                client_id="client",
                user_access_token="token",
                broadcaster_user_id="100",
                user_id="100",
                moderator_user_id="100",
            ),
            bus,
        )
        return adapter, bus

    @staticmethod
    def _message(
        message_type: str,
        *,
        message_id: str,
        subscription_type: str = "",
        event: dict | None = None,
        session: dict | None = None,
    ) -> str:
        metadata = {
            "message_id": message_id,
            "message_type": message_type,
        }
        if subscription_type:
            metadata["subscription_type"] = subscription_type
        payload: dict[str, object] = {}
        if session is not None:
            payload["session"] = session
        if subscription_type:
            payload["subscription"] = {
                "type": subscription_type,
                "status": "enabled",
            }
        if event is not None:
            payload["event"] = event
        return json.dumps(
            {"metadata": metadata, "payload": payload}
        )

    def test_welcome_subscribes_all_requested_types(self) -> None:
        adapter, _bus = self._adapter()
        calls: list[str] = []
        adapter._subscribe_all = lambda session_id: calls.append(
            session_id
        )

        adapter._handle_message(
            self._message(
                "session_welcome",
                message_id="welcome-1",
                session={"id": "session-1"},
            )
        )

        self.assertEqual(calls, ["session-1"])

    def test_duplicate_message_id_is_ignored(self) -> None:
        adapter, bus = self._adapter()
        raw = self._message(
            "notification",
            message_id="same",
            subscription_type="channel.follow",
            event={
                "user_id": "1",
                "user_login": "cloud",
                "user_name": "Cloud",
            },
        )

        adapter._handle_message(raw)
        adapter._handle_message(raw)

        self.assertEqual(len(bus.events("events")), 1)

    def test_chat_maps_to_normalized_chat_channel(self) -> None:
        adapter, bus = self._adapter()
        adapter._handle_message(
            self._message(
                "notification",
                message_id="chat-1",
                subscription_type="channel.chat.message",
                event={
                    "user_id": "1",
                    "user_login": "cloud",
                    "user_name": "Cloud",
                    "message_id": "msg-1",
                    "color": "#63e6ff",
                    "message": {"text": "Salut Midgar"},
                },
            )
        )

        event = bus.events("chat")[0]
        self.assertEqual(event.type, "message")
        self.assertEqual(event.platform, "twitch")
        self.assertEqual(event.payload["display_name"], "Cloud")
        self.assertEqual(event.payload["text"], "Salut Midgar")

    def test_sub_cheer_and_raid_feed_events_and_alerts(self) -> None:
        adapter, bus = self._adapter()
        notifications = [
            (
                "channel.subscribe",
                {
                    "user_id": "1",
                    "user_login": "cloud",
                    "user_name": "Cloud",
                    "tier": "1000",
                    "is_gift": False,
                },
            ),
            (
                "channel.cheer",
                {
                    "user_id": "2",
                    "user_login": "tifa",
                    "user_name": "Tifa",
                    "bits": 250,
                    "message": "Go!",
                    "is_anonymous": False,
                },
            ),
            (
                "channel.raid",
                {
                    "from_broadcaster_user_id": "3",
                    "from_broadcaster_user_login": "barret",
                    "from_broadcaster_user_name": "Barret",
                    "viewers": 42,
                },
            ),
        ]
        for index, (kind, event) in enumerate(notifications):
            adapter._handle_message(
                self._message(
                    "notification",
                    message_id=f"event-{index}",
                    subscription_type=kind,
                    event=event,
                )
            )

        self.assertEqual(
            [event.type for event in bus.events("events")],
            ["subscription", "cheer", "raid"],
        )
        self.assertEqual(
            [event.type for event in bus.events("alerts")],
            ["subscription", "cheer", "raid"],
        )
        self.assertEqual(
            bus.events("events")[-1].payload["viewer_count"],
            42,
        )

    def test_reconnect_message_records_twitch_reconnect_url(self) -> None:
        adapter, _bus = self._adapter()
        adapter._handle_message(
            self._message(
                "session_reconnect",
                message_id="reconnect-1",
                session={
                    "id": "session-2",
                    "reconnect_url": "wss://reconnect.example/ws",
                },
            )
        )

        self.assertEqual(
            adapter._reconnect_url,
            "wss://reconnect.example/ws",
        )

    def test_follow_condition_uses_moderator_id(self) -> None:
        adapter, _bus = self._adapter()

        self.assertEqual(
            adapter._condition_for("channel.follow"),
            {
                "broadcaster_user_id": "100",
                "moderator_user_id": "100",
            },
        )

    def test_create_subscription_posts_websocket_transport(self) -> None:
        adapter, _bus = self._adapter()
        captured: dict[str, object] = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(
                request.data.decode("utf-8")
            )
            captured["headers"] = dict(request.header_items())
            return Response()

        with patch(
            "stream_state_router.platforms.twitch.urlopen",
            side_effect=fake_urlopen,
        ):
            adapter._create_subscription(
                "channel.chat.message",
                "session-1",
            )

        self.assertEqual(
            captured["url"],
            "https://api.twitch.tv/helix/eventsub/subscriptions",
        )
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["type"], "channel.chat.message")
        self.assertEqual(
            body["transport"],
            {
                "method": "websocket",
                "session_id": "session-1",
            },
        )
        self.assertEqual(
            body["condition"],
            {
                "broadcaster_user_id": "100",
                "user_id": "100",
            },
        )


if __name__ == "__main__":
    unittest.main()
