from __future__ import annotations

import json
import unittest
from urllib.request import Request, urlopen

from stream_state_router.services.api import APIConfig, LocalControlAPI


class MediaAPIContractTests(unittest.TestCase):
    def test_media_route_and_async_request_status_match_streamdeck_contract(self):
        calls: list[tuple[str, dict[str, object]]] = []
        statuses = {
            "media-1": {
                "request_id": "media-1",
                "action": "play",
                "status": "completed",
                "success": True,
                "error": "",
                "state": {
                    "provider": "vlc",
                    "playback_state": "playing",
                },
            }
        }

        def action(name, payload):
            calls.append((str(name), dict(payload)))
            return {
                "request_id": "media-1",
                "status": "accepted",
            }

        api = LocalControlAPI(
            APIConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
            ),
            status=lambda: {
                "media": {
                    "running": True,
                    "state": {
                        "playback_state": "stopped",
                    },
                }
            },
            action=action,
            request_status=lambda request_id: statuses.get(request_id),
        )
        api.start()
        try:
            request = Request(
                f"http://127.0.0.1:{api.bound_port}/media/play",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2.0) as response:
                accepted = json.loads(response.read())

            self.assertEqual(response.status, 202)
            self.assertTrue(accepted["ok"])
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["request_id"], "media-1")
            self.assertEqual(calls, [("media.play", {})])

            with urlopen(
                f"http://127.0.0.1:{api.bound_port}/requests/media-1",
                timeout=2.0,
            ) as response:
                completed = json.loads(response.read())

            self.assertTrue(completed["ok"])
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["success"])
            self.assertEqual(completed["action"], "play")
        finally:
            api.stop()


if __name__ == "__main__":
    unittest.main()
