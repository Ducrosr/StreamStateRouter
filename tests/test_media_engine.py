from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from stream_state_router.events import EventBus
from stream_state_router.media import (
    JellyfinConfig,
    JellyfinProvider,
    MediaEngine,
    MediaEngineConfig,
    MediaState,
    MediaStateStore,
)


class _Provider:
    def __init__(self, name: str, states: list[MediaState]):
        self.name = name
        self.states = list(states)
        self.artwork_calls: list[str] = []
        self.control_calls: list[tuple[str, float | None]] = []

    def poll(self) -> MediaState:
        if self.states:
            return self.states.pop(0)
        return MediaState(provider=self.name)

    def artwork(self, key: str):
        self.artwork_calls.append(key)
        return (b"img", "image/jpeg")

    def control(
        self,
        action: str,
        *,
        position_seconds: float | None = None,
    ) -> None:
        self.control_calls.append((action, position_seconds))


class MediaEngineTests(unittest.TestCase):
    def test_store_revisions_only_change_for_state_changes(self) -> None:
        store = MediaStateStore(clock=lambda: 10.0)
        state = MediaState(
            provider="test",
            track_id="1",
            title="Track",
            playback="playing",
        )
        first = store.update(state)
        second = store.update(state)

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 1)

    def test_engine_prefers_first_active_provider(self) -> None:
        store = MediaStateStore()
        idle = _Provider("idle", [MediaState(provider="idle")])
        active = _Provider(
            "active",
            [
                MediaState(
                    provider="active",
                    track_id="x",
                    title="Song",
                    playback="playing",
                )
            ],
        )
        engine = MediaEngine(
            MediaEngineConfig(),
            store,
            providers=(idle, active),
        )

        state = engine.poll_once()

        self.assertEqual(state.provider, "active")
        self.assertEqual(state.title, "Song")

    def test_engine_publishes_track_change(self) -> None:
        store = MediaStateStore()
        bus = EventBus()
        provider = _Provider(
            "media",
            [
                MediaState(
                    provider="media",
                    track_id="a",
                    title="A",
                    playback="playing",
                ),
                MediaState(
                    provider="media",
                    track_id="b",
                    title="B",
                    playback="playing",
                ),
            ],
        )
        engine = MediaEngine(
            MediaEngineConfig(),
            store,
            providers=(provider,),
            event_bus=bus,
        )

        engine.poll_once()
        engine.poll_once()

        events = bus.events("media")
        self.assertEqual(
            [event.type for event in events],
            ["track_changed", "track_changed"],
        )
        self.assertEqual(events[-1].payload["title"], "B")

    def test_control_requires_provider_capability(self) -> None:
        store = MediaStateStore()
        provider = _Provider(
            "media",
            [
                MediaState(
                    provider="media",
                    track_id="a",
                    playback="playing",
                    supports_media_control=False,
                )
            ],
        )
        engine = MediaEngine(
            MediaEngineConfig(),
            store,
            providers=(provider,),
        )
        engine.poll_once()

        with self.assertRaisesRegex(
            RuntimeError,
            "n’annonce pas le contrôle média distant",
        ):
            engine.control("next")

        self.assertEqual(provider.control_calls, [])

    def test_control_delegates_to_active_provider(self) -> None:
        store = MediaStateStore()
        provider = _Provider(
            "media",
            [
                MediaState(
                    provider="media",
                    track_id="a",
                    playback="playing",
                    supports_media_control=True,
                    supported_commands=("NextTrack", "Seek"),
                )
            ],
        )
        engine = MediaEngine(
            MediaEngineConfig(),
            store,
            providers=(provider,),
        )
        engine.poll_once()

        engine.control("next")
        engine.control("seek", position_seconds=42.5)

        self.assertEqual(
            provider.control_calls,
            [("next", None), ("seek", 42.5)],
        )

    def test_artwork_is_delegated_to_active_provider(self) -> None:
        store = MediaStateStore()
        provider = _Provider(
            "media",
            [
                MediaState(
                    provider="media",
                    track_id="a",
                    artwork_key="cover",
                    playback="playing",
                )
            ],
        )
        engine = MediaEngine(
            MediaEngineConfig(),
            store,
            providers=(provider,),
        )
        engine.poll_once()

        self.assertEqual(engine.artwork("cover"), (b"img", "image/jpeg"))
        self.assertEqual(provider.artwork_calls, ["cover"])


class JellyfinProviderTests(unittest.TestCase):
    def test_session_mapping_normalizes_now_playing_state(self) -> None:
        payload = [
            {
                "Id": "session-1",
                "Client": "Jellyfin Web",
                "DeviceId": "pc",
                "DeviceName": "Streaming PC",
                "NowPlayingItem": {
                    "Id": "track-1",
                    "Name": "Mako Reactor",
                    "Artists": ["Shinra Radio"],
                    "Album": "Midgar",
                    "RunTimeTicks": 2_400_000_000,
                    "ImageTags": {"Primary": "abc"},
                    "MediaType": "Audio",
                },
                "SupportsMediaControl": True,
                "SupportedCommands": [
                    "PlayPause",
                    "NextTrack",
                    "PreviousTrack",
                    "Seek",
                ],
                "PlayState": {
                    "IsPaused": False,
                    "PositionTicks": 600_000_000,
                    "CanSeek": True,
                    "VolumeLevel": 72,
                },
            }
        ]
        provider = JellyfinProvider(
            JellyfinConfig(
                enabled=True,
                base_url="http://jellyfin.local",
                token="secret",
                device_name="Streaming",
            )
        )
        with patch.object(
            provider,
            "_request",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            state = provider.poll()

        self.assertEqual(state.track_id, "track-1")
        self.assertEqual(state.title, "Mako Reactor")
        self.assertEqual(state.artists, ("Shinra Radio",))
        self.assertEqual(state.duration_seconds, 240.0)
        self.assertEqual(state.position_seconds, 60.0)
        self.assertEqual(state.playback, "playing")
        self.assertEqual(state.volume, 72.0)
        self.assertTrue(state.supports_media_control)
        self.assertIn("NextTrack", state.supported_commands)
        self.assertEqual(state.artwork_key, "track-1")

    def test_control_posts_to_active_session(self) -> None:
        provider = JellyfinProvider(
            JellyfinConfig(
                enabled=True,
                base_url="http://jellyfin.local",
                token="secret",
            )
        )
        provider._active_session_id = "session-1"
        calls: list[tuple[str, dict, str]] = []

        def request(path, *, query=None, method="GET"):
            calls.append((path, dict(query or {}), method))
            return b""

        with patch.object(provider, "_request", side_effect=request):
            provider.control("next")
            provider.control("seek", position_seconds=12.5)

        self.assertEqual(
            calls[0],
            (
                "/Sessions/session-1/Playing/NextTrack",
                {},
                "POST",
            ),
        )
        self.assertEqual(
            calls[1],
            (
                "/Sessions/session-1/Playing/Seek",
                {"seekPositionTicks": 125_000_000},
                "POST",
            ),
        )

    def test_session_selector_rejects_other_device(self) -> None:
        provider = JellyfinProvider(
            JellyfinConfig(
                enabled=True,
                base_url="http://jellyfin.local",
                token="secret",
                device_id="wanted",
            )
        )
        payload = [
            {
                "Id": "other",
                "DeviceId": "other",
                "NowPlayingItem": {"Id": "track"},
            }
        ]
        with patch.object(
            provider,
            "_request",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            state = provider.poll()

        self.assertEqual(state.track_id, "")
        self.assertEqual(state.playback, "stopped")


if __name__ == "__main__":
    unittest.main()
