from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from stream_state_router.ui.main_window import MainWindow


class ManualOverrideAPITests(unittest.TestCase):
    def test_override_action_forwards_release_mode_and_returns_status(self) -> None:
        service = Mock()
        service.manual_override_status.return_value = {
            "active": True,
            "release_mode": "stream_end",
            "stream_seen_active": False,
        }
        window = SimpleNamespace(
            _service=service,
            _dispatcher=object(),
        )

        result = MainWindow._api_action(
            window,
            "override",
            {
                "state": {
                    "Game": "Manual",
                    "OverlayProfile": "Vanilla",
                    "CaptureProfile": "Default",
                    "AudioProfile": "Default",
                    "LayoutProfile": "Vanilla",
                },
                "release_mode": "stream_end",
            },
        )

        service.set_manual_override.assert_called_once()
        kwargs = service.set_manual_override.call_args.kwargs
        self.assertEqual(kwargs["release_mode"], "stream_end")
        self.assertIsNone(kwargs["duration_seconds"])
        self.assertEqual(
            result["manual_override"]["release_mode"],
            "stream_end",
        )
        self.assertEqual(result["state"]["Game"], "Manual")

    def test_override_action_keeps_duration_backward_compatible(self) -> None:
        service = Mock()
        service.manual_override_status.return_value = {
            "active": True,
            "release_mode": "duration",
            "remaining_seconds": 60,
        }
        window = SimpleNamespace(
            _service=service,
            _dispatcher=object(),
        )

        MainWindow._api_action(
            window,
            "override",
            {
                "state": {"Game": "Manual"},
                "duration_seconds": 60,
            },
        )

        kwargs = service.set_manual_override.call_args.kwargs
        self.assertEqual(kwargs["release_mode"], "manual")
        self.assertEqual(kwargs["duration_seconds"], 60.0)

    def test_auto_action_clears_override_and_returns_status(self) -> None:
        service = Mock()
        service.paused = False
        service.manual_override_status.return_value = {
            "active": False,
        }
        window = SimpleNamespace(
            _service=service,
            _dispatcher=object(),
        )

        result = MainWindow._api_action(
            window,
            "auto",
            {},
        )

        service.pause.assert_called_once_with(False)
        service.clear_manual_override.assert_called_once_with()
        self.assertFalse(result["paused"])
        self.assertFalse(result["manual_override"]["active"])

    def test_manual_clear_respects_safe_live_refusal(self) -> None:
        service = Mock()
        window = SimpleNamespace(
            _service=service,
            _safe_live_confirm=Mock(return_value=False),
            _log=Mock(),
        )

        MainWindow._clear_override(window)

        service.clear_manual_override.assert_not_called()
        window._log.assert_not_called()

    def test_force_reapply_respects_safe_live_refusal(self) -> None:
        service = Mock()
        window = SimpleNamespace(
            _service=service,
            _safe_live_confirm=Mock(return_value=False),
        )

        MainWindow._force_reapply(window)

        service.request_force_reapply.assert_not_called()

    def test_override_action_requires_runtime(self) -> None:
        window = SimpleNamespace(
            _service=None,
            _dispatcher=None,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Runtime non disponible",
        ):
            MainWindow._api_action(
                window,
                "override",
                {"state": {"Game": "Manual"}},
            )


if __name__ == "__main__":
    unittest.main()
