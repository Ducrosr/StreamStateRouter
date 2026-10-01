from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MediaState:
    provider: str = ""
    player: str = ""
    session_id: str = ""
    track_id: str = ""
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    duration_seconds: float = 0.0
    position_seconds: float = 0.0
    playback: str = "stopped"
    volume: float | None = None
    can_seek: bool = False
    can_next: bool = False
    can_previous: bool = False
    artwork_key: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def playing(self) -> bool:
        return self.playback == "playing"

    @property
    def paused(self) -> bool:
        return self.playback == "paused"

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "player": self.player,
            "session_id": self.session_id,
            "track_id": self.track_id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration_seconds": self.duration_seconds,
            "position_seconds": self.position_seconds,
            "playback": self.playback,
            "playing": self.playing,
            "paused": self.paused,
            "volume": self.volume,
            "can_seek": self.can_seek,
            "can_next": self.can_next,
            "can_previous": self.can_previous,
            "artwork_key": self.artwork_key,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MediaStateSnapshot:
    revision: int
    updated_at: float
    state: MediaState

    def as_mapping(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "updated_at": self.updated_at,
            **self.state.as_mapping(),
        }


class MediaStateStore:
    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._revision = 0
        self._updated_at = float(clock())
        self._state = MediaState()

    def update(self, state: MediaState) -> MediaStateSnapshot:
        if not isinstance(state, MediaState):
            raise TypeError("state doit être un MediaState")
        with self._lock:
            if state != self._state:
                self._state = state
                self._revision += 1
                self._updated_at = float(self._clock())
            return MediaStateSnapshot(
                self._revision,
                self._updated_at,
                self._state,
            )

    def snapshot(self) -> MediaStateSnapshot:
        with self._lock:
            return MediaStateSnapshot(
                self._revision,
                self._updated_at,
                self._state,
            )
