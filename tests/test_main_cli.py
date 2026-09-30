from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

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

    def test_system_check_json_exits_before_single_instance(self):
        config = {
            "obs": {"enabled": False},
            "profiles": {},
            "layout_profiles": {},
        }
        report = SimpleNamespace(
            ok=True,
            as_mapping=lambda: {
                "status": "ready",
                "ok": True,
                "config": {"valid": True, "errors": []},
                "capabilities": {
                    "status": "ready",
                    "summary": "ready",
                    "items": [],
                    "findings": [],
                },
            },
        )
        output = io.StringIO()

        with (
            patch.object(
                app_main,
                "load_config_unvalidated",
                return_value=config,
            ),
            patch(
                "stream_state_router.services.system_check.run_system_check",
                return_value=report,
            ) as system_check,
            patch.object(app_main, "SingleInstanceGuard") as guard,
            redirect_stdout(output),
        ):
            code = app_main.main(["--system-check-json"])

        self.assertEqual(code, 0)
        system_check.assert_called_once_with(config)
        guard.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ready")

    def test_system_check_can_report_semantically_invalid_config(self):
        config = {
            "schema_version": 999,
            "router": {},
            "rules": [],
            "obs": {"enabled": False},
            "api": {},
            "profiles": {},
            "layout_profiles": {},
            "activation_policies": {},
        }
        report = SimpleNamespace(
            ok=False,
            as_mapping=lambda: {
                "status": "error",
                "ok": False,
                "config": {
                    "valid": False,
                    "errors": ["schema_version doit valoir 6"],
                },
                "capabilities": {
                    "status": "error",
                    "summary": "configuration invalide",
                    "items": [],
                    "findings": [],
                },
            },
        )
        output = io.StringIO()

        with (
            patch.object(
                app_main,
                "load_config_unvalidated",
                return_value=config,
            ),
            patch(
                "stream_state_router.services.system_check.run_system_check",
                return_value=report,
            ) as system_check,
            patch.object(app_main, "SingleInstanceGuard") as guard,
            redirect_stdout(output),
        ):
            code = app_main.main(["--system-check-json"])

        self.assertEqual(code, 1)
        system_check.assert_called_once_with(config)
        guard.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["config"]["valid"])
        self.assertTrue(payload["config"]["errors"])

    def test_diagnostic_cli_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            app_main.parse_args(
                [
                    "--check-config",
                    "--system-check",
                ]
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


if __name__ == "__main__":
    unittest.main()
