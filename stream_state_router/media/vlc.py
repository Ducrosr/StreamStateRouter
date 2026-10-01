from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import re
from typing import Mapping
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import MediaState


@dataclass(frozen=True, slots=True)
class VLCConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        host = str(self.host or "").strip().casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "VLC HTTP doit rester local (127.0.0.1, localhost ou ::1)"
            )
        port = int(self.port)
        if not 1 <= port <= 65535:
            raise ValueError("Port VLC invalide")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 10.0:
            raise ValueError("Timeout VLC invalide (0.1..10 s)")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "password", str(self.password or ""))
        object.__setattr__(self, "timeout_seconds", timeout)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


class VLCHttpTransport:
    """Minimal VLC Lua HTTP JSON transport using Python stdlib only."""

    def __init__(self, config: VLCConfig):
        self.config = config
        self._opener = build_opener(
            ProxyHandler({}),
            _NoRedirectHandler(),
        )

    @property
    def base_url(self) -> str:
        host = str(self.config.host or "").strip()
        if host == "::1":
            host = "[::1]"
        elif host == "localhost":
            host = "127.0.0.1"
        return f"http://{host}:{int(self.config.port)}"

    def get_json(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        query = ""
        if params:
            query = "?" + urlencode(
                {
                    str(key): str(value)
                    for key, value in params.items()
                    if value is not None
                }
            )
        token = base64.b64encode(
            (":" + self.config.password).encode("utf-8")
        ).decode("ascii")
        request = Request(
            self.base_url + str(path) + query,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            method="GET",
        )
        with self._opener.open(
            request,
            timeout=float(self.config.timeout_seconds),
        ) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Réponse JSON VLC trop volumineuse")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Réponse JSON VLC invalide")
        return payload


    def get_bytes(
        self,
        path: str,
        *,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        token = base64.b64encode(
            (":" + self.config.password).encode("utf-8")
        ).decode("ascii")
        request = Request(
            self.base_url + str(path),
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "image/*",
                "Cache-Control": "no-cache",
            },
            method="GET",
        )
        limit = max(1, int(max_bytes))
        with self._opener.open(
            request,
            # Artwork is optional metadata. Never let a cover fetch block
            # media controls for the full provider timeout.
            timeout=min(float(self.config.timeout_seconds), 2.0),
        ) as response:
            raw = response.read(limit + 1)
            content_type = str(
                response.headers.get("Content-Type") or ""
            )
        if len(raw) > limit:
            raise ValueError("Pochette VLC trop volumineuse")
        return raw, content_type

