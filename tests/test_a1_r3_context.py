from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stream_state_router.obs.client import OBSClientManager
from stream_state_router.obs.fade_helpers import FadeHelperManifestStore
from stream_state_router.obs.dispatcher import DispatchResult
from stream_state_router.obs.layouts import OBSLayoutManager
from stream_state_router.obs.models import OBSConnectionConfig
from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.rules import RuleSet
from stream_state_router.services.recovery import RuntimeMarker
from stream_state_router.services.runtime import RoutingService


class _RequestError(Exception):
    code = 600


class _AliveWorker:
    def is_alive(self):
        return True


class _Callbacks:
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

    def emit(self, callback_name, payload):
        for function in tuple(self.functions):
            if function.__name__ == callback_name:
                function(payload)


class _Server:
    source = "[Webcam] Avatar"
    source_uuid = "uuid-event-avatar"
    source_kind = "image_source"

    def __init__(self):
        self.current_collection = "Collection A"
        self.collections = {
            "Collection A": {self.source: {}},
            "Collection B": {self.source: {}},
        }
        self.event_client = None
        self.roundtrip_request = ""
        self.block_request = ""
        self.request_started = threading.Event()
        self.release_request = threading.Event()

    def emit_collection(self, name):
        event_client = self.event_client
        if event_client is None:
            raise RuntimeError("event stream unavailable")
        event_client.callback.emit(
            "on_current_scene_collection_changing",
            SimpleNamespace(scene_collection_name=name),
        )
        self.current_collection = name
        event_client.callback.emit(
            "on_current_scene_collection_changed",
            SimpleNamespace(scene_collection_name=name),
        )

    def _filters(self):
        return self.collections[self.current_collection][self.source]

    def _perform(self, request, payload):
        if request == "GetVersion":
            return {"obsVersion": "32.2.2"}
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": self.current_collection}
        if request == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": self.source,
                        "inputUuid": self.source_uuid,
                        "inputKind": self.source_kind,
                    }
                ]
            }
        if request == "GetSourceFilterList":
            return {
                "filters": [
                    {
                        "filterName": name,
                        "filterKind": state["kind"],
                        "filterEnabled": state["enabled"],
                    }
                    for name, state in self._filters().items()
                ]
            }
        if request == "CreateSourceFilter":
            name = str(payload.get("filterName") or "")
            filters = self._filters()
            if name in filters:
                raise RuntimeError("duplicate filter")
            filters[name] = {
                "kind": str(payload.get("filterKind") or ""),
                "enabled": True,
                "settings": dict(payload.get("filterSettings") or {}),
            }
            return {}
        if request == "GetSourceFilter":
            name = str(payload.get("filterName") or "")
            state = self._filters().get(name)
            if state is None:
                raise _RequestError("source not found")
            return {
                "filterName": name,
                "filterKind": state["kind"],
                "filterEnabled": state["enabled"],
                "filterSettings": dict(state["settings"]),
            }
        if request == "SetSourceFilterSettings":
            name = str(payload.get("filterName") or "")
            state = self._filters().get(name)
            if state is None:
                raise _RequestError("source not found")
            settings = payload.get("filterSettings")
            if isinstance(settings, dict):
                if bool(payload.get("overlay", True)):
                    state["settings"].update(settings)
                else:
                    state["settings"] = dict(settings)
            return {}
        if request == "SetSourceFilterEnabled":
            name = str(payload.get("filterName") or "")
            state = self._filters().get(name)
            if state is None:
                raise _RequestError("source not found")
            state["enabled"] = bool(payload.get("filterEnabled"))
            return {}
        raise AssertionError(f"Unexpected request: {request}")

    def send(self, request, data=None):
        payload = dict(data or {})
        if request == "BroadcastCustomEvent":
            if self.event_client is None:
                raise RuntimeError("event stream unavailable")
            self.event_client.callback.emit(
                "on_custom_event",
                SimpleNamespace(
                    event_data=dict(payload.get("eventData") or {})
                ),
            )
            return {}

        roundtrip = bool(self.roundtrip_request) and request == self.roundtrip_request
        if roundtrip:
            self.roundtrip_request = ""
            self.emit_collection("Collection B")

        if self.block_request and request == self.block_request:
            self.request_started.set()
            if not self.release_request.wait(2.0):
                raise RuntimeError("blocked request was not released")

        try:
            return self._perform(request, payload)
        finally:
            if roundtrip:
                self.emit_collection("Collection A")


