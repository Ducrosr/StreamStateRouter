from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from stream_state_router.ui.main_window import MainWindow


class ConfigEditLockTests(unittest.TestCase):
    def test_require_edit_mode_allows_mutation_when_enabled(self) -> None:
        window = SimpleNamespace(_config_edit_enabled=True)

        allowed = MainWindow._require_edit_mode(
            window,
            "Modifier la configuration",
        )

        self.assertTrue(allowed)

    def test_require_edit_mode_blocks_and_explains_when_locked(self) -> None:
        window = SimpleNamespace(_config_edit_enabled=False)

        with patch(
            "stream_state_router.ui.main_window.QMessageBox.information"
        ) as information:
            allowed = MainWindow._require_edit_mode(
                window,
                "Importer une configuration",
            )

        self.assertFalse(allowed)
        information.assert_called_once()
        message = information.call_args.args[2]
        self.assertIn("Importer une configuration", message)
        self.assertIn("Édition active", message)

    def test_apply_edit_mode_disables_mutating_surfaces_only(self) -> None:
        pages = {index: Mock() for index in (2, 3, 4, 5)}
        tabs = Mock()
        tabs.widget.side_effect = lambda index: pages.get(index)
        capture = Mock()
        repair = Mock()
        import_action = Mock()
        restore_action = Mock()
        repair_action = Mock()
        button = Mock()
        button.style.return_value = Mock()

        window = SimpleNamespace(
            _config_edit_enabled=False,
            edit_mode_button=button,
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

        MainWindow._apply_edit_mode(window)

        for page in pages.values():
            page.setEnabled.assert_called_once_with(False)
        capture.setEnabled.assert_called_once_with(False)
        repair.setEnabled.assert_called_once_with(False)
        import_action.setEnabled.assert_called_once_with(False)
        restore_action.setEnabled.assert_called_once_with(False)
        repair_action.setEnabled.assert_called_once_with(False)
        button.setText.assert_called_once_with(
            "Configuration verrouillée"
        )

    def test_collection_import_edit_requirement_is_conservative(self) -> None:
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
            "future_unknown_mode",
        ):
            self.assertTrue(
                MainWindow._collection_import_requires_edit_mode(mode),
                mode,
            )

    def test_toggle_edit_mode_persists_and_reapplies(self) -> None:
        settings = Mock()
        apply_mode = Mock()
        window = SimpleNamespace(
            _config_edit_enabled=True,
            _window_settings=settings,
            _apply_edit_mode=apply_mode,
        )

        MainWindow._toggle_edit_mode(window)

        self.assertFalse(window._config_edit_enabled)
        settings.setValue.assert_called_once_with(
            "main_window/config_edit_enabled",
            False,
        )
        settings.sync.assert_called_once_with()
        apply_mode.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
