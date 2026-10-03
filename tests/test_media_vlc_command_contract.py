from __future__ import annotations

import unittest

from stream_state_router.media import (
    MediaProviderCommandError,
    MediaRuntime,
    MediaRuntimeConfig,
    VLCConfig,
    VLCProvider,
)


class EmptyCommandResponseTransport:
    def get_json(self, _path, params=None):
        if dict(params or {}).get("command"):
            return {}
        return {"state": "playing"}


class VLCCommandContractTests(unittest.TestCase):
    def test_one_shot_command_requires_complete_status_response(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=EmptyCommandResponseTransport(),
        )

        with self.assertRaises(MediaProviderCommandError):
            provider.next()

    def test_incomplete_one_shot_response_becomes_uncertain_in_runtime(self) -> None:
        provider = VLCProvider(
            VLCConfig(),
            transport=EmptyCommandResponseTransport(),
        )
        runtime = MediaRuntime(
            MediaRuntimeConfig(enabled=True, poll_seconds=10.0),
            provider,
        )
        runtime.start()
        self.addCleanup(runtime.stop)

        request_id = runtime.request("next")
        import time

        deadline = time.monotonic() + 1.0
        row = runtime.command_status(request_id)
        while (
            (row is None or row.get("status") not in {"uncertain", "failed"})
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            row = runtime.command_status(request_id)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "uncertain")
        self.assertFalse(row["success"])


if __name__ == "__main__":
    unittest.main()
