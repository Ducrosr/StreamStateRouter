from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from stream_state_router.services.api import APIConfig, LocalControlAPI


class APITests(unittest.TestCase):
    def setUp(self):
        self.requests = {"abc": {"request_id": "abc", "status": "completed", "action": "layout.apply"}}
        self.api = LocalControlAPI(
            APIConfig(enabled=True, port=0, token="secret", max_body_bytes=32),
            status=lambda: {"paused": False},
            action=lambda action, payload: {"status": "accepted", "request_id": "abc"},
            request_status=lambda request_id: self.requests.get(request_id),
        )
        self.api.start()
        self.base = f"http://127.0.0.1:{self.api.bound_port}"

    def tearDown(self):
        self.api.stop()

    def open(self, path, *, data=None, headers=None):
        request = Request(self.base + path, data=data, headers=headers or {})
        return urlopen(request, timeout=2)

    def test_request_status_endpoint_requires_token_and_returns_completion(self):
        with self.assertRaises(HTTPError) as unauthorized:
            self.open("/requests/abc")
        self.assertEqual(unauthorized.exception.code, 401)

        response = self.open("/requests/abc", headers={"Authorization": "Bearer secret"})
        payload = json.load(response)
        self.assertEqual(payload["status"], "completed")

    def test_post_rejects_wrong_content_type(self):
        with self.assertRaises(HTTPError) as error:
            self.open(
                "/reapply",
                data=b"{}",
                headers={"Authorization": "Bearer secret", "Content-Type": "text/plain"},
            )
        self.assertEqual(error.exception.code, 415)

    def test_post_rejects_oversized_body_before_read(self):
        with self.assertRaises(HTTPError) as error:
            self.open(
                "/reapply",
                data=b'{"payload":"abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"}',
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            )
        self.assertEqual(error.exception.code, 413)

    def test_browser_origin_is_explicitly_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.open(
                "/status",
                headers={"Authorization": "Bearer secret", "Origin": "http://example.invalid"},
            )
        self.assertEqual(error.exception.code, 403)

    def test_async_action_returns_202(self):
        response = self.open(
            "/reapply",
            data=b"{}",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 202)
        payload = json.load(response)
        self.assertEqual(payload["request_id"], "abc")


if __name__ == "__main__":
    unittest.main()
