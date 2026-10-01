from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import time
from typing import Mapping

from PySide6.QtCore import QObject, QTimer, Signal

from ..events import EventBus
from ..services.secrets import (
    SecretStoreError,
    SecretStoreUnavailable,
)
from .twitch import (
    build_default_subscriptions,
    required_scopes,
)
from .twitch_credentials import (
    TwitchCredentialBundle,
    TwitchCredentialStore,
)
from .twitch_qt import TwitchQtEventSubService
from .twitch_session import (
    TwitchDeviceAuthorization,
    TwitchHelixClient,
    TwitchOAuthDeviceClient,
    TwitchOAuthTokens,
    TwitchTokenValidation,
)


@dataclass(frozen=True, slots=True)
class TwitchPlatformConfig:
    enabled: bool = False
    client_id: str = ""
    broadcaster_user_id: str = ""
    moderator_user_id: str = ""


def build_twitch_platform_config(
    config: Mapping[str, object],
) -> TwitchPlatformConfig:
    platforms = config.get("platforms")
    if not isinstance(platforms, Mapping):
        return TwitchPlatformConfig()
    raw = platforms.get("twitch")
    if not isinstance(raw, Mapping):
        return TwitchPlatformConfig()
    return TwitchPlatformConfig(
        enabled=bool(raw.get("enabled", False)),
        client_id=str(raw.get("client_id") or "").strip(),
        broadcaster_user_id=str(
            raw.get("broadcaster_user_id") or ""
        ).strip(),
        moderator_user_id=str(
            raw.get("moderator_user_id") or ""
        ).strip(),
    )


def twitch_default_scopes() -> tuple[str, ...]:
    return required_scopes(
        build_default_subscriptions(
            broadcaster_user_id="self",
            user_id="self",
            moderator_user_id="self",
        )
    )


