from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stream_state_router.obs.client import (
    OBSClientManager,
    OBSRequestError,
    OBSResourceNotFoundError,
    OBSSceneCollectionContextChangedError,
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


class _NotReadyRequestError(Exception):
    code = 207


class _NotReadyReqClient:
    def __init__(self, **_kwargs):
        pass

    def send(self, request, data=None, raw=False):
        raise _NotReadyRequestError(
            "Request GetVersion returned code 207. "
            "With message: OBS is not ready to perform the request."
        )


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


class _ClosableReqClient:
    def __init__(self, **_kwargs):
        self.disconnected = False

    def send(self, request, data=None, raw=False):
        return {"obsVersion": "32.2.2"}

    def disconnect(self):
        self.disconnected = True


class _AliveWorker:
    def is_alive(self):
        return True


class _CallbackRegistry:
    def __init__(self):
        self.functions = []

    def register(self, functions):
        try:
            values = list(functions)
        except TypeError:
            values = [functions]
        for function in values:
            if function not in self.functions:
                self.functions.append(function)

    def emit(self, callback_name, data):
        for function in tuple(self.functions):
            if function.__name__ == callback_name:
                function(data)


class _CollectionEventServer:
    def __init__(self):
        self.current_collection = "Collection A"
        self.event_client = None
        self.roundtrip_request = ""
        self.request_error = False
        self.broadcast_count = 0
        self.fail_broadcast_at = 0

    def emit_collection(self, name):
        event_client = self.event_client
        if event_client is None:
            raise RuntimeError("event client unavailable")
        event_client.callback.emit(
            "on_current_scene_collection_changing",
            SimpleNamespace(scene_collection_name=name),
        )
        self.current_collection = name
        event_client.callback.emit(
            "on_current_scene_collection_changed",
            SimpleNamespace(scene_collection_name=name),
        )

    def send(self, request, data=None):
        payload = dict(data or {})
        if request == "GetVersion":
            return {"obsVersion": "32.2.2"}
        if request == "GetSceneCollectionList":
            return {
                "currentSceneCollectionName": self.current_collection
            }
        if request == "BroadcastCustomEvent":
            self.broadcast_count += 1
            if (
                self.fail_broadcast_at
                and self.broadcast_count == self.fail_broadcast_at
            ):
                raise _FakeRequestError("barrier refused")
            event_client = self.event_client
            if event_client is None:
                raise RuntimeError("event client unavailable")
            event_client.callback.emit(
                "on_custom_event",
                SimpleNamespace(
                    **dict(payload.get("eventData") or {})
                ),
            )
            return {}
        if request == self.roundtrip_request:
            self.emit_collection("Collection B")
            self.emit_collection("Collection A")
            if self.request_error:
                error = _FakeRequestError("source not found")
                error.code = 600
                raise error
            return {
                "filterName": "Filter",
                "filterKind": "color_filter_v2",
                "filterEnabled": False,
                "filterSettings": {"opacity": 1.0},
            }
        return {}


class _CollectionReqClient:
    server = None

    def __init__(self, **_kwargs):
        self.server = type(self).server

    def send(self, request, data=None, raw=False):
        return self.server.send(request, data)

    def disconnect(self):
        pass


class _CollectionEventClient:
    server = None

    def __init__(self, **_kwargs):
        self.server = type(self).server
        self.callback = _CallbackRegistry()
        self.worker = _AliveWorker()
        self.server.event_client = self

    def disconnect(self):
        if self.server.event_client is self:
            self.server.event_client = None


class OBSClientManagerTests(unittest.TestCase):
    def test_close_disconnects_owned_req_client_and_marks_manager_disconnected(self):
        fake_obs = SimpleNamespace(ReqClient=_ClosableReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True))

        with patch("stream_state_router.obs.client._obs", fake_obs):
            manager.send("GetVersion")
            client = manager._client
            self.assertTrue(manager.connected)

            manager.close()

        self.assertTrue(client.disconnected)
        self.assertIsNone(manager._client)
        self.assertFalse(manager.connected)

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

    def test_request_counter_counts_submitted_requests_without_reset_on_failure(self):
        fake_obs = SimpleNamespace(ReqClient=_FakeReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True, reconnect_seconds=3.0))

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch("stream_state_router.obs.client._OBS_REQUEST_ERRORS", (_FakeRequestError,)),
        ):
            with self.assertRaises(OBSResourceNotFoundError):
                manager.send("GetSceneItemTransform", {"sceneName": "Test", "sceneItemId": 7})
            manager.send("GetVersion")

        self.assertEqual(manager.request_count, 2)

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

    def test_probe_reports_obs_startup_as_transient_not_ready_state(self):
        fake_obs = SimpleNamespace(ReqClient=_NotReadyReqClient)
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True, reconnect_seconds=3.0)
        )

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch(
                "stream_state_router.obs.client._OBS_REQUEST_ERRORS",
                (_NotReadyRequestError,),
            ),
        ):
            ok, message = manager.probe()

        self.assertFalse(ok)
        self.assertTrue(manager.connected)
        self.assertIn("encore en cours d'initialisation", message)

    def test_transport_error_marks_connection_unavailable(self):
        fake_obs = SimpleNamespace(ReqClient=_TransportFailReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True, reconnect_seconds=3.0))

        with patch("stream_state_router.obs.client._obs", fake_obs):
            with self.assertRaises(OBSUnavailableError):
                manager.send("GetVersion")

        self.assertFalse(manager.connected)
        self.assertGreater(manager._last_failure, 0.0)


    def test_session_generation_changes_across_transport_lifecycle(self):
        fake_obs = SimpleNamespace(ReqClient=_ClosableReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True))

        self.assertEqual(manager.session_generation, 0)
        with patch("stream_state_router.obs.client._obs", fake_obs):
            manager.send("GetVersion")
            connected_generation = manager.session_generation
            manager.close()

        self.assertGreater(connected_generation, 0)
        self.assertGreater(manager.session_generation, connected_generation)

    def test_guarded_send_refuses_changed_session_without_reconnecting(self):
        _FakeReqClient.instances = 0
        fake_obs = SimpleNamespace(ReqClient=_FakeReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True))

        with patch("stream_state_router.obs.client._obs", fake_obs):
            manager.send("GetVersion")
            generation = manager.session_generation
            manager.close()
            instances_before = _FakeReqClient.instances

            with self.assertRaisesRegex(OBSUnavailableError, "refusing reconnect"):
                manager.send(
                    "SetInputMute",
                    {"inputUuid": "mic-1", "inputMuted": True},
                    expected_session_generation=generation,
                )

        self.assertEqual(_FakeReqClient.instances, instances_before)

    def test_legacy_send_keeps_existing_reconnect_behavior(self):
        _FakeReqClient.instances = 0
        fake_obs = SimpleNamespace(ReqClient=_FakeReqClient)
        manager = OBSClientManager(OBSConnectionConfig(enabled=True))

        with patch("stream_state_router.obs.client._obs", fake_obs):
            manager.send("GetVersion")
            manager.close()
            manager.send("GetVersion")

        self.assertEqual(_FakeReqClient.instances, 2)

    def test_transport_failure_invalidates_session_generation(self):
        fake_obs = SimpleNamespace(ReqClient=_TransportFailReqClient)
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True, reconnect_seconds=3.0)
        )

        with patch("stream_state_router.obs.client._obs", fake_obs):
            with self.assertRaises(OBSUnavailableError):
                manager.send("GetVersion")

        self.assertGreaterEqual(manager.session_generation, 2)

    def test_custom_event_barrier_accepts_real_obsws_payload_shape(self):
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True)
        )
        token = "barrier-token"
        payload = SimpleNamespace(
            **{manager._BARRIER_KEY: token}
        )

        manager.on_custom_event(payload)

        self.assertIn(token, manager._event_barriers_seen)

    def test_scene_collection_generation_detects_round_trip_during_request(self):
        server = _CollectionEventServer()
        _CollectionReqClient.server = server
        _CollectionEventClient.server = server
        fake_obs = SimpleNamespace(
            ReqClient=_CollectionReqClient,
            EventClient=_CollectionEventClient,
        )
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True, timeout_seconds=0.5)
        )

        with patch("stream_state_router.obs.client._obs", fake_obs):
            manager.send("GetVersion")
            session = manager.session_generation
            name, generation = manager.scene_collection_context(
                expected_session_generation=session,
            )
            self.assertEqual(name, "Collection A")

            server.roundtrip_request = "GetSourceFilter"
            with self.assertRaises(
                OBSSceneCollectionContextChangedError
            ) as captured:
                manager.send(
                    "GetSourceFilter",
                    {
                        "sourceName": "Input",
                        "filterName": "Filter",
                    },
                    expected_session_generation=session,
                    expected_scene_collection_generation=generation,
                )

        self.assertTrue(captured.exception.request_submitted)
        self.assertEqual(server.current_collection, "Collection A")
        self.assertGreater(
            manager.scene_collection_generation,
            generation,
        )

    def test_collection_round_trip_overrides_resource_not_found_absence(self):
        server = _CollectionEventServer()
        server.request_error = True
        _CollectionReqClient.server = server
        _CollectionEventClient.server = server
        fake_obs = SimpleNamespace(
            ReqClient=_CollectionReqClient,
            EventClient=_CollectionEventClient,
        )
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True, timeout_seconds=0.5)
        )

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch(
                "stream_state_router.obs.client._OBS_REQUEST_ERRORS",
                (_FakeRequestError,),
            ),
        ):
            manager.send("GetVersion")
            session = manager.session_generation
            _name, generation = manager.scene_collection_context(
                expected_session_generation=session,
            )

            server.roundtrip_request = "GetSourceFilter"
            with self.assertRaises(
                OBSSceneCollectionContextChangedError
            ) as captured:
                manager.send(
                    "GetSourceFilter",
                    {
                        "sourceName": "Input",
                        "filterName": "Filter",
                    },
                    expected_session_generation=session,
                    expected_scene_collection_generation=generation,
                )

        self.assertTrue(captured.exception.request_submitted)
        self.assertEqual(server.current_collection, "Collection A")


    def test_failed_post_barrier_is_reported_as_submitted_context_uncertainty(self):
        server = _CollectionEventServer()
        _CollectionReqClient.server = server
        _CollectionEventClient.server = server
        fake_obs = SimpleNamespace(
            ReqClient=_CollectionReqClient,
            EventClient=_CollectionEventClient,
        )
        manager = OBSClientManager(
            OBSConnectionConfig(enabled=True, timeout_seconds=0.5)
        )

        with (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch(
                "stream_state_router.obs.client._OBS_REQUEST_ERRORS",
                (_FakeRequestError,),
            ),
        ):
            manager.send("GetVersion")
            session = manager.session_generation
            _name, generation = manager.scene_collection_context(
                expected_session_generation=session,
            )

            # scene_collection_context consumed two barriers; the guarded
            # request consumes one pre-barrier and then this failing post one.
            server.fail_broadcast_at = server.broadcast_count + 2

            with self.assertRaises(
                OBSSceneCollectionContextChangedError
            ) as captured:
                manager.send(
                    "SetSourceFilterSettings",
                    {
                        "sourceName": "Input",
                        "filterName": "Filter",
                        "filterSettings": {"opacity": 0.2},
                    },
                    expected_session_generation=session,
                    expected_scene_collection_generation=generation,
                )

        self.assertTrue(captured.exception.request_submitted)


if __name__ == "__main__":
    unittest.main()
