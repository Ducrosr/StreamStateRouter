from __future__ import annotations

import threading
import unittest

from stream_state_router.obs.dispatcher import OBSDispatcher
from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.rules import RuleSet
from stream_state_router.services.runtime import RoutingService, _OBSCommand


class BlockingProgramSceneClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.scene_read_started = threading.Event()
        self.release_scene_read = threading.Event()

    def send(self, request: str, payload=None):
        row = (str(request), dict(payload or {}))
        self.calls.append(row)
        if request == "GetCurrentProgramScene":
            self.scene_read_started.set()
            if not self.release_scene_read.wait(2.0):
                raise TimeoutError("test did not release scene read")
            return {"currentProgramSceneName": "Idle"}
        if request == "CreateInput":
            return {"sceneItemId": 77}
        raise AssertionError(f"unexpected request: {request}")


class PresentationFoundationRuntimeRegressionTests(unittest.TestCase):
    def test_browser_source_create_is_rejected_if_runtime_stops_during_scene_discovery(self):
        client = BlockingProgramSceneClient()
        service = RoutingService(
            StateRouterEngine(RuleSet([]), debounce_ms=0),
            OBSDispatcher(client, {}),
            provider=object(),
        )
        with service._lock:
            service._runtime_operational = True
            service._accept_obs_commands = True
            service._stopping = False
            service._command_generation = 7

        command = _OBSCommand(
            request_id="regression-f155-02",
            generation=7,
            action="widget.browser_source.create",
            options={
                "input_name": "[SSR] Chat",
                "url": "http://127.0.0.1:8766/widgets/chat/",
                "width": 720,
                "height": 900,
            },
        )

        worker = threading.Thread(
            target=service._execute_obs_command,
            args=(command,),
            name="test-runtime-worker",
        )
        worker.start()
        self.assertTrue(client.scene_read_started.wait(1.0))

        with service._lock:
            service._accept_obs_commands = False
            service._runtime_operational = False
            service._stopping = True
            service._command_generation += 1

        client.release_scene_read.set()
        worker.join(2.0)
        self.assertFalse(worker.is_alive())

        self.assertTrue(
            any(request == "GetCurrentProgramScene" for request, _ in client.calls)
        )
        self.assertFalse(
            any(request == "CreateInput" for request, _ in client.calls),
            client.calls,
        )


if __name__ == "__main__":
    unittest.main()
