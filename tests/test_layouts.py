from __future__ import annotations

import unittest
from unittest.mock import patch

from stream_state_router.obs.layouts import OBSLayoutManager, split_module_source


class FakeLayoutClient:
    def __init__(self):
        self.calls = []
        self.transforms = {
            1: {
                "positionX": 100.0,
                "positionY": 200.0,
                "width": 200.0,
                "height": 100.0,
                "scaleX": 1.0,
                "scaleY": 1.0,
                "alignment": 5,
                "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_NONE",
            },
            2: {
                "positionX": 300.0,
                "positionY": 200.0,
                "width": 100.0,
                "height": 100.0,
                "scaleX": 1.0,
                "scaleY": 1.0,
                "alignment": 5,
                "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_NONE",
            },
        }
        self.items = {
            "[Webcam] Cadre": 1,
            "[Webcam] Avatar": 2,
            "Unrelated": 3,
            "[Webcam:locked] Permanent": 4,
        }

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "Gameplay",
                "scenes": [{"sceneName": "Gameplay"}, {"sceneName": "Pause"}],
            }
        if request == "GetSceneItemList":
            return {
                "sceneItems": [
                    {"sourceName": "[Webcam] Cadre", "sceneItemId": 1, "sceneItemEnabled": True},
                    {"sourceName": "[Webcam] Avatar", "sceneItemId": 2, "sceneItemEnabled": True},
                    {"sourceName": "Unrelated", "sceneItemId": 3, "sceneItemEnabled": True},
                    {"sourceName": "[Webcam:locked] Permanent", "sceneItemId": 4, "sceneItemEnabled": True},
                ]
            }
        if request == "GetSceneItemTransform":
            return {"sceneItemTransform": dict(self.transforms[int(payload["sceneItemId"])])}
        if request == "GetSceneItemId":
            return {"sceneItemId": self.items.get(payload["sourceName"], 0)}
        if request in {"SetSceneItemTransform", "SetSceneItemEnabled"}:
            return {}
        raise AssertionError(f"Unexpected request: {request}")


