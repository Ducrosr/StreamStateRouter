from __future__ import annotations

import unittest

from stream_state_router.obs.catalog import OBSResourceCatalogReader
from stream_state_router.obs.client import OBSRequestError


class _FakeCatalogClient:
    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []

    def send(self, request, data=None):
        self.requests.append((request, data))
        responses = {
            "GetVersion": {
                "availableRequests": [
                    "GetSceneCollectionList",
                    "GetSceneList",
                    "GetGroupList",
                    "GetSceneItemList",
                    "GetGroupSceneItemList",
                    "GetInputList",
                    "GetInputSettings",
                    "GetSourceFilterList",
                    "GetSourceFilter",
                    "GetSceneTransitionList",
                    "GetVideoSettings",
                ],
            },
            "GetSceneCollectionList": {
                "currentSceneCollectionName": "Main",
            },
            "GetSceneList": {
                "currentProgramSceneName": "In Game",
                "currentProgramSceneUuid": "scene-1",
                "scenes": [
                    {"sceneName": "In Game", "sceneUuid": "scene-1", "sceneIndex": 0},
                    {"sceneName": "Nested", "sceneUuid": "scene-2", "sceneIndex": 1},
                ],
            },
            "GetGroupList": {
                "groups": ["WebCam"],
            },
            "GetInputList": {
                "inputs": [
                    {
                        "inputName": "Capture de jeu",
                        "inputKind": "game_capture",
                        "inputUuid": "input-1",
                    },
                    {
                        "inputName": "Avatar Dynamic",
                        "inputKind": "image_source",
                        "inputUuid": "input-2",
                    },
                ],
            },
            "GetSceneTransitionList": {
                "transitions": [
                    {
                        "transitionName": "Transition Mako",
                        "transitionKind": "obs_stinger_transition",
                        "transitionUuid": "transition-1",
                    }
                ],
            },
            "GetVideoSettings": {
                "baseWidth": 1920,
                "baseHeight": 1080,
            },
            "GetInputSettings": {
                "inputKind": "game_capture",
                "inputSettings": {
                    "window": "Dofus",
                    "rgb10a2_space": "srgb",
                },
            },
            "GetSourceFilterList": {
                "filters": [
                    {
                        "filterName": "Avatar FX - Swap Glitch",
                        "filterKind": "shader_filter",
                        "filterEnabled": False,
                    }
                ],
            },
            "GetSourceFilter": {
                "filterName": "Avatar FX - Swap Glitch",
                "filterKind": "shader_filter",
                "filterEnabled": False,
                "filterSettings": {"from_file": True},
            },
        }
        if request == "GetSceneItemList":
            scene = data["sceneName"]
            if scene == "In Game":
                return {
                    "sceneItems": [
                        {
                            "sourceName": "WebCam",
                            "sourceUuid": "group-1",
                            "sceneItemId": 10,
                            "sceneItemEnabled": True,
                            "isGroup": True,
                            "sourceType": "OBS_SOURCE_TYPE_SCENE",
                        },
                        {
                            "sourceName": "Camera",
                            "sourceUuid": "camera-1",
                            "sceneItemId": 11,
                            "sceneItemEnabled": True,
                            "sourceType": "OBS_SOURCE_TYPE_INPUT",
                            "inputKind": "dshow_input",
                        },
                        {
                            "sourceName": "Camera",
                            "sourceUuid": "camera-1",
                            "sceneItemId": 12,
                            "sceneItemEnabled": False,
                            "sourceType": "OBS_SOURCE_TYPE_INPUT",
                            "inputKind": "dshow_input",
                        },
                        {
                            "sourceName": "Backup Only",
                            "sceneItemId": 99,
                            "groupItemBackup": True,
                        },
                    ]
                }
            if scene == "Nested":
                return {
                    "sceneItems": [
                        {
                            "sourceName": "Avatar Dynamic",
                            "sourceUuid": "avatar-1",
                            "sceneItemId": 20,
                            "sceneItemEnabled": True,
                            "sourceType": "OBS_SOURCE_TYPE_INPUT",
                            "inputKind": "image_source",
                        }
                    ]
                }
        if request == "GetGroupSceneItemList":
            return {
                "sceneItems": [
                    {
                        "sourceName": "Avatar Dynamic",
                        "sourceUuid": "avatar-1",
                        "sceneItemId": 30,
                        "sceneItemEnabled": True,
                        "sourceType": "OBS_SOURCE_TYPE_INPUT",
                        "inputKind": "image_source",
                    }
                ]
            }
        if request in responses:
            return responses[request]
        raise AssertionError(f"Unexpected request: {request} {data!r}")


