from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QMessageBox

from stream_state_router.services.config import config_revision
from stream_state_router.ui.main_window import MainWindow


class _FakeControl:
    def __init__(self) -> None:
        self.enabled = True
        self.tooltip = ""

    def setEnabled(self, value: bool) -> None:
        self.enabled = bool(value)

    def setToolTip(self, value: str) -> None:
        self.tooltip = str(value)


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, _duration: int = 0) -> None:
        self.messages.append(message)


class ManualRollbackTests(unittest.TestCase):
    def test_config_checkpoint_enables_undo_only_in_edit_mode(self) -> None:
        control = _FakeControl()
        previous = {"schema_version": 6, "value": 1}
        current = {"schema_version": 6, "value": 2}
        window = SimpleNamespace(
            config=copy.deepcopy(current),
            _edit_mode=False,
            _manual_undo=None,
            _manual_undo_controls=[control],
            _obs_connection_available=lambda: True,
        )
        window._refresh_manual_undo_controls = lambda: (
            MainWindow._refresh_manual_undo_controls(window)
        )

        MainWindow._set_config_undo_checkpoint(
            window,
            "Capture OBS",
            previous,
        )

        self.assertIsNotNone(window._manual_undo)
        self.assertFalse(control.enabled)
        self.assertIn("Mode édition", control.tooltip)

        window._edit_mode = True
        MainWindow._refresh_manual_undo_controls(window)

        self.assertTrue(control.enabled)
        self.assertIn("Capture OBS", control.tooltip)

    def test_config_rollback_restores_previous_snapshot(self) -> None:
        previous = {"schema_version": 6, "value": 1}
        current = {"schema_version": 6, "value": 2}
        status = _FakeStatusBar()
        activity = []
        window = SimpleNamespace(
            config=copy.deepcopy(current),
            _saved_revision=config_revision(previous),
            _edit_mode=True,
            _manual_undo={
                "kind": "config",
                "label": "Import OBS",
                "previous_config": copy.deepcopy(previous),
                "after_revision": config_revision(current),
            },
            _manual_undo_request_id="",
            _manual_undo_controls=[],
            _require_edit_mode=lambda _operation: True,
            _collect_settings=Mock(),
            _load_config_into_ui=Mock(),
            _refresh_config_revision_status=Mock(),
            _refresh_dashboard_summary=Mock(),
            _record_user_activity=activity.append,
            _refresh_manual_undo_controls=Mock(),
            statusBar=lambda: status,
        )
        window._clear_manual_undo_checkpoint = lambda: (
            MainWindow._clear_manual_undo_checkpoint(window)
        )

        MainWindow._undo_last_manual_operation(window)

        self.assertEqual(window.config, previous)
        self.assertIsNone(window._manual_undo)
        window._load_config_into_ui.assert_called_once_with()
        window._refresh_config_revision_status.assert_called_once_with(
            draft_dirty=False
        )
        self.assertEqual(
            activity[-1].message,
            "Dernière opération annulée",
        )

    def test_config_rollback_protects_later_edits(self) -> None:
        previous = {"schema_version": 6, "value": 1}
        after = {"schema_version": 6, "value": 2}
        later = {"schema_version": 6, "value": 3}
        window = SimpleNamespace(
            config=copy.deepcopy(later),
            _saved_revision=config_revision(previous),
            _edit_mode=True,
            _manual_undo={
                "kind": "config",
                "label": "Import OBS",
                "previous_config": copy.deepcopy(previous),
                "after_revision": config_revision(after),
            },
            _manual_undo_request_id="",
            _manual_undo_controls=[],
            _require_edit_mode=lambda _operation: True,
            _collect_settings=Mock(),
            _load_config_into_ui=Mock(),
            _refresh_config_revision_status=Mock(),
            _refresh_dashboard_summary=Mock(),
            _record_user_activity=Mock(),
            _refresh_manual_undo_controls=Mock(),
            statusBar=lambda: _FakeStatusBar(),
        )
        window._clear_manual_undo_checkpoint = lambda: (
            MainWindow._clear_manual_undo_checkpoint(window)
        )

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.question",
            return_value=QMessageBox.No,
        ):
            MainWindow._undo_last_manual_operation(window)

        self.assertEqual(window.config, later)
        self.assertIsNotNone(window._manual_undo)
        window._load_config_into_ui.assert_not_called()

    def test_layout_checkpoint_is_invalidated_by_later_layout_change(self) -> None:
        refresh = Mock()
        window = SimpleNamespace(
            _manual_undo={
                "kind": "layout_obs",
                "label": "Application du layout « FPS »",
            },
            _manual_undo_request_id="",
            _manual_undo_controls=[],
            _refresh_manual_undo_controls=refresh,
        )
        window._clear_manual_undo_checkpoint = lambda: (
            MainWindow._clear_manual_undo_checkpoint(window)
        )

        MainWindow._invalidate_layout_undo_checkpoint(window)

        self.assertIsNone(window._manual_undo)
        refresh.assert_called_once_with()

    def test_layout_checkpoint_requires_live_obs(self) -> None:
        control = _FakeControl()
        window = SimpleNamespace(
            _manual_undo={
                "kind": "layout_obs",
                "label": "Application du layout « FPS »",
            },
            _manual_undo_controls=[control],
            _edit_mode=False,
            _obs_connection_available=lambda: False,
        )

        MainWindow._refresh_manual_undo_controls(window)

        self.assertFalse(control.enabled)
        self.assertIn("Connexion OBS requise", control.tooltip)

        window._obs_connection_available = lambda: True
        MainWindow._refresh_manual_undo_controls(window)

        self.assertTrue(control.enabled)
        self.assertIn("FPS", control.tooltip)


if __name__ == "__main__":
    unittest.main()
