from __future__ import annotations

import unittest

from stream_state_router.obs.catalog import OBSResourceCatalogReader


class FakeClient:
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
            return {
                "currentProgramSceneName": "In Game",
                "scenes": [
                    {"sceneName": "In Game", "sceneUuid": "scene-main"},
                    {"sceneName": "Nested", "sceneUuid": "scene-nested"},
                ],
            }
        if request == "GetSceneItemList":
            if data["sceneName"] == "In Game":
                return {
                    "sceneItems": [
                        {
                            "sourceName": "Chat",
                            "sourceUuid": "chat",
                            "sourceType": "OBS_SOURCE_TYPE_INPUT",
                            "inputKind": "browser_source",
                            "sceneItemId": 10,
                            "sceneItemEnabled": True,
                            "sceneItemLocked": False,
                        },
                        {
                            "sourceName": "Chat",
                            "sourceUuid": "chat",
                            "sourceType": "OBS_SOURCE_TYPE_INPUT",
                            "inputKind": "browser_source",
                            "sceneItemId": 11,
                            "sceneItemEnabled": False,
                            "sceneItemLocked": False,
                        },
                        {
                            "sourceName": "Widgets",
                            "isGroup": True,
                            "sourceType": "OBS_SOURCE_TYPE_SCENE",
                            "sceneItemId": 12,
                        },
                        {
                            "sourceName": "Nested",
                            "sourceType": "OBS_SOURCE_TYPE_SCENE",
                            "inputKind": "scene",
                            "sceneItemId": 13,
                        },
                        {
                            "sourceName": "Backup",
                            "sceneItemId": 99,
                            "group_item_backup": True,
                        },
                    ]
                }
            return {
                "sceneItems": [
                    {
                        "sourceName": "Nested Image",
                        "sourceType": "OBS_SOURCE_TYPE_INPUT",
                        "inputKind": "image_source",
                        "sceneItemId": 20,
                    }
                ]
            }
        if request == "GetGroupSceneItemList":
            return {
                "sceneItems": [
                    {
                        "sourceName": "Grouped Browser",
                        "sourceType": "OBS_SOURCE_TYPE_INPUT",
                        "inputKind": "browser_source",
                        "sceneItemId": 30,
                    }
                ]
            }
        if request == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": "Chat",
                        "inputKind": "browser_source",
                        "inputUuid": "chat",
                    },
                    {
                        "inputName": "Avatar Dynamic",
                        "inputKind": "image_source",
                        "inputUuid": "avatar",
                    },
                ]
            }
        if request == "GetInputSettings":
            return {"inputSettings": {"file": "avatar.png"}}
        if request == "GetSourceFilterList":
            if data["sourceName"] == "Avatar Dynamic":
                return {
                    "filters": [
                        {
                            "filterName": "Avatar FX",
                            "filterKind": "shader_filter",
                            "filterEnabled": False,
                            "filterIndex": 0,
                            "filterSettings": {"strength": 1.0},
                        }
                    ]
                }
            if data["sourceName"] == "In Game":
                return {
                    "filters": [
                        {
                            "filterName": "Dofus - WebCam - MOVE",
                            "filterKind": "move_source_filter",
                            "filterEnabled": False,
                            "filterIndex": 0,
                            "filterSettings": {"duration": 450},
                        }
                    ]
                }
            return {"filters": []}
        if request == "GetSourceFilter":
            return {"filterSettings": {"strength": 1.0}}
        if request == "GetSceneTransitionList":
            return {
                "transitions": [
                    {
                        "transitionName": "Transition Mako",
                        "transitionKind": "obs_stinger_transition",
                    }
                ]
            }
        if request == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        raise AssertionError(f"Unexpected OBS request: {request}")


