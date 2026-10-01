from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from .models import MediaState


_TICKS_PER_SECOND = 10_000_000


@dataclass(frozen=True, slots=True)
class JellyfinConfig:
    enabled: bool = False
    base_url: str = ""
    token: str = ""
    device_id: str = ""
    device_name: str = ""
    client_name: str = ""
    timeout_seconds: float = 3.0


class JellyfinProvider:
    name = "jellyfin"

    def __init__(self, config: JellyfinConfig):
        self.config = config
        self._active_session_id = ""
        self._active_artwork_key = ""

    def _authorization(self) -> str:
        return (
            'MediaBrowser Client="StreamStateRouter", '
            'Device="SSR", '
            'DeviceId="stream-state-router", '
            'Version="1", '
            f'Token="{self.config.token}"'
        )

    def _request(
        self,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        method: str = "GET",
    ) -> bytes:
        base = self.config.base_url.rstrip("/") + "/"
        relative = path.lstrip("/")
        url = urljoin(base, relative)
        if query:
            encoded = urlencode(
                {
                    str(key): str(value)
                    for key, value in query.items()
                    if value not in (None, "")
                }
            )
            if encoded:
                url += "?" + encoded
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization(),
                "X-Emby-Token": self.config.token,
                "User-Agent": "StreamStateRouter/1",
            },
            method=str(method or "GET").upper(),
        )
        with urlopen(
            request,
            timeout=max(0.5, float(self.config.timeout_seconds)),
        ) as response:
            return response.read()

    @staticmethod
    def _ticks(value: object) -> float:
        try:
            return max(0.0, float(value or 0) / _TICKS_PER_SECOND)
        except (TypeError, ValueError):
            return 0.0

    def _score(self, session: Mapping[str, Any]) -> tuple[int, str]:
        item = session.get("NowPlayingItem")
        if not isinstance(item, Mapping):
            return (-1, "")
        score = 0
        device_id = str(session.get("DeviceId") or "")
        device_name = str(session.get("DeviceName") or "")
        client = str(session.get("Client") or "")
        if self.config.device_id:
            if device_id.casefold() != self.config.device_id.casefold():
                return (-1, "")
            score += 100
        if self.config.device_name:
            if self.config.device_name.casefold() not in device_name.casefold():
                return (-1, "")
            score += 50
        if self.config.client_name:
            if self.config.client_name.casefold() not in client.casefold():
                return (-1, "")
            score += 25
        play_state = session.get("PlayState")
        if isinstance(play_state, Mapping):
            score += 5 if not bool(play_state.get("IsPaused")) else 2
        return (score, str(session.get("Id") or ""))

    def poll(self) -> MediaState:
        if (
            not self.config.enabled
            or not self.config.base_url.strip()
            or not self.config.token.strip()
        ):
            self._active_session_id = ""
            self._active_artwork_key = ""
            return MediaState(provider=self.name)

        raw = json.loads(self._request("/Sessions").decode("utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError("Réponse Jellyfin /Sessions invalide")

        candidates: list[tuple[int, str, Mapping[str, Any]]] = []
        for session in raw:
            if not isinstance(session, Mapping):
                continue
            score, session_id = self._score(session)
            if score >= 0:
                candidates.append((score, session_id, session))
        if not candidates:
            self._active_session_id = ""
            self._active_artwork_key = ""
            return MediaState(provider=self.name)

        _score, session_id, session = max(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        item = session.get("NowPlayingItem")
        assert isinstance(item, Mapping)
        play = session.get("PlayState")
        play_state = play if isinstance(play, Mapping) else {}

        artists_raw = item.get("Artists")
        artists = tuple(
            str(value)
            for value in (
                artists_raw if isinstance(artists_raw, list) else []
            )
            if str(value).strip()
        )
        if not artists:
            album_artists = item.get("AlbumArtists")
            if isinstance(album_artists, list):
                artists = tuple(
                    str(value.get("Name") or "")
                    for value in album_artists
                    if isinstance(value, Mapping)
                    and str(value.get("Name") or "").strip()
                )

        paused = bool(play_state.get("IsPaused"))
        playback = "paused" if paused else "playing"
        volume_raw = play_state.get("VolumeLevel")
        try:
            volume = (
                float(volume_raw)
                if volume_raw is not None
                else None
            )
        except (TypeError, ValueError):
            volume = None

        item_id = str(item.get("Id") or "")
        image_tags = item.get("ImageTags")
        artwork_key = ""
        if (
            item_id
            and isinstance(image_tags, Mapping)
            and image_tags.get("Primary")
        ):
            artwork_key = item_id

        supported_raw = session.get("SupportedCommands")
        supported_commands = tuple(
            str(value)
            for value in (
                supported_raw
                if isinstance(supported_raw, list)
                else []
            )
            if str(value).strip()
        )
        supports_media_control = bool(
            session.get("SupportsMediaControl", False)
        )

        self._active_session_id = session_id
        self._active_artwork_key = artwork_key
        return MediaState(
            provider=self.name,
            player=str(session.get("DeviceName") or session.get("Client") or ""),
            session_id=session_id,
            track_id=item_id,
            title=str(item.get("Name") or ""),
            artists=artists,
            album=str(item.get("Album") or ""),
            duration_seconds=self._ticks(item.get("RunTimeTicks")),
            position_seconds=self._ticks(play_state.get("PositionTicks")),
            playback=playback,
            volume=volume,
            can_seek=bool(play_state.get("CanSeek")),
            can_next=True,
            can_previous=supports_media_control,
            supports_media_control=supports_media_control,
            supported_commands=supported_commands,
            artwork_key=artwork_key,
            metadata={
                "client": str(session.get("Client") or ""),
                "device_id": str(session.get("DeviceId") or ""),
                "media_type": str(item.get("MediaType") or item.get("Type") or ""),
            },
        )

    def control(
        self,
        action: str,
        *,
        position_seconds: float | None = None,
    ) -> None:
        session_id = self._active_session_id
        if not session_id:
            raise RuntimeError("Aucune session Jellyfin active")
        commands = {
            "play_pause": "PlayPause",
            "play": "Unpause",
            "pause": "Pause",
            "stop": "Stop",
            "next": "NextTrack",
            "previous": "PreviousTrack",
            "seek": "Seek",
        }
        command = commands.get(str(action or "").strip().casefold())
        if command is None:
            raise ValueError(f"Commande média inconnue : {action}")
        query: dict[str, object] = {}
        if command == "Seek":
            if position_seconds is None:
                raise ValueError("position_seconds requis pour seek")
            query["seekPositionTicks"] = max(
                0,
                int(float(position_seconds) * _TICKS_PER_SECOND),
            )
        self._request(
            f"/Sessions/{quote(session_id, safe='')}/Playing/{command}",
            query=query,
            method="POST",
        )

    def artwork(self, key: str) -> tuple[bytes, str] | None:
        wanted = str(key or "").strip()
        if not wanted or wanted != self._active_artwork_key:
            return None
        body = self._request(
            f"/Items/{quote(wanted, safe='')}/Images/Primary",
            query={"maxWidth": 512, "quality": 90},
        )
        return (body, "image/jpeg")
