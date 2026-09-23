from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import main as app_main


class MainCliTests(unittest.TestCase):
    def test_declarative_coverage_exits_before_single_instance_and_runtime(self):
        config = {
            "profiles": {
                "audio": {
                    "Default": {
                        "actions": [
                            {
                                "type": "input_mute",
                                "params": {"input": "Mic", "muted": True},
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }
        output = io.StringIO()

        with (
            patch.object(app_main, "load_config", return_value=config),
            patch.object(app_main, "SingleInstanceGuard") as guard,
            redirect_stdout(output),
        ):
            code = app_main.main(["--declarative-coverage-json"])

        self.assertEqual(code, 0)
        guard.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["summary"]["actions"]["total"], 1)
        self.assertEqual(
            payload["summary"]["actions"]["counts"]["declarative_executable"],
            1,
        )

    def test_human_coverage_cli_is_offline_and_readable(self):
        config = {
            "profiles": {
                "audio": {
                    "Default": {
                        "actions": [
                            {
                                "type": "input_volume_db",
                                "params": {"input": "Music", "volume_db": -8.0},
                            }
                        ]
                    }
                }
            },
            "layout_profiles": {},
        }
        output = io.StringIO()

        with (
            patch.object(app_main, "load_config", return_value=config),
            patch.object(app_main, "SingleInstanceGuard") as guard,
            redirect_stdout(output),
        ):
            code = app_main.main(["--declarative-coverage"])

        self.assertEqual(code, 0)
        guard.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("Couverture de migration déclarative", rendered)
        self.assertIn("exécutables 100.0%", rendered)
        self.assertNotIn("Actions non exécutables actuellement", rendered)

    def test_gui_exception_does_not_claim_clean_shutdown(self):
        config = {}
        marker = Mock()
        marker.finalized = False
        guard = Mock()
        guard.already_running = False

        with (
            patch.object(app_main, "load_config", return_value=config),
            patch.object(app_main, "RuntimeMarker", return_value=marker),
            patch.object(app_main, "SingleInstanceGuard", return_value=guard),
            patch.object(
                app_main,
                "run_gui",
                side_effect=RuntimeError("gui crashed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "gui crashed"):
                app_main.main([])

        marker.start.assert_called_once_with()
        marker.clean_shutdown.assert_not_called()
        guard.close.assert_called_once_with()

    def test_gui_return_without_runtime_finalization_stays_dirty(self):
        config = {}
        marker = Mock()
        marker.finalized = False
        guard = Mock()
        guard.already_running = False

        with (
            patch.object(app_main, "load_config", return_value=config),
            patch.object(app_main, "RuntimeMarker", return_value=marker),
            patch.object(app_main, "SingleInstanceGuard", return_value=guard),
            patch.object(app_main, "run_gui", return_value=0),
        ):
            code = app_main.main([])

        self.assertEqual(code, 0)
        marker.clean_shutdown.assert_not_called()
        guard.close.assert_called_once_with()

    def test_headless_normal_return_finalizes_marker(self):
        config = {}
        marker = Mock()
        marker.finalized = False
        guard = Mock()
        guard.already_running = False

        with (
            patch.object(app_main, "load_config", return_value=config),
            patch.object(app_main, "RuntimeMarker", return_value=marker),
            patch.object(app_main, "SingleInstanceGuard", return_value=guard),
            patch.object(app_main, "run_headless", return_value=0),
        ):
            code = app_main.main(["--headless"])

        self.assertEqual(code, 0)
        marker.start.assert_called_once_with()
        marker.clean_shutdown.assert_called_once_with()
        guard.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
