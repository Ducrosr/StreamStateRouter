from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from stream_state_router.services.runtime import RuntimeShutdownResult
from stream_state_router.ui.main_window import MainWindow


def _default_config() -> dict:
    return json.loads(
        Path("config/default.json").read_text(encoding="utf-8")
    )


class ApplyRecoveryTests(unittest.TestCase):
    def test_runtime_preflight_accepts_default_configuration(self) -> None:
        MainWindow._preflight_runtime_config(
            SimpleNamespace(),
            _default_config(),
        )

    def test_persisted_rollback_restores_snapshot_and_startup(self) -> None:
        previous = _default_config()
        dummy = SimpleNamespace(
            _last_saved_config={"old": True},
            _saved_revision="new-revision",
        )

        with (
            patch(
                "stream_state_router.ui.main_window.save_config"
            ) as save,
            patch(
                "stream_state_router.ui.main_window.set_startup_enabled"
            ) as startup,
        ):
            ok, detail = MainWindow._rollback_persisted_apply(
                dummy,
                previous,
                True,
            )

        self.assertTrue(ok)
        self.assertEqual(detail, "")
        save.assert_called_once_with(previous)
        startup.assert_called_once_with(True)
        self.assertEqual(dummy._last_saved_config, previous)
        self.assertNotEqual(dummy._saved_revision, "new-revision")

    def test_persisted_rollback_reports_save_failure(self) -> None:
        previous = _default_config()
        dummy = SimpleNamespace(
            _last_saved_config={"old": True},
            _saved_revision="new-revision",
        )

        with (
            patch(
                "stream_state_router.ui.main_window.save_config",
                side_effect=OSError("disk full"),
            ),
            patch(
                "stream_state_router.ui.main_window.set_startup_enabled"
            ) as startup,
        ):
            ok, detail = MainWindow._rollback_persisted_apply(
                dummy,
                previous,
                False,
            )

        self.assertFalse(ok)
        self.assertIn("disk full", detail)
        startup.assert_called_once_with(False)
        self.assertEqual(dummy._saved_revision, "new-revision")

    def test_recovery_merges_pending_cleanup_before_restart(self) -> None:
        pending_from_failed_runtime = (
            {"kind": "layout_fade", "source": "Avatar"},
        )
        current = Mock()
        current.stop.return_value = RuntimeShutdownResult(
            worker_stopped=True,
            dispatch_quiescent=True,
            cleanup_complete=False,
            pending_cleanup=pending_from_failed_runtime,
        )
        starter = Mock()
        dummy = SimpleNamespace(
            _service=current,
            _pending_cleanup_transfer=(
                {"kind": "activation_hide", "source": "TV"},
            ),
            _start_runtime=starter,
        )
        previous = _default_config()

        ok, detail = MainWindow._recover_previous_runtime(
            dummy,
            previous,
            bootstrap_foreground=None,
            startup_layout_profile="",
            startup_layout_routing_baseline="",
        )

        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(
            len(dummy._pending_cleanup_transfer),
            2,
        )
        starter.assert_called_once()
        self.assertEqual(
            starter.call_args.kwargs["config_data"],
            previous,
        )

    def test_recovery_refuses_concurrent_runtime_when_partial_stop_fails(self) -> None:
        current = Mock()
        current.stop.return_value = RuntimeShutdownResult(
            worker_stopped=False,
            dispatch_quiescent=False,
            cleanup_complete=True,
        )
        starter = Mock()
        dummy = SimpleNamespace(
            _service=current,
            _pending_cleanup_transfer=(),
            _start_runtime=starter,
        )

        ok, detail = MainWindow._recover_previous_runtime(
            dummy,
            _default_config(),
            bootstrap_foreground=None,
            startup_layout_profile="",
            startup_layout_routing_baseline="",
        )

        self.assertFalse(ok)
        self.assertIn("n’a pas pu être arrêté", detail)
        starter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
