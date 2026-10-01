from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol

from ..services.secrets import WindowsDPAPISecretStore
from .twitch_session import TwitchOAuthTokens, TwitchTokenValidation


class SecretStoreProtocol(Protocol):
    def set(self, name: str, value: str | bytes) -> None: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TwitchCredentialBundle:
    client_id: str
    access_token: str
    refresh_token: str
    user_id: str = ""
    login: str = ""
    scopes: tuple[str, ...] = ()
    expires_in: int = 0

    def with_validation(
        self,
        validation: TwitchTokenValidation,
    ) -> "TwitchCredentialBundle":
        return TwitchCredentialBundle(
            client_id=self.client_id,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            user_id=validation.user_id,
            login=validation.login,
            scopes=tuple(validation.scopes),
            expires_in=validation.expires_in,
        )

    def rotate(
        self,
        tokens: TwitchOAuthTokens,
    ) -> "TwitchCredentialBundle":
        return TwitchCredentialBundle(
            client_id=self.client_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            user_id=self.user_id,
            login=self.login,
            scopes=tuple(tokens.scopes) or self.scopes,
            expires_in=tokens.expires_in,
        )


class TwitchCredentialStore:
    SECRET_NAME = "twitch.oauth"

    def __init__(
        self,
        secret_store: SecretStoreProtocol | None = None,
    ):
        self._store = (
            secret_store
            if secret_store is not None
            else WindowsDPAPISecretStore()
        )

    def save(
        self,
        bundle: TwitchCredentialBundle,
    ) -> None:
        if not bundle.client_id.strip():
            raise ValueError("client_id requis")
        if not bundle.access_token.strip():
            raise ValueError("access_token requis")
        if not bundle.refresh_token.strip():
            raise ValueError("refresh_token requis")
        payload = {
            "schema_version": 1,
            "client_id": bundle.client_id,
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "user_id": bundle.user_id,
            "login": bundle.login,
            "scopes": list(bundle.scopes),
            "expires_in": int(bundle.expires_in),
        }
        self._store.set(
            self.SECRET_NAME,
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def load(self) -> TwitchCredentialBundle | None:
        raw = self._store.get(self.SECRET_NAME)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Secret Twitch illisible"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Secret Twitch invalide")
        if int(payload.get("schema_version") or 0) != 1:
            raise RuntimeError(
                "Version de secret Twitch non supportée"
            )
        bundle = TwitchCredentialBundle(
            client_id=str(payload.get("client_id") or ""),
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            user_id=str(payload.get("user_id") or ""),
            login=str(payload.get("login") or ""),
            scopes=tuple(
                str(item)
                for item in payload.get("scopes", [])
                if str(item)
            )
            if isinstance(payload.get("scopes"), list)
            else (),
            expires_in=int(payload.get("expires_in") or 0),
        )
        if (
            not bundle.client_id
            or not bundle.access_token
            or not bundle.refresh_token
        ):
            raise RuntimeError(
                "Secret Twitch incomplet"
            )
        return bundle

    def delete(self) -> None:
        self._store.delete(self.SECRET_NAME)
