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
        self.enabled = {1: True, 2: True, 3: True, 4: True}

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": getattr(self, "scene_collection", "Collection A")}
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
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": self.enabled.get(int(payload["sceneItemId"]), True)}
        if request == "SetSceneItemTransform":
            item_id = int(payload["sceneItemId"])
            if item_id in self.transforms:
                self.transforms[item_id].update(dict(payload.get("sceneItemTransform") or {}))
            return {}
        if request == "SetSceneItemEnabled":
            self.enabled[int(payload["sceneItemId"])] = bool(payload["sceneItemEnabled"])
            return {}
        raise AssertionError(f"Unexpected request: {request}")


class LayoutTests(unittest.TestCase):
    def test_module_name_parser(self):
        self.assertEqual(split_module_source("[Webcam] Cadre"), ("Webcam", "Cadre"))
        self.assertIsNone(split_module_source("Webcam Cadre"))
        self.assertIsNone(split_module_source("[Webcam]"))

    def test_topology_scan_honors_cooperative_shutdown_before_obs_io(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        def stop_now():
            raise RuntimeError("shutdown requested")

        manager.set_cooperative_yield(stop_now)

        with self.assertRaisesRegex(RuntimeError, "shutdown requested"):
            manager.scan_scene_topology("Gameplay")

        self.assertEqual(client.calls, [])

    def test_lightweight_topology_scan_does_not_read_transforms(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        topology = manager.scan_scene_topology("Gameplay")

        self.assertEqual(
            {item.source for item in topology},
            {"[Webcam] Cadre", "[Webcam] Avatar"},
        )
        self.assertFalse(any(request == "GetSceneItemTransform" for request, _ in client.calls))

    def test_apply_matching_layout_performs_no_mutation_writes(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        client.calls.clear()

        result = manager.apply_profile(profile, record_undo=False)

        self.assertEqual(result.missing_sources, ())
        writes = [
            request
            for request, _payload in client.calls
            if request in {"SetSceneItemTransform", "SetSceneItemEnabled"}
        ]
        self.assertEqual(writes, [])

    def test_noop_apply_scales_to_hundred_elements_without_writes(self):
        class LargeClient(FakeLayoutClient):
            def __init__(self, count):
                super().__init__()
                self.items = {f"[Test] Item {index:03d}": index + 1 for index in range(count)}
                self.transforms = {
                    index + 1: {
                        "positionX": float(index * 10),
                        "positionY": float(index * 5),
                        "width": 100.0,
                        "height": 50.0,
                        "scaleX": 1.0,
                        "scaleY": 1.0,
                        "alignment": 5,
                        "rotation": 0.0,
                        "boundsType": "OBS_BOUNDS_NONE",
                    }
                    for index in range(count)
                }
                self.enabled = {index + 1: True for index in range(count)}

            def send(self, request, data=None):
                payload = dict(data or {})
                if request == "GetSceneItemList":
                    self.calls.append((request, payload))
                    return {
                        "sceneItems": [
                            {
                                "sourceName": source,
                                "sceneItemId": item_id,
                                "sceneItemEnabled": True,
                            }
                            for source, item_id in self.items.items()
                        ]
                    }
                return super().send(request, data)

        for count in (10, 100):
            with self.subTest(count=count):
                client = LargeClient(count)
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                client.calls.clear()

                manager.apply_profile(profile, record_undo=False)

                writes = [
                    request
                    for request, _payload in client.calls
                    if request in {"SetSceneItemTransform", "SetSceneItemEnabled"}
                ]
                self.assertEqual(writes, [])

    def test_discovery_keeps_each_source_as_a_distinct_module(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        modules = manager.discover_scene("Gameplay")
        self.assertEqual(list(modules), ["[Webcam] Avatar", "[Webcam] Cadre"])
        self.assertEqual(modules["[Webcam] Avatar"][0].module, "Webcam")
        self.assertEqual(modules["[Webcam] Avatar"][0].element, "Avatar")


    def test_capture_result_reports_partial_nested_read(self):
        class PartialClient(FakeLayoutClient):
            def send(self, request, data=None):
                payload = dict(data or {})
                if request == "GetSceneItemList" and payload.get("sceneName") == "[Webcam] Cadre":
                    raise RuntimeError("nested read failed")
                return super().send(request, data)

        client = PartialClient()
        manager = OBSLayoutManager(client)

        result = manager.capture_profile_result("Gameplay")

        self.assertFalse(result.complete)
        self.assertTrue(result.warnings)
        self.assertGreater(result.captured_modules, 0)

    def test_compacted_child_may_have_no_module_overrides(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        parent = manager.capture_profile("Gameplay")
        child = manager.capture_profile("Gameplay", extends="Base")

        compact = compact_layout_overrides(child, parent)

        self.assertEqual(compact.get("modules"), {})
        self.assertEqual(compact.get("extends"), "Base")

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

    def test_fade_opacity_recovery_is_bounded_on_missing_filter(self):
        class MissingOnceClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.settings_attempts = 0

            def send(self, request, data=None):
                if request == "SetSourceFilterSettings":
                    self.settings_attempts += 1
                    if self.settings_attempts == 1:
                        from stream_state_router.obs.client import OBSResourceNotFoundError
                        raise OBSResourceNotFoundError(request, "missing filter")
                    self.calls.append((request, dict(data or {})))
                    return {}
                if request == "GetSourceFilterList":
                    self.calls.append((request, dict(data or {})))
                    return {"filters": []}
                if request == "CreateSourceFilter":
                    self.calls.append((request, dict(data or {})))
                    return {}
                return super().send(request, data)

        client = MissingOnceClient()
        manager = OBSLayoutManager(client)

        manager._set_source_opacity("[Webcam] Avatar", 0.5)

        self.assertEqual(client.settings_attempts, 2)
        self.assertEqual(
            [request for request, _payload in client.calls if request == "CreateSourceFilter"],
            ["CreateSourceFilter"],
        )

    def test_fade_persistent_settings_error_does_not_recurse(self):
        class PersistentFailureClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.settings_attempts = 0

            def send(self, request, data=None):
                if request == "SetSourceFilterSettings":
                    self.settings_attempts += 1
                    from stream_state_router.obs.client import OBSResourceNotFoundError
                    raise OBSResourceNotFoundError(request, "missing filter")
                if request == "GetSourceFilterList":
                    self.calls.append((request, dict(data or {})))
                    return {"filters": []}
                if request == "CreateSourceFilter":
                    self.calls.append((request, dict(data or {})))
                    return {}
                return super().send(request, data)

        client = PersistentFailureClient()
        manager = OBSLayoutManager(client)

        with self.assertRaises(Exception):
            manager._set_source_opacity("[Webcam] Avatar", 0.5)

        self.assertEqual(client.settings_attempts, 2)

    def test_pending_fade_cleanup_is_retained_until_neutralization_succeeds(self):
        from stream_state_router.obs.client import OBSUnavailableError

        class FadeCleanupClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.fail_cleanup = True

            def send(self, request, data=None):
                if request == "SetSourceFilterSettings":
                    self.calls.append((request, dict(data or {})))
                    if self.fail_cleanup:
                        raise OBSUnavailableError("offline")
                    return {}
                return super().send(request, data)

        client = FadeCleanupClient()
        manager = OBSLayoutManager(client)

        warnings = manager._neutralize_fade_sources(["[Webcam] Avatar"])
        self.assertTrue(warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

        client.fail_cleanup = False
        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())

    def test_fade_cleanup_is_prearmed_before_first_opacity_io(self):
        from stream_state_router.obs.client import OBSUnavailableError

        class FirstFadeWriteUncertainClient(FakeLayoutClient):
            def send(self, request, data=None):
                if request == "SetSourceFilterSettings":
                    self.calls.append((request, dict(data or {})))
                    raise OBSUnavailableError("response lost")
                return super().send(request, data)

        client = FirstFadeWriteUncertainClient()
        client.scene_collection = "Collection A"
        manager = OBSLayoutManager(client)
        warnings = []
        prepared = [{
            "target_enabled": True,
            "current_enabled": False,
            "visibility_changed": True,
            "source": "[Webcam] Avatar",
            "container": "Gameplay",
            "transform_changed": False,
            "target_transform": {},
            "current_transform": {},
        }]

        manager._animate_layout_transition(
            prepared,
            mode="fade",
            duration_ms=1,
            steps=1,
            warnings=warnings,
        )

        exported = manager.export_pending_fade_cleanup()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["source"], "[Webcam] Avatar")
        self.assertEqual(exported[0]["collection"], "Collection A")
        self.assertTrue(warnings)

    def test_immediate_fade_cleanup_never_writes_in_foreign_collection(self):
        client = FakeLayoutClient()
        client.scene_collection = "Collection B"
        manager = OBSLayoutManager(client)
        client.calls.clear()

        warnings = manager._neutralize_fade_sources(
            ["[Webcam] Avatar"],
            collection="Collection A",
        )

        self.assertTrue(warnings)
        self.assertFalse(
            any(request == "SetSourceFilterSettings" for request, _ in client.calls)
        )
        exported = manager.export_pending_fade_cleanup()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["collection"], "Collection A")

    def test_fade_cleanup_export_import_preserves_collection_context(self):
        from stream_state_router.obs.client import OBSUnavailableError

        class FadeCleanupClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.fail_cleanup = True

            def send(self, request, data=None):
                if request == "SetSourceFilterSettings":
                    self.calls.append((request, dict(data or {})))
                    if self.fail_cleanup:
                        raise OBSUnavailableError("offline")
                    return {}
                return super().send(request, data)

        client_a = FadeCleanupClient()
        client_a.scene_collection = "Collection A"
        manager_a = OBSLayoutManager(client_a)
        self.assertTrue(manager_a._neutralize_fade_sources(["[Webcam] Avatar"]))
        exported = manager_a.export_pending_fade_cleanup()

        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["kind"], "layout_fade")
        self.assertEqual(exported[0]["collection"], "Collection A")

        client_b = FadeCleanupClient()
        client_b.fail_cleanup = False
        client_b.scene_collection = "Collection B"
        manager_b = OBSLayoutManager(client_b)
        self.assertEqual(manager_b.import_pending_fade_cleanup(exported), 1)
        client_b.calls.clear()

        self.assertEqual(manager_b.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager_b.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(request == "SetSourceFilterSettings" for request, _ in client_b.calls)
        )

        client_b.scene_collection = "Collection A"
        self.assertEqual(manager_b.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager_b.pending_fade_cleanup(), ())
        self.assertTrue(
            any(request == "SetSourceFilterSettings" for request, _ in client_b.calls)
        )

    def test_fade_transition_context_probe_overrides_stale_collection(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        manager._last_scene_collection = "Collection A"
        client.scene_collection = "Collection B"

        self.assertEqual(
            manager._fade_collection_context(probe=True),
            "Collection B",
        )

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

    def test_diff_detects_scale_change_below_position_tolerance(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        element = profile["modules"]["[Webcam] Cadre"]["elements"][0]
        element["transform"]["scaleX"] = float(element["transform"].get("scaleX", 1.0)) + 0.1

        diffs = manager.diff_profile(profile)

        self.assertTrue(any("scaleX" in change for diff in diffs for change in diff.changes))

    def test_diff_includes_support_items(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        if not profile.get("support_items"):
            self.skipTest("Fake layout has no support items")
        support = profile["support_items"][0]
        support["transform"]["positionX"] = float(support["transform"].get("positionX", 0.0)) + 50.0

        diffs = manager.diff_profile(profile)

        self.assertTrue(any(diff.module == "[interne]" for diff in diffs))

    def test_get_current_item_reports_unknown_visibility_instead_of_true(self):
        class VisibilityFailureClient(FakeLayoutClient):
            def send(self, request, data=None):
                if request == "GetSceneItemEnabled":
                    raise RuntimeError("visibility timeout")
                return super().send(request, data)

        client = VisibilityFailureClient()
        manager = OBSLayoutManager(client)
        item = manager._get_current_item("Gameplay", "[Webcam] Cadre")

        self.assertIsNone(item["enabled"])
        self.assertIn("timeout", item["enabled_error"])

    def test_undo_snapshot_is_consumed_only_after_successful_restore(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] += 10.0
        manager.apply_profile(profile)
        self.assertEqual(len(manager._undo_stack), 1)

        original_apply = manager.apply_profile
        def fail_restore(*args, **kwargs):
            from stream_state_router.obs.layouts import LayoutApplyResult
            return LayoutApplyResult(warnings=("temporary failure",))
        manager.apply_profile = fail_restore
        failed = manager.undo_last()
        self.assertTrue(failed.warnings)
        self.assertEqual(len(manager._undo_stack), 1)

        manager.apply_profile = original_apply
        restored = manager.undo_last()
        self.assertFalse(restored.warnings)
        self.assertEqual(len(manager._undo_stack), 0)

    def test_undo_refuses_snapshot_from_another_scene_collection(self):
        client = FakeLayoutClient()
        client.scene_collection = "Collection A"
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] += 10.0
        manager.apply_profile(profile)
        self.assertEqual(len(manager._undo_stack), 1)
        client.scene_collection = "Collection B"
        client.calls.clear()

        result = manager.undo_last()

        self.assertTrue(any("Scene Collection" in warning for warning in result.warnings))
        self.assertEqual(len(manager._undo_stack), 1)
        self.assertFalse(any(request.startswith("SetSceneItem") for request, _ in client.calls))

    def test_reconnect_session_invalidates_existing_snapshot(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] += 10.0
        manager.apply_profile(profile)
        manager.invalidate_session()

        result = manager.undo_last()

        self.assertTrue(any("session OBS précédente" in warning for warning in result.warnings))
        self.assertEqual(len(manager._undo_stack), 1)

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
