from __future__ import annotations

import threading
import time
import unittest

from stream_state_router.events import EventBus
from stream_state_router.media import (
    MediaArtworkStore,
    MediaRuntime,
    MediaRuntimeConfig,
    MediaState,
    MediaStateStore,
)


class FakeProvider:
    name = "fake"
    capabilities = (
        "play",
        "pause",
        "stop",
        "next",
        "previous",
        "seek",
        "set_volume",
        "play_uri",
        "enqueue_uri",
        "clear_queue",
        "artwork",
    )

    def __init__(self) -> None:
        self.playback_state = "stopped"
        self.position = 0.0
        self.volume = 100.0
        self.fail_state = False
        self.fail_pause = False
        self.fail_artwork = False
        self.track_id = "track-1"
        self.artwork_calls = 0
        self.calls: list[tuple[str, object, int]] = []

    def _record(self, action: str, value=None) -> None:
        self.calls.append(
            (action, value, threading.get_ident())
        )

    def state(self) -> MediaState:
        self._record("state")
        if self.fail_state:
            raise RuntimeError("provider offline")
        return MediaState(
            provider=self.name,
            connected=True,
            playback_state=self.playback_state,
            title="Midgar Radio",
            artwork_url="file:///C:/Music/cover.jpg",
            uri="file:///C:/Music/track.flac",
            duration_seconds=180,
            position_seconds=self.position,
            volume_percent=self.volume,
            track_id=self.track_id,
        )

    def artwork(self) -> tuple[bytes, str]:
        self._record("artwork")
        self.artwork_calls += 1
        if self.fail_artwork:
            raise RuntimeError("artwork unavailable")
        return b"jpeg-cover", "image/jpeg"

    def play(self) -> None:
        self._record("play")
        self.playback_state = "playing"

    def pause(self) -> None:
        self._record("pause")
        self.playback_state = "paused"
        if self.fail_pause:
            raise RuntimeError("ambiguous pause")

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
    def test_media_state_normalizes_non_finite_external_values(self) -> None:
        state = MediaState(
            provider=" vlc ",
            connected=1,
            playback_state="OPENING-UNKNOWN",
            title=" Track ",
            duration_seconds=float("nan"),
            position_seconds=float("inf"),
            volume_percent=float("-inf"),
        )

        self.assertEqual(state.provider, "vlc")
        self.assertTrue(state.connected)
        self.assertEqual(state.playback_state, "unknown")
        self.assertEqual(state.title, "Track")
        self.assertEqual(state.duration_seconds, 0.0)
        self.assertEqual(state.position_seconds, 0.0)
        self.assertEqual(state.playback_rate, 0.0)
        self.assertEqual(state.volume_percent, 0.0)

    def test_artwork_store_rejects_active_svg_content(self) -> None:
        store = MediaArtworkStore()

        with self.assertRaisesRegex(
            ValueError,
            "Type de pochette",
        ):
            store.update(
                b"<svg><script>alert(1)</script></svg>",
                content_type="image/svg+xml",
                identity="track-svg",
            )

        self.assertFalse(store.snapshot()["available"])

    def test_runtime_stamps_observation_time_and_capabilities(self) -> None:
        provider = FakeProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
            wall_clock=lambda: 1234.5,
        )

        state = runtime.poll_once()

        self.assertEqual(state.observed_at_unix, 1234.5)
        self.assertIn("play", state.capabilities)
        self.assertIn("artwork", state.capabilities)
        public = runtime.state_store.public_snapshot()
        self.assertEqual(public["observed_at_unix"], 1234.5)
        self.assertIn("play", public["capabilities"])

    def test_state_store_marks_old_observation_stale(self) -> None:
        now = [10.0]
        store = MediaStateStore("fake", clock=lambda: now[0])
        store.update(
            MediaState(
                provider="fake",
                connected=True,
                playback_state="playing",
            )
        )

        fresh = store.public_snapshot(stale_after_seconds=3.0)
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["age_seconds"], 0.0)

        now[0] = 13.5
        stale = store.public_snapshot(stale_after_seconds=3.0)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["age_seconds"], 3.5)

    def test_poll_contains_invalid_provider_return_in_disconnected_state(self) -> None:
        provider = FakeProvider()
        provider.state = lambda: {"state": "playing"}
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
        )

        state = runtime.poll_once()

        self.assertFalse(state.connected)
        self.assertEqual(state.playback_state, "unknown")
        self.assertGreater(state.observed_at_unix, 0.0)
        self.assertIn("play", state.capabilities)
        self.assertIn("MediaState", state.error)

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
        self.assertNotIn("uri", events[-1].payload)
        self.assertNotIn("artwork_url", events[-1].payload)
        self.assertNotIn("error", events[-1].payload)

    def test_poll_marks_provider_disconnected_then_recovers(self) -> None:
        provider = FakeProvider()
        bus = EventBus()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
            event_bus=bus,
        )
        provider.fail_state = True

        offline = runtime.poll_once()
        provider.fail_state = False
        recovered = runtime.poll_once()

        self.assertFalse(offline.connected)
        self.assertEqual(offline.error, "provider offline")
        self.assertGreater(offline.observed_at_unix, 0.0)
        self.assertIn("play", offline.capabilities)
        self.assertTrue(recovered.connected)
        self.assertEqual(recovered.error, "")
        self.assertEqual(
            [event.type for event in bus.events("media")],
            ["state_changed", "state_changed"],
        )

    def test_artwork_is_loaded_once_per_track_identity(self) -> None:
        provider = FakeProvider()
        artwork_store = MediaArtworkStore()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
            artwork_store=artwork_store,
        )

        runtime.poll_once()
        provider.position = 10
        runtime.poll_once()

        first = artwork_store.snapshot()
        self.assertTrue(first["available"])
        self.assertEqual(first["content"], b"jpeg-cover")
        self.assertEqual(first["content_type"], "image/jpeg")
        self.assertEqual(provider.artwork_calls, 1)

        provider.track_id = "track-2"
        runtime.poll_once()
        second = artwork_store.snapshot()
        self.assertGreater(second["revision"], first["revision"])
        self.assertEqual(provider.artwork_calls, 2)

    def test_artwork_failure_does_not_disconnect_media_state(self) -> None:
        provider = FakeProvider()
        provider.fail_artwork = True
        artwork_store = MediaArtworkStore()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
            artwork_store=artwork_store,
        )

        state = runtime.poll_once()

        self.assertTrue(state.connected)
        self.assertEqual(state.error, "")
        self.assertFalse(artwork_store.snapshot()["available"])
        self.assertEqual(provider.artwork_calls, 1)

    def test_artwork_failure_retries_after_backoff(self) -> None:
        provider = FakeProvider()
        provider.fail_artwork = True
        now = [100.0]
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=False),
            provider,
            artwork_store=MediaArtworkStore(),
            clock=lambda: now[0],
        )

        runtime.poll_once()
        self.assertEqual(provider.artwork_calls, 1)

        now[0] = 104.0
        runtime.poll_once()
        self.assertEqual(provider.artwork_calls, 1)

        provider.fail_artwork = False
        now[0] = 105.1
        runtime.poll_once()
        self.assertEqual(provider.artwork_calls, 2)
        self.assertTrue(runtime.artwork_store.snapshot()["available"])

    def test_failed_command_is_not_retried_automatically(self) -> None:
        provider = FakeProvider()
        provider.fail_pause = True
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=0.1),
            provider,
        )
        runtime.start()
        self.addCleanup(runtime.stop)

        request_id = runtime.request("pause")
        deadline = time.monotonic() + 1.0
        status = None
        while status is None and time.monotonic() < deadline:
            status = runtime.command_status(request_id)
            if status is None:
                time.sleep(0.01)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertFalse(status["success"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "ambiguous pause")
        pause_calls = [
            row for row in provider.calls if row[0] == "pause"
        ]
        self.assertEqual(len(pause_calls), 1)

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
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["state"]["playback_state"], "paused")
        self.assertNotIn("uri", status["state"])
        self.assertNotIn("artwork_url", status["state"])
        self.assertNotIn("error", status["state"])

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

    def test_shutdown_rejects_new_commands_and_cancels_queued_requests(self) -> None:
        provider = FakeProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
        )
        runtime.start()

        # Freeze the worker in a provider call so a second command remains
        # queued long enough for stop() to own and cancel it deterministically.
        entered = threading.Event()
        release = threading.Event()
        original_play = provider.play

        def blocking_play() -> None:
            entered.set()
            release.wait(1.0)
            original_play()

        provider.play = blocking_play
        active = runtime.request("play")
        self.assertTrue(entered.wait(1.0))
        queued = runtime.request("next")

        result_holder: list[bool] = []

        def stop_runtime() -> None:
            result_holder.append(runtime.stop(timeout=1.5))

        stopper = threading.Thread(target=stop_runtime)
        stopper.start()
        deadline = time.monotonic() + 1.0
        while runtime.running and time.monotonic() < deadline:
            time.sleep(0.01)

        with self.assertRaises(RuntimeError):
            runtime.request("pause")

        queued_status = runtime.command_status(queued)
        self.assertIsNotNone(queued_status)
        assert queued_status is not None
        self.assertFalse(queued_status["success"])
        self.assertEqual(queued_status["status"], "failed")
        self.assertEqual(
            queued_status["error"],
            "Media Runtime en arrêt",
        )

        release.set()
        stopper.join(2.0)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(result_holder, [True])

        active_status = runtime.command_status(active)
        self.assertIsNotNone(active_status)
        assert active_status is not None
        self.assertTrue(active_status["success"])
        self.assertEqual(
            len([row for row in provider.calls if row[0] == "state"]),
            1,
        )

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