class TwitchPlatformController(QObject):
    status_changed = Signal(object)
    error = Signal(str)
    authorization_required = Signal(str, str, int)
    account_ready = Signal(object)

    _authorization_started = Signal(object, str)
    _poll_completed = Signal(object, str)
    _saved_prepared = Signal(object, str)

    def __init__(
        self,
        event_bus: EventBus,
        *,
        credential_store: TwitchCredentialStore | None = None,
        oauth: TwitchOAuthDeviceClient | None = None,
        helix: TwitchHelixClient | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.event_bus = event_bus
        self.helix = helix or TwitchHelixClient()
        self.oauth = oauth or TwitchOAuthDeviceClient()
        self._credential_store = credential_store
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="SSR-Twitch-Auth",
        )
        self._service: TwitchQtEventSubService | None = None
        self._config = TwitchPlatformConfig()
        self._bundle: TwitchCredentialBundle | None = None
        self._pending_auth: TwitchDeviceAuthorization | None = None
        self._pending_client_id = ""
        self._poll_in_flight = False
        self._authorization_started_at = 0.0

        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(False)
        self._poll_timer.timeout.connect(self._poll_authorization)

        self._authorization_started.connect(
            self._on_authorization_started
        )
        self._poll_completed.connect(
            self._on_poll_completed
        )
        self._saved_prepared.connect(
            self._on_saved_prepared
        )

    def configure(
        self,
        config: TwitchPlatformConfig,
    ) -> None:
        self._config = config

    def start_saved(self) -> None:
        if not self._config.enabled:
            self._emit_status("disabled")
            return
        if not self._config.client_id:
            self._emit_status("not_configured")
            return
        try:
            store = self._store()
            bundle = store.load()
        except Exception as exc:
            self.error.emit(str(exc))
            self._emit_status("credential_error")
            return
        if bundle is None:
            self._emit_status("authorization_required")
            return
        if bundle.client_id != self._config.client_id:
            self._emit_status(
                "authorization_required",
                reason="client_id_changed",
            )
            return
        self._emit_status("validating")
        future = self._executor.submit(
            self._prepare_saved_bundle,
            bundle,
        )
        future.add_done_callback(self._saved_prepare_done)

    def begin_authorization(
        self,
        *,
        client_id: str | None = None,
    ) -> None:
        client = str(
            client_id
            if client_id is not None
            else self._config.client_id
        ).strip()
        if not client:
            self.error.emit("Client ID Twitch requis")
            return
        self.stop_service()
        self._cancel_authorization()
        self._pending_client_id = client
        self._emit_status("authorizing")
        future = self._executor.submit(
            self.oauth.start,
            client_id=client,
            scopes=twitch_default_scopes(),
        )
        future.add_done_callback(
            self._authorization_start_done
        )

    def disconnect_account(self) -> None:
        self.stop_service()
        self._cancel_authorization()
        self._bundle = None
        try:
            self._store().delete()
        except Exception as exc:
            self.error.emit(str(exc))
        self._emit_status("authorization_required")

    def stop_service(self) -> None:
        service = self._service
        self._service = None
        if service is not None:
            service.close()

    def close(self) -> None:
        self.stop_service()
        self._cancel_authorization()
        self._executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

    def _store(self) -> TwitchCredentialStore:
        if self._credential_store is None:
            self._credential_store = TwitchCredentialStore()
        return self._credential_store

    def _prepare_saved_bundle(
        self,
        bundle: TwitchCredentialBundle,
    ) -> TwitchCredentialBundle:
        try:
            validation = self.helix.validate_token(
                bundle.access_token
            )
            return bundle.with_validation(validation)
        except Exception as first_error:
            if "401" not in str(first_error):
                raise
        refreshed = self.oauth.refresh(
            client_id=bundle.client_id,
            refresh_token=bundle.refresh_token,
        )
        rotated = bundle.rotate(refreshed)
        validation = self.helix.validate_token(
            rotated.access_token
        )
        rotated = rotated.with_validation(validation)
        self._store().save(rotated)
        return rotated

    def _saved_prepare_done(self, future: Future) -> None:
        try:
            bundle = future.result()
        except Exception as exc:
            self._saved_prepared.emit(None, str(exc))
        else:
            self._saved_prepared.emit(bundle, "")

    def _on_saved_prepared(
        self,
        bundle: TwitchCredentialBundle | None,
        error: str,
    ) -> None:
        if error or bundle is None:
            self.error.emit(
                error or "Session Twitch invalide"
            )
            self._emit_status("authorization_required")
            return
        try:
            self._store().save(bundle)
        except Exception as exc:
            self.error.emit(str(exc))
            self._emit_status("credential_error")
            return
        self._bundle = bundle
        self.account_ready.emit(bundle)
        self._start_service(bundle)

    def _authorization_start_done(
        self,
        future: Future,
    ) -> None:
        try:
            authorization = future.result()
        except Exception as exc:
            self._authorization_started.emit(None, str(exc))
        else:
            self._authorization_started.emit(
                authorization,
                "",
            )

    def _on_authorization_started(
        self,
        authorization: TwitchDeviceAuthorization | None,
        error: str,
    ) -> None:
        if error or authorization is None:
            self.error.emit(
                error or "Autorisation Twitch impossible"
            )
            self._emit_status("authorization_required")
            return
        self._pending_auth = authorization
        self._authorization_started_at = time.monotonic()
        self.authorization_required.emit(
            authorization.verification_uri,
            authorization.user_code,
            authorization.expires_in,
        )
        self._poll_timer.setInterval(
            max(1000, authorization.interval * 1000)
        )
        self._poll_timer.start()
        self._emit_status(
            "waiting_for_user",
            user_code=authorization.user_code,
            verification_uri=authorization.verification_uri,
        )

    def _poll_authorization(self) -> None:
        authorization = self._pending_auth
        if (
            authorization is None
            or self._poll_in_flight
            or not self._pending_client_id
        ):
            return
        elapsed = time.monotonic() - self._authorization_started_at
        if elapsed >= authorization.expires_in:
            self._cancel_authorization()
            self.error.emit("Code Twitch expiré")
            self._emit_status("authorization_required")
            return
        self._poll_in_flight = True
        future = self._executor.submit(
            self.oauth.exchange,
            client_id=self._pending_client_id,
            device_code=authorization.device_code,
            scopes=authorization.scopes,
        )
        future.add_done_callback(self._poll_done)

    def _poll_done(self, future: Future) -> None:
        try:
            tokens = future.result()
        except Exception as exc:
            self._poll_completed.emit(None, str(exc))
        else:
            self._poll_completed.emit(tokens, "")

    def _on_poll_completed(
        self,
        tokens: TwitchOAuthTokens | None,
        error: str,
    ) -> None:
        self._poll_in_flight = False
        if error:
            self._cancel_authorization()
            self.error.emit(error)
            self._emit_status("authorization_required")
            return
        if tokens is None:
            return

        client_id = self._pending_client_id
        self._cancel_authorization()
        bundle = TwitchCredentialBundle(
            client_id=client_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            scopes=tuple(tokens.scopes),
            expires_in=tokens.expires_in,
        )
        self._emit_status("validating")
        future = self._executor.submit(
            self._validate_new_bundle,
            bundle,
        )
        future.add_done_callback(self._saved_prepare_done)

    def _validate_new_bundle(
        self,
        bundle: TwitchCredentialBundle,
    ) -> TwitchCredentialBundle:
        validation = self.helix.validate_token(
            bundle.access_token
        )
        if (
            validation.client_id
            and validation.client_id != bundle.client_id
        ):
            raise RuntimeError(
                "Le token Twitch appartient à un autre Client ID"
            )
        missing = sorted(
            set(twitch_default_scopes())
            - set(validation.scopes),
            key=str.casefold,
        )
        if missing:
            raise RuntimeError(
                "Scopes Twitch manquants : "
                + ", ".join(missing)
            )
        return bundle.with_validation(validation)

    def _start_service(
        self,
        bundle: TwitchCredentialBundle,
    ) -> None:
        self.stop_service()
        service = TwitchQtEventSubService(
            self.event_bus,
            client_id=bundle.client_id,
            access_token_provider=lambda: (
                self._bundle.access_token
                if self._bundle is not None
                else ""
            ),
            broadcaster_user_id=(
                self._config.broadcaster_user_id
                or bundle.user_id
            ),
            user_id=bundle.user_id,
            moderator_user_id=(
                self._config.moderator_user_id
                or self._config.broadcaster_user_id
                or bundle.user_id
            ),
            helix=self.helix,
            parent=self,
        )
        service.status_changed.connect(
            self._relay_service_status
        )
        service.error.connect(self.error.emit)
        service.subscription_result.connect(
            self._relay_subscription_result
        )
        self._service = service
        service.start()

    def _relay_service_status(self, status: object) -> None:
        if isinstance(status, Mapping):
            payload = dict(status)
            payload["account_login"] = (
                self._bundle.login
                if self._bundle is not None
                else ""
            )
            self.status_changed.emit(payload)

    def _relay_subscription_result(
        self,
        subscription: str,
        success: bool,
        error: str,
    ) -> None:
        if success:
            return
        self.error.emit(
            f"EventSub {subscription} : {error}"
        )

    def _cancel_authorization(self) -> None:
        self._poll_timer.stop()
        self._pending_auth = None
        self._pending_client_id = ""
        self._poll_in_flight = False
        self._authorization_started_at = 0.0

    def _emit_status(
        self,
        state: str,
        **extra: object,
    ) -> None:
        payload: dict[str, object] = {
            "state": state,
            "enabled": self._config.enabled,
            "client_id": self._config.client_id,
            "account_login": (
                self._bundle.login
                if self._bundle is not None
                else ""
            ),
        }
        payload.update(extra)
        self.status_changed.emit(payload)
