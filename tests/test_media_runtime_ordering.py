from __future__ import annotations

import threading
import time
import unittest

from stream_state_router.events import EventBus
from stream_state_router.media import MediaRuntime, MediaRuntimeConfig, MediaState


class StableProvider:
    name = "stable"
    capabilities = ()

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_state = False
        self.calls = 0

    def state(self) -> MediaState:
        self.calls += 1
        if self.block_state:
            self.entered.set()
            self.release.wait(1.0)
        return MediaState(
            provider=self.name,
            connected=True,
            playback_state="playing",
            title="Stable Track",
            track_id="1",
        )


class BlockingStateEventBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, **kwargs):
        if kwargs.get("type") == "state_changed":
            self.entered.set()
            self.release.wait(1.0)
        return super().publish(**kwargs)


class MediaRuntimeOrderingTests(unittest.TestCase):
    def test_stop_linearizes_after_in_progress_state_event_publication(self) -> None:
        provider = StableProvider()
        bus = BlockingStateEventBus()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
            event_bus=bus,
        )
        runtime.start()
        self.assertTrue(bus.entered.wait(1.0))

        stopped: list[bool] = []
        stopper = threading.Thread(
            target=lambda: stopped.append(runtime.stop(timeout=1.0))
        )
        stopper.start()

        # publish() is inside the runtime lifecycle barrier. stop() must not
        # report a linearized shutdown before this already-admitted event ends.
        time.sleep(0.05)
        self.assertTrue(stopper.is_alive())

        bus.release.set()
        stopper.join(2.0)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(stopped, [True])
        events = bus.events("media")
        self.assertEqual([event.type for event in events], ["state_changed"])

    def test_stop_during_provider_state_cannot_publish_state_event(self) -> None:
        provider = StableProvider()
        provider.block_state = True
        bus = EventBus()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
            event_bus=bus,
        )
        runtime.start()
        self.assertTrue(provider.entered.wait(1.0))

        self.assertFalse(runtime.stop(timeout=0.01))
        provider.release.set()
        self.assertTrue(runtime.stop(timeout=1.0))

        self.assertEqual(bus.events("media"), ())
        self.assertEqual(runtime.state_store.snapshot()["revision"], 0)

    def test_restart_of_same_runtime_republishes_current_semantic_state(self) -> None:
        provider = StableProvider()
        bus = EventBus()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
            event_bus=bus,
        )
        runtime.start()

        deadline = time.monotonic() + 1.0
        while len(bus.events("media")) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(bus.events("media")), 1)
        self.assertTrue(runtime.stop(timeout=1.0))

        runtime.start()
        deadline = time.monotonic() + 1.0
        while len(bus.events("media")) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime.stop(timeout=1.0)

        # A fresh worker lifecycle must announce its first observation even if
        # the semantic media state is identical to the previous lifecycle.
        self.assertEqual(len(bus.events("media")), 2)


if __name__ == "__main__":
    unittest.main()