class _NoGroupsCatalogClient(_FakeCatalogClient):
    def send(self, request, data=None):
        if request == "GetGroupList":
            self.requests.append((request, data))
            raise OBSRequestError("GetGroupList", "unsupported")
        return super().send(request, data)


class _TransportFailCatalogClient(_FakeCatalogClient):
    def send(self, request, data=None):
        if request == "GetSceneList":
            raise TimeoutError("timed out")
        return super().send(request, data)


class OBSResourceCatalogTests(unittest.TestCase):
    def test_sync_builds_read_only_lightweight_catalog(self):
        client = _FakeCatalogClient()
        catalog = OBSResourceCatalogReader(client).sync()

        self.assertEqual(catalog.collection, "Main")
        self.assertEqual(catalog.current_program_scene, "In Game")
        self.assertEqual(catalog.current_program_scene_uuid, "scene-1")
        self.assertEqual(catalog.scenes[0].uuid, "scene-1")
        input_uuids = {item.name: item.uuid for item in catalog.inputs}
        self.assertEqual(input_uuids["Capture de jeu"], "input-1")
        self.assertEqual(input_uuids["Avatar Dynamic"], "input-2")
        self.assertTrue(catalog.supports("GetSceneItemList"))
        self.assertEqual(catalog.canvas, (1920, 1080))
        self.assertEqual([item.name for item in catalog.scenes], ["In Game", "Nested"])
        self.assertEqual(catalog.groups, ("WebCam",))
        self.assertEqual(len(catalog.inputs), 2)
        self.assertEqual(len(catalog.transitions), 1)

        cameras = [
            item
            for item in catalog.scene_items
            if item.container == "In Game" and item.source == "Camera"
        ]
        self.assertEqual([item.occurrence for item in cameras], [0, 1])
        self.assertEqual([item.scene_item_id for item in cameras], [11, 12])
        self.assertFalse(any(item.source == "Backup Only" for item in catalog.scene_items))

        request_names = [request for request, _data in client.requests]
        self.assertNotIn("GetInputSettings", request_names)
        self.assertNotIn("GetSourceFilterList", request_names)
        self.assertTrue(all(name.startswith("Get") for name in request_names))

    def test_transport_failure_is_not_downgraded_to_partial_catalog(self):
        client = _TransportFailCatalogClient()

        with self.assertRaises(TimeoutError):
            OBSResourceCatalogReader(client).sync()

    def test_input_and_filter_details_are_loaded_on_demand(self):
        client = _FakeCatalogClient()
        reader = OBSResourceCatalogReader(client)

        details = reader.input_details("Capture de jeu")
        self.assertEqual(details.input.kind, "game_capture")
        self.assertEqual(details.settings["rgb10a2_space"], "srgb")
        self.assertEqual(details.filters[0].name, "Avatar FX - Swap Glitch")

        filter_details = reader.filter_details(
            "Avatar Dynamic",
            "Avatar FX - Swap Glitch",
        )
        self.assertFalse(filter_details.filter.enabled)
        self.assertTrue(filter_details.settings["from_file"])

    def test_group_list_failure_falls_back_to_scene_group_discovery(self):
        client = _NoGroupsCatalogClient()

        catalog = OBSResourceCatalogReader(client).sync()

        self.assertEqual(catalog.groups, ("WebCam",))
        self.assertTrue(any("Group list unavailable" in item for item in catalog.warnings))
        self.assertTrue(
            all(request.startswith("Get") for request, _data in client.requests)
        )


if __name__ == "__main__":
    unittest.main()
