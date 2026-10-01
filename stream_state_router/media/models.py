from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math


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
    playback_rate: float = 0.0
    volume_percent: float = 0.0
    track_id: str = ""
    observed_at_unix: float = 0.0
    capabilities: tuple[str, ...] = ()
    error: str = ""

    def __post_init__(self) -> None:
        state = str(self.playback_state or "unknown").strip().casefold()
        if state not in MEDIA_PLAYBACK_STATES:
            state = "unknown"

        def finite_number(
            value: object,
            *,
            maximum: float | None = None,
        ) -> float:
            try:
                number = float(value or 0.0)
            except (TypeError, ValueError, OverflowError):
                number = 0.0
            if not math.isfinite(number):
                number = 0.0
            number = max(0.0, number)
            if maximum is not None:
                number = min(maximum, number)
            return number

        object.__setattr__(self, "provider", str(self.provider or "").strip())
        object.__setattr__(self, "connected", bool(self.connected))
        object.__setattr__(self, "playback_state", state)
        for field_name in (
            "title",
            "artist",
            "album",
            "artwork_url",
            "uri",
            "track_id",
            "error",
        ):
            object.__setattr__(
                self,
                field_name,
                str(getattr(self, field_name) or "").strip(),
            )
        object.__setattr__(
            self,
            "duration_seconds",
            finite_number(self.duration_seconds),
        )
        object.__setattr__(
            self,
            "position_seconds",
            finite_number(self.position_seconds),
        )
        object.__setattr__(
            self,
            "playback_rate",
            finite_number(self.playback_rate, maximum=16.0),
        )
        object.__setattr__(
            self,
            "volume_percent",
            finite_number(self.volume_percent, maximum=200.0),
        )
        object.__setattr__(
            self,
            "observed_at_unix",
            finite_number(self.observed_at_unix),
        )
        object.__setattr__(
            self,
            "capabilities",
            tuple(
                sorted(
                    {
                        str(item).strip().casefold()
                        for item in self.capabilities
                        if str(item).strip()
                    }
                )
            ),
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
            round(self.playback_rate, 3),
            round(self.volume_percent, 2),
            self.track_id,
            self.capabilities,
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
            "playback_rate": self.playback_rate,
            "volume_percent": self.volume_percent,
            "track_id": self.track_id,
            "observed_at_unix": self.observed_at_unix,
            "capabilities": list(self.capabilities),
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
            "playback_rate": self.playback_rate,
            "volume_percent": self.volume_percent,
            "track_id": self.track_id,
            "observed_at_unix": self.observed_at_unix,
            "capabilities": list(self.capabilities),
        }



def media_artwork_identity(
    *,
    provider: object = "",
    track_id: object = "",
    uri: object = "",
    title: object = "",
    artwork_url: object = "",
) -> str:
    """Stable private identity joining an observation to its cached artwork."""

    parts = (
        str(provider or "").strip(),
        str(track_id or "").strip(),
        str(uri or "").strip(),
        str(title or "").strip(),
        str(artwork_url or "").strip(),
    )
    if not any(parts[1:]):
        return ""
    raw = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
