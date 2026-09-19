from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stream_state_router.obs.client import (
    OBSClientManager,
    OBSRequestError,
    OBSResourceNotFoundError,
    OBSUnavailableError,
)
from stream_state_router.obs.models import OBSConnectionConfig


class _FakeRequestError(Exception):
    pass


class _FakeReqClient:
    instances = 0

    def __init__(self, **_kwargs):
        type(self).instances += 1
        self.fail_request_once = True

    def send(self, request, data=None, raw=False):
        if request == "GetSceneItemTransform" and self.fail_request_once:
            self.fail_request_once = False
            raise _FakeRequestError("No scene items were found")
        return {"obsVersion": "32.2.2"}


class _GenericRequestFailReqClient:
    def __init__(self, **_kwargs):
        pass

    def send(self, request, data=None, raw=False):
        raise _FakeRequestError("Request refused by OBS")


class _TransportFailReqClient:
    def __init__(self, **_kwargs):
        pass

    def send(self, request, data=None, raw=False):
        raise TimeoutError("timed out")


class OBSClientManagerTests(unittest.TestCase):
    def test_request_error_keeps_connection_alive_and_does_not_start_backoff(self):
        _FakeReqClient.instances = 0
        fake_obs = SimpleNamespace(ReqClient=_FakeReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True, reconnect_seconds=3.0))

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch("stream_state_router.obs.client._OBS_REQUEST_ERRORS", (_FakeRequestError,)),
        ):
            with self.assertRaises(OBSResourceNotFoundError):
                manager.send("GetSceneItemTransform", {"sceneName": "Test", "sceneItemId": 7})

            self.assertTrue(manager.connected)
            self.assertEqual(manager._last_failure, 0.0)
            self.assertEqual(_FakeReqClient.instances, 1)

            response = manager.send("GetVersion")
            self.assertEqual(response["obsVersion"], "32.2.2")
            self.assertEqual(_FakeReqClient.instances, 1)

    def test_generic_request_error_is_not_treated_as_confirmed_absence(self):
        fake_obs = SimpleNamespace(ReqClient=_GenericRequestFailReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True, reconnect_seconds=3.0))

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch("stream_state_router.obs.client._OBS_REQUEST_ERRORS", (_FakeRequestError,)),
        ):
            with self.assertRaises(OBSRequestError) as captured:
                manager.send("SetSceneItemEnabled", {"sceneName": "Test", "sceneItemId": 7})

        self.assertNotIsInstance(captured.exception, OBSResourceNotFoundError)
        self.assertTrue(manager.connected)

    def test_transport_error_marks_connection_unavailable(self):
        fake_obs = SimpleNamespace(ReqClient=_TransportFailReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True, reconnect_seconds=3.0))

        with patch("stream_state_router.obs.client._obs", fake_obs):
            with self.assertRaises(OBSUnavailableError):
                manager.send("GetVersion")

        self.assertFalse(manager.connected)
        self.assertGreater(manager._last_failure, 0.0)


if __name__ == "__main__":
    unittest.main()
