from __future__ import annotations

import unittest

from stream_state_router.obs.catalog import (
    InputRef,
    OBSResourceCatalog,
    SceneItemRef,
    SceneRef,
)
from stream_state_router.obs.observed import (
    build_execution_bindings,
    observe_desired_state,
)
from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    PropertyKey,
)


class _ObservedClient:
    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []
        self.scene_items = [
            {
                "sourceName": "Chat",
                "sourceUuid": "chat-1",
                "sceneItemId": 7,
                "sceneItemEnabled": True,
            }
        ]

    def send(self, request, data=None):
        self.requests.append((request, data))
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "In Game"}
        if request == "GetSceneItemList":
            return {"sceneItems": list(self.scene_items)}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": True}
        if request == "GetInputMute":
            return {"inputMuted": True}
        if request == "GetInputVolume":
            return {"inputVolumeDb": -7.5}
        if request == "GetInputSettings":
            return {
                "inputSettings": {
                    "window": "Overwatch",
                    "rgb10a2_space": "2100pq",
                }
            }
        if request == "GetSourceFilter":
            return {
                "filterEnabled": False,
                "filterSettings": {
                    "alpha_percent": 100,
                },
            }
        raise AssertionError(request)


def _catalog() -> OBSResourceCatalog:
    return OBSResourceCatalog(
        collection="Main",
        current_program_scene="In Game",
        current_program_scene_uuid="scene-1",
        canvas=(1920, 1080),
        scenes=(),
        groups=(),
        scene_items=(
            SceneItemRef(
                collection="Main",
                root_scene="In Game",
                container="In Game",
                container_kind="scene",
                path=("In Game",),
                source="Chat",
                source_uuid="chat-1",
                source_kind="scene",
                occurrence=0,
                scene_item_id=7,
                enabled=True,
            ),
        ),
        inputs=(),
        transitions=(),
    )