class VLCProvider:
    """MediaProvider implementation for VLC's local Lua HTTP interface."""

    def __init__(
        self,
        config: VLCConfig,
        *,
        transport: VLCHttpTransport | None = None,
    ):
        self.config = config
        self._transport = transport or VLCHttpTransport(config)

    @property
    def name(self) -> str:
        return "vlc"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return (
            "play",
            "pause",
            "stop",
            "next",
            "previous",
            "seek",
            "set_volume",
            "play_uri",
            "enqueue_uri",
            "clear_queue",
            "artwork",
        )

    @staticmethod
    def _number(value: object, default: float = 0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return result if math.isfinite(result) else default

    @staticmethod
    def _meta(payload: Mapping[str, object]) -> Mapping[str, object]:
        information = payload.get("information")
        if not isinstance(information, Mapping):
            return {}
        category = information.get("category")
        if not isinstance(category, Mapping):
            return {}
        meta = category.get("meta")
        return meta if isinstance(meta, Mapping) else {}

    def artwork(self) -> tuple[bytes, str]:
        return self._transport.get_bytes("/art")

    def state(self) -> MediaState:
        raw = self._transport.get_json("/requests/status.json")
        meta = self._meta(raw)
        playback = self._playback_state(raw)

        raw_volume = self._number(raw.get("volume"), 0.0)
        # VLC's HTTP API uses 256 as the 100% reference and permits boost
        # values above it. SSR exposes a normalized 0..200% value.
        volume_percent = raw_volume * 100.0 / 256.0

        title = str(meta.get("title") or "").strip()
        filename = str(meta.get("filename") or "").strip()
        if not title:
            title = filename

        return MediaState(
            provider=self.name,
            connected=True,
            playback_state=playback,
            title=title,
            artist=str(meta.get("artist") or "").strip(),
            album=str(meta.get("album") or "").strip(),
            artwork_url=str(
                meta.get("artwork_url")
                or meta.get("artworkurl")
                or ""
            ).strip(),
            uri=str(
                meta.get("url")
                or meta.get("uri")
                or filename
                or ""
            ).strip(),
            duration_seconds=self._number(raw.get("length"), 0.0),
            position_seconds=self._number(raw.get("time"), 0.0),
            playback_rate=self._number(raw.get("rate"), 1.0),
            volume_percent=volume_percent,
            track_id=str(raw.get("currentplid") or "").strip(),
        )

    @staticmethod
    def _playback_state(payload: Mapping[str, object]) -> str:
        playback = str(
            payload.get("state") or "unknown"
        ).strip().casefold()
        if playback == "opening":
            return "playing"
        if playback in {"playing", "paused", "stopped"}:
            return playback
        return "unknown"

    def _command(
        self,
        command: str,
        **params: object,
    ) -> Mapping[str, object]:
        values: dict[str, object] = {"command": command}
        values.update(params)
        return self._transport.get_json(
            "/requests/status.json",
            values,
        )

    @staticmethod
    def _validated_media_uri(uri: str) -> str:
        value = str(uri or "").strip()
        if not value:
            raise ValueError("URI média requise")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("URI média non autorisée : caractère de contrôle")
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return value
        if value.startswith(("\\\\", "//")):
            raise ValueError("URI média non autorisée : chemin UNC")
        try:
            parts = urlsplit(value)
            scheme = parts.scheme.casefold()
            hostname = str(parts.hostname or "").casefold()
            # Accessing .port validates malformed/non-numeric ports.
            _port = parts.port
        except ValueError as exc:
            raise ValueError("URI média non autorisée : URL invalide") from exc

        if scheme in {"http", "https"}:
            if (
                not hostname
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError(
                    "URI média non autorisée : URL HTTP(S) invalide"
                )
            return value

        if scheme == "file":
            if (
                parts.username is not None
                or parts.password is not None
                or _port is not None
                or hostname not in {"", "localhost"}
            ):
                raise ValueError(
                    "URI média non autorisée : autorité file:// invalide"
                )
            decoded_path = unquote(parts.path)
            # Decode once, matching the backend interpretation closely enough
            # to catch encoded UNC forms without recursive decoding.
            if (
                decoded_path.startswith("//")
                or decoded_path.startswith("\\\\")
                or decoded_path.startswith("/\\\\")
                or decoded_path.startswith("/\\")
            ):
                raise ValueError(
                    "URI média non autorisée : chemin UNC"
                )
            if not re.match(r"^/[A-Za-z]:[\\/]", decoded_path):
                raise ValueError(
                    "URI média non autorisée : chemin file:// absolu requis"
                )
            return value

        raise ValueError(
            "URI média non autorisée : utiliser un chemin local, "
            "file:// local, http:// ou https://"
        )

    def play(self) -> None:
        before = self._transport.get_json("/requests/status.json")
        playback = self._playback_state(before)
        if playback == "playing":
            return
        command = (
            "pl_forceresume"
            if playback == "paused"
            else "pl_play"
        )
        after = self._command(command)
        if self._playback_state(after) != "playing":
            raise RuntimeError("VLC n'a pas démarré la lecture")

    def pause(self) -> None:
        self._command("pl_forcepause")

    def stop(self) -> None:
        self._command("pl_stop")

    def next(self) -> None:
        self._command("pl_next")

    def previous(self) -> None:
        self._command("pl_previous")

    def seek(self, seconds: float) -> None:
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Position média invalide")
        self._command("seek", val=f"{value:.3f}")

    def set_volume(self, percent: float) -> None:
        value = float(percent)
        if not math.isfinite(value) or not 0.0 <= value <= 200.0:
            raise ValueError("Volume média hors plage (0..200%)")
        vlc_value = int(round(value * 256.0 / 100.0))
        self._command("volume", val=vlc_value)

    def play_uri(self, uri: str) -> None:
        self._command(
            "in_play",
            input=self._validated_media_uri(uri),
        )

    def enqueue_uri(self, uri: str) -> None:
        self._command(
            "in_enqueue",
            input=self._validated_media_uri(uri),
        )

    def clear_queue(self) -> None:
        self._command("pl_empty")
