from __future__ import annotations

import unittest

from stream_state_router.media import (
    MediaState,
    VLCConfig,
    VLCHttpTransport,
    VLCProvider,
)


class FakeTransport:
    def __init__(self, status=None):
        self.status = status or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, path, params=None):
        self.calls.append((str(path), dict(params or {})))
        return self.status


class VLCProviderTests(unittest.TestCase):
    def test_config_accepts_only_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(VLCConfig(host=host).host, host)

        with self.assertRaisesRegex(ValueError, "doit rester local"):
            VLCConfig(host="192.168.1.50")

    def test_ipv6_base_url_is_bracketed(self) -> None:
        transport = VLCHttpTransport(
            VLCConfig(host="::1", port=8080, password="secret")
        )
        self.assertEqual(transport.base_url, "http://[::1]:8080")

    def test_status_is_normalized_without_leaking_vlc_shape(self) -> None:
        transport = FakeTransport(
            {
                "state": "playing",
                "length": 245,
                "time": 42,
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
                volume_percent=100,
                track_id="17",
            ),
        )
        self.assertEqual(
            transport.calls,
            [("/requests/status.json", {})],
        )

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

    def test_controls_use_idempotent_pause_resume_and_encoded_values(self) -> None:
        transport = FakeTransport({})
        provider = VLCProvider(VLCConfig(), transport=transport)

        provider.play()
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
                    {"command": "pl_forceresume"},
                ),
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


if __name__ == "__main__":
    unittest.main()
