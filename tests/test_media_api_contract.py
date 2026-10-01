from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.request import Request, urlopen

from stream_state_router.media import (
    MediaProviderCommandError,
    MediaRuntime,
    MediaRuntimeConfig,
    MediaState,
)
from stream_state_router.services.api import APIConfig, LocalControlAPI
from stream_state_router.ui.main_window import MainWindow


class BlockingMediaProvider:
    name = "fake"
    capabilities = ("next",)

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.next_calls = 0

    def state(self) -> MediaState:
        return MediaState(
            provider=self.name,
            connected=True,
            playback_state="playing",
            title="Track",
        )

    def next(self) -> None:
        self.next_calls += 1
        self.entered.set()
        self.release.wait(1.0)


class MediaAPIContractTests(unittest.TestCase):
    def test_accepted_slow_media_command_is_never_temporarily_missing(self):
        provider = BlockingMediaProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
        )
        runtime.start()

        def action(name, _payload):
            self.assertEqual(name, "media.next")
            request_id = runtime.request("next")
            return {
                "request_id": request_id,
                "status": "accepted",
            }

        api = LocalControlAPI(
            APIConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
            ),
            status=lambda: {},
            action=action,
            request_status=runtime.command_status,
        )
        api.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api.bound_port}/media/next",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2.0) as response:
                accepted = json.loads(response.read())

            request_id = accepted["request_id"]
            self.assertTrue(provider.entered.wait(1.0))

            with urlopen(
                f"http://127.0.0.1:{api.bound_port}/requests/{request_id}",
                timeout=2.0,
            ) as response:
                in_flight = json.loads(response.read())

            self.assertIn(in_flight["status"], {"queued", "running"})
            self.assertIsNone(in_flight["success"])

            provider.release.set()
            deadline = time.monotonic() + 1.0
            terminal = None
            while time.monotonic() < deadline:
                with urlopen(
                    f"http://127.0.0.1:{api.bound_port}/requests/{request_id}",
                    timeout=2.0,
                ) as response:
                    terminal = json.loads(response.read())
                if terminal["status"] == "completed":
                    break
                time.sleep(0.01)

            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertEqual(terminal["status"], "completed")
            self.assertTrue(terminal["success"])
            self.assertEqual(provider.next_calls, 1)
        finally:
            provider.release.set()
            runtime.stop()
            api.stop()

    def test_media_api_requires_seek_volume_and_uri_parameters(self):
        runtime = SimpleNamespace(
            running=True,
            request=lambda *_args, **_kwargs: "media-1",
        )
        window = SimpleNamespace(_media_runtime=runtime)

        with self.assertRaisesRegex(ValueError, "seconds requis"):
            MainWindow._api_action(window, "media.seek", {})
        with self.assertRaisesRegex(ValueError, "percent requis"):
            MainWindow._api_action(window, "media.set_volume", {})
        with self.assertRaisesRegex(ValueError, "uri requis"):
            MainWindow._api_action(window, "media.play_uri", {})
        with self.assertRaisesRegex(ValueError, "uri requis"):
            MainWindow._api_action(window, "media.enqueue_uri", {"uri": " "})

    def test_media_route_and_async_request_status_match_streamdeck_contract(self):
        calls: list[tuple[str, dict[str, object]]] = []
        statuses = {
            "media-1": {
                "request_id": "media-1",
                "action": "play",
                "status": "completed",
                "success": True,
                "error": "",
                "state": {
                    "provider": "vlc",
                    "playback_state": "playing",
                },
            }
        }

        def action(name, payload):
            calls.append((str(name), dict(payload)))
            return {
                "request_id": "media-1",
                "status": "accepted",
            }

        api = LocalControlAPI(
            APIConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
            ),
            status=lambda: {
                "media": {
                    "running": True,
                    "state": {
                        "playback_state": "stopped",
                    },
                }
            },
            action=action,
            request_status=lambda request_id: statuses.get(request_id),
        )
        api.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api.bound_port}/media/play",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2.0) as response:
                accepted = json.loads(response.read())

            self.assertEqual(response.status, 202)
            self.assertTrue(accepted["ok"])
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["request_id"], "media-1")
            self.assertEqual(calls, [("media.play", {})])

            with urlopen(
                f"http://127.0.0.1:{api.bound_port}/requests/media-1",
                timeout=2.0,
            ) as response:
                completed = json.loads(response.read())

            self.assertTrue(completed["ok"])
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["success"])
            self.assertEqual(completed["action"], "play")
        finally:
            api.stop()


    def test_uncertain_media_command_is_exposed_as_terminal_status(self):
        class AmbiguousProvider(BlockingMediaProvider):
            def next(self) -> None:
                self.next_calls += 1
                raise MediaProviderCommandError("response lost")

        provider = AmbiguousProvider()
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
        )
        runtime.start()

        def action(name, _payload):
            self.assertEqual(name, "media.next")
            request_id = runtime.request("next")
            return {
                "request_id": request_id,
                "status": "accepted",
            }

        api = LocalControlAPI(
            APIConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
            ),
            status=lambda: {},
            action=action,
            request_status=runtime.command_status,
        )
        api.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api.bound_port}/media/next",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2.0) as response:
                accepted = json.loads(response.read())

            request_id = accepted["request_id"]
            deadline = time.monotonic() + 1.0
            payload = None
            while time.monotonic() < deadline:
                with urlopen(
                    (
                        f"http://127.0.0.1:{api.bound_port}"
                        f"/requests/{request_id}"
                    ),
                    timeout=2.0,
                ) as response:
                    payload = json.loads(response.read())
                if payload["status"] == "uncertain":
                    break
                time.sleep(0.01)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "uncertain")
            self.assertFalse(payload["success"])
            self.assertIn("incertain", payload["error"])
            self.assertEqual(provider.next_calls, 1)
        finally:
            runtime.stop()
            api.stop()


if __name__ == "__main__":
    unittest.main()
