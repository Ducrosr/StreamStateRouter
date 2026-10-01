from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import re
from typing import Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout VLC invalide")
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
        self._opener = build_opener(_NoRedirectHandler())

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

    def state(self) -> MediaState:
        raw = self._transport.get_json("/requests/status.json")
        meta = self._meta(raw)
        playback = str(raw.get("state") or "unknown").strip().casefold()
        if playback == "opening":
            playback = "playing"
        if playback not in {"playing", "paused", "stopped"}:
            playback = "unknown"

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
            volume_percent=volume_percent,
            track_id=str(raw.get("currentplid") or "").strip(),
        )

    def _command(self, command: str, **params: object) -> None:
        values: dict[str, object] = {"command": command}
        values.update(params)
        self._transport.get_json(
            "/requests/status.json",
            values,
        )

    @staticmethod
    def _validated_media_uri(uri: str) -> str:
        value = str(uri or "").strip()
        if not value:
            raise ValueError("URI média requise")
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return value
        parts = urlsplit(value)
        scheme = parts.scheme.casefold()
        if scheme in {"http", "https"} and parts.netloc:
            return value
        if (
            scheme == "file"
            and parts.path
            and str(parts.hostname or "").casefold()
            in {"", "localhost"}
        ):
            return value
        raise ValueError(
            "URI média non autorisée : utiliser un chemin local, "
            "file:// local, http:// ou https://"
        )

    def play(self) -> None:
        self._command("pl_forceresume")

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
