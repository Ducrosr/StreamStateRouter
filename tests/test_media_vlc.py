from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stream_state_router.media import (
    MediaProviderCommandError,
    MediaState,
    VLCConfig,
    VLCHttpTransport,
    VLCProvider,
)


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class FakeOpener:
    def __init__(
        self,
        body: bytes,
        content_type: str = "application/json",
    ):
        self.body = body
        self.content_type = content_type
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.body, self.content_type)


class ArtworkServer:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "image/jpeg",
        chunk_delay: float = 0.0,
        pre_header_delay: float = 0.0,
    ):
        self.body = body
        self.content_type = content_type
        self.chunk_delay = float(chunk_delay)
        self.pre_header_delay = float(pre_header_delay)
        self.authorization = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                outer.authorization = self.headers.get(
                    "Authorization",
                    "",
                )
                if outer.pre_header_delay > 0:
                    time.sleep(outer.pre_header_delay)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", outer.content_type)
                    self.send_header(
                        "Content-Length",
                        str(len(outer.body)),
                    )
                    self.end_headers()
                    if outer.chunk_delay > 0:
                        for byte in outer.body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(outer.chunk_delay)
                    else:
                        self.wfile.write(outer.body)
                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                ):
                    return

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


class FakeTransport:
    def __init__(
        self,
        status=None,
        artwork: bytes = b"cover",
        artwork_type: str = "image/jpeg",
    ):
        self.status = status or {}
        self.artwork = artwork
        self.artwork_type = artwork_type
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, path, params=None):
        values = dict(params or {})
        self.calls.append((str(path), values))
        command = str(values.get("command") or "")
        if command in {"pl_play", "pl_forceresume"}:
            self.status["state"] = "playing"
        elif command == "pl_forcepause":
            self.status["state"] = "paused"
        elif command == "pl_stop":
            self.status["state"] = "stopped"
        elif command and "state" not in self.status:
            # VLC status.json processes the command then returns getstatus(),
            # which always includes the resulting playlist state.
            self.status["state"] = "stopped"
        return self.status

    def get_bytes(self, path, *, max_bytes=8 * 1024 * 1024):
        self.calls.append((str(path), {"max_bytes": max_bytes}))
        return self.artwork, self.artwork_type


