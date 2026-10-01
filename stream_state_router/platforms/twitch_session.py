from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .twitch import (
    TwitchEventSubMessageProcessor,
    TwitchEventSubResult,
    TwitchSubscriptionSpec,
)


TWITCH_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
TWITCH_EVENTSUB_CREATE_URL = (
    "https://api.twitch.tv/helix/eventsub/subscriptions"
)
TWITCH_TOKEN_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_DEVICE_CODE_URL = "https://id.twitch.tv/oauth2/device"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"


@dataclass(frozen=True, slots=True)
class TwitchSessionInstruction:
    result: TwitchEventSubResult
    subscribe: tuple[dict[str, object], ...] = ()
    reconnect_url: str = ""
    close_old_after_welcome: bool = False


@dataclass(slots=True)
class TwitchEventSubSessionCoordinator:
    processor: TwitchEventSubMessageProcessor
    subscriptions: Sequence[TwitchSubscriptionSpec]
    clock: Callable[[], float] = time.monotonic
    session_id: str = ""
    keepalive_timeout_seconds: int | None = None
    last_message_at: float = 0.0
    reconnect_url: str = ""
    awaiting_reconnect_welcome: bool = False
    connected: bool = False
    subscription_payloads: tuple[dict[str, object], ...] = field(
        default_factory=tuple
    )

    def handle(
        self,
        frame: Mapping[str, object],
    ) -> TwitchSessionInstruction:
        now = self.clock()
        result = self.processor.process(frame)
        self.last_message_at = now

        if result.kind == "welcome":
            self.session_id = result.session_id
            self.keepalive_timeout_seconds = (
                result.keepalive_timeout_seconds
            )
            self.connected = True
            if self.awaiting_reconnect_welcome:
                self.awaiting_reconnect_welcome = False
                self.reconnect_url = ""
                self.subscription_payloads = ()
                return TwitchSessionInstruction(
                    result=result,
                    close_old_after_welcome=True,
                )

            payloads = tuple(
                spec.create_payload(result.session_id)
                for spec in self.subscriptions
            )
            self.subscription_payloads = payloads
            return TwitchSessionInstruction(
                result=result,
                subscribe=payloads,
            )

        if result.kind == "reconnect":
            self.reconnect_url = result.reconnect_url
            self.awaiting_reconnect_welcome = bool(
                result.reconnect_url
            )
            return TwitchSessionInstruction(
                result=result,
                reconnect_url=result.reconnect_url,
            )

        if result.kind == "revocation":
            return TwitchSessionInstruction(result=result)

        return TwitchSessionInstruction(result=result)

    def connection_lost(self) -> None:
        self.connected = False
        self.session_id = ""
        self.keepalive_timeout_seconds = None
        self.reconnect_url = ""
        self.awaiting_reconnect_welcome = False
        self.subscription_payloads = ()

    def keepalive_expired(
        self,
        *,
        now: float | None = None,
        grace_seconds: float = 1.0,
    ) -> bool:
        timeout = self.keepalive_timeout_seconds
        if (
            not self.connected
            or timeout is None
            or self.last_message_at <= 0
        ):
            return False
        current = self.clock() if now is None else float(now)
        deadline = (
            self.last_message_at
            + max(1, int(timeout))
            + max(0.0, float(grace_seconds))
        )
        return current > deadline


@dataclass(frozen=True, slots=True)
class TwitchDeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TwitchOAuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]
    token_type: str = "bearer"


@dataclass(frozen=True, slots=True)
class TwitchTokenValidation:
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_in: int


