from __future__ import annotations

from types import SimpleNamespace
import unittest

from stream_state_router.ui.main_window import MainWindow


class _FakeStyle:
    def unpolish(self, _widget) -> None:
        pass

    def polish(self, _widget) -> None:
        pass


class _FakeButton:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = True
        self.object_name = ""
        self._style = _FakeStyle()

    def setText(self, value: str) -> None:
        self.text = value

    def setEnabled(self, value: bool) -> None:
        self.enabled = bool(value)

    def setObjectName(self, value: str) -> None:
        self.object_name = value

    def style(self):
        return self._style


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, _duration: int = 0) -> None:
        self.messages.append(message)


class _FakeService:
    def __init__(self, *, paused: bool) -> None:
        self.paused = bool(paused)
        self.pause_calls: list[bool] = []

    def pause(self, value: bool) -> None:
        value = bool(value)
        self.pause_calls.append(value)
        self.paused = value


def _window(service: _FakeService):
    status = _FakeStatusBar()
    activity = []
    return SimpleNamespace(
        _service=service,
        _edit_mode=False,
        _edit_mode_owned_pause=False,
        edit_mode_button=_FakeButton(),
        pause_button=_FakeButton(),
        statusBar=lambda: status,
        _record_user_activity=activity.append,
        _test_status=status,
        _test_activity=activity,
    )


class EditModeTests(unittest.TestCase):
    def test_edit_mode_owns_and_releases_pause_when_runtime_was_running(self) -> None:
        service = _FakeService(paused=False)
        window = _window(service)

        MainWindow._toggle_edit_mode(window)

        self.assertTrue(window._edit_mode)
        self.assertTrue(window._edit_mode_owned_pause)
        self.assertEqual(service.pause_calls, [True])
        self.assertFalse(window.pause_button.enabled)
        self.assertEqual(window.pause_button.text, "Suspendu (édition)")

        MainWindow._toggle_edit_mode(window)

        self.assertFalse(window._edit_mode)
        self.assertFalse(window._edit_mode_owned_pause)
        self.assertEqual(service.pause_calls, [True, False])
        self.assertTrue(window.pause_button.enabled)
        self.assertEqual(window.pause_button.text, "Suspendre")

    def test_edit_mode_preserves_preexisting_pause(self) -> None:
        service = _FakeService(paused=True)
        window = _window(service)

        MainWindow._toggle_edit_mode(window)
        MainWindow._toggle_edit_mode(window)

        self.assertEqual(service.pause_calls, [])
        self.assertTrue(service.paused)
        self.assertEqual(window.pause_button.text, "Reprendre")

    def test_edit_mode_pause_ownership_survives_runtime_replacement(self) -> None:
        first = _FakeService(paused=False)
        window = _window(first)

        MainWindow._toggle_edit_mode(window)
        replacement = _FakeService(paused=True)
        window._service = replacement

        MainWindow._toggle_edit_mode(window)

        self.assertEqual(first.pause_calls, [True])
        self.assertEqual(replacement.pause_calls, [False])
        self.assertFalse(replacement.paused)

    def test_preexisting_pause_remains_after_runtime_replacement(self) -> None:
        first = _FakeService(paused=True)
        window = _window(first)

        MainWindow._toggle_edit_mode(window)
        replacement = _FakeService(paused=True)
        window._service = replacement

        MainWindow._toggle_edit_mode(window)

        self.assertEqual(first.pause_calls, [])
        self.assertEqual(replacement.pause_calls, [])
        self.assertTrue(replacement.paused)


if __name__ == "__main__":
    unittest.main()
