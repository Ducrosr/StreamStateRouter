from __future__ import annotations

import unittest

from stream_state_router.media import VLCConfig, VLCProvider


class NonApplyingTransport:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, path, params=None):
        values = dict(params or {})
        self.calls.append((str(path), values))
        # Simulate a syntactically successful VLC HTTP response where the
        # requested transport state did not actually take effect.
        return {"state": self.state}


class VLCCommandPostconditionTests(unittest.TestCase):
    def test_pause_rejects_success_response_that_is_still_playing(self) -> None:
        transport = NonApplyingTransport("playing")
        provider = VLCProvider(VLCConfig(), transport=transport)

        with self.assertRaisesRegex(RuntimeError, "pause"):
            provider.pause()

        self.assertEqual(
            transport.calls,
            [
                (
                    "/requests/status.json",
                    {"command": "pl_forcepause"},
                )
            ],
        )

    def test_stop_rejects_success_response_that_is_still_playing(self) -> None:
        transport = NonApplyingTransport("playing")
        provider = VLCProvider(VLCConfig(), transport=transport)

        with self.assertRaisesRegex(RuntimeError, "arrêt"):
            provider.stop()

        self.assertEqual(
            transport.calls,
            [
                (
                    "/requests/status.json",
                    {"command": "pl_stop"},
                )
            ],
        )

    def test_pause_and_stop_accept_matching_postconditions(self) -> None:
        paused = VLCProvider(
            VLCConfig(),
            transport=NonApplyingTransport("paused"),
        )
        stopped = VLCProvider(
            VLCConfig(),
            transport=NonApplyingTransport("stopped"),
        )

        paused.pause()
        stopped.stop()


if __name__ == "__main__":
    unittest.main()
