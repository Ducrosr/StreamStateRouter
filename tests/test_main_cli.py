from __future__ import annotations

import io
import json
import unittest
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
            code = app_main.main(["--declarative-coverage"])

        self.assertEqual(code, 0)
        guard.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["summary"]["actions"]["total"], 1)
        self.assertEqual(
            payload["summary"]["actions"]["counts"]["declarative_executable"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
