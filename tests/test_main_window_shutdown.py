from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from stream_state_router.services.runtime import RuntimeShutdownResult
from stream_state_router.ui.main_window import MainWindow


class MainWindowShutdownTests(unittest.TestCase):
    def test_stop_timeout_keeps_runtime_marker_open_for_late_checkpoint(self):
        pending = ({"kind": "activation_hide", "target": {"scene": "A"}},)
        result = RuntimeShutdownResult(
            worker_stopped=False,
            dispatch_quiescent=False,
            cleanup_complete=False,
            pending_cleanup=pending,
            shutdown_phase="worker_timeout",
        )
        service = SimpleNamespace(stop=Mock(return_value=result))
        marker = SimpleNamespace(
            finish=Mock(),
            checkpoint_pending_cleanup=Mock(),
        )
        window = SimpleNamespace(
            _service=service,
            _runtime_marker=marker,
            _log=Mock(),
        )

        observed = MainWindow._stop_runtime_for_exit(window)

        self.assertIs(observed, result)
        marker.finish.assert_not_called()
        marker.checkpoint_pending_cleanup.assert_called_once_with(pending)
        window._log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