class LayoutTests(unittest.TestCase):
    def test_module_name_parser(self):
        self.assertEqual(split_module_source("[Webcam] Cadre"), ("Webcam", "Cadre"))
        self.assertIsNone(split_module_source("Webcam Cadre"))
        self.assertIsNone(split_module_source("[Webcam]"))

    def test_discovery_keeps_each_source_as_a_distinct_module(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        modules = manager.discover_scene("Gameplay")
        self.assertEqual(list(modules), ["[Webcam] Avatar", "[Webcam] Cadre"])
        self.assertEqual(modules["[Webcam] Avatar"][0].module, "Webcam")
        self.assertEqual(modules["[Webcam] Avatar"][0].element, "Avatar")


    def test_locked_convention_is_excluded_from_discovery_and_recapture(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        modules = manager.discover_scene("Gameplay")
        self.assertNotIn("[Webcam:locked] Permanent", modules)

        profile = manager.capture_profile("Gameplay")
        self.assertNotIn("[Webcam:locked] Permanent", profile["modules"])
        self.assertFalse(
            any(
                request == "GetSceneItemTransform" and payload.get("sceneItemId") == 4
                for request, payload in client.calls
            )
        )

    def test_capture_stores_each_obs_source_as_its_own_module(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        profile = manager.capture_profile("Gameplay")
        cadre = profile["modules"]["[Webcam] Cadre"]
        avatar = profile["modules"]["[Webcam] Avatar"]
        self.assertEqual(cadre["module_type"], "Webcam")
        self.assertEqual(cadre["display_name"], "Cadre")
        self.assertEqual(cadre["base_bounds"], {
            "x": 100.0,
            "y": 200.0,
            "width": 200.0,
            "height": 100.0,
        })
        self.assertEqual(cadre["geometry"], cadre["base_bounds"])
        self.assertTrue(cadre["visible"])
        self.assertEqual(len(cadre["elements"]), 1)
        self.assertEqual(len(avatar["elements"]), 1)

    def test_apply_resizes_and_moves_only_selected_module(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Cadre"]["geometry"] = {
            "x": 200.0,
            "y": 300.0,
            "width": 400.0,
            "height": 200.0,
        }
        client.calls.clear()

        result = manager.apply_profile(profile)

        self.assertEqual(result.elements_applied, 2)
        transform_calls = [payload for request, payload in client.calls if request == "SetSceneItemTransform"]
        self.assertEqual(len(transform_calls), 2)
        by_id = {call["sceneItemId"]: call["sceneItemTransform"] for call in transform_calls}
        self.assertEqual(by_id[1]["positionX"], 200.0)
        self.assertEqual(by_id[1]["positionY"], 300.0)
        self.assertEqual(by_id[1]["scaleX"], 2.0)
        self.assertEqual(by_id[1]["scaleY"], 2.0)
        self.assertEqual(by_id[2]["positionX"], 300.0)
        self.assertEqual(by_id[2]["positionY"], 200.0)


    def test_apply_refreshes_stale_scene_item_ids_after_one_source_is_deleted(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")

        # Simulate a structural OBS edit after capture: one source is deleted and
        # OBS assigns a new scene-item id to the remaining source. The manager
        # still has the old ids in its discovery cache. Only the deleted source
        # should be reported missing.
        del client.items["[Webcam] Cadre"]
        client.transforms.pop(1, None)
        avatar_transform = client.transforms.pop(2)
        client.items["[Webcam] Avatar"] = 20
        client.transforms[20] = avatar_transform
        client.calls.clear()

        result = manager.apply_profile(profile)

        self.assertEqual(result.elements_applied, 1)
        self.assertEqual(result.elements_skipped, 1)
        self.assertEqual(result.missing_sources, ("[Webcam] Cadre",))
        refreshed_ids = [
            payload
            for request, payload in client.calls
            if request == "GetSceneItemId" and payload.get("sourceName") == "[Webcam] Avatar"
        ]
        self.assertTrue(refreshed_ids)

    def test_apply_does_not_trust_a_stale_id_reused_by_another_source(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")

        # OBS structural edits can reuse a numeric scene-item id instead of
        # merely making it invalid. In that case retry-on-error is insufficient:
        # GetSceneItemTransform(old_id) succeeds, but refers to the wrong source.
        # Avatar used to be id 2; after the edit id 2 belongs to another source
        # and Avatar moved to id 20. A fresh apply must resolve Avatar by name.
        avatar_transform = dict(client.transforms[2])
        client.items["[Webcam] Avatar"] = 20
        client.transforms[20] = avatar_transform
        client.transforms[2] = {
            "positionX": 999.0,
            "positionY": 999.0,
            "width": 10.0,
            "height": 10.0,
            "scaleX": 1.0,
            "scaleY": 1.0,
            "alignment": 5,
            "rotation": 0.0,
            "boundsType": "OBS_BOUNDS_NONE",
        }
        client.calls.clear()

        result = manager.apply_profile(profile)

        self.assertEqual(result.elements_applied, 2)
        avatar_resolutions = [
            payload
            for request, payload in client.calls
            if request == "GetSceneItemId" and payload.get("sourceName") == "[Webcam] Avatar"
        ]
        self.assertTrue(avatar_resolutions)
        avatar_reads = [
            payload["sceneItemId"]
            for request, payload in client.calls
            if request == "GetSceneItemTransform" and payload.get("sceneItemId") in {2, 20}
        ]
        self.assertIn(20, avatar_reads)
        self.assertNotEqual(avatar_reads[0], 2)

    def test_move_transition_uses_one_global_timeline_for_all_sources(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["transition"] = {"mode": "move", "duration_ms": 2000, "steps": 8}
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
        profile["modules"]["[Webcam] Avatar"]["geometry"]["x"] = 700.0
        client.calls.clear()

        with patch("stream_state_router.obs.layouts.time.sleep") as sleep_mock:
            result = manager.apply_profile(profile, record_undo=False)

        self.assertEqual(result.elements_applied, 2)
        # Eight animation frames share one clock. The old implementation slept
        # seven times per source (14 sleeps for two items, ~4 s instead of 2 s).
        self.assertEqual(sleep_mock.call_count, 7)
        transform_calls = [
            payload for request, payload in client.calls if request == "SetSceneItemTransform"
        ]
        self.assertEqual(len(transform_calls), 16)

    def test_excluded_module_element_is_left_untouched(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Avatar"]["elements"][0]["included"] = False
        client.calls.clear()

        result = manager.apply_profile(profile)

        self.assertEqual(result.elements_applied, 1)
        self.assertEqual(result.elements_skipped, 1)
        changed_ids = {
            payload["sceneItemId"]
            for request, payload in client.calls
            if request == "SetSceneItemTransform"
        }
        self.assertEqual(changed_ids, {1})

    def test_runtime_visibility_owner_keeps_geometry_but_never_applies_visibility(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        # Capture an old-style profile before the runtime owner is registered.
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Avatar"]["visible"] = True
        profile["modules"]["[Webcam] Avatar"]["elements"][0]["enabled"] = True

        manager.set_runtime_visibility_owners({("Gameplay", "[Webcam] Avatar")})
        client.calls.clear()
        manager.apply_profile(profile, record_undo=False)

        # Geometry remains LayoutProfile-owned.
        transform_ids = [
            payload["sceneItemId"]
            for request, payload in client.calls
            if request == "SetSceneItemTransform"
        ]
        self.assertIn(2, transform_ids)

        # Runtime-owned visibility is not touched, even for a pre-v5 profile.
        visibility_ids = [
            payload["sceneItemId"]
            for request, payload in client.calls
            if request == "SetSceneItemEnabled"
        ]
        self.assertNotIn(2, visibility_ids)

    def test_capture_marks_runtime_visibility_as_non_layout_owned(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        manager.set_runtime_visibility_owners({("Gameplay", "[Webcam] Avatar")})

        profile = manager.capture_profile("Gameplay")
        element = profile["modules"]["[Webcam] Avatar"]["elements"][0]

        self.assertFalse(element["follow_visibility"])
        self.assertFalse(element["enabled"])
        self.assertEqual(element["visibility_owner"], "runtime")

    def test_activation_visibility_resolves_fresh_id_even_if_stale_id_still_works(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        # Seed the layout cache with Avatar=id 2.
        manager.discover_scene("Gameplay")
        self.assertEqual(manager._scene_item_cache[("Gameplay", "[Webcam] Avatar")], 2)

        # OBS structurally changes: id 2 is still a valid scene item, but Avatar
        # moved to id 20. A retry-on-error strategy would mutate the wrong item.
        client.items["[Webcam] Avatar"] = 20
        client.calls.clear()

        manager.set_activation_item_enabled(
            "Gameplay",
            "[Webcam] Avatar",
            False,
            container_kind="scene",
        )

        resolutions = [
            payload
            for request, payload in client.calls
            if request == "GetSceneItemId"
            and payload.get("sourceName") == "[Webcam] Avatar"
        ]
        mutations = [
            payload
            for request, payload in client.calls
            if request == "SetSceneItemEnabled"
        ]
        self.assertTrue(resolutions)
        self.assertEqual(mutations[-1]["sceneItemId"], 20)
        self.assertNotEqual(mutations[-1]["sceneItemId"], 2)

    def test_persisted_runtime_visibility_owner_survives_policy_owner_removal(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        manager.set_runtime_visibility_owners({("Gameplay", "[Webcam] Avatar")})
        profile = manager.capture_profile("Gameplay")
        element = profile["modules"]["[Webcam] Avatar"]["elements"][0]
        self.assertEqual(element["visibility_owner"], "runtime")

        # Characterization only: removing the live policy ownership set does
        # not silently turn a persisted runtime marker back into layout-owned
        # visibility. This semantic remains intentionally unchanged.
        manager.set_runtime_visibility_owners(set())
        self.assertTrue(
            manager.runtime_visibility_owned(
                "Gameplay",
                "[Webcam] Avatar",
                element,
            )
        )

        manager.client.calls.clear()
        manager.apply_profile(profile, record_undo=False)
        visibility_ids = [
            payload["sceneItemId"]
            for request, payload in manager.client.calls
            if request == "SetSceneItemEnabled"
        ]
        self.assertNotIn(2, visibility_ids)

    def test_preview_and_undo_preserve_runtime_owned_visibility(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        manager.set_runtime_visibility_owners({("Gameplay", "[Webcam] Avatar")})
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Avatar"]["geometry"]["x"] += 10.0

        client.calls.clear()
        manager.preview_profile(profile)
        manager.commit_preview()
        manager.undo_last()

        avatar_visibility = [
            payload
            for request, payload in client.calls
            if request == "SetSceneItemEnabled"
            and payload.get("sceneItemId") == 2
        ]
        self.assertEqual(avatar_visibility, [])



if __name__ == "__main__":
    unittest.main()
