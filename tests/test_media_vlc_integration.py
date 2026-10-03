from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from stream_state_router.media import (
    MediaRuntime,
    MediaRuntimeConfig,
    VLCConfig,
    VLCProvider,
)
from stream_state_router.services.api import APIConfig, LocalControlAPI


VALID_JPEG = b"\xff\xd8\xff\xe0SSR-INTEGRATION-JPEG"


class FakeVLCLoopbackServer:
    def __init__(self, *, password: str = "secret") -> None:
        self.password = password
        self.state = "stopped"
        self.currentplid = 1
        self.position = 0.0
        self.volume = 256
        self.calls: list[tuple[str, dict[str, list[str]]]] = []
        self.lock = threading.RLock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _authorized(self) -> bool:
                token = base64.b64encode(
                    (":" + outer.password).encode("utf-8")
                ).decode("ascii")
                return self.headers.get("Authorization", "") == f"Basic {token}"

            def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    self._send_json({"error": "unauthorized"}, status=401)
                    return

                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                with outer.lock:
                    outer.calls.append((parsed.path, query))

                    if parsed.path == "/art":
                        item = query.get("item", [""])[0]
                        if item != str(outer.currentplid):
                            self.send_response(404)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(VALID_JPEG)))
                        self.end_headers()
                        self.wfile.write(VALID_JPEG)
                        return

                    if parsed.path != "/requests/status.json":
                        self._send_json({"error": "not_found"}, status=404)
                        return

                    command = query.get("command", [""])[0]
                    if command == "pl_play" or command == "pl_forceresume":
                        outer.state = "playing"
                    elif command == "pl_forcepause":
                        outer.state = "paused"
                    elif command == "pl_stop":
                        outer.state = "stopped"
                    elif command == "pl_next":
                        outer.currentplid += 1
                        outer.position = 0.0
                    elif command == "pl_previous":
                        outer.currentplid = max(0, outer.currentplid - 1)
                        outer.position = 0.0
                    elif command == "seek":
                        outer.position = float(query.get("val", ["0"])[0])
                    elif command == "volume":
                        outer.volume = int(query.get("val", ["0"])[0])

                    payload = {
                        "state": outer.state,
                        "length": 180,
                        "time": outer.position,
                        "rate": 1.0,
                        "volume": outer.volume,
                        "currentplid": outer.currentplid,
                        "information": {
                            "category": {
                                "meta": {
                                    "title": f"Track {outer.currentplid}",
                                    "artist": "SSR Integration",
                                    "album": "Midgar Radio",
                                    "url": (
                                        "file:///C:/Music/"
                                        f"track-{outer.currentplid}.flac"
                                    ),
                                    "artwork_url": (
                                        "file:///C:/Music/"
                                        f"cover-{outer.currentplid}.jpg"
                                    ),
                                }
                            }
                        },
                    }
                    self._send_json(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)


def wait_terminal(runtime: MediaRuntime, request_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        row = runtime.command_status(request_id)
        if row and row.get("status") in {
            "completed",
            "failed",
            "expired",
            "uncertain",
        }:
            return row
        time.sleep(0.01)
    raise AssertionError(f"commande non terminale : {request_id}")


class VLCMediaRuntimeIntegrationTests(unittest.TestCase):
    def _runtime(self, server: FakeVLCLoopbackServer) -> MediaRuntime:
        provider = VLCProvider(
            VLCConfig(
                host="127.0.0.1",
                port=server.port,
                password="secret",
                timeout_seconds=0.5,
            )
        )
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=0.1),
            provider,
        )
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime

    def test_real_transport_provider_and_runtime_round_trip(self) -> None:
        with FakeVLCLoopbackServer() as server:
            runtime = self._runtime(server)

            deadline = time.monotonic() + 2.0
            snapshot = runtime.state_store.public_snapshot()
            while (
                (
                    not snapshot.get("connected")
                    or snapshot.get("track_id") != "1"
                    or not runtime.artwork_store.snapshot().get("available")
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                snapshot = runtime.state_store.public_snapshot()

            self.assertTrue(snapshot["connected"])
            self.assertEqual(snapshot["title"], "Track 1")
            self.assertTrue(runtime.artwork_store.snapshot()["available"])

            pause = wait_terminal(runtime, runtime.request("pause"))
            self.assertEqual(pause["status"], "completed")
            self.assertEqual(pause["state"]["playback_state"], "paused")

            play = wait_terminal(runtime, runtime.request("play"))
            self.assertEqual(play["status"], "completed")
            self.assertEqual(play["state"]["playback_state"], "playing")

            next_result = wait_terminal(runtime, runtime.request("next"))
            self.assertEqual(next_result["status"], "completed")
            self.assertEqual(next_result["state"]["track_id"], "2")
            self.assertEqual(next_result["state"]["title"], "Track 2")

            artwork = runtime.artwork_store.snapshot()
            self.assertTrue(artwork["available"])
            self.assertEqual(artwork["content"], VALID_JPEG)

            art_items = [
                query.get("item", [""])[0]
                for path, query in server.calls
                if path == "/art"
            ]
            self.assertIn("1", art_items)
            self.assertIn("2", art_items)
            self.assertNotIn("", art_items)

    def test_local_control_api_round_trip_reaches_vlc_once(self) -> None:
        with FakeVLCLoopbackServer() as server:
            runtime = self._runtime(server)

            def action(name, _payload):
                if name != "media.next":
                    raise ValueError(f"action inattendue : {name}")
                return {
                    "request_id": runtime.request("next"),
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
                        "running": runtime.running,
                        "state": runtime.state_store.public_snapshot(),
                    }
                },
                action=action,
                request_status=runtime.command_status,
            )
            api.start()
            self.addCleanup(api.stop)

            request = Request(
                f"http://127.0.0.1:{api.bound_port}/media/next",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2.0) as response:
                accepted = json.loads(response.read())
            self.assertEqual(response.status, 202)
            request_id = accepted["request_id"]

            deadline = time.monotonic() + 2.0
            terminal = None
            while time.monotonic() < deadline:
                with urlopen(
                    (
                        f"http://127.0.0.1:{api.bound_port}"
                        f"/requests/{request_id}"
                    ),
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
            self.assertEqual(terminal["state"]["track_id"], "2")

            commands = [
                query.get("command", [""])[0]
                for path, query in server.calls
                if path == "/requests/status.json"
                and query.get("command")
            ]
            self.assertEqual(commands.count("pl_next"), 1)

    def test_wrong_password_is_disconnected_and_command_failure_is_certain(self) -> None:
        with FakeVLCLoopbackServer(password="correct") as server:
            provider = VLCProvider(
                VLCConfig(
                    host="127.0.0.1",
                    port=server.port,
                    password="wrong",
                    timeout_seconds=0.5,
                )
            )
            runtime = MediaRuntime(
                MediaRuntimeConfig(enabled=True, poll_seconds=0.1),
                provider,
            )
            runtime.start()
            self.addCleanup(runtime.stop)

            deadline = time.monotonic() + 2.0
            state = runtime.state_store.public_snapshot()
            while state.get("revision", 0) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
                state = runtime.state_store.public_snapshot()

            self.assertFalse(state["connected"])

            result = wait_terminal(runtime, runtime.request("next"))
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["success"])
            self.assertNotEqual(result["status"], "uncertain")
            self.assertNotIn("uri", result["state"])
            self.assertNotIn("error", result["state"])


if __name__ == "__main__":
    unittest.main()
