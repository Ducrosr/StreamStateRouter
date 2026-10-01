from __future__ import annotations

import unittest

from stream_state_router.events import EventBus


class EventBusTests(unittest.TestCase):
    def test_publish_history_and_after_cursor(self) -> None:
        ticks = iter([1.0, 2.0, 3.0])
        bus = EventBus(
            history_limit=2,
            clock=lambda: next(ticks),
        )

        first = bus.publish(
            channel="chat",
            type="message",
            platform="twitch",
            payload={"text": "one"},
        )
        second = bus.publish(
            channel="chat",
            type="message",
            platform="youtube",
            payload={"text": "two"},
        )
        third = bus.publish(
            channel="chat",
            type="message",
            platform="twitch",
            payload={"text": "three"},
        )

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(third.sequence, 3)
        self.assertEqual(
            [
                item.payload["text"]
                for item in bus.events("chat")
            ],
            ["two", "three"],
        )
        self.assertEqual(
            [
                item.sequence
                for item in bus.events("chat", after=2)
            ],
            [3],
        )

    def test_snapshot_pages_oldest_first_without_skipping_retained_events(self) -> None:
        bus = EventBus(history_limit=50)
        for index in range(30):
            bus.publish(
                channel="events",
                type="item",
                payload={"index": index + 1},
            )

        first = bus.snapshot("events", after=0, limit=20)
        self.assertEqual(
            [item["sequence"] for item in first["events"]],
            list(range(1, 21)),
        )
        self.assertEqual(first["next_after"], 20)
        self.assertTrue(first["has_more"])

        second = bus.snapshot(
            "events",
            after=first["next_after"],
            limit=20,
        )
        self.assertEqual(
            [item["sequence"] for item in second["events"]],
            list(range(21, 31)),
        )
        self.assertEqual(second["next_after"], 30)
        self.assertFalse(second["has_more"])

    def test_snapshot_exposes_stable_stream_identity_and_history_boundary(self) -> None:
        bus = EventBus(history_limit=2)
        stream_id = bus.stream_id
        for index in range(3):
            bus.publish(
                channel="alerts",
                type="item",
                payload={"index": index},
            )

        snapshot = bus.snapshot("alerts", after=1, limit=20)

        self.assertEqual(snapshot["stream_id"], stream_id)
        self.assertEqual(snapshot["earliest_available_sequence"], 2)
        self.assertTrue(snapshot["cursor_before_history"])
        self.assertFalse(snapshot["initial"])

        other = EventBus()
        self.assertNotEqual(other.stream_id, stream_id)

    def test_subscriber_can_filter_channel_and_failure_is_isolated(self) -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(
            lambda event: seen.append(event.type),
            channel="alerts",
        )
        bus.subscribe(
            lambda _event: (_ for _ in ()).throw(
                RuntimeError("consumer failure")
            )
        )

        bus.publish(
            channel="chat",
            type="message",
        )
        bus.publish(
            channel="alerts",
            type="subscription",
        )

        self.assertEqual(seen, ["subscription"])

    def test_snapshot_is_json_friendly(self) -> None:
        bus = EventBus()
        bus.publish(
            channel="media",
            type="track_changed",
            platform="jellyfin",
            payload={"title": "Sector 08"},
        )

        snapshot = bus.snapshot("media")

        self.assertEqual(snapshot["channel"], "media")
        self.assertEqual(snapshot["latest_sequence"], 1)
        self.assertEqual(
            snapshot["events"][0]["payload"]["title"],
            "Sector 08",
        )


if __name__ == "__main__":
    unittest.main()
