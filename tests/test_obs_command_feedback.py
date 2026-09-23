from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stream_state_router.ui.main_window import MainWindow


class _FakeButton:
    def __init__(self, text: str) -> None:
        self._text = text
        self.enabled = True

    def text(self) -> str:
        return self._text

    def setText(self, value: str) -> None:
        self._text = str(value)

    def setEnabled(self, value: bool) -> None:
        self.enabled = bool(value)


class ObsCommandFeedbackTests(unittest.TestCase):
    def _window(self):
        return SimpleNamespace(
            _pending_obs_controls={},
            _refresh_obs_connected_controls=Mock(),
            sender=lambda: None,
        )

    def test_track_disables_control_and_sets_busy_text(self) -> None:
        window = self._window()
        button = _FakeButton("Réappliquer SSR")

        MainWindow._track_obs_request(
            window,
            "abc",
            busy_text="Réapplication…",
            control=button,
        )

        self.assertFalse(button.enabled)
        self.assertEqual(button.text(), "Réapplication…")
        self.assertIn("abc", window._pending_obs_controls)

    def test_success_feedback_restores_control_after_short_ack(self) -> None:
        window = self._window()
        button = _FakeButton("Appliquer maintenant")
        MainWindow._track_obs_request(
            window,
            "abc",
            busy_text="Application…",
            control=button,
        )

        callbacks = []
        with patch(
            "stream_state_router.ui.main_window.QTimer.singleShot",
            side_effect=lambda _ms, callback: callbacks.append(callback),
        ):
            MainWindow._finish_obs_request_feedback(
                window,
                "abc",
                success=True,
            )

        self.assertEqual(button.text(), "✓ Appliquer maintenant")
        self.assertFalse(button.enabled)
        self.assertEqual(len(callbacks), 1)

        callbacks[0]()

        self.assertEqual(button.text(), "Appliquer maintenant")
        self.assertTrue(button.enabled)
        window._refresh_obs_connected_controls.assert_called_once_with()

    def test_failure_feedback_uses_failure_marker(self) -> None:
        window = self._window()
        button = _FakeButton("Capturer l’état actuel")
        MainWindow._track_obs_request(
            window,
            "abc",
            busy_text="Capture…",
            control=button,
        )

        with patch(
            "stream_state_router.ui.main_window.QTimer.singleShot",
            side_effect=lambda _ms, _callback: None,
        ):
            MainWindow._finish_obs_request_feedback(
                window,
                "abc",
                success=False,
            )

        self.assertEqual(button.text(), "✕ Capturer l’état actuel")
        self.assertNotIn("abc", window._pending_obs_controls)


if __name__ == "__main__":
    unittest.main()
