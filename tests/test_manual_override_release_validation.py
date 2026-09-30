from __future__ import annotations

import math
import unittest

from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.models import StreamState
from stream_state_router.router.rules import RuleSet
from stream_state_router.services.runtime import RoutingService


class _Dispatcher:
    client = None


class ManualOverrideReleaseValidationTests(unittest.TestCase):
    def _service(self) -> RoutingService:
        return RoutingService(
            StateRouterEngine(RuleSet([]), debounce_ms=0),
            _Dispatcher(),
            provider=object(),
        )

    def test_duration_mode_requires_positive_duration(self) -> None:
        service = self._service()
        with self.assertRaisesRegex(ValueError, "duration_seconds doit être > 0"):
            service.set_manual_override(
                StreamState(game="Manual"),
                release_mode="duration",
            )

    def test_non_finite_duration_is_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                service = self._service()
                with self.assertRaisesRegex(
                    ValueError,
                    "duration_seconds doit être un nombre fini",
                ):
                    service.set_manual_override(
                        StreamState(game="Manual"),
                        duration_seconds=value,
                    )

    def test_duration_cannot_be_combined_with_stream_end(self) -> None:
        service = self._service()
        with self.assertRaisesRegex(
            ValueError,
            "duration_seconds ne peut pas être combiné",
        ):
            service.set_manual_override(
                StreamState(game="Manual"),
                duration_seconds=30,
                release_mode="stream_end",
            )

    def test_unknown_release_mode_is_rejected(self) -> None:
        service = self._service()
        with self.assertRaisesRegex(ValueError, "release_mode doit être"):
            service.set_manual_override(
                StreamState(game="Manual"),
                release_mode="mystery",
            )


if __name__ == "__main__":
    unittest.main()
