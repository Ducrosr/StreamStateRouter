from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..events import EventBus
from .twitch import TwitchEventSubConfig


@dataclass(frozen=True, slots=True)
class TwitchTokenInfo:
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_in: int

    def as_mapping(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "user_id": self.user_id,
            "login": self.login,
            "scopes": list(self.scopes),
            "expires_in": self.expires_in,
        }


@dataclass(frozen=True, slots=True)
class TwitchAudienceState:
    live: bool = False
    viewer_count: int = 0
    title: str = ""
    game_id: str = ""
    game_name: str = ""
    started_at: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "live": self.live,
            "viewer_count": self.viewer_count,
            "title": self.title,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "started_at": self.started_at,
        }


class TwitchHelixClient:
    def __init__(self, config: TwitchEventSubConfig):
        self.config = config

    def _helix(
        self,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        url = "https://api.twitch.tv/helix/" + path.lstrip("/")
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
                "Authorization": (
                    f"Bearer {self.config.user_access_token}"
                ),
                "Client-Id": self.config.client_id,
                "Accept": "application/json",
                "User-Agent": "StreamStateRouter/1",
            },
            method="GET",
        )
        with urlopen(request, timeout=10.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Réponse Twitch Helix invalide")
        return payload

    def validate_token(self) -> TwitchTokenInfo:
        request = Request(
            "https://id.twitch.tv/oauth2/validate",
            headers={
                "Authorization": (
                    f"OAuth {self.config.user_access_token}"
                ),
                "Accept": "application/json",
                "User-Agent": "StreamStateRouter/1",
            },
            method="GET",
        )
        with urlopen(request, timeout=10.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise RuntimeError("Réponse de validation Twitch invalide")
        scopes_raw = payload.get("scopes")
        scopes = tuple(
            str(value)
            for value in (
                scopes_raw if isinstance(scopes_raw, list) else []
            )
            if str(value).strip()
        )
        return TwitchTokenInfo(
            client_id=str(payload.get("client_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            login=str(payload.get("login") or ""),
            scopes=scopes,
            expires_in=int(payload.get("expires_in") or 0),
        )

    def audience(self) -> TwitchAudienceState:
        broadcaster = self.config.broadcaster_user_id.strip()
        if not broadcaster:
            return TwitchAudienceState()
        payload = self._helix(
            "streams",
            query={"user_id": broadcaster},
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            return TwitchAudienceState()
        row = rows[0]
        if not isinstance(row, Mapping):
            return TwitchAudienceState()
        return TwitchAudienceState(
            live=True,
            viewer_count=max(0, int(row.get("viewer_count") or 0)),
            title=str(row.get("title") or ""),
            game_id=str(row.get("game_id") or ""),
            game_name=str(row.get("game_name") or ""),
            started_at=str(row.get("started_at") or ""),
        )


class TwitchAudiencePoller:
    def __init__(
        self,
        client: TwitchHelixClient,
        event_bus: EventBus,
        *,
        interval_seconds: float = 30.0,
    ):
        self.client = client
        self.event_bus = event_bus
        self.interval_seconds = max(10.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state: TwitchAudienceState | None = None
        self._last_error = ""

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def last_error(self) -> str:
        return self._last_error

    def poll_once(self) -> TwitchAudienceState:
        state = self.client.audience()
        self._last_error = ""
        if state != self._last_state:
            self._last_state = state
            self.event_bus.publish(
                channel="audience",
                type="state",
                platform="twitch",
                payload=state.as_mapping(),
            )
        return state

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._last_error = str(exc)
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run,
            name="SSR-TwitchAudience",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
