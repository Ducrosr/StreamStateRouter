from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_TWITCH_SCOPES = (
    "user:read:chat",
    "moderator:read:followers",
    "channel:read:subscriptions",
    "bits:read",
)


@dataclass(frozen=True, slots=True)
class TwitchDeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class TwitchOAuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]
    token_type: str = "bearer"


def _form_request(
    url: str,
    values: dict[str, str],
) -> dict:
    data = urlencode(values).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "StreamStateRouter/1",
        },
    )
    try:
        with urlopen(request, timeout=10.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Twitch OAuth HTTP {exc.code}: {body or exc.reason}"
            ) from exc
        message = str(
            payload.get("message")
            or payload.get("error_description")
            or payload.get("error")
            or exc.reason
        )
        error = RuntimeError(
            f"Twitch OAuth HTTP {exc.code}: {message}"
        )
        setattr(error, "oauth_status", int(exc.code))
        setattr(error, "oauth_message", message)
        raise error from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Réponse Twitch OAuth invalide")
    return payload


def begin_device_authorization(
    client_id: str,
    *,
    scopes: Iterable[str] = DEFAULT_TWITCH_SCOPES,
) -> TwitchDeviceAuthorization:
    wanted_client = str(client_id or "").strip()
    if not wanted_client:
        raise ValueError("Client ID Twitch requis")
    wanted_scopes = tuple(
        sorted(
            {
                str(scope).strip()
                for scope in scopes
                if str(scope).strip()
            }
        )
    )
    payload = _form_request(
        "https://id.twitch.tv/oauth2/device",
        {
            "client_id": wanted_client,
            "scopes": " ".join(wanted_scopes),
        },
    )
    return TwitchDeviceAuthorization(
        device_code=str(payload.get("device_code") or ""),
        user_code=str(payload.get("user_code") or ""),
        verification_uri=str(
            payload.get("verification_uri") or ""
        ),
        expires_in=max(1, int(payload.get("expires_in") or 1)),
        interval=max(1, int(payload.get("interval") or 5)),
    )


def poll_device_tokens(
    client_id: str,
    authorization: TwitchDeviceAuthorization,
    *,
    scopes: Iterable[str] = DEFAULT_TWITCH_SCOPES,
) -> TwitchOAuthTokens | None:
    wanted_scopes = tuple(
        sorted(
            {
                str(scope).strip()
                for scope in scopes
                if str(scope).strip()
            }
        )
    )
    try:
        payload = _form_request(
            "https://id.twitch.tv/oauth2/token",
            {
                "client_id": str(client_id or "").strip(),
                "scopes": " ".join(wanted_scopes),
                "device_code": authorization.device_code,
                "grant_type": (
                    "urn:ietf:params:oauth:grant-type:device_code"
                ),
            },
        )
    except RuntimeError as exc:
        if str(getattr(exc, "oauth_message", "")).casefold() in {
            "authorization_pending",
            "authorization pending",
        }:
            return None
        raise
    scopes_raw = payload.get("scope")
    return TwitchOAuthTokens(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=max(0, int(payload.get("expires_in") or 0)),
        scopes=tuple(
            str(scope)
            for scope in (
                scopes_raw if isinstance(scopes_raw, list) else []
            )
            if str(scope).strip()
        ),
        token_type=str(payload.get("token_type") or "bearer"),
    )


def refresh_user_tokens(
    client_id: str,
    refresh_token: str,
) -> TwitchOAuthTokens:
    wanted_refresh = str(refresh_token or "").strip()
    if not wanted_refresh:
        raise ValueError("Refresh token Twitch requis")
    payload = _form_request(
        "https://id.twitch.tv/oauth2/token",
        {
            "client_id": str(client_id or "").strip(),
            "grant_type": "refresh_token",
            "refresh_token": wanted_refresh,
        },
    )
    scopes_raw = payload.get("scope")
    return TwitchOAuthTokens(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=max(0, int(payload.get("expires_in") or 0)),
        scopes=tuple(
            str(scope)
            for scope in (
                scopes_raw if isinstance(scopes_raw, list) else []
            )
            if str(scope).strip()
        ),
        token_type=str(payload.get("token_type") or "bearer"),
    )
