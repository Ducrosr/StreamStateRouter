from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from stream_state_router.ui.main_window import MainWindow
from stream_state_router.ui.presentation import UserActivityEntry


class UserActivityUITests(unittest.TestCase):
    def test_repeated_activity_is_coalesced_across_interleaved_events(self) -> None:
        window = SimpleNamespace(
            _user_activity_history=[],
            _refresh_user_activity=Mock(),
        )
        first = UserActivityEntry("Bad", "Application incomplète", "Échec : Jeu")
        second = UserActivityEntry("Muted", "Configuration sélectionnée", "Overwatch")

        with (
            patch(
                "stream_state_router.ui.main_window.time.monotonic",
                side_effect=[10.0, 11.0, 12.0, 13.0],
            ),
            patch(
                "stream_state_router.ui.main_window.time.strftime",
                side_effect=["10:00:00", "10:00:01", "10:00:02", "10:00:03"],
            ),
        ):
            MainWindow._record_user_activity(window, first)
            MainWindow._record_user_activity(window, second)
            MainWindow._record_user_activity(window, first)
            MainWindow._record_user_activity(window, second)

        self.assertEqual(len(window._user_activity_history), 2)
        self.assertEqual(window._user_activity_history[0][1], first)
        self.assertEqual(window._user_activity_history[0][2], 2)
        self.assertEqual(window._user_activity_history[1][1], second)
        self.assertEqual(window._user_activity_history[1][2], 2)

    def test_repeat_window_expires_after_five_seconds(self) -> None:
        window = SimpleNamespace(
            _user_activity_history=[],
            _refresh_user_activity=Mock(),
        )
        entry = UserActivityEntry("Bad", "Application incomplète", "Échec : Capture")

        with (
            patch(
                "stream_state_router.ui.main_window.time.monotonic",
                side_effect=[10.0, 16.0],
            ),
            patch(
                "stream_state_router.ui.main_window.time.strftime",
                side_effect=["10:00:00", "10:00:06"],
            ),
        ):
            MainWindow._record_user_activity(window, entry)
            MainWindow._record_user_activity(window, entry)

        self.assertEqual(len(window._user_activity_history), 2)
        self.assertEqual(
            [row[2] for row in window._user_activity_history],
            [1, 1],
        )


if __name__ == "__main__":
    unittest.main()
