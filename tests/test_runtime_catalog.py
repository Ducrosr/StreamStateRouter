from __future__ import annotations

import unittest
from types import SimpleNamespace

from stream_state_router.services.runtime import RoutingService, _OBSCommand


class FakeCatalogClient:
    def __init__(self):
        self.calls = []

    @property
    def request_count(self):
        return len(self.calls)

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": "Midgar"}
        if request == "GetSceneList":
            return {"currentProgramSceneName": "In Game", "scenes": []}
        if request == "GetInputList":
            return {"inputs": []}
        if request == "GetSceneTransitionList":
            return {"transitions": []}
        if request == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        raise AssertionError(f"Unexpected request: {request}")


class RuntimeCatalogTests(unittest.TestCase):
    def test_catalog_sync_command_is_read_only_and_cached(self):
        client = FakeCatalogClient()
        dispatcher = SimpleNamespace(client=client)
        service = RoutingService(SimpleNamespace(), dispatcher)
        command = _OBSCommand(
            request_id="catalog-test",
            generation=0,
            action="catalog.sync",
            options={"include_settings": False},
        )

        service._execute_obs_command(command)

        snapshot = service.catalog_snapshot()
        self.assertEqual(snapshot["scene_collection"], "Midgar")
        self.assertEqual(snapshot["canvas"], {"width": 1920, "height": 1080})
        summary = service.catalog_summary()
        self.assertEqual(summary["scene_count"], 0)
        self.assertEqual(summary["requests_used"], 5)
        self.assertFalse(
            any(
                request.startswith(("Set", "Create", "Remove"))
                for request, _data in client.calls
            )
        )
        status = service.command_status("catalog-test")
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["scene_collection"], "Midgar")
        self.assertEqual(status["result"]["scene_count"], 0)
        self.assertTrue(status["result"]["complete"])
        self.assertNotIn("scenes", status["result"])

    def test_catalog_warnings_do_not_turn_read_only_sync_into_write_failure(self):
        class PartialClient(FakeCatalogClient):
            def send(self, request, data=None):
                if request == "GetSceneCollectionList":
                    self.calls.append((request, data))
                    raise RuntimeError("collection unavailable")
                return super().send(request, data)

        client = PartialClient()
        service = RoutingService(SimpleNamespace(), SimpleNamespace(client=client))
        command = _OBSCommand(
            request_id="catalog-partial",
            generation=0,
            action="catalog.sync",
            options={},
        )

        service._execute_obs_command(command)

        status = service.command_status("catalog-partial")
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "completed")
        self.assertTrue(status["result"]["warnings"])
        self.assertFalse(status["result"]["complete"])
        self.assertFalse(
            any(
                request.startswith(("Set", "Create", "Remove"))
                for request, _data in client.calls
            )
        )


if __name__ == "__main__":
    unittest.main()
