from __future__ import annotations

import unittest

from stream_state_router.planning import (
    DesiredAssignment,
    DesiredState,
    PropertyKey,
)
from stream_state_router.services.declarative import (
    ControlScope,
    DeclarativePlanningService,
)


class _ServiceClient:
    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []

    def send(self, request, data=None):
        self.requests.append((request, data))
        if request == "GetVersion":
            return {
                "availableRequests": [
                    "GetSceneCollectionList",
                    "GetSceneList",
                    "GetGroupList",
                    "GetSceneItemList",
                    "GetGroupSceneItemList",
                    "GetInputList",
                    "GetSceneTransitionList",
                    "GetVideoSettings",
                    "GetCurrentProgramScene",
                    "GetSceneItemId",
                    "GetSceneItemEnabled",
                    "GetInputSettings",
                    "GetInputMute",
                    "GetInputVolume",
                    "GetSourceFilter",
                    "GetSourceFilterList",
                ]
            }
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": "Main"}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "In Game",
                "currentProgramSceneUuid": "scene-1",
                "scenes": [
                    {
                        "sceneName": "In Game",
                        "sceneUuid": "scene-1",
                        "sceneIndex": 0,
                    }
                ],
            }
        if request == "GetGroupList":
            return {"groups": []}
        if request == "GetSceneItemList":
            return {
                "sceneItems": [
                    {
                        "sourceName": "Chat",
                        "sourceUuid": "chat-1",
                        "sceneItemId": 7,
                        "sceneItemEnabled": True,
                        "sourceType": "OBS_SOURCE_TYPE_SCENE",
                        "inputKind": "scene",
                    }
                ]
            }
        if request == "GetInputList":
            return {"inputs": []}
        if request == "GetSceneTransitionList":
            return {"transitions": []}
        if request == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        if request == "GetSceneItemId":
            return {"sceneItemId": 7}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": True}
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "In Game"}
        if request == "GetSourceFilterList":
            return {
                "filters": [
                    {
                        "filterName": "Existing Filter",
                        "filterKind": "shader_filter",
                        "filterEnabled": True,
                    }
                ]
            }
        raise AssertionError(f"Unexpected request: {request}")


class DeclarativePlanningServiceTests(unittest.TestCase):
    def test_dry_run_reuses_catalog_until_explicit_refresh(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="Layout Overwatch")]
        )
        client = _ServiceClient()
        service = DeclarativePlanningService(client)

        first = service.dry_run(desired)
        structural_after_first = [
            request
            for request, _data in client.requests
            if request
            in {
                "GetVersion",
                "GetSceneCollectionList",
                "GetSceneList",
                "GetGroupList",
                "GetSceneItemList",
                "GetGroupSceneItemList",
                "GetInputList",
                "GetSceneTransitionList",
                "GetVideoSettings",
            }
        ]
        second = service.dry_run(desired)
        structural_after_second = [
            request
            for request, _data in client.requests
            if request
            in {
                "GetVersion",
                "GetSceneCollectionList",
                "GetSceneList",
                "GetGroupList",
                "GetSceneItemList",
                "GetGroupSceneItemList",
                "GetInputList",
                "GetSceneTransitionList",
                "GetVideoSettings",
            }
        ]

        self.assertEqual(len(first.operations), 1)
        self.assertEqual(first.operations[0].operation, "SetSceneItemVisibility")
        self.assertEqual(first.as_mapping(), second.as_mapping())
        self.assertEqual(structural_after_first, structural_after_second)
        self.assertEqual(
            [r for r, _d in client.requests].count("GetSceneItemEnabled"),
            2,
        )
        self.assertTrue(
            all(request.startswith("Get") for request, _data in client.requests)
        )

        before_refresh = len(structural_after_second)
        service.dry_run(desired, refresh_catalog=True)
        structural_after_refresh = [
            request
            for request, _data in client.requests
            if request
            in {
                "GetVersion",
                "GetSceneCollectionList",
                "GetSceneList",
                "GetGroupList",
                "GetSceneItemList",
                "GetGroupSceneItemList",
                "GetInputList",
                "GetSceneTransitionList",
                "GetVideoSettings",
            }
        ]
        self.assertGreater(len(structural_after_refresh), before_refresh)

    def test_blank_collection_intent_is_bound_to_catalog_collection(self):
        key = PropertyKey.scene_item_visibility(
            collection="",
            container="In Game",
            source="Chat",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="test")]
        )
        service = DeclarativePlanningService(_ServiceClient())

        plan = service.plan_state(desired)

        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].key.collection, "Main")

    def test_missing_resource_is_blocked_before_targeted_observation(self):
        key = PropertyKey.input_setting(
            collection="Main",
            input_name="Missing Input",
            setting="text",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, "secret", provenance="test")]
        )
        client = _ServiceClient()
        service = DeclarativePlanningService(client)

        plan = service.plan_state(desired)

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diff[0].status, "blocked")
        self.assertEqual(plan.diagnostics[0].code, "input_missing")
        self.assertFalse(
            any(request == "GetInputSettings" for request, _data in client.requests)
        )

    def test_condition_blocked_filter_skips_filter_discovery_and_read(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Chat",
            filter_name="Existing Filter",
        )
        desired = DesiredState.build(
            [
                DesiredAssignment.create(
                    key,
                    True,
                    provenance="game:Conditional",
                )
            ]
        )
        client = _ServiceClient()
        service = DeclarativePlanningService(client)

        plan = service.plan_state(
            desired,
            blocked_provenance={
                "game:Conditional": "conditions OBS non satisfaites"
            },
        )

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "condition_blocked")
        self.assertFalse(
            any(
                request in {"GetSourceFilterList", "GetSourceFilter"}
                for request, _data in client.requests
            )
        )

    def test_missing_filter_is_blocked_before_filter_read(self):
        key = PropertyKey.filter_enabled(
            collection="Main",
            source="Chat",
            filter_name="Missing Filter",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, True, provenance="test")]
        )
        client = _ServiceClient()
        service = DeclarativePlanningService(client)

        plan = service.plan_state(desired)

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "filter_missing")
        self.assertTrue(
            any(request == "GetSourceFilterList" for request, _data in client.requests)
        )
        self.assertFalse(
            any(request == "GetSourceFilter" for request, _data in client.requests)
        )

    def test_container_delegation_does_not_block_external_occurrence(self):
        scope = ControlScope(
            excluded_containers=frozenset({"External Component"})
        )
        internal = PropertyKey.scene_item_visibility(
            collection="Main",
            container="External Component",
            source="Internal Item",
        )
        external = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="External Component",
        )

        self.assertTrue(scope.block_reason(internal))
        self.assertEqual(scope.block_reason(external), "")

    def test_generic_control_scope_blocks_delegated_container(self):
        key = PropertyKey.scene_item_visibility(
            collection="Main",
            container="In Game",
            source="Chat",
        )
        desired = DesiredState.build(
            [DesiredAssignment.create(key, False, provenance="test")]
        )
        client = _ServiceClient()
        service = DeclarativePlanningService(
            client,
            scope=ControlScope(excluded_containers=frozenset({"In Game"})),
        )

        plan = service.plan_state(desired)

        self.assertTrue(plan.blocked)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.diagnostics[0].code, "resource_out_of_scope")

    def test_catalog_invalidation_forces_resync(self):
        client = _ServiceClient()
        service = DeclarativePlanningService(client)

        service.sync_catalog()
        previous = len(client.requests)
        service.invalidate_catalog()
        service.dry_run(DesiredState.empty())

        self.assertGreater(len(client.requests), previous)


if __name__ == "__main__":
    unittest.main()
