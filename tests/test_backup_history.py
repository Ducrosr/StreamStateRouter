from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QMessageBox

from stream_state_router.ui.main_window import MainWindow


def _config() -> dict:
    return json.loads(
        Path("config/default.json").read_text(encoding="utf-8")
    )


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, _duration: int = 0) -> None:
        self.messages.append(message)


def _window(saved: dict, current: dict):
    activity = []
    status = _FakeStatusBar()
    return SimpleNamespace(
        _last_saved_config=copy.deepcopy(saved),
        config=copy.deepcopy(current),
        _draft_dirty=True,
        _load_config_into_ui=Mock(),
        _refresh_config_revision_status=Mock(),
        _refresh_dashboard_summary=Mock(),
        _record_user_activity=activity.append,
        statusBar=lambda: status,
        _test_activity=activity,
        _test_status=status,
    )


class BackupHistoryTests(unittest.TestCase):
    def test_backup_load_refuses_to_replace_unsaved_draft_without_confirmation(self) -> None:
        saved = _config()
        current = copy.deepcopy(saved)
        current["router"]["debounce_ms"] = 777
        incoming = copy.deepcopy(saved)
        incoming["router"]["debounce_ms"] = 250
        window = _window(saved, current)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.question",
            return_value=QMessageBox.No,
        ):
            loaded = MainWindow._load_backup_as_draft(
                window,
                incoming,
                source_name="config-backup.json",
            )

        self.assertFalse(loaded)
        self.assertEqual(window.config, current)
        window._load_config_into_ui.assert_not_called()

    def test_backup_load_replaces_confirmed_draft_and_keeps_diff_dirty(self) -> None:
        saved = _config()
        current = copy.deepcopy(saved)
        current["router"]["debounce_ms"] = 777
        incoming = copy.deepcopy(saved)
        incoming["router"]["debounce_ms"] = 250
        window = _window(saved, current)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ):
            loaded = MainWindow._load_backup_as_draft(
                window,
                incoming,
                source_name="config-backup.json",
            )

        self.assertTrue(loaded)
        self.assertEqual(window.config, incoming)
        self.assertTrue(window._draft_dirty)
        window._load_config_into_ui.assert_called_once_with()
        window._refresh_config_revision_status.assert_called_once_with(
            draft_dirty=True
        )
        window._refresh_dashboard_summary.assert_called_once_with()
        self.assertEqual(
            window._test_activity[-1].message,
            "Sauvegarde chargée comme brouillon",
        )

    def test_identical_backup_is_loaded_clean(self) -> None:
        saved = _config()
        window = _window(saved, copy.deepcopy(saved))

        loaded = MainWindow._load_backup_as_draft(
            window,
            copy.deepcopy(saved),
            source_name="same.json",
        )

        self.assertTrue(loaded)
        self.assertFalse(window._draft_dirty)
        window._refresh_config_revision_status.assert_called_once_with(
            draft_dirty=False
        )
        self.assertIn(
            "identique",
            window._test_status.messages[-1],
        )


if __name__ == "__main__":
    unittest.main()