class VLCProviderTests(unittest.TestCase):
    def test_config_accepts_only_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(VLCConfig(host=host).host, host)

        with self.assertRaisesRegex(ValueError, "doit rester local"):
            VLCConfig(host="192.168.1.50")

        normalized = VLCConfig(
            host=" LOCALHOST ",
            port="8081",
            password=None,
            timeout_seconds="1.5",
        )
        self.assertEqual(normalized.host, "localhost")
        self.assertEqual(normalized.port, 8081)
        self.assertEqual(normalized.password, "")
        self.assertEqual(normalized.timeout_seconds, 1.5)

    def test_config_bounds_vlc_timeout(self) -> None:
        for value in (0, 0.09, 10.01, float("inf")):
            with self.assertRaisesRegex(ValueError, "Timeout VLC"):
                VLCConfig(timeout_seconds=value)

        self.assertEqual(
            VLCConfig(timeout_seconds=0.1).timeout_seconds,
            0.1,
        )
        self.assertEqual(
            VLCConfig(timeout_seconds=10).timeout_seconds,
            10.0,
        )

    def test_ipv6_base_url_is_bracketed(self) -> None:
        transport = VLCHttpTransport(
            VLCConfig(host="::1", port=8080, password="secret")
        )
        self.assertEqual(transport.base_url, "http://[::1]:8080")

    def test_http_transport_authenticates_locally_and_url_encodes_params(self) -> None:
        transport = VLCHttpTransport(
            VLCConfig(password="secret", timeout_seconds=1.25)
        )
        opener = FakeOpener(b'{"state":"stopped"}')
        transport._opener = opener

        payload = transport.get_json(
            "/requests/status.json",
            {
                "command": "in_play",
                "input": r"C:\Music\Mako Reactor.flac",
            },
        )

        self.assertEqual(payload["state"], "stopped")
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 1.25)
        self.assertTrue(
            request.full_url.startswith(
                "http://127.0.0.1:8080/requests/status.json?"
            )
        )
        self.assertIn("command=in_play", request.full_url)
        self.assertIn("Mako+Reactor.flac", request.full_url)
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic OnNlY3JldA==",
        )

    def test_http_transport_rejects_oversized_json_response(self) -> None:
        transport = VLCHttpTransport(VLCConfig(password="secret"))
        transport._opener = FakeOpener(
            b"x" * (2 * 1024 * 1024 + 1)
        )

        with self.assertRaisesRegex(
            ValueError,
            "trop volumineuse",
        ):
            transport.get_json("/requests/status.json")

    def test_http_transport_fetches_authenticated_artwork_bytes(self) -> None:
        with ArtworkServer(
            b"jpeg-bytes",
            content_type="image/jpeg",
        ) as server:
            transport = VLCHttpTransport(
                VLCConfig(
                    port=server.port,
                    password="secret",
                    timeout_seconds=1.25,
                )
            )

            body, content_type = transport.get_bytes("/art")

        self.assertEqual(body, b"jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(
            server.authorization,
            "Basic OnNlY3JldA==",
        )

    def test_artwork_request_timeout_is_capped_at_two_seconds(self) -> None:
        transport = VLCHttpTransport(
            VLCConfig(password="secret", timeout_seconds=10.0)
        )

        self.assertEqual(transport.artwork_timeout_seconds, 2.0)

    def test_provider_artwork_can_target_observed_playlist_item(self) -> None:
        transport = FakeTransport(
            artwork=b"png-bytes",
            artwork_type="image/png",
        )
        provider = VLCProvider(VLCConfig(), transport=transport)

        provider.artwork("17")

        self.assertEqual(
            transport.calls,
            [
                (
                    "/art?item=17",
                    {"max_bytes": 8 * 1024 * 1024},
                )
            ],
        )

    def test_provider_artwork_refuses_unqualified_current_item(self) -> None:
        transport = FakeTransport(
            artwork=b"png-bytes",
            artwork_type="image/png",
        )
        provider = VLCProvider(VLCConfig(), transport=transport)

        with self.assertRaisesRegex(ValueError, "playlist VLC requis"):
            provider.artwork()

        self.assertEqual(transport.calls, [])

    def test_artwork_header_limit_ignores_body_bytes_received_with_marker(self) -> None:
        body = b"\xff\xd8\xff" + b"x" * 65500
        header = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"\r\n"
        )
        first = header[:40]
        second_capacity = 65536
        second = (header[40:] + body)[:second_capacity]
        consumed = max(0, second_capacity - len(header[40:]))
        third = body[consumed:]

        class FakeSocket:
            def __init__(self):
                self.chunks = [first, second, third, b""]
                self.sent = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _value):
                return None

            def sendall(self, data):
                self.sent += bytes(data)

            def recv(self, size):
                if not self.chunks:
                    return b""
                chunk = self.chunks.pop(0)
                self.assert_size = size
                return chunk

        fake = FakeSocket()
        transport = VLCHttpTransport(
            VLCConfig(password="secret", timeout_seconds=1.0)
        )
        with patch(
            "stream_state_router.media.vlc.socket.create_connection",
            return_value=fake,
        ):
            content, content_type = transport.get_bytes("/art")

        self.assertEqual(content, body)
        self.assertEqual(content_type, "image/jpeg")

    def test_http_transport_rejects_oversized_artwork(self) -> None:
        with ArtworkServer(b"x" * 9) as server:
            transport = VLCHttpTransport(
                VLCConfig(
                    port=server.port,
                    password="secret",
                )
            )
            with self.assertRaisesRegex(
                ValueError,
                "trop volumineuse",
            ):
                transport.get_bytes("/art", max_bytes=8)

    def test_artwork_deadline_is_total_for_progressive_body(self) -> None:
        with ArtworkServer(
            b"abcdef",
            chunk_delay=0.12,
        ) as server:
            transport = VLCHttpTransport(
                VLCConfig(
                    port=server.port,
                    password="secret",
                    timeout_seconds=0.3,
                )
            )
            started = time.monotonic()
            with self.assertRaisesRegex(
                TimeoutError,
                "Délai total",
            ):
                transport.get_bytes("/art")
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.7)

    def test_artwork_deadline_includes_delayed_headers(self) -> None:
        with ArtworkServer(
            b"jpeg",
            pre_header_delay=0.35,
        ) as server:
            transport = VLCHttpTransport(
                VLCConfig(
                    port=server.port,
                    password="secret",
                    timeout_seconds=0.2,
                )
            )
            started = time.monotonic()
            with self.assertRaisesRegex(
                TimeoutError,
                "Délai total",
            ):
                transport.get_bytes("/art")
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.6)

    def test_status_is_normalized_without_leaking_vlc_shape(self) -> None:
        transport = FakeTransport(
            {
                "state": "playing",
                "length": 245,
                "time": 42,
                "rate": 1.25,
                "volume": 256,
                "currentplid": 17,
                "information": {
                    "category": {
                        "meta": {
                            "title": "Mako Reactor",
                            "artist": "Suno",
                            "album": "Midgar Radio",
                            "filename": "mako-reactor.flac",
                            "artwork_url": "file:///cover.jpg",
                            "url": "file:///music/mako-reactor.flac",
                        }
                    }
                },
            }
        )
        provider = VLCProvider(VLCConfig(), transport=transport)

        state = provider.state()

        self.assertEqual(
            state,
            MediaState(
                provider="vlc",
                connected=True,
                playback_state="playing",
                title="Mako Reactor",
                artist="Suno",
                album="Midgar Radio",
                artwork_url="file:///cover.jpg",
                uri="file:///music/mako-reactor.flac",
                duration_seconds=245,
                position_seconds=42,
                playback_rate=1.25,
                volume_percent=100,
                track_id="17",
            ),
        )
        self.assertEqual(
            transport.calls,
            [("/requests/status.json", {})],
        )

    def test_playlist_id_preserves_zero_and_drops_negative_sentinel(self) -> None:
        for raw_id, expected in ((0, "0"), (-1, ""), (None, "")):
            with self.subTest(raw_id=raw_id):
                provider = VLCProvider(
                    VLCConfig(),
                    transport=FakeTransport(
                        {
                            "state": "stopped",
                            "currentplid": raw_id,
                        }
                    ),
                )

                self.assertEqual(provider.state().track_id, expected)

    def test_empty_status_is_not_treated_as_connected(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=FakeTransport({}),
        )

        with self.assertRaisesRegex(ValueError, "incomplète"):
            provider.state()

    def test_status_falls_back_to_filename_for_title(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=FakeTransport(
                {
                    "state": "paused",
                    "information": {
                        "category": {
                            "meta": {"filename": "track.ogg"}
                        }
                    },
                }
            ),
        )

        state = provider.state()

        self.assertEqual(state.title, "track.ogg")
        self.assertEqual(state.playback_state, "paused")

    def test_command_transport_failure_is_marked_ambiguous(self) -> None:
        class FailingTransport(FakeTransport):
            def get_json(self, path, params=None):
                values = dict(params or {})
                self.calls.append((str(path), values))
                if values.get("command"):
                    raise OSError("connection reset")
                return {"state": "playing"}

        provider = VLCProvider(
            VLCConfig(),
            transport=FailingTransport({"state": "playing"}),
        )

        with self.assertRaises(MediaProviderCommandError):
            provider.next()

    def test_http_401_command_refusal_is_certain_failure(self) -> None:
        class UnauthorizedTransport(FakeTransport):
            def get_json(self, path, params=None):
                values = dict(params or {})
                self.calls.append((str(path), values))
                if values.get("command"):
                    raise HTTPError(
                        "http://127.0.0.1:8080/requests/status.json",
                        401,
                        "Unauthorized",
                        {},
                        None,
                    )
                return {"state": "playing"}

        provider = VLCProvider(
            VLCConfig(),
            transport=UnauthorizedTransport({"state": "playing"}),
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
            provider.next()

        self.assertNotIsInstance(
            caught.exception,
            MediaProviderCommandError,
        )

    def test_controls_use_explicit_pause_and_encoded_values(self) -> None:
        transport = FakeTransport({"state": "playing"})
        provider = VLCProvider(VLCConfig(), transport=transport)

        provider.pause()
        provider.stop()
        provider.next()
        provider.previous()
        provider.seek(12.5)
        provider.set_volume(75)
        provider.play_uri(r"C:\Music\Mako Reactor.flac")
        provider.enqueue_uri("file:///C:/Music/Next.flac")
        provider.clear_queue()

        self.assertEqual(
            transport.calls,
            [
                (
                    "/requests/status.json",
                    {"command": "pl_forcepause"},
                ),
                (
                    "/requests/status.json",
                    {"command": "pl_stop"},
                ),
                (
                    "/requests/status.json",
                    {"command": "pl_next"},
                ),
                (
                    "/requests/status.json",
                    {"command": "pl_previous"},
                ),
                (
                    "/requests/status.json",
                    {"command": "seek", "val": "12.500"},
                ),
                (
                    "/requests/status.json",
                    {"command": "volume", "val": 192},
                ),
                (
                    "/requests/status.json",
                    {
                        "command": "in_play",
                        "input": r"C:\Music\Mako Reactor.flac",
                    },
                ),
                (
                    "/requests/status.json",
                    {
                        "command": "in_enqueue",
                        "input": "file:///C:/Music/Next.flac",
                    },
                ),
                (
                    "/requests/status.json",
                    {"command": "pl_empty"},
                ),
            ],
        )

    def test_play_rejects_unknown_state_without_sending_command(self) -> None:
        transport = FakeTransport({"state": "buffering"})
        provider = VLCProvider(VLCConfig(), transport=transport)

        with self.assertRaisesRegex(RuntimeError, "État VLC inconnu"):
            provider.play()

        self.assertEqual(
            transport.calls,
            [("/requests/status.json", {})],
        )

    def test_play_maps_stopped_paused_and_playing_without_toggle(self) -> None:
        for initial, expected_command in (
            ("stopped", "pl_play"),
            ("paused", "pl_forceresume"),
            ("playing", None),
        ):
            with self.subTest(initial=initial):
                transport = FakeTransport({"state": initial})
                provider = VLCProvider(VLCConfig(), transport=transport)

                provider.play()

                commands = [
                    row[1].get("command")
                    for row in transport.calls
                    if row[1].get("command")
                ]
                if expected_command is None:
                    self.assertEqual(commands, [])
                else:
                    self.assertEqual(commands, [expected_command])
                self.assertEqual(transport.status["state"], "playing")

    def test_play_reports_failure_when_playlist_cannot_start(self) -> None:
        class EmptyPlaylistTransport(FakeTransport):
            def get_json(self, path, params=None):
                values = dict(params or {})
                self.calls.append((str(path), values))
                return {"state": "stopped"}

        provider = VLCProvider(
            VLCConfig(),
            transport=EmptyPlaylistTransport({"state": "stopped"}),
        )

        with self.assertRaisesRegex(RuntimeError, "pas démarré"):
            provider.play()

    def test_media_uri_rejects_unc_encoded_and_ambiguous_file_forms(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=FakeTransport({}),
        )
        rejected = (
            "file://///server/share/music.flac",
            "file:///%2F%2Fserver/share/music.flac",
            "file:///%5C%5Cserver/share/music.flac",
            "file:relative.mp3",
            "file:///relative.mp3",
            "file://user@localhost/C:/Music/a.mp3",
            "https://user:secret@example.test/audio",
            "https://example.test:bad/audio",
            "https://example.test/audio\nnext",
            "file:///C:/Music/a%0Ab.mp3",
            "https://example.test/audio%0Dnext",
            "https://example.test/audio%7Fnext",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non autorisée"):
                    provider.play_uri(value)

    def test_media_uri_accepts_windows_file_uri_with_unicode_and_percent(self) -> None:
        transport = FakeTransport({})
        provider = VLCProvider(VLCConfig(), transport=transport)

        value = "file:///C:/Musique/%C3%89t%C3%A9%20Mako%25.flac"
        provider.play_uri(value)

        self.assertEqual(
            transport.calls[-1],
            (
                "/requests/status.json",
                {"command": "in_play", "input": value},
            ),
        )

    def test_control_validation_rejects_unsafe_numbers_and_empty_uri(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=FakeTransport({}),
        )

        with self.assertRaises(ValueError):
            provider.seek(-1)
        with self.assertRaises(ValueError):
            provider.set_volume(201)
        with self.assertRaises(ValueError):
            provider.play_uri("")
        with self.assertRaises(ValueError):
            provider.enqueue_uri("")
        for value in (
            "ftp://example.test/music.flac",
            "rtsp://example.test/live",
            r"\\server\share\music.flac",
            "relative-track.flac",
            "file://server/share/music.flac",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "non autorisée",
                ):
                    provider.play_uri(value)

    def test_media_uri_accepts_local_file_and_http_sources(self) -> None:
        transport = FakeTransport({})
        provider = VLCProvider(VLCConfig(), transport=transport)

        provider.play_uri("file:///C:/Music/Local.flac")
        provider.enqueue_uri("https://radio.example.test/live")

        self.assertEqual(
            transport.calls,
            [
                (
                    "/requests/status.json",
                    {
                        "command": "in_play",
                        "input": "file:///C:/Music/Local.flac",
                    },
                ),
                (
                    "/requests/status.json",
                    {
                        "command": "in_enqueue",
                        "input": "https://radio.example.test/live",
                    },
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
