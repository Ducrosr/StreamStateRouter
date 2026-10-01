from __future__ import annotations

from dataclasses import dataclass


MEDIA_PLAYBACK_STATES = frozenset(
    {"playing", "paused", "stopped", "unknown"}
)


@dataclass(frozen=True, slots=True)
class MediaState:
    """Normalized, provider-agnostic media state exposed to SSR consumers."""

    provider: str
    connected: bool
    playback_state: str = "unknown"
    title: str = ""
    artist: str = ""
    album: str = ""
    artwork_url: str = ""
    uri: str = ""
    duration_seconds: float = 0.0
    position_seconds: float = 0.0
    volume_percent: float = 0.0
    track_id: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        state = str(self.playback_state or "unknown").strip().casefold()
        if state not in MEDIA_PLAYBACK_STATES:
            state = "unknown"
        object.__setattr__(self, "playback_state", state)
        object.__setattr__(
            self,
            "duration_seconds",
            max(0.0, float(self.duration_seconds or 0.0)),
        )
        object.__setattr__(
            self,
            "position_seconds",
            max(0.0, float(self.position_seconds or 0.0)),
        )
        object.__setattr__(
            self,
            "volume_percent",
            max(0.0, min(200.0, float(self.volume_percent or 0.0))),
        )

    @property
    def playing(self) -> bool:
        return self.playback_state == "playing"

    def semantic_key(self) -> tuple[object, ...]:
        """State identity excluding continuously changing playback position."""

        return (
            self.provider,
            self.connected,
            self.playback_state,
            self.title,
            self.artist,
            self.album,
            self.artwork_url,
            self.uri,
            round(self.duration_seconds, 3),
            round(self.volume_percent, 2),
            self.track_id,
            self.error,
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "connected": self.connected,
            "playback_state": self.playback_state,
            "playing": self.playing,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork_url": self.artwork_url,
            "uri": self.uri,
            "duration_seconds": self.duration_seconds,
            "position_seconds": self.position_seconds,
            "volume_percent": self.volume_percent,
            "track_id": self.track_id,
            "error": self.error,
        }

    def as_public_mapping(self) -> dict[str, object]:
        """Browser/event-safe projection without local paths or backend errors."""

        return {
            "provider": self.provider,
            "connected": self.connected,
            "playback_state": self.playback_state,
            "playing": self.playing,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_seconds": self.duration_seconds,
            "position_seconds": self.position_seconds,
            "volume_percent": self.volume_percent,
            "track_id": self.track_id,
        }
