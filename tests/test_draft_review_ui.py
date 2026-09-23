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


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.tooltip = ""

    def setText(self, value: str) -> None:
        self.text = value

    def setToolTip(self, value: str) -> None:
        self.tooltip = value


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = True

    def setEnabled(self, value: bool) -> None:
        self.enabled = bool(value)


def _draft_window(saved: dict, draft: dict):
    status = _FakeStatusBar()
    activity = []
    window = SimpleNamespace(
        _last_saved_config=copy.deepcopy(saved),
        config=copy.deepcopy(draft),
        _draft_dirty=True,
        _collect_settings=Mock(),
        _load_config_into_ui=Mock(),
        _refresh_config_revision_status=Mock(),
        _refresh_dashboard_summary=Mock(),
        _record_user_activity=activity.append,
        statusBar=lambda: status,
        _test_status=status,
        _test_activity=activity,
    )
    return window


class DraftReviewUITests(unittest.TestCase):
    def test_discard_draft_restores_last_saved_config(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)
        draft["router"]["debounce_ms"] = 777
        window = _draft_window(saved, draft)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ):
            MainWindow._discard_draft(window)

        self.assertEqual(window.config, saved)
        self.assertFalse(window._draft_dirty)
        window._load_config_into_ui.assert_called_once_with()
        window._refresh_dashboard_summary.assert_called_once_with()
        window._refresh_config_revision_status.assert_called_once_with(
            draft_dirty=False
        )
        self.assertTrue(window._test_activity)
        self.assertEqual(
            window._test_activity[-1].message,
            "Brouillon abandonné",
        )

    def test_discard_draft_cancel_preserves_changes(self) -> None:
        saved = _config()
        draft = copy.deepcopy(saved)
        draft["router"]["debounce_ms"] = 777
        window = _draft_window(saved, draft)
        before = copy.deepcopy(window.config)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.question",
            return_value=QMessageBox.No,
        ):
            MainWindow._discard_draft(window)

        self.assertEqual(window.config, before)
        self.assertTrue(window._draft_dirty)
        window._load_config_into_ui.assert_not_called()
        window._refresh_dashboard_summary.assert_not_called()

    def test_discard_noop_marks_false_dirty_state_clean(self) -> None:
        saved = _config()
        window = _draft_window(saved, copy.deepcopy(saved))

        MainWindow._discard_draft(window)

        self.assertFalse(window._draft_dirty)
        window._refresh_config_revision_status.assert_called_once_with(
            draft_dirty=False
        )
        window._load_config_into_ui.assert_not_called()

    def test_revision_status_controls_draft_buttons(self) -> None:
        window = SimpleNamespace(
            _draft_dirty=False,
            _saved_revision="saved",
            _applied_revision="saved",
            _expert_mode=False,
            unsaved=_FakeLabel(),
            review_draft_button=_FakeButton(),
            discard_draft_button=_FakeButton(),
        )

        MainWindow._refresh_config_revision_status(
            window,
            draft_dirty=False,
        )
        self.assertFalse(window.review_draft_button.enabled)
        self.assertFalse(window.discard_draft_button.enabled)
        self.assertEqual(window.unsaved.text, "Configuration à jour")

        MainWindow._refresh_config_revision_status(
            window,
            draft_dirty=True,
        )
        self.assertTrue(window.review_draft_button.enabled)
        self.assertTrue(window.discard_draft_button.enabled)
        self.assertEqual(
            window.unsaved.text,
            "Modifications non enregistrées",
        )


if __name__ == "__main__":
    unittest.main()