class TwitchOAuthDeviceClient:
    """Public-client Device Code OAuth flow for the desktop application."""

    def __init__(
        self,
        *,
        opener=urlopen,
        timeout_seconds: float = 5.0,
    ):
        self._opener = opener
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    def start(
        self,
        *,
        client_id: str,
        scopes: Sequence[str],
    ) -> TwitchDeviceAuthorization:
        client = str(client_id or "").strip()
        if not client:
            raise ValueError("client_id requis")
        normalized_scopes = tuple(
            sorted(
                {
                    str(scope).strip()
                    for scope in scopes
                    if str(scope).strip()
                },
                key=str.casefold,
            )
        )
        payload = self._post_form(
            TWITCH_DEVICE_CODE_URL,
            {
                "client_id": client,
                "scopes": " ".join(normalized_scopes),
            },
        )
        return TwitchDeviceAuthorization(
            device_code=str(payload.get("device_code") or ""),
            user_code=str(payload.get("user_code") or ""),
            verification_uri=str(
                payload.get("verification_uri") or ""
            ),
            expires_in=int(payload.get("expires_in") or 0),
            interval=max(1, int(payload.get("interval") or 5)),
            scopes=normalized_scopes,
        )

    def exchange(
        self,
        *,
        client_id: str,
        device_code: str,
        scopes: Sequence[str],
    ) -> TwitchOAuthTokens | None:
        client = str(client_id or "").strip()
        code = str(device_code or "").strip()
        if not client:
            raise ValueError("client_id requis")
        if not code:
            raise ValueError("device_code requis")
        normalized_scopes = tuple(
            sorted(
                {
                    str(scope).strip()
                    for scope in scopes
                    if str(scope).strip()
                },
                key=str.casefold,
            )
        )
        try:
            payload = self._post_form(
                TWITCH_TOKEN_URL,
                {
                    "client_id": client,
                    "scopes": " ".join(normalized_scopes),
                    "device_code": code,
                    "grant_type": (
                        "urn:ietf:params:oauth:grant-type:device_code"
                    ),
                },
            )
        except RuntimeError as exc:
            if "authorization_pending" in str(exc).casefold():
                return None
            raise
        return self._tokens(payload)

    def refresh(
        self,
        *,
        client_id: str,
        refresh_token: str,
    ) -> TwitchOAuthTokens:
        client = str(client_id or "").strip()
        token = str(refresh_token or "").strip()
        if not client:
            raise ValueError("client_id requis")
        if not token:
            raise ValueError("refresh_token requis")
        payload = self._post_form(
            TWITCH_TOKEN_URL,
            {
                "client_id": client,
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
        )
        return self._tokens(payload)

    @staticmethod
    def _tokens(payload: Mapping[str, object]) -> TwitchOAuthTokens:
        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or "").strip()
        if not access or not refresh:
            raise RuntimeError("Réponse OAuth Twitch incomplète")
        scopes_raw = payload.get("scope")
        return TwitchOAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(payload.get("expires_in") or 0),
            scopes=tuple(
                str(item)
                for item in (
                    scopes_raw
                    if isinstance(scopes_raw, list)
                    else []
                )
                if str(item)
            ),
            token_type=str(
                payload.get("token_type") or "bearer"
            ),
        )

    def _post_form(
        self,
        url: str,
        values: Mapping[str, object],
    ) -> Mapping[str, object]:
        request = Request(
            url,
            data=urlencode(
                {
                    str(key): str(value)
                    for key, value in values.items()
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                )
            },
            method="POST",
        )
        try:
            with self._opener(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(
                    exc.read().decode("utf-8")
                )
                if isinstance(payload, Mapping):
                    detail = str(
                        payload.get("message")
                        or payload.get("error")
                        or ""
                    )
            except Exception:
                detail = ""
            suffix = f" : {detail}" if detail else ""
            raise RuntimeError(
                f"Twitch OAuth HTTP {exc.code}{suffix}"
            ) from exc
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise RuntimeError("Réponse OAuth Twitch invalide")
        return payload


class TwitchHelixClient:
    """Minimal HTTPS client for token validation and EventSub subscription.

    Access tokens are supplied by the caller per request. This class does not
    persist or log credentials.
    """

    def __init__(
        self,
        *,
        opener=urlopen,
        timeout_seconds: float = 5.0,
    ):
        self._opener = opener
        self._timeout_seconds = max(
            1.0,
            float(timeout_seconds),
        )

    def validate_token(
        self,
        access_token: str,
    ) -> TwitchTokenValidation:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("access_token requis")
        request = Request(
            TWITCH_TOKEN_VALIDATE_URL,
            headers={"Authorization": f"OAuth {token}"},
            method="GET",
        )
        payload = self._json_request(request)
        scopes_raw = payload.get("scopes")
        scopes = tuple(
            str(item)
            for item in (
                scopes_raw
                if isinstance(scopes_raw, list)
                else []
            )
            if str(item)
        )
        return TwitchTokenValidation(
            client_id=str(payload.get("client_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            login=str(payload.get("login") or ""),
            scopes=scopes,
            expires_in=int(payload.get("expires_in") or 0),
        )

    def create_subscription(
        self,
        *,
        client_id: str,
        access_token: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        client = str(client_id or "").strip()
        token = str(access_token or "").strip()
        if not client:
            raise ValueError("client_id requis")
        if not token:
            raise ValueError("access_token requis")
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            TWITCH_EVENTSUB_CREATE_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": client,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._json_request(request)

    def _json_request(
        self,
        request: Request,
    ) -> Mapping[str, object]:
        try:
            with self._opener(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = ""
            try:
                body = exc.read()
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, Mapping):
                    detail = str(
                        payload.get("message")
                        or payload.get("error")
                        or ""
                    )
            except Exception:
                detail = ""
            suffix = f" : {detail}" if detail else ""
            raise RuntimeError(
                f"Twitch HTTP {exc.code}{suffix}"
            ) from exc
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise RuntimeError(
                "Réponse Twitch JSON invalide"
            )
        return payload
