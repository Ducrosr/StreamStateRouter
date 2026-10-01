from __future__ import annotations

import threading
import time
import unittest

from stream_state_router.events import EventBus
from stream_state_router.media import (
    MediaRuntime,
    MediaRuntimeConfig,
    MediaState,
    MediaStateStore,
)


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.playback_state = "stopped"
        self.position = 0.0
        self.volume = 100.0
        self.calls: list[tuple[str, object, int]] = []

    def _record(self, action: str, value=None) -> None:
        self.calls.append(
            (action, value, threading.get_ident())
        )

    def state(self) -> MediaState:
        self._record("state")
        return MediaState(
            provider=self.name,
            connected=True,
            playback_state=self.playback_state,
            title="Midgar Radio",
            duration_seconds=180,
            position_seconds=self.position,
            volume_percent=self.volume,
            track_id="track-1",
        )

    def play(self) -> None:
        self._record("play")
        self.playback_state = "playing"

    def pause(self) -> None:
        self._record("pause")
        self.playback_state = "paused"

    def stop(self) -> None:
        self._record("stop")
        self.playback_state = "stopped"

    def next(self) -> None:
        self._record("next")

    def previous(self) -> None:
        self._record("previous")

    def seek(self, seconds: float) -> None:
        self._record("seek", seconds)
        self.position = seconds

    def set_volume(self, percent: float) -> None:
        self._record("set_volume", percent)
        self.volume = percent

    def play_uri(self, uri: str) -> None:
        self._record("play_uri", uri)
        self.playback_state = "playing"

    def enqueue_uri(self, uri: str) -> None:
        self._record("enqueue_uri", uri)

    def clear_queue(self) -> None:
        self._record("clear_queue")


class MediaRuntimeTests(unittest.TestCase):
    def test_state_store_returns_revisioned_snapshot(self) -> None:
        store = MediaStateStore("fake")
        first = store.snapshot()
        second = store.update(
            MediaState(
                provider="fake",
                connected=True,
                playback_state="playing",
                title="Track",
            )
        )

        self.assertEqual(first["revision"], 0)
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["title"], "Track")
        self.assertTrue(second["playing"])

    def test_poll_publishes_only_semantic_state_changes(self) -> None:
        provider = FakeProvider()
        bus = EventBus()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False, poll_seconds=0.5),
            provider,
            event_bus=bus,
        )

        runtime.poll_once()
        provider.position = 5
        runtime.poll_once()
        provider.playback_state = "playing"
        runtime.poll_once()

        events = bus.events("media")
        self.assertEqual(
            [event.type for event in events],
            ["state_changed", "state_changed"],
        )
        self.assertEqual(
            events[-1].payload["playback_state"],
            "playing",
        )

    def test_worker_serializes_commands_and_provider_polling(self) -> None:
        provider = FakeProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=0.1),
            provider,
        )
        runtime.start()
        self.addCleanup(runtime.stop)

        deadline = time.monotonic() + 1.0
        while (
            not any(call[0] == "state" for call in provider.calls)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        request_id = runtime.request("pause")
        status = None
        deadline = time.monotonic() + 1.0
        while status is None and time.monotonic() < deadline:
            status = runtime.command_status(request_id)
            if status is None:
                time.sleep(0.01)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status["success"])
        self.assertEqual(status["state"]["playback_state"], "paused")

        worker_threads = {
            thread_id
            for _action, _value, thread_id in provider.calls
        }
        self.assertEqual(len(worker_threads), 1)
        self.assertNotIn(threading.get_ident(), worker_threads)

    def test_queue_commands_are_executed_on_media_worker(self) -> None:
        provider = FakeProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=0.1),
            provider,
        )
        runtime.start()
        self.addCleanup(runtime.stop)

        first = runtime.request(
            "enqueue_uri",
            uri="file:///C:/Music/A.flac",
        )
        second = runtime.request("clear_queue")

        deadline = time.monotonic() + 1.0
        while (
            (
                runtime.command_status(first) is None
                or runtime.command_status(second) is None
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        first_status = runtime.command_status(first)
        second_status = runtime.command_status(second)
        self.assertIsNotNone(first_status)
        self.assertIsNotNone(second_status)
        assert first_status is not None
        assert second_status is not None
        self.assertTrue(first_status["success"])
        self.assertTrue(second_status["success"])
        actions = [row[0] for row in provider.calls]
        self.assertIn("enqueue_uri", actions)
        self.assertIn("clear_queue", actions)

    def test_invalid_action_and_disabled_runtime_are_rejected(self) -> None:
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            FakeProvider(),
        )

        with self.assertRaises(RuntimeError):
            runtime.request("play")

        runtime.config = MediaRuntimeConfig(enabled=True)
        runtime.start()
        self.addCleanup(runtime.stop)
        with self.assertRaises(ValueError):
            runtime.request("explode")


if __name__ == "__main__":
    unittest.main()