class ObservedStateReaderTests(unittest.TestCase):
    def test_reads_only_properties_requested_by_desired_state(self):
        visibility = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        program_scene = PropertyKey.program_scene(collection="Main")
        input_mute = PropertyKey.input_mute(
            collection="Main",
            input_name="Mic",
        )
        input_volume = PropertyKey.input_volume_db(
            collection="Main",
            input_name="Music",
        )
        input_setting = PropertyKey.input_setting(
            collection="Main",
            input_name="Capture de jeu",
            setting="rgb10a2_space",
        )
        filter_enabled = PropertyKey.filter_enabled(
            collection="Main",
            source="Avatar Dynamic",
            filter_name="Swap Glitch",
        )
        filter_setting = PropertyKey.filter_setting(
            collection="Main",
            source="Lost Signal",
            filter_name="Shader",
            setting="alpha_percent",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(program_scene, "Other"),
                DesiredAssignment.create(visibility, False),
                DesiredAssignment.create(input_mute, False),
                DesiredAssignment.create(input_volume, -12.0),
                DesiredAssignment.create(input_setting, "srgb"),
                DesiredAssignment.create(filter_enabled, True),
                DesiredAssignment.create(filter_setting, 80),
            ]
        )
        client = _ObservedClient()

        observed = observe_desired_state(client, _catalog(), desired)

        self.assertEqual(observed.get(program_scene).value, "In Game")
        self.assertTrue(observed.get(visibility).known)
        self.assertTrue(observed.get(visibility).value)
        self.assertTrue(observed.get(input_mute).value)
        self.assertEqual(observed.get(input_volume).value, -7.5)
        self.assertEqual(observed.get(input_setting).value, "2100pq")
        self.assertFalse(observed.get(filter_enabled).value)
        self.assertEqual(observed.get(filter_setting).value, 100)
        self.assertEqual(
            [request for request, _data in client.requests].count("GetInputSettings"),
            1,
        )
        self.assertEqual(
            [request for request, _data in client.requests].count("GetSourceFilter"),
            2,
        )
        self.assertTrue(
            all(request.startswith("Get") for request, _data in client.requests)
        )
        self.assertEqual(
            [request for request, _data in client.requests].count("GetSceneItemList"),
            1,
        )
        self.assertNotIn(
            "GetSceneItemId",
            [request for request, _data in client.requests],
        )

    def test_non_boolean_executor_observations_are_unknown(self):
        mute = PropertyKey.input_mute(
            collection="Main",
            input_name="Mic",
        )
        visibility = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(mute, True),
                DesiredAssignment.create(visibility, False),
            ]
        )
        client = _ObservedClient()
        original_send = client.send

        def send(request, data=None):
            if request == "GetInputMute":
                client.requests.append((request, data))
                return {"inputMuted": 1}
            if request == "GetSceneItemEnabled":
                client.requests.append((request, data))
                return {"sceneItemEnabled": "true"}
            return original_send(request, data)

        client.send = send
        observed = observe_desired_state(client, _catalog(), desired)

        self.assertFalse(observed.get(mute).known)
        self.assertEqual(observed.get(mute).code, "input_mute_unknown")
        self.assertFalse(observed.get(visibility).known)
        self.assertEqual(
            observed.get(visibility).code,
            "scene_item_visibility_unknown",
        )

    def test_execution_bindings_require_scene_and_input_uuids(self):
        visibility = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        mute = PropertyKey.input_mute(
            collection="Main",
            input_name="Mic",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(visibility, False),
                DesiredAssignment.create(mute, True),
            ]
        )
        base = _catalog()
        catalog = OBSResourceCatalog(
            collection=base.collection,
            current_program_scene=base.current_program_scene,
            current_program_scene_uuid=base.current_program_scene_uuid,
            canvas=base.canvas,
            scenes=(SceneRef("In Game", "scene-1", 0),),
            groups=(),
            scene_items=base.scene_items,
            inputs=(InputRef("Mic", "wasapi_input_capture", "mic-1"),),
            transitions=(),
        )

        bindings = build_execution_bindings(catalog, desired)

        self.assertEqual(len(bindings), 2)
        self.assertEqual(
            {binding.key for binding in bindings},
            {visibility, mute},
        )

    def test_same_filter_is_read_once_for_multiple_properties(self):
        enabled = PropertyKey.filter_enabled(
            collection="Main",
            source="Lost Signal",
            filter_name="Shader",
        )
        alpha = PropertyKey.filter_setting(
            collection="Main",
            source="Lost Signal",
            filter_name="Shader",
            setting="alpha_percent",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(enabled, True),
                DesiredAssignment.create(alpha, 80),
            ]
        )
        client = _ObservedClient()

        observe_desired_state(client, _catalog(), desired)

        self.assertEqual(
            [request for request, _data in client.requests].count("GetSourceFilter"),
            1,
        )

    def test_collection_mismatch_is_unknown_without_obs_request(self):
        key = PropertyKey.input_setting(
            collection="Other",
            input_name="Capture de jeu",
            setting="window",
        )
        desired = DesiredState.build([DesiredAssignment.create(key, "Dofus")])
        client = _ObservedClient()

        observed = observe_desired_state(client, _catalog(), desired)

        self.assertFalse(observed.get(key).known)
        self.assertEqual(client.requests, [])

    def test_layout_is_left_unknown_for_layout_manager_integration(self):
        key = PropertyKey.layout_profile(collection="Main", scene="In Game")
        desired = DesiredState.build([DesiredAssignment.create(key, "Dofus")])
        client = _ObservedClient()

        observed = observe_desired_state(client, _catalog(), desired)

        self.assertFalse(observed.get(key).known)
        self.assertEqual(client.requests, [])


    def test_reordered_duplicate_occurrence_is_reported_stale(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Camera",
            occurrence=0,
        )
        catalog = OBSResourceCatalog(
            collection="Main",
            current_program_scene="In Game",
            current_program_scene_uuid="scene-1",
            canvas=(1920, 1080),
            scenes=(),
            groups=(),
            scene_items=(
                SceneItemRef(
                    collection="Main",
                    root_scene="In Game",
                    container="In Game",
                    container_kind="scene",
                    path=("In Game",),
                    source="Camera",
                    source_uuid="camera-1",
                    source_kind="input",
                    occurrence=0,
                    scene_item_id=7,
                    enabled=True,
                ),
                SceneItemRef(
                    collection="Main",
                    root_scene="In Game",
                    container="In Game",
                    container_kind="scene",
                    path=("In Game",),
                    source="Camera",
                    source_uuid="camera-1",
                    source_kind="input",
                    occurrence=1,
                    scene_item_id=8,
                    enabled=False,
                ),
            ),
            inputs=(),
            transitions=(),
        )
        client = _ObservedClient()
        client.scene_items = [
            {
                "sourceName": "Camera",
                "sourceUuid": "camera-1",
                "sceneItemId": 8,
                "sceneItemEnabled": False,
            },
            {
                "sourceName": "Camera",
                "sourceUuid": "camera-1",
                "sceneItemId": 7,
                "sceneItemEnabled": True,
            },
        ]
        desired = DesiredState.build([DesiredAssignment.create(key, False)])

        observed = observe_desired_state(client, catalog, desired)

        self.assertFalse(observed.get(key).known)
        self.assertEqual(observed.get(key).code, "stale_reference")
        self.assertFalse(
            any(request == "GetSceneItemEnabled" for request, _data in client.requests)
        )

    def test_scene_item_id_zero_is_a_valid_bound_identity(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        base = _catalog()
        catalog = OBSResourceCatalog(
            collection=base.collection,
            current_program_scene=base.current_program_scene,
            current_program_scene_uuid=base.current_program_scene_uuid,
            canvas=base.canvas,
            scenes=base.scenes,
            groups=base.groups,
            scene_items=(
                SceneItemRef(
                    collection="Main",
                    root_scene="In Game",
                    container="In Game",
                    container_kind="scene",
                    path=("In Game",),
                    source="Chat",
                    source_uuid="chat-1",
                    source_kind="scene",
                    occurrence=0,
                    scene_item_id=0,
                    enabled=True,
                ),
            ),
            inputs=base.inputs,
            transitions=base.transitions,
        )
        client = _ObservedClient()
        client.scene_items[0]["sceneItemId"] = 0
        desired = DesiredState.build([DesiredAssignment.create(key, False)])

        observed = observe_desired_state(client, catalog, desired)

        self.assertTrue(observed.get(key).known)
        enabled_request = next(
            data
            for request, data in client.requests
            if request == "GetSceneItemEnabled"
        )
        self.assertEqual(enabled_request["sceneItemId"], 0)

if __name__ == "__main__":
    unittest.main()
