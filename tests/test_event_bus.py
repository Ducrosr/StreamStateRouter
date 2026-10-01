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

    def test_snapshot_paginates_oldest_pending_events_without_skipping(self) -> None:
        bus = EventBus(history_limit=50)
        for index in range(30):
            bus.publish(
                channel="chat",
                type="message",
                payload={"index": index + 1},
            )

        first = bus.snapshot("chat", after=0, limit=20)
        self.assertEqual(
            [event["sequence"] for event in first["events"]],
            list(range(1, 21)),
        )
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_after"], 20)

        second = bus.snapshot(
            "chat",
            after=first["next_after"],
            limit=20,
            stream_id=first["stream_id"],
        )
        self.assertEqual(
            [event["sequence"] for event in second["events"]],
            list(range(21, 31)),
        )
        self.assertFalse(second["has_more"])
        self.assertEqual(second["mode"], "incremental")

    def test_snapshot_resets_cursor_when_stream_identity_changes(self) -> None:
        old_bus = EventBus()
        old_bus.publish(channel="events", type="old")
        old_stream = old_bus.stream_id

        new_bus = EventBus()
        new_bus.publish(channel="events", type="new")
        snapshot = new_bus.snapshot(
            "events",
            after=30,
            limit=20,
            stream_id=old_stream,
        )

        self.assertNotEqual(snapshot["stream_id"], old_stream)
        self.assertTrue(snapshot["reset_required"])
        self.assertEqual(snapshot["mode"], "reset")
        self.assertEqual(snapshot["after"], 0)
        self.assertEqual(
            [event["type"] for event in snapshot["events"]],
            ["new"],
        )

    def test_snapshot_reports_incremental_gap_after_history_eviction(self) -> None:
        bus = EventBus(history_limit=2)
        first = bus.publish(channel="alerts", type="one")
        bus.publish(channel="alerts", type="two")
        bus.publish(channel="alerts", type="three")

        snapshot = bus.snapshot(
            "alerts",
            after=first.sequence - 1,
            limit=20,
            stream_id=bus.stream_id,
        )

        self.assertTrue(snapshot["gap"])
        self.assertEqual(snapshot["dropped_through"], first.sequence)
        self.assertEqual(
            [event["type"] for event in snapshot["events"]],
            ["two", "three"],
        )

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
