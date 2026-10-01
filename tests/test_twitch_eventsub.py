from __future__ import annotations

import unittest

from stream_state_router.events import EventBus
from stream_state_router.platforms import (
    TwitchEventSubMessageProcessor,
    build_default_subscriptions,
    required_scopes,
)


def frame(
    message_type: str,
    *,
    message_id: str,
    subscription_type: str = "",
    event: dict | None = None,
    session: dict | None = None,
    subscription: dict | None = None,
) -> dict:
    metadata = {
        "message_type": message_type,
        "message_id": message_id,
    }
    if subscription_type:
        metadata["subscription_type"] = subscription_type
    payload: dict = {}
    if event is not None:
        payload["event"] = event
    if session is not None:
        payload["session"] = session
    if subscription is not None:
        payload["subscription"] = subscription
    return {"metadata": metadata, "payload": payload}


class TwitchEventSubFoundationTests(unittest.TestCase):
    def test_default_subscriptions_and_required_scopes(self) -> None:
        specs = build_default_subscriptions(
            broadcaster_user_id="123",
            user_id="456",
        )

        self.assertEqual(
            [spec.type for spec in specs],
            [
                "channel.chat.message",
                "channel.follow",
                "channel.subscribe",
                "channel.subscription.gift",
                "channel.subscription.message",
                "channel.cheer",
                "channel.raid",
                "channel.channel_points_custom_reward_redemption.add",
            ],
        )
        follow = next(
            spec for spec in specs if spec.type == "channel.follow"
        )
        self.assertEqual(follow.version, "2")
        self.assertEqual(
            dict(follow.condition),
            {
                "broadcaster_user_id": "123",
                "moderator_user_id": "123",
            },
        )
        raid = next(
            spec for spec in specs if spec.type == "channel.raid"
        )
        self.assertEqual(
            dict(raid.condition),
            {"to_broadcaster_user_id": "123"},
        )
        self.assertEqual(
            required_scopes(specs),
            (
                "bits:read",
                "channel:read:redemptions",
                "channel:read:subscriptions",
                "moderator:read:followers",
                "user:read:chat",
            ),
        )
        self.assertEqual(
            follow.create_payload("session-1")["transport"],
            {
                "method": "websocket",
                "session_id": "session-1",
            },
        )

    def test_welcome_keepalive_reconnect_and_revocation(self) -> None:
        bus = EventBus()
        processor = TwitchEventSubMessageProcessor(bus)

        welcome = processor.process(
            frame(
                "session_welcome",
                message_id="welcome",
                session={
                    "id": "session-a",
                    "keepalive_timeout_seconds": 10,
                },
            )
        )
        self.assertEqual(welcome.kind, "welcome")
        self.assertEqual(welcome.session_id, "session-a")
        self.assertEqual(welcome.keepalive_timeout_seconds, 10)

        keepalive = processor.process(
            frame(
                "session_keepalive",
                message_id="keepalive",
            )
        )
        self.assertEqual(keepalive.kind, "keepalive")

        reconnect = processor.process(
            frame(
                "session_reconnect",
                message_id="reconnect",
                session={
                    "id": "session-a",
                    "reconnect_url": "wss://example.test/reconnect",
                },
            )
        )
        self.assertEqual(reconnect.kind, "reconnect")
        self.assertEqual(
            reconnect.reconnect_url,
            "wss://example.test/reconnect",
        )

        revoked = processor.process(
            frame(
                "revocation",
                message_id="revoked",
                subscription_type="channel.follow",
                subscription={
                    "type": "channel.follow",
                    "status": "authorization_revoked",
                },
            )
        )
        self.assertEqual(revoked.kind, "revocation")
        self.assertEqual(
            revoked.subscription_status,
            "authorization_revoked",
        )

    def test_chat_notification_is_normalized_and_deduplicated(self) -> None:
        bus = EventBus()
        processor = TwitchEventSubMessageProcessor(bus)
        raw = frame(
            "notification",
            message_id="chat-1",
            subscription_type="channel.chat.message",
            event={
                "user_id": "1",
                "user_login": "cloud",
                "user_name": "Cloud",
                "color": "#44ccff",
                "message_id": "twitch-message-1",
                "message_type": "text",
                "message": {"text": "<b>Hello Midgar</b>"},
                "badges": [{"set_id": "subscriber"}],
            },
        )

        result = processor.process(raw)
        duplicate = processor.process(raw)
        events = bus.events("chat")

        self.assertEqual(result.published, 1)
        self.assertFalse(result.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "message")
        self.assertEqual(events[0].platform, "twitch")
        self.assertEqual(
            events[0].payload["text"],
            "<b>Hello Midgar</b>",
        )
        self.assertEqual(events[0].payload["color"], "#44ccff")

    def test_follow_subscription_cheer_raid_and_reward_feed_alerts(self) -> None:
        bus = EventBus()
        processor = TwitchEventSubMessageProcessor(bus)

        cases = [
            (
                "channel.follow",
                {
                    "user_id": "1",
                    "user_login": "cloud",
                    "user_name": "Cloud",
                },
                "follow",
            ),
            (
                "channel.subscribe",
                {
                    "user_id": "2",
                    "user_login": "tifa",
                    "user_name": "Tifa",
                    "tier": "1000",
                    "is_gift": False,
                },
                "subscription",
            ),
            (
                "channel.subscription.gift",
                {
                    "user_id": "3",
                    "user_login": "aerith",
                    "user_name": "Aerith",
                    "total": 5,
                    "tier": "1000",
                },
                "subscription_gift",
            ),
            (
                "channel.subscription.message",
                {
                    "user_id": "4",
                    "user_login": "barret",
                    "user_name": "Barret",
                    "tier": "1000",
                    "cumulative_months": 7,
                    "message": {"text": "AVALANCHE!"},
                },
                "resubscription",
            ),
            (
                "channel.cheer",
                {
                    "user_id": "5",
                    "user_login": "redxiii",
                    "user_name": "Red XIII",
                    "bits": 500,
                    "message": "Go!",
                },
                "cheer",
            ),
            (
                "channel.raid",
                {
                    "from_broadcaster_user_id": "6",
                    "from_broadcaster_user_login": "yuffie",
                    "from_broadcaster_user_name": "Yuffie",
                    "viewers": 42,
                },
                "raid",
            ),
            (
                "channel.channel_points_custom_reward_redemption.add",
                {
                    "id": "redemption-1",
                    "user_id": "7",
                    "user_login": "vincent",
                    "user_name": "Vincent",
                    "user_input": "Do the thing",
                    "reward": {
                        "id": "reward-1",
                        "title": "Limit Break",
                        "cost": 5000,
                    },
                },
                "reward_redemption",
            ),
        ]

        for index, (subscription_type, event, expected) in enumerate(cases):
            result = processor.process(
                frame(
                    "notification",
                    message_id=f"event-{index}",
                    subscription_type=subscription_type,
                    event=event,
                )
            )
            self.assertEqual(result.published, 2)

        event_types = [event.type for event in bus.events("events")]
        alert_types = [event.type for event in bus.events("alerts")]
        expected = [case[2] for case in cases]
        self.assertEqual(event_types, expected)
        self.assertEqual(alert_types, expected)
        raid = next(
            event for event in bus.events("alerts")
            if event.type == "raid"
        )
        self.assertEqual(raid.payload["viewers"], 42)
        reward = next(
            event for event in bus.events("alerts")
            if event.type == "reward_redemption"
        )
        self.assertEqual(reward.payload["reward_title"], "Limit Break")

    def test_unknown_notification_is_ignored_without_breaking_bus(self) -> None:
        bus = EventBus()
        processor = TwitchEventSubMessageProcessor(bus)

        result = processor.process(
            frame(
                "notification",
                message_id="unknown-1",
                subscription_type="channel.unknown.future_event",
                event={"foo": "bar"},
            )
        )

        self.assertEqual(result.published, 0)
        self.assertEqual(bus.sequence, 0)


if __name__ == "__main__":
    unittest.main()
