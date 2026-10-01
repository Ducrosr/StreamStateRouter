from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..platforms import (
    DEFAULT_TWITCH_SCOPES,
    TwitchDeviceAuthorization,
    TwitchOAuthTokens,
    poll_device_tokens,
)


class _OAuthBridge(QObject):
    completed = Signal(object)
    failed = Signal(str)
    pending = Signal()


class TwitchDeviceOAuthDialog(QDialog):
    def __init__(
        self,
        client_id: str,
        authorization: TwitchDeviceAuthorization,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Connexion Twitch")
        self.resize(560, 300)
        self.client_id = str(client_id)
        self.authorization = authorization
        self.tokens: TwitchOAuthTokens | None = None
        self._deadline = (
            time.monotonic() + authorization.expires_in
        )
        self._polling = False
        self._closed = False

        root = QVBoxLayout(self)
        title = QLabel("Autoriser StreamStateRouter sur Twitch")
        title.setStyleSheet(
            "font-size: 15pt; font-weight: 700;"
        )
        root.addWidget(title)

        explanation = QLabel(
            "La page Twitch doit afficher les permissions demandées. "
            "Une fois l’autorisation validée dans le navigateur, "
            "SSR récupérera automatiquement les tokens."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("Muted")
        root.addWidget(explanation)

        code_caption = QLabel("Code à vérifier")
        code_caption.setObjectName("Muted")
        root.addWidget(code_caption)

        self.code = QLabel(authorization.user_code)
        self.code.setStyleSheet(
            "font-size: 24pt; font-weight: 800;"
        )
        root.addWidget(self.code)

        self.url = QLabel(authorization.verification_uri)
        self.url.setTextInteractionFlags(
            self.url.textInteractionFlags()
            | self.url.textInteractionFlags().TextSelectableByMouse
        )
        self.url.setWordWrap(True)
        root.addWidget(self.url)

        self.status = QLabel("En attente de l’autorisation Twitch…")
        self.status.setObjectName("Muted")
        root.addWidget(self.status)

        scopes = QLabel(
            "Permissions : " + ", ".join(DEFAULT_TWITCH_SCOPES)
        )
        scopes.setObjectName("Muted")
        scopes.setWordWrap(True)
        root.addWidget(scopes)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        root.addLayout(row)

        self._bridge = _OAuthBridge(self)
        self._bridge.completed.connect(self._on_completed)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.pending.connect(self._on_pending)

        self._timer = QTimer(self)
        self._timer.setInterval(
            max(1000, authorization.interval * 1000)
        )
        self._timer.timeout.connect(self._poll_async)
        self._timer.start()
        QTimer.singleShot(100, self._poll_async)

    def _poll_async(self) -> None:
        if self._polling or self._closed:
            return
        if time.monotonic() >= self._deadline:
            self.status.setText(
                "Le code Twitch a expiré. Relancez la connexion."
            )
            self._timer.stop()
            return
        self._polling = True

        def worker() -> None:
            try:
                tokens = poll_device_tokens(
                    self.client_id,
                    self.authorization,
                )
            except Exception as exc:
                self._bridge.failed.emit(str(exc))
                return
            if tokens is None:
                self._bridge.pending.emit()
                return
            self._bridge.completed.emit(tokens)

        threading.Thread(
            target=worker,
            name="SSR-TwitchOAuthPoll",
            daemon=True,
        ).start()

    def _on_pending(self) -> None:
        self._polling = False
        self.status.setText("En attente de l’autorisation Twitch…")

    def _on_failed(self, message: str) -> None:
        self._polling = False
        self._timer.stop()
        self.status.setText(f"Échec OAuth : {message}")

    def _on_completed(self, tokens: TwitchOAuthTokens) -> None:
        self._polling = False
        self._timer.stop()
        self.tokens = tokens
        self.status.setText("✓ Twitch autorisé.")
        QTimer.singleShot(150, self.accept)

    def done(self, result: int) -> None:
        self._closed = True
        self._timer.stop()
        super().done(result)