class _ReqClient:
    server = None

    def __init__(self, **_kwargs):
        self.server = type(self).server

    def send(self, request, data=None, raw=False):
        return self.server.send(request, data)

    def disconnect(self):
        pass


class _EventClient:
    server = None

    def __init__(self, **_kwargs):
        self.server = type(self).server
        self.callback = _Callbacks()
        self.worker = _AliveWorker()
        self.server.event_client = self

    def disconnect(self):
        if self.server.event_client is self:
            self.server.event_client = None


class _RuntimeDispatcher:
    def __init__(self, client, layout_manager, source):
        self.client = client
        self.layout_manager = layout_manager
        self.source = source

    def dispatch_change(self, _change):
        return DispatchResult(0, 0, ())

    def dispatch_state(self, _state, force=False):
        return DispatchResult(0, 0, ())

    def pending_domains(self, _state=None):
        return ()

    def execute_layout_profile(self, _name, preview=False):
        self.layout_manager._set_source_opacity(self.source, 0.0)
        return SimpleNamespace(warnings=(), missing_sources=())


class _NullProvider:
    def get(self):
        return None


class A1R3ContextTests(unittest.TestCase):
    def _patches(self, server):
        _ReqClient.server = server
        _EventClient.server = server
        fake_obs = SimpleNamespace(ReqClient=_ReqClient, EventClient=_EventClient)
        return (
            patch("stream_state_router.obs.client._obs", fake_obs),
            patch(
                "stream_state_router.obs.client._OBS_REQUEST_ERRORS",
                (_RequestError,),
            ),
        )

    @staticmethod
    def _fixture(tmp, server):
        client = OBSClientManager(
            OBSConnectionConfig(
                enabled=True,
                host="127.0.0.1",
                port=4455,
                timeout_seconds=0.5,
                reconnect_seconds=0.0,
            )
        )
        store = FadeHelperManifestStore(Path(tmp) / "helper-manifest.json")
        manager = OBSLayoutManager(client, fade_helper_store=store)
        marker = RuntimeMarker()
        marker.path = Path(tmp) / "runtime.json"
        marker.start()

        def persist():
            marker.checkpoint_pending_cleanup(
                manager.export_pending_fade_cleanup()
            )

        manager.set_pending_cleanup_changed(persist)
        return client, store, manager, marker

    def test_round_trip_recovery_read_keeps_disk_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, _store, manager, marker = self._fixture(tmp, server)
                identity = manager._prepare_fade_filter(server.source, "Collection A")
                manager._set_source_opacity(server.source, 0.3)

                server.collections["Collection B"][server.source] = {
                    identity.filter_name: {
                        "kind": identity.filter_kind,
                        "enabled": False,
                        "settings": {"opacity": 1.0},
                    }
                }
                server.roundtrip_request = "GetSourceFilter"

                warnings = manager.retry_pending_fade_cleanup()

                self.assertTrue(warnings)
                self.assertEqual(manager.pending_fade_cleanup(), (server.source,))
                a_state = server.collections["Collection A"][server.source][
                    identity.filter_name
                ]
                self.assertTrue(a_state["enabled"])
                self.assertAlmostEqual(float(a_state["settings"]["opacity"]), 0.3)
                disk = json.loads(marker.path.read_text(encoding="utf-8"))
                self.assertEqual(len(disk["pending_cleanup"]), 1)
                client.close()

    def test_round_trip_absence_keeps_renamed_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, _store, manager, marker = self._fixture(tmp, server)
                identity = manager._prepare_fade_filter(server.source, "Collection A")
                manager._set_source_opacity(server.source, 0.3)

                a_filters = server.collections["Collection A"][server.source]
                renamed = "Renamed correction"
                state = a_filters.pop(identity.filter_name)
                state["settings"]["contrast"] = 0.5
                a_filters[renamed] = state
                server.collections["Collection B"][server.source] = {}
                server.roundtrip_request = "GetSourceFilterList"

                warnings = manager.retry_pending_fade_cleanup()

                self.assertTrue(warnings)
                self.assertEqual(manager.pending_fade_cleanup(), (server.source,))
                self.assertTrue(a_filters[renamed]["enabled"])
                self.assertAlmostEqual(
                    float(a_filters[renamed]["settings"]["opacity"]),
                    0.3,
                )
                self.assertEqual(
                    float(a_filters[renamed]["settings"]["contrast"]),
                    0.5,
                )
                disk = json.loads(marker.path.read_text(encoding="utf-8"))
                self.assertEqual(len(disk["pending_cleanup"]), 1)
                client.close()

    def test_round_trip_state_read_cannot_mark_observed_from_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, store, manager, marker = self._fixture(tmp, server)
                client.send("GetVersion")
                identity = store.prepare_layout_fade(
                    connection_host="127.0.0.1",
                    connection_port=4455,
                    collection="Collection A",
                    source_uuid=server.source_uuid,
                    source_alias=server.source,
                    source_kind=server.source_kind,
                    session_generation=client.session_generation,
                )
                server.collections["Collection B"][server.source] = {
                    identity.filter_name: {
                        "kind": identity.filter_kind,
                        "enabled": True,
                        "settings": {"opacity": 1.0, "contrast": 0.37},
                    }
                }
                server.roundtrip_request = "GetSourceFilter"

                with self.assertRaisesRegex(RuntimeError, "Scene Collection"):
                    manager._prepare_fade_filter(server.source, "Collection A")

                current = store.get(identity.helper_id)
                self.assertIsNotNone(current)
                self.assertEqual(current.state, "prepared")
                self.assertEqual(dict(current.non_temporary_settings or {}), {})
                self.assertEqual(len(manager.export_pending_fade_cleanup()), 1)
                disk = json.loads(marker.path.read_text(encoding="utf-8"))
                self.assertEqual(len(disk["pending_cleanup"]), 1)
                client.close()

    def test_inflight_opacity_round_trip_is_durably_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, _store, manager, marker = self._fixture(tmp, server)
                identity = manager._prepare_fade_filter(server.source, "Collection A")
                server.collections["Collection B"][server.source] = {
                    identity.filter_name: {
                        "kind": identity.filter_kind,
                        "enabled": True,
                        "settings": {"opacity": 0.73},
                    }
                }
                server.roundtrip_request = "SetSourceFilterSettings"

                with self.assertRaisesRegex(RuntimeError, "Scene Collection"):
                    manager._set_source_opacity(server.source, 0.0)

                pending = manager.export_pending_fade_cleanup()
                self.assertEqual(len(pending), 1)
                self.assertTrue(pending[0]["context_uncertain"])
                disk = json.loads(marker.path.read_text(encoding="utf-8"))
                self.assertTrue(
                    disk["pending_cleanup"][0]["context_uncertain"]
                )
                b_state = server.collections["Collection B"][server.source][
                    identity.filter_name
                ]
                self.assertAlmostEqual(float(b_state["settings"]["opacity"]), 0.0)

                retry = manager.retry_pending_fade_cleanup()
                self.assertTrue(retry)
                self.assertEqual(len(manager.export_pending_fade_cleanup()), 1)
                self.assertAlmostEqual(float(b_state["settings"]["opacity"]), 0.0)
                client.close()

    def test_recovery_mutation_uncertainty_survives_retry(self):
        for phase in ("neutralize", "disable"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                server = _Server()
                p1, p2 = self._patches(server)
                with p1, p2:
                    client, _store, manager, marker = self._fixture(tmp, server)
                    identity = manager._prepare_fade_filter(
                        server.source,
                        "Collection A",
                    )
                    if phase == "neutralize":
                        manager._set_source_opacity(server.source, 0.3)

                    server.collections["Collection B"][server.source] = {
                        identity.filter_name: {
                            "kind": identity.filter_kind,
                            "enabled": True,
                            "settings": {"opacity": 0.73},
                        }
                    }
                    server.roundtrip_request = (
                        "SetSourceFilterSettings"
                        if phase == "neutralize"
                        else "SetSourceFilterEnabled"
                    )

                    first = manager.retry_pending_fade_cleanup()

                    self.assertTrue(first)
                    pending = manager.export_pending_fade_cleanup()
                    self.assertEqual(len(pending), 1)
                    self.assertTrue(pending[0]["context_uncertain"])
                    disk = json.loads(marker.path.read_text(encoding="utf-8"))
                    self.assertTrue(
                        disk["pending_cleanup"][0]["context_uncertain"]
                    )
                    second = manager.retry_pending_fade_cleanup()
                    self.assertTrue(second)
                    self.assertEqual(len(manager.export_pending_fade_cleanup()), 1)
                    client.close()

    def test_failed_uncertainty_clear_survives_real_journal_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, store, manager, marker = self._fixture(tmp, server)
                manager._prepare_fade_filter(server.source, "Collection A")

                fail_clear = {"armed": True}

                def persist_with_clear_failure():
                    rows = manager.export_pending_fade_cleanup()
                    if (
                        fail_clear["armed"]
                        and rows
                        and not bool(rows[0].get("context_uncertain"))
                    ):
                        fail_clear["armed"] = False
                        with patch(
                            "stream_state_router.services.recovery.os.replace",
                            side_effect=OSError("injected uncertainty clear failure"),
                        ):
                            marker.checkpoint_pending_cleanup(rows)
                        return
                    marker.checkpoint_pending_cleanup(rows)

                manager.set_pending_cleanup_changed(persist_with_clear_failure)

                with self.assertRaisesRegex(OSError, "uncertainty clear failure"):
                    manager._set_source_opacity(server.source, 0.2)

                disk_before = json.loads(marker.path.read_text(encoding="utf-8"))
                self.assertTrue(
                    disk_before["pending_cleanup"][0]["context_uncertain"]
                )

                restarted_marker = RuntimeMarker()
                restarted_marker.path = marker.path
                restarted_marker.start()
                restarted = OBSLayoutManager(client, fade_helper_store=store)
                restarted.set_pending_cleanup_changed(
                    lambda: restarted_marker.checkpoint_pending_cleanup(
                        restarted.export_pending_fade_cleanup()
                    )
                )
                restarted.import_pending_fade_cleanup(
                    restarted_marker.previous_pending_cleanup
                )

                warnings = restarted.retry_pending_fade_cleanup()

                self.assertTrue(warnings)
                pending = restarted.export_pending_fade_cleanup()
                self.assertEqual(len(pending), 1)
                self.assertTrue(pending[0]["context_uncertain"])
                disk_after = json.loads(
                    restarted_marker.path.read_text(encoding="utf-8")
                )
                self.assertTrue(
                    disk_after["pending_cleanup"][0]["context_uncertain"]
                )
                client.close()


    def test_routing_service_stop_cannot_mask_inflight_collection_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server()
            p1, p2 = self._patches(server)
            with p1, p2:
                client, _store, manager, marker = self._fixture(tmp, server)
                identity = manager._prepare_fade_filter(
                    server.source,
                    "Collection A",
                )
                server.collections["Collection B"][server.source] = {
                    identity.filter_name: {
                        "kind": identity.filter_kind,
                        "enabled": True,
                        "settings": {"opacity": 0.73},
                    }
                }
                server.roundtrip_request = "SetSourceFilterSettings"
                server.block_request = "SetSourceFilterSettings"

                dispatcher = _RuntimeDispatcher(
                    client,
                    manager,
                    server.source,
                )
                service = RoutingService(
                    StateRouterEngine(RuleSet([]), debounce_ms=0),
                    dispatcher,
                    poll_ms=20,
                    provider=_NullProvider(),
                    obs_probe_seconds=10.0,
                )
                service.start()
                stop_result = {}

                try:
                    service.request_layout("apply", "A1 shutdown")
                    self.assertTrue(
                        server.request_started.wait(1.0),
                        "fade mutation never became in-flight",
                    )

                    def stop_service():
                        stop_result["value"] = service.stop(timeout=1.0)

                    stopper = threading.Thread(target=stop_service)
                    stopper.start()
                    deadline = __import__("time").monotonic() + 1.0
                    while (
                        not service._stopping
                        and __import__("time").monotonic() < deadline
                    ):
                        __import__("time").sleep(0.01)
                    self.assertTrue(
                        service._stopping,
                        "RoutingService.stop did not close admission",
                    )

                    server.release_request.set()
                    stopper.join(2.0)
                    self.assertFalse(stopper.is_alive())

                    pending = manager.export_pending_fade_cleanup()
                    self.assertEqual(len(pending), 1)
                    self.assertTrue(pending[0]["context_uncertain"])
                    disk = json.loads(
                        marker.path.read_text(encoding="utf-8")
                    )
                    self.assertTrue(
                        disk["pending_cleanup"][0]["context_uncertain"]
                    )
                    b_state = server.collections["Collection B"][
                        server.source
                    ][identity.filter_name]
                    self.assertAlmostEqual(
                        float(b_state["settings"]["opacity"]),
                        0.0,
                    )
                    result = stop_result["value"]
                    self.assertTrue(result.worker_stopped)
                    self.assertFalse(result.cleanup_complete)
                finally:
                    server.release_request.set()
                    service.stop(timeout=1.0)
                    client.close()



if __name__ == "__main__":
    unittest.main()