class OBSResourceCatalogTests(unittest.TestCase):
    def test_sync_discovers_resources_without_mutations(self):
        client = FakeClient()
        catalog = OBSResourceCatalogReader(client).sync(include_settings=True)

        self.assertEqual(catalog.scene_collection, "Midgar")
        self.assertEqual(catalog.current_program_scene, "In Game")
        self.assertEqual((catalog.canvas_width, catalog.canvas_height), (1920, 1080))
        self.assertEqual([scene.name for scene in catalog.scenes], ["In Game", "Nested"])
        chat_occurrences = [
            item.occurrence for item in catalog.scene_items if item.source_name == "Chat"
        ]
        self.assertEqual(chat_occurrences, [1, 2])
        self.assertTrue(any(item.source_kind == "group" for item in catalog.scene_items))
        self.assertTrue(any(item.source_kind == "scene" for item in catalog.scene_items))
        self.assertTrue(any(item.container == "Widgets" for item in catalog.scene_items))
        self.assertFalse(any(item.source_name == "Backup" for item in catalog.scene_items))
        avatar = next(item for item in catalog.inputs if item.name == "Avatar Dynamic")
        self.assertEqual(avatar.settings, {"file": "avatar.png"})
        avatar_filter = next(item for item in catalog.filters if item.name == "Avatar FX")
        self.assertEqual(avatar_filter.settings, {"strength": 1.0})
        move_filter = next(
            item for item in catalog.filters if item.name == "Dofus - WebCam - MOVE"
        )
        self.assertEqual(move_filter.source_name, "In Game")
        self.assertEqual(move_filter.settings, {"duration": 450})
        self.assertEqual(catalog.transitions[0].name, "Transition Mako")
        self.assertGreater(catalog.requests_used, 0)
        self.assertFalse(
            any(
                request.startswith(("Set", "Create", "Remove"))
                for request, _data in client.calls
            )
        )

    def test_nested_scene_is_not_recursively_duplicated_per_parent(self):
        client = FakeClient()
        catalog = OBSResourceCatalogReader(client).sync()
        nested_images = [
            item for item in catalog.scene_items if item.source_name == "Nested Image"
        ]
        self.assertEqual(len(nested_images), 1)
        self.assertEqual(nested_images[0].root_scene, "Nested")

    def test_default_sync_does_not_fetch_detailed_input_settings(self):
        client = FakeClient()
        OBSResourceCatalogReader(client).sync(include_settings=False)
        requests = [request for request, _data in client.calls]
        self.assertNotIn("GetInputSettings", requests)
        self.assertNotIn("GetSourceFilter", requests)

    def test_group_children_are_cached_when_group_is_reused(self):
        client = FakeClient()
        original_send = client.send

        def send(request, data=None):
            if request == "GetSceneItemList" and data["sceneName"] == "Nested":
                client.calls.append((request, data))
                return {
                    "sceneItems": [
                        {
                            "sourceName": "Widgets",
                            "isGroup": True,
                            "sourceType": "OBS_SOURCE_TYPE_SCENE",
                            "sceneItemId": 21,
                        }
                    ]
                }
            return original_send(request, data)

        client.send = send
        catalog = OBSResourceCatalogReader(client).sync()
        group_reads = [
            request for request, _data in client.calls if request == "GetGroupSceneItemList"
        ]
        self.assertEqual(len(group_reads), 1)
        grouped = [
            item for item in catalog.scene_items if item.source_name == "Grouped Browser"
        ]
        self.assertEqual(len(grouped), 2)
        self.assertEqual({item.root_scene for item in grouped}, {"In Game", "Nested"})
        self.assertEqual(grouped[0].identity, grouped[1].identity)

    def test_sync_yields_before_each_obs_request(self):
        client = FakeClient()
        checkpoints = []

        def checkpoint():
            checkpoints.append(client.request_count)

        OBSResourceCatalogReader(
            client,
            cooperative_yield=checkpoint,
        ).sync()

        self.assertEqual(len(checkpoints), client.request_count)

    def test_sync_can_be_cancelled_before_next_obs_request(self):
        client = FakeClient()
        checkpoints = []

        def checkpoint():
            checkpoints.append(client.request_count)
            if len(checkpoints) == 3:
                raise RuntimeError("shutdown requested")

        with self.assertRaisesRegex(RuntimeError, "shutdown requested"):
            OBSResourceCatalogReader(
                client,
                cooperative_yield=checkpoint,
            ).sync()

        self.assertEqual(client.request_count, 2)


if __name__ == "__main__":
    unittest.main()
