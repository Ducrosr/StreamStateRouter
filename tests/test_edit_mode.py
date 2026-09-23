from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
        _apply_edit_mode_surfaces=Mock(),
        _test_status=status,
        _test_activity=activity,
    )


class _FakeScroll:
    def __init__(self, page) -> None:
        self._page = page

    def widget(self):
        return self._page


class _FakeTabs:
    def __init__(self, pages) -> None:
        self.pages = dict(pages)
        self.tooltips = {}

    def widget(self, index):
        return self.pages.get(index)

    def setTabToolTip(self, index, text) -> None:
        self.tooltips[index] = text


class EditModeTests(unittest.TestCase):
    def test_mutating_surfaces_are_read_only_outside_edit_mode(self) -> None:
        pages = {
            index: Mock()
            for index in (2, 3, 4, 5)
        }
        tabs = _FakeTabs(
            {
                index: _FakeScroll(page)
                for index, page in pages.items()
            }
        )
        capture = Mock()
        repair = Mock()
        import_action = Mock()
        restore_action = Mock()
        repair_action = Mock()
        window = SimpleNamespace(
            _edit_mode=False,
            tabs=tabs,
            rules_tab_index=2,
            profiles_tab_index=3,
            layouts_tab_index=4,
            settings_tab_index=5,
            capture_current_button=capture,
            repair_refs_button=repair,
            _import_config_action=import_action,
            _restore_backup_action=restore_action,
            _repair_refs_action=repair_action,
        )

        MainWindow._apply_edit_mode_surfaces(window)

        for page in pages.values():
            page.setEnabled.assert_called_once_with(False)
        capture.setEnabled.assert_called_once_with(False)
        repair.setEnabled.assert_called_once_with(False)
        import_action.setEnabled.assert_called_once_with(False)
        restore_action.setEnabled.assert_called_once_with(False)
        repair_action.setEnabled.assert_called_once_with(False)
        self.assertTrue(
            all("Mode édition" in value for value in tabs.tooltips.values())
        )

    def test_require_edit_mode_blocks_outside_edit_session(self) -> None:
        window = SimpleNamespace(_edit_mode=False)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.information"
        ) as information:
            allowed = MainWindow._require_edit_mode(
                window,
                "Importer une configuration",
            )

        self.assertFalse(allowed)
        information.assert_called_once()
        self.assertIn(
            "Importer une configuration",
            information.call_args.args[2],
        )

    def test_collection_completion_policy_allows_only_read_only_analysis(self) -> None:
        self.assertFalse(
            MainWindow._collection_import_requires_edit_mode(
                "guided_analysis"
            )
        )
        for mode in (
            "snapshot_profile",
            "logic_migration",
            "reference_repair",
            "guided_current_state_capture",
            "future_mode",
        ):
            self.assertTrue(
                MainWindow._collection_import_requires_edit_mode(mode),
                mode,
            )

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
