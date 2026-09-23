from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stream_state_router.services.runtime import (
    RoutingService,
    RuntimeEvent,
    summarize_obs_drift,
)


def _report(*, changed: bool = True) -> dict[str, object]:
    return {
        "available": True,
        "plan": {
            "diff": [
                {
                    "property": {
                        "kind": "input_mute",
                        "collection": "Main",
                        "source": "Mic",
                        "container": "",
                        "filter": "",
                        "setting": "",
                    },
                    "status": "change" if changed else "converged",
                    "observed": True,
                    "desired": False,
                    "provenance": ["Audio/Game"],
                },
                {
                    "property": {
                        "kind": "layout_profile",
                        "collection": "Main",
                        "container": "In Game",
                        "source": "",
                        "filter": "",
                        "setting": "",
                    },
                    "status": "unknown",
                    "observed": None,
                    "desired": "FPS",
                    "provenance": ["Layout/FPS"],
                },
            ]
        },
    }


class DriftSummaryTests(unittest.TestCase):
    def test_summary_counts_only_verified_changes(self) -> None:
        status = summarize_obs_drift(
            _report(),
            config_revision="abc",
            generation=7,
            checked_at=123.0,
        )

        self.assertTrue(status["available"])
        self.assertTrue(status["detected"])
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["unknown_count"], 1)
        self.assertTrue(status["coverage_limited"])
        self.assertTrue(status["signature"])
        self.assertEqual(status["changes"][0]["observed"], True)
        self.assertEqual(status["changes"][0]["desired"], False)

    def test_converged_summary_clears_signature(self) -> None:
        status = summarize_obs_drift(
            _report(changed=False),
            config_revision="abc",
            generation=7,
            checked_at=123.0,
        )

        self.assertFalse(status["detected"])
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["signature"], "")
        self.assertEqual(status["unknown_count"], 1)


class DriftProbeTests(unittest.TestCase):
    def _service(self, report: dict[str, object]):
        events: list[RuntimeEvent] = []
        state = object()
        service = SimpleNamespace(
            _last_obs_connected=True,
            _declarative_planning=object(),
            _last_drift_probe=0.0,
            drift_probe_seconds=2.0,
            _lock=threading.RLock(),
            _dispatch_lock=threading.RLock(),
            _stopping=False,
            _paused=False,
            _resume_revalidation_pending=False,
            _pending_dispatch=None,
            _active_obs_command=None,
            engine=SimpleNamespace(current_state=state),
            _dispatch_generation=4,
            config_revision="cfg",
            _last_drift_status={
                "available": False,
                "detected": False,
                "signature": "",
            },
            _build_current_declarative_plan=Mock(return_value=report),
            _emit=events.append,
            logger=Mock(),
        )
        return service, events

    def test_probe_emits_only_when_drift_state_changes(self) -> None:
        service, events = self._service(_report())

        with (
            patch(
                "stream_state_router.services.runtime.time.monotonic",
                side_effect=[10.0, 20.0, 30.0],
            ),
            patch(
                "stream_state_router.services.runtime.time.time",
                side_effect=[100.0, 110.0, 120.0],
            ),
        ):
            RoutingService._probe_obs_drift_if_due(service)
            RoutingService._probe_obs_drift_if_due(service)
            service._build_current_declarative_plan.return_value = _report(
                changed=False
            )
            RoutingService._probe_obs_drift_if_due(service)

        self.assertEqual(
            [event.kind for event in events],
            ["obs_drift", "obs_drift_cleared"],
        )
        self.assertEqual(events[0].payload["count"], 1)

    def test_probe_skips_while_runtime_is_paused(self) -> None:
        service, events = self._service(_report())
        service._paused = True

        RoutingService._probe_obs_drift_if_due(service)

        service._build_current_declarative_plan.assert_not_called()
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
