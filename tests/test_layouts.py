from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from stream_state_router.obs.client import OBSResourceNotFoundError
from stream_state_router.obs.layouts import (
    OBSLayoutManager,
    LayoutSnapshot,
    compact_layout_overrides,
    split_module_source,
)
from stream_state_router.obs.fade_helpers import (
    FadeHelperManifestError,
    MemoryFadeHelperManifestStore,
    LEGACY_LAYOUT_FADE_FILTER,
)


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
        self.source_kinds = {
            "[Webcam] Cadre": "input",
            "[Webcam] Avatar": "input",
            "Unrelated": "input",
            "[Webcam:locked] Permanent": "input",
        }
        self.shared_in_pause = set()
        self.unknown_enabled = set()
        self.null_enabled = set()
        self.failed_enabled = set()
        self.config = SimpleNamespace(
            enabled=True,
            host="127.0.0.1",
            port=4455,
        )
        self.session_generation = 0
        self.input_uuids = {
            name: f"uuid-{index}"
            for index, name in enumerate(self.items, start=1)
        }
        self.source_filters = {}

    def _scene_item_row(self, source, scene_item_id):
        row = {
            "sourceName": source,
            "sceneItemId": scene_item_id,
            "sceneItemEnabled": self.enabled.get(scene_item_id, True),
        }
        kind = self.source_kinds.get(source, "input")
        if kind == "group":
            row["isGroup"] = True
            row["sourceType"] = "OBS_SOURCE_TYPE_SCENE"
        elif kind == "scene":
            row["sourceType"] = "OBS_SOURCE_TYPE_SCENE"
            row["inputKind"] = "scene"
        elif kind == "input":
            row["sourceType"] = "OBS_SOURCE_TYPE_INPUT"
            row["inputKind"] = "image_source"
        return row

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": getattr(self, "scene_collection", "Collection A")}
        if request == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": name,
                        "inputUuid": self.input_uuids.get(name, ""),
                        "inputKind": "image_source",
                    }
                    for name, kind in self.source_kinds.items()
                    if kind == "input"
                ]
            }
        if request == "GetSourceFilterList":
            source = str(payload.get("sourceName") or "")
            filters = self.source_filters.get(source, {})
            return {
                "filters": [
                    {
                        "filterName": name,
                        "filterKind": state["kind"],
                        "filterEnabled": state["enabled"],
                    }
                    for name, state in filters.items()
                ]
            }
        if request == "CreateSourceFilter":
            source = str(payload.get("sourceName") or "")
            name = str(payload.get("filterName") or "")
            filters = self.source_filters.setdefault(source, {})
            if name in filters:
                raise RuntimeError("duplicate filter")
            filters[name] = {
                "kind": str(payload.get("filterKind") or ""),
                "enabled": True,
                "settings": dict(payload.get("filterSettings") or {}),
            }
            return {}
        if request == "GetSourceFilter":
            source = str(payload.get("sourceName") or "")
            name = str(payload.get("filterName") or "")
            state = self.source_filters.get(source, {}).get(name)
            if state is None:
                raise OBSResourceNotFoundError(request, "missing filter")
            return {
                "filterName": name,
                "filterKind": state["kind"],
                "filterEnabled": state["enabled"],
                "filterSettings": dict(state["settings"]),
            }
        if request == "SetSourceFilterEnabled":
            source = str(payload.get("sourceName") or "")
            name = str(payload.get("filterName") or "")
            state = self.source_filters.get(source, {}).get(name)
            if state is None:
                raise OBSResourceNotFoundError(request, "missing filter")
            state["enabled"] = bool(payload.get("filterEnabled"))
            return {}
        if request == "SetSourceFilterSettings":
            source = str(payload.get("sourceName") or "")
            name = str(payload.get("filterName") or "")
            state = self.source_filters.get(source, {}).get(name)
            if state is None:
                raise OBSResourceNotFoundError(request, "missing filter")
            settings = payload.get("filterSettings")
            if isinstance(settings, dict):
                if bool(payload.get("overlay", True)):
                    state["settings"].update(settings)
                else:
                    state["settings"] = dict(settings)
            return {}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "Gameplay",
                "scenes": [{"sceneName": "Gameplay"}, {"sceneName": "Pause"}],
            }
        if request == "GetSceneItemList":
            scene = str(payload.get("sceneName") or "")
            if scene == "Pause":
                rows = [
                    self._scene_item_row(source, 100 + index)
                    for index, source in enumerate(sorted(self.shared_in_pause), start=1)
                ]
                return {"sceneItems": rows}
            if scene != "Gameplay":
                return {"sceneItems": []}
            return {
                "sceneItems": [
                    self._scene_item_row("[Webcam] Cadre", 1),
                    self._scene_item_row("[Webcam] Avatar", 2),
                    self._scene_item_row("Unrelated", 3),
                    self._scene_item_row("[Webcam:locked] Permanent", 4),
                ]
            }
        if request == "GetGroupSceneItemList":
            return {"sceneItems": []}
        if request == "GetSceneItemTransform":
            return {"sceneItemTransform": dict(self.transforms[int(payload["sceneItemId"])])}
        if request == "GetSceneItemId":
            return {"sceneItemId": self.items.get(payload["sourceName"], 0)}
        if request == "GetSceneItemEnabled":
            item_id = int(payload["sceneItemId"])
            if item_id in self.failed_enabled:
                raise RuntimeError("visibility timeout")
            if item_id in self.unknown_enabled:
                return {}
            if item_id in self.null_enabled:
                return {"sceneItemEnabled": None}
            return {"sceneItemEnabled": self.enabled.get(item_id, True)}
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

    def test_list_scenes_accepts_modern_current_program_scene_fallback(self):
        class ModernFallbackClient(FakeLayoutClient):
            def send(self, request, data=None):
                if request == "GetSceneList":
                    self.calls.append((request, dict(data or {})))
                    return {
                        "currentProgramSceneName": "",
                        "scenes": [
                            {"sceneName": "Gameplay"},
                            {"sceneName": "Pause"},
                        ],
                    }
                if request == "GetCurrentProgramScene":
                    self.calls.append((request, dict(data or {})))
                    return {
                        "sceneName": "Pause",
                        "sceneUuid": "pause-uuid",
                    }
                return super().send(request, data)

        client = ModernFallbackClient()
        manager = OBSLayoutManager(client)

        names, current = manager.list_scenes()

        self.assertEqual(names, ["Gameplay", "Pause"])
        self.assertEqual(current, "Pause")
        self.assertIn(
            ("GetCurrentProgramScene", {}),
            client.calls,
        )

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

    def test_undo_refuses_snapshot_when_transport_died_before_runtime_probe(self):
        class SessionAwareClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.session_generation = 7
                self.transport_dead = True

            def send(self, request, data=None, *, expected_session_generation=None):
                if expected_session_generation is not None:
                    self.asserted_generation = expected_session_generation
                    if self.transport_dead:
                        self.session_generation += 1
                        raise RuntimeError("transport closed")
                return super().send(request, data)

        client = SessionAwareClient()
        manager = OBSLayoutManager(client)
        manager._undo_stack.append(
            LayoutSnapshot(
                {"scene": "Gameplay", "modules": {}},
                "apply",
                collection="Collection A",
                generation=0,
                obs_session_generation=7,
                complete=True,
            )
        )

        result = manager.undo_last()

        self.assertTrue(result.warnings)
        self.assertIn("Session OBS non vérifiable", result.warnings[0])
        self.assertEqual(client.asserted_generation, 7)
        self.assertEqual(manager._snapshot_generation, 1)
        self.assertEqual(len(manager._undo_stack), 1)
        self.assertFalse(
            any(request.startswith("Set") for request, _payload in client.calls)
        )

    def test_undo_refuses_same_collection_when_session_changes_during_validation(self):
        class ReconnectingCollectionClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.session_generation = 11

            def send(self, request, data=None, *, expected_session_generation=None):
                if request == "GetVersion":
                    return {}
                if request == "GetSceneCollectionList":
                    self.session_generation = 12
                    return {"currentSceneCollectionName": "Collection A"}
                return super().send(request, data)

        client = ReconnectingCollectionClient()
        manager = OBSLayoutManager(client)
        manager._undo_stack.append(
            LayoutSnapshot(
                {"scene": "Gameplay", "modules": {}},
                "apply",
                collection="Collection A",
                generation=0,
                obs_session_generation=11,
                complete=True,
            )
        )

        result = manager.undo_last()

        self.assertTrue(result.warnings)
        self.assertIn("Session OBS modifiée", result.warnings[0])
        self.assertEqual(manager._snapshot_generation, 1)
        self.assertFalse(
            any(request.startswith("Set") for request, _payload in client.calls)
        )

    def test_restore_refuses_reconnect_during_profile_preparation(self):
        class GuardedRestoreClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.session_generation = 11
                self.fail_restore_read = False

            def send(self, request, data=None, *, expected_session_generation=None):
                if (
                    expected_session_generation is not None
                    and self.session_generation != expected_session_generation
                ):
                    raise RuntimeError("stale OBS session")
                if request == "GetVersion":
                    return {}
                if (
                    request == "GetSceneItemId"
                    and expected_session_generation is not None
                    and self.fail_restore_read
                ):
                    self.session_generation = 12
                    raise RuntimeError("transport closed")
                return super().send(request, data)

        client = GuardedRestoreClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        snapshot = LayoutSnapshot(
            profile,
            "apply",
            collection="Collection A",
            generation=0,
            obs_session_generation=11,
            complete=True,
        )
        manager._undo_stack.append(snapshot)
        client.calls.clear()
        client.fail_restore_read = True

        result = manager.undo_last()

        self.assertTrue(result.warnings)
        self.assertIn("Restauration interrompue", result.warnings[0])
        self.assertFalse(
            any(request.startswith("Set") for request, _payload in client.calls)
        )

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
                if request == "GetSceneList":
                    return {
                        "currentProgramSceneName": "Gameplay",
                        "scenes": [
                            {"sceneName": "Gameplay"},
                            {"sceneName": "Pause"},
                            {"sceneName": "[Webcam] Cadre"},
                        ],
                    }
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
        # The unchanged Avatar is processed but intentionally receives no OBS
        # mutation; no-op writes are suppressed.
        self.assertEqual(len(transform_calls), 1)
        by_id = {call["sceneItemId"]: call["sceneItemTransform"] for call in transform_calls}
        self.assertEqual(set(by_id), {1})
        self.assertEqual(by_id[1]["positionX"], 200.0)
        self.assertEqual(by_id[1]["positionY"], 300.0)
        self.assertEqual(by_id[1]["scaleX"], 2.0)
        self.assertEqual(by_id[1]["scaleY"], 2.0)


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

    def test_composite_visibility_matrix_never_uses_temporary_fade_filters(self):
        visibility_cases = (
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        )
        for source_type in ("scene", "group"):
            for mode in ("fade", "move_fade"):
                for current_visible, target_visible in visibility_cases:
                    with self.subTest(
                        source_type=source_type,
                        mode=mode,
                        current_visible=current_visible,
                        target_visible=target_visible,
                    ):
                        client = FakeLayoutClient()
                        client.source_kinds["[Webcam] Cadre"] = source_type
                        manager = OBSLayoutManager(client)
                        profile = manager.capture_profile("Gameplay")
                        composite = profile["modules"]["[Webcam] Cadre"]
                        composite["geometry"]["x"] = 500.0
                        composite["visible"] = target_visible
                        client.enabled[1] = current_visible
                        profile["transition"] = {
                            "mode": mode,
                            "duration_ms": 1,
                            "steps": 1,
                        }
                        client.calls.clear()

                        with patch("stream_state_router.obs.layouts.time.sleep"):
                            result = manager.apply_profile(profile, record_undo=False)

                        self.assertEqual(result.missing_sources, ())
                        self.assertEqual(client.enabled[1], target_visible)
                        self.assertTrue(
                            any(
                                request == "SetSceneItemTransform"
                                and int(payload.get("sceneItemId", 0)) == 1
                                for request, payload in client.calls
                            )
                        )
                        self.assertFalse(
                            any(
                                "SourceFilter" in request
                                and payload.get("sourceName") == "[Webcam] Cadre"
                                for request, payload in client.calls
                            )
                        )

    def test_fade_uses_current_type_instead_of_stale_profile_metadata(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }

                # Profile says input, but OBS now exposes this occurrence as a
                # nested scene. The saved metadata must not authorize a helper.
                client.source_kinds["[Webcam] Cadre"] = "scene"
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertEqual(result.missing_sources, ())
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_fade_treats_missing_profile_type_as_direct(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                element = profile["modules"]["[Webcam] Cadre"]["elements"][0]
                element.pop("source_type", None)
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertEqual(result.missing_sources, ())
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_fade_treats_unknown_current_type_as_direct(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.source_kinds["[Webcam] Cadre"] = "unknown"
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertEqual(result.missing_sources, ())
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_fade_treats_shared_source_as_direct(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.shared_in_pause.add("[Webcam] Cadre")
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertEqual(result.missing_sources, ())
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_fade_inventory_requires_authoritative_scene_item_payloads(self):
        invalid_payloads = {
            "missing": {},
            "null": {"sceneItems": None},
            "wrong_type": {"sceneItems": "not-a-list"},
            "bad_row": {"sceneItems": [None]},
            "unknown_row": {
                "sceneItems": [
                    {
                        "sourceName": "Unclassified",
                        "sceneItemId": 101,
                        "sceneItemEnabled": True,
                    }
                ]
            },
        }

        for label, invalid_response in invalid_payloads.items():
            for mode in ("fade", "move_fade"):
                with self.subTest(label=label, mode=mode):
                    class PartialSceneClient(FakeLayoutClient):
                        def send(
                            self,
                            request,
                            data=None,
                            _invalid_response=invalid_response,
                        ):
                            payload = dict(data or {})
                            if (
                                request == "GetSceneItemList"
                                and payload.get("sceneName") == "Pause"
                            ):
                                self.calls.append((request, payload))
                                return _invalid_response
                            return super().send(request, data)

                    client = PartialSceneClient()
                    manager = OBSLayoutManager(client)
                    profile = manager.capture_profile("Gameplay")
                    profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                    profile["transition"] = {
                        "mode": mode,
                        "duration_ms": 1,
                        "steps": 1,
                    }
                    client.calls.clear()

                    with patch("stream_state_router.obs.layouts.time.sleep"):
                        result = manager.apply_profile(profile, record_undo=False)

                    self.assertEqual(result.missing_sources, ())
                    self.assertFalse(
                        any(
                            "SourceFilter" in request
                            and payload.get("sourceName") == "[Webcam] Cadre"
                            for request, payload in client.calls
                        )
                    )
                    self.assertAlmostEqual(
                        client.transforms[1]["positionX"],
                        500.0,
                        places=6,
                    )

    def test_fade_inventory_requires_authoritative_group_item_payloads(self):
        invalid_payloads = (
            {},
            {"sceneItems": None},
            {"sceneItems": [None]},
            {
                "sceneItems": [
                    {
                        "sourceName": "Unclassified Child",
                        "sceneItemId": 201,
                        "sceneItemEnabled": True,
                    }
                ]
            },
        )

        for invalid_response in invalid_payloads:
            for mode in ("fade", "move_fade"):
                with self.subTest(response=invalid_response, mode=mode):
                    class PartialGroupClient(FakeLayoutClient):
                        def send(
                            self,
                            request,
                            data=None,
                            _invalid_response=invalid_response,
                        ):
                            payload = dict(data or {})
                            if (
                                request == "GetSceneItemList"
                                and payload.get("sceneName") == "Gameplay"
                            ):
                                response = super().send(request, data)
                                response["sceneItems"].append(
                                    {
                                        "sourceName": "Broken Group",
                                        "sceneItemId": 99,
                                        "sceneItemEnabled": True,
                                        "isGroup": True,
                                        "sourceType": "OBS_SOURCE_TYPE_SCENE",
                                    }
                                )
                                return response
                            if (
                                request == "GetGroupSceneItemList"
                                and payload.get("sceneName") == "Broken Group"
                            ):
                                self.calls.append((request, payload))
                                return _invalid_response
                            return super().send(request, data)

                    client = PartialGroupClient()
                    manager = OBSLayoutManager(client)
                    profile = manager.capture_profile("Gameplay")
                    profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                    profile["transition"] = {
                        "mode": mode,
                        "duration_ms": 1,
                        "steps": 1,
                    }
                    client.calls.clear()

                    with patch("stream_state_router.obs.layouts.time.sleep"):
                        result = manager.apply_profile(profile, record_undo=False)

                    self.assertEqual(result.missing_sources, ())
                    self.assertFalse(
                        any(
                            "SourceFilter" in request
                            and payload.get("sourceName") == "[Webcam] Cadre"
                            for request, payload in client.calls
                        )
                    )

    def test_fade_inventory_accepts_explicit_empty_scene_item_list(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        current_types, occurrences, complete = manager._fade_runtime_inventory()

        self.assertTrue(complete)
        self.assertEqual(current_types[("Gameplay", "[Webcam] Cadre")], "input")
        self.assertEqual(occurrences["[Webcam] Cadre"], 1)

    def test_fade_inventory_rejects_ambiguous_group_discriminator(self):
        ambiguous_values = ("missing", None, "")

        for marker in ambiguous_values:
            for mode in ("fade", "move_fade"):
                with self.subTest(marker=marker, mode=mode):
                    class AmbiguousGroupClient(FakeLayoutClient):
                        def send(self, request, data=None, _marker=marker):
                            payload = dict(data or {})
                            if (
                                request == "GetSceneItemList"
                                and payload.get("sceneName") == "Pause"
                            ):
                                self.calls.append((request, payload))
                                row = {
                                    "sourceName": "G",
                                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                                    "inputKind": None,
                                    "sceneItemId": 201,
                                    "sceneItemEnabled": True,
                                }
                                if _marker != "missing":
                                    row["isGroup"] = _marker
                                return {"sceneItems": [row]}
                            if (
                                request == "GetGroupSceneItemList"
                                and payload.get("sceneName") == "G"
                            ):
                                self.calls.append((request, payload))
                                return {
                                    "sceneItems": [
                                        self._scene_item_row("[Webcam] Cadre", 301)
                                    ]
                                }
                            return super().send(request, data)

                    client = AmbiguousGroupClient()
                    manager = OBSLayoutManager(client)
                    _types, _occurrences, complete = manager._fade_runtime_inventory()
                    self.assertFalse(complete)

                    profile = manager.capture_profile("Gameplay")
                    profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                    profile["modules"]["[Webcam] Cadre"]["visible"] = True
                    profile["transition"] = {
                        "mode": mode,
                        "duration_ms": 1,
                        "steps": 1,
                    }
                    client.enabled[1] = False
                    client.calls.clear()

                    with patch("stream_state_router.obs.layouts.time.sleep"):
                        manager.apply_profile(profile, record_undo=False)

                    self.assertTrue(client.enabled[1])
                    self.assertAlmostEqual(client.transforms[1]["positionX"], 500.0)
                    self.assertFalse(
                        any(
                            "SourceFilter" in request
                            and payload.get("sourceName") == "[Webcam] Cadre"
                            for request, payload in client.calls
                        )
                    )

    def test_fade_inventory_rejects_referenced_scene_missing_from_catalog(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                class MissingNestedSceneClient(FakeLayoutClient):
                    def send(self, request, data=None):
                        payload = dict(data or {})
                        if request == "GetSceneList":
                            self.calls.append((request, payload))
                            return {
                                "currentProgramSceneName": "Gameplay",
                                "scenes": [
                                    {"sceneName": "Gameplay"},
                                    {"sceneName": "Pause"},
                                ],
                            }
                        if (
                            request == "GetSceneItemList"
                            and payload.get("sceneName") == "Pause"
                        ):
                            self.calls.append((request, payload))
                            return {
                                "sceneItems": [
                                    {
                                        "sourceName": "Nested",
                                        "sourceType": "OBS_SOURCE_TYPE_SCENE",
                                        "inputKind": "scene",
                                        "isGroup": False,
                                        "sceneItemId": 201,
                                        "sceneItemEnabled": True,
                                    }
                                ]
                            }
                        if (
                            request == "GetSceneItemList"
                            and payload.get("sceneName") == "Nested"
                        ):
                            self.calls.append((request, payload))
                            return {
                                "sceneItems": [
                                    self._scene_item_row("[Webcam] Cadre", 301)
                                ]
                            }
                        return super().send(request, data)

                client = MissingNestedSceneClient()
                manager = OBSLayoutManager(client)
                _types, occurrences, complete = manager._fade_runtime_inventory()
                self.assertFalse(complete)
                self.assertEqual(occurrences["[Webcam] Cadre"], 1)
                self.assertFalse(
                    any(
                        request == "GetSceneItemList"
                        and payload.get("sceneName") == "Nested"
                        for request, payload in client.calls
                    )
                )

                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    manager.apply_profile(profile, record_undo=False)

                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_fade_inventory_counts_referenced_scene_when_catalogued(self):
        class CataloguedNestedSceneClient(FakeLayoutClient):
            def send(self, request, data=None):
                payload = dict(data or {})
                if request == "GetSceneList":
                    self.calls.append((request, payload))
                    return {
                        "currentProgramSceneName": "Gameplay",
                        "scenes": [
                            {"sceneName": "Gameplay"},
                            {"sceneName": "Pause"},
                            {"sceneName": "Nested"},
                        ],
                    }
                if (
                    request == "GetSceneItemList"
                    and payload.get("sceneName") == "Pause"
                ):
                    self.calls.append((request, payload))
                    return {
                        "sceneItems": [
                            {
                                "sourceName": "Nested",
                                "sourceType": "OBS_SOURCE_TYPE_SCENE",
                                "inputKind": "scene",
                                "isGroup": False,
                                "sceneItemId": 201,
                                "sceneItemEnabled": True,
                            }
                        ]
                    }
                if (
                    request == "GetSceneItemList"
                    and payload.get("sceneName") == "Nested"
                ):
                    self.calls.append((request, payload))
                    return {
                        "sceneItems": [
                            self._scene_item_row("[Webcam] Cadre", 301)
                        ]
                    }
                return super().send(request, data)

        client = CataloguedNestedSceneClient()
        manager = OBSLayoutManager(client)

        _types, occurrences, complete = manager._fade_runtime_inventory()

        self.assertTrue(complete)
        self.assertEqual(occurrences["[Webcam] Cadre"], 2)

    def test_fade_inventory_rejects_non_text_source_names(self):
        bad_names = (
            {"name": "[Webcam] Cadre"},
            ["[Webcam] Cadre"],
            101,
        )

        for bad_name in bad_names:
            for container_kind in ("scene", "group"):
                for mode in ("fade", "move_fade"):
                    with self.subTest(
                        bad_name=bad_name,
                        container_kind=container_kind,
                        mode=mode,
                    ):
                        class BadSourceNameClient(FakeLayoutClient):
                            def send(
                                self,
                                request,
                                data=None,
                                _bad_name=bad_name,
                                _container_kind=container_kind,
                            ):
                                payload = dict(data or {})
                                if (
                                    request == "GetSceneItemList"
                                    and payload.get("sceneName") == "Pause"
                                ):
                                    self.calls.append((request, payload))
                                    if _container_kind == "scene":
                                        return {
                                            "sceneItems": [
                                                {
                                                    "sourceName": _bad_name,
                                                    "sourceType": "OBS_SOURCE_TYPE_INPUT",
                                                    "inputKind": "image_source",
                                                    "sceneItemId": 201,
                                                    "sceneItemEnabled": True,
                                                }
                                            ]
                                        }
                                    return {
                                        "sceneItems": [
                                            {
                                                "sourceName": "G",
                                                "sourceType": "OBS_SOURCE_TYPE_SCENE",
                                                "isGroup": True,
                                                "sceneItemId": 202,
                                                "sceneItemEnabled": True,
                                            }
                                        ]
                                    }
                                if (
                                    request == "GetGroupSceneItemList"
                                    and payload.get("sceneName") == "G"
                                ):
                                    self.calls.append((request, payload))
                                    return {
                                        "sceneItems": [
                                            {
                                                "sourceName": _bad_name,
                                                "sourceType": "OBS_SOURCE_TYPE_INPUT",
                                                "inputKind": "image_source",
                                                "sceneItemId": 301,
                                                "sceneItemEnabled": True,
                                            }
                                        ]
                                    }
                                return super().send(request, data)

                        client = BadSourceNameClient()
                        manager = OBSLayoutManager(client)
                        _types, _occurrences, complete = manager._fade_runtime_inventory()
                        self.assertFalse(complete)

                        profile = manager.capture_profile("Gameplay")
                        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                        profile["transition"] = {
                            "mode": mode,
                            "duration_ms": 1,
                            "steps": 1,
                        }
                        client.calls.clear()

                        with patch("stream_state_router.obs.layouts.time.sleep"):
                            manager.apply_profile(profile, record_undo=False)

                        self.assertFalse(
                            any(
                                "SourceFilter" in request
                                and payload.get("sourceName") == "[Webcam] Cadre"
                                for request, payload in client.calls
                            )
                        )

    def test_fade_inventory_rejects_non_text_scene_names(self):
        bad_names = ({"name": "Pause"}, ["Pause"], 101)

        for bad_name in bad_names:
            with self.subTest(bad_name=bad_name):
                class BadSceneNameClient(FakeLayoutClient):
                    def send(self, request, data=None, _bad_name=bad_name):
                        if request == "GetSceneList":
                            self.calls.append((request, dict(data or {})))
                            return {
                                "currentProgramSceneName": "Gameplay",
                                "scenes": [
                                    {"sceneName": "Gameplay"},
                                    {"sceneName": _bad_name},
                                ],
                            }
                        return super().send(request, data)

                manager = OBSLayoutManager(BadSceneNameClient())
                _types, _occurrences, complete = manager._fade_runtime_inventory()
                self.assertFalse(complete)

    def test_fade_inventory_accepts_numeric_text_identifiers(self):
        class NumericTextClient(FakeLayoutClient):
            def send(self, request, data=None):
                payload = dict(data or {})
                if request == "GetSceneList":
                    self.calls.append((request, payload))
                    return {
                        "currentProgramSceneName": "Gameplay",
                        "scenes": [
                            {"sceneName": "Gameplay"},
                            {"sceneName": "101"},
                        ],
                    }
                if (
                    request == "GetSceneItemList"
                    and payload.get("sceneName") == "101"
                ):
                    self.calls.append((request, payload))
                    return {
                        "sceneItems": [
                            {
                                "sourceName": "202",
                                "sourceType": "OBS_SOURCE_TYPE_INPUT",
                                "inputKind": "image_source",
                                "sceneItemId": 202,
                                "sceneItemEnabled": True,
                            }
                        ]
                    }
                return super().send(request, data)

        manager = OBSLayoutManager(NumericTextClient())
        _types, occurrences, complete = manager._fade_runtime_inventory()

        self.assertTrue(complete)
        self.assertEqual(occurrences["202"], 1)

    def test_unknown_visibility_uses_direct_target_for_all_a0_source_types(self):
        for response_kind in ("missing", "null", "error"):
            for source_type in ("input", "scene", "group"):
                for mode in ("fade", "move_fade"):
                    for target_visible in (True, False):
                        with self.subTest(
                            response_kind=response_kind,
                            source_type=source_type,
                            mode=mode,
                            target_visible=target_visible,
                        ):
                            client = FakeLayoutClient()
                            client.source_kinds["[Webcam] Cadre"] = source_type
                            manager = OBSLayoutManager(client)
                            profile = manager.capture_profile("Gameplay")
                            profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                            profile["modules"]["[Webcam] Cadre"]["visible"] = target_visible
                            profile["transition"] = {
                                "mode": mode,
                                "duration_ms": 1,
                                "steps": 1,
                            }

                            client.enabled[1] = not target_visible
                            if response_kind == "missing":
                                client.unknown_enabled.add(1)
                            elif response_kind == "null":
                                client.null_enabled.add(1)
                            else:
                                client.failed_enabled.add(1)
                            client.calls.clear()

                            with patch("stream_state_router.obs.layouts.time.sleep"):
                                result = manager.apply_profile(
                                    profile,
                                    record_undo=False,
                                )

                            self.assertEqual(client.enabled[1], target_visible)
                            self.assertAlmostEqual(
                                client.transforms[1]["positionX"],
                                500.0,
                                places=6,
                            )
                            self.assertTrue(
                                any(
                                    "Visibilité actuelle inconnue" in warning
                                    for warning in result.warnings
                                )
                            )
                            self.assertFalse(
                                any(
                                    "SourceFilter" in request
                                    and payload.get("sourceName") == "[Webcam] Cadre"
                                    for request, payload in client.calls
                                )
                            )

    def test_get_current_item_treats_null_visibility_as_unknown(self):
        client = FakeLayoutClient()
        client.null_enabled.add(1)
        manager = OBSLayoutManager(client)

        item = manager._get_current_item("Gameplay", "[Webcam] Cadre")

        self.assertIsNone(item["enabled"])
        self.assertIn("non booléenne", item["enabled_error"])

    def test_fade_with_unknown_visibility_stays_on_direct_path(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.unknown_enabled.add(1)
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertTrue(
                    any("Visibilité actuelle inconnue" in warning for warning in result.warnings)
                )
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_runtime_owned_visibility_never_enters_fade_pipeline(self):
        for mode in ("fade", "move_fade"):
            with self.subTest(mode=mode):
                client = FakeLayoutClient()
                manager = OBSLayoutManager(client)
                manager.set_runtime_visibility_owners(
                    {("Gameplay", "[Webcam] Cadre")}
                )
                profile = manager.capture_profile("Gameplay")
                profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
                profile["transition"] = {
                    "mode": mode,
                    "duration_ms": 1,
                    "steps": 1,
                }
                client.calls.clear()

                with patch("stream_state_router.obs.layouts.time.sleep"):
                    result = manager.apply_profile(profile, record_undo=False)

                self.assertEqual(result.missing_sources, ())
                self.assertFalse(
                    any(
                        "SourceFilter" in request
                        and payload.get("sourceName") == "[Webcam] Cadre"
                        for request, payload in client.calls
                    )
                )

    def test_composite_interruption_between_fade_phases_never_creates_helper(self):
        client = FakeLayoutClient()
        client.source_kinds["[Webcam] Cadre"] = "scene"
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
        profile["transition"] = {
            "mode": "fade",
            "duration_ms": 1,
            "steps": 1,
        }

        original_set_transform = manager._set_transform

        def cancel_after_transform(container, source, transform):
            original_set_transform(container, source, transform)
            if source == "[Webcam] Cadre":
                raise RuntimeError("cancelled between fade phases")

        manager._set_transform = cancel_after_transform
        client.calls.clear()

        with self.assertRaisesRegex(
            RuntimeError,
            "Transition layout interrompue: cancelled between fade phases",
        ):
            with patch("stream_state_router.obs.layouts.time.sleep"):
                manager.apply_profile(profile, record_undo=False)

        self.assertFalse(
            any(
                "SourceFilter" in request
                and payload.get("sourceName") == "[Webcam] Cadre"
                for request, payload in client.calls
            )
        )


    def test_set_source_opacity_never_creates_missing_helper(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)

        with self.assertRaisesRegex(RuntimeError, "création implicite interdite"):
            manager._set_source_opacity("[Webcam] Avatar", 0.5)

        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_duplicate_schema3_obligations_are_quarantined_without_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)

        base = {
            "kind": "layout_fade",
            "source": "[Webcam] Avatar",
            "collection": "Collection A",
            "helper_id": identity.helper_id,
            "source_uuid": identity.source_uuid,
            "source_kind": identity.source_kind,
            "connection": {
                "host": identity.connection_host,
                "port": identity.connection_port,
            },
            "filter_name": identity.filter_name,
            "filter_kind": identity.filter_kind,
            "cleanup_action": "neutralize_disable",
        }
        contradictory = dict(base)
        contradictory["filter_name"] = identity.filter_name + "-other"

        manager.import_pending_fade_cleanup((base, contradictory))
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(
            any("dupliquée ou contradictoire" in item for item in warnings),
            warnings,
        )
        exported = manager.export_pending_fade_cleanup()
        self.assertEqual(len(exported), 1)
        self.assertTrue(exported[0]["ambiguous"])
        self.assertEqual(exported[0]["helper_id"], identity.helper_id)
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_legacy_v2_cleanup_never_mutates_or_creates(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                },
            )
        )
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_legacy_obligation_blocks_new_helper_creation(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                },
            )
        )
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "legacy ambigu"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_repeated_legacy_cleanup_warning_is_deduplicated(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                },
            )
        )

        first = manager.retry_pending_fade_cleanup()
        second = manager.retry_pending_fade_cleanup()

        self.assertTrue(first)
        self.assertEqual(second, ())
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

    def test_session_invalidation_forgets_active_helper_only(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        self.assertIn("[Webcam] Avatar", manager._active_fade_helpers)

        manager.invalidate_session()

        self.assertNotIn("[Webcam] Avatar", manager._active_fade_helpers)
        self.assertIsNotNone(store.get(identity.helper_id))
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

    def test_missing_input_uuid_refuses_helper_before_persistence_or_create(self):
        client = FakeLayoutClient()
        client.input_uuids["[Webcam] Avatar"] = ""
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        journal_calls = []
        manager.set_pending_cleanup_changed(
            lambda: journal_calls.append("journal")
        )
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "Identité d'input OBS incomplète"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(store.entries(), ())
        self.assertEqual(journal_calls, [])
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_duplicate_input_identity_refuses_helper_before_persistence(self):
        class DuplicateInputClient(FakeLayoutClient):
            def send(self, request, data=None):
                if request == "GetInputList":
                    self.calls.append((request, dict(data or {})))
                    row = {
                        "inputName": "[Webcam] Avatar",
                        "inputUuid": "uuid-duplicate",
                        "inputKind": "image_source",
                    }
                    return {"inputs": [dict(row), dict(row)]}
                return super().send(request, data)

        client = DuplicateInputClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)

        with self.assertRaisesRegex(RuntimeError, "non résolu de façon unique"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(store.entries(), ())
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_active_fade_stops_when_obs_session_changes_mid_write(self):
        class SessionChangeClient(FakeLayoutClient):
            change_session_on_opacity = False

            def send(self, request, data=None):
                result = super().send(request, data)
                if request == "SetSourceFilterSettings" and self.change_session_on_opacity:
                    self.change_session_on_opacity = False
                    self.session_generation += 1
                return result

        client = SessionChangeClient()
        client.session_generation = 7
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(lambda: None)
        manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")
        client.change_session_on_opacity = True

        with self.assertRaisesRegex(RuntimeError, "Session OBS modifiée"):
            manager._set_source_opacity("[Webcam] Avatar", 0.25)

        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

    def test_manifest_then_journal_precede_first_filter_mutation(self):
        events = []

        class RecordingStore(MemoryFadeHelperManifestStore):
            def prepare_layout_fade(self, **kwargs):
                events.append("manifest")
                return super().prepare_layout_fade(**kwargs)

        class RecordingClient(FakeLayoutClient):
            def send(self, request, data=None):
                if request == "CreateSourceFilter":
                    events.append("create")
                return super().send(request, data)

        client = RecordingClient()
        manager = OBSLayoutManager(client, fade_helper_store=RecordingStore())
        manager.set_pending_cleanup_changed(lambda: events.append("journal"))

        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )

        self.assertTrue(identity.helper_id)
        self.assertLess(events.index("manifest"), events.index("journal"))
        self.assertLess(events.index("journal"), events.index("create"))

    def test_manifest_failure_blocks_journal_and_filter_creation(self):
        events = []

        class FailingStore(MemoryFadeHelperManifestStore):
            def prepare_layout_fade(self, **kwargs):
                events.append("manifest")
                raise FadeHelperManifestError("manifest unavailable")

        client = FakeLayoutClient()
        manager = OBSLayoutManager(client, fade_helper_store=FailingStore())
        manager.set_pending_cleanup_changed(lambda: events.append("journal"))
        client.calls.clear()

        with self.assertRaisesRegex(
            FadeHelperManifestError,
            "manifest unavailable",
        ):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(events, ["manifest"])
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_journal_failure_blocks_filter_creation(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(
            lambda: (_ for _ in ()).throw(OSError("disk unavailable"))
        )

        with self.assertRaisesRegex(OSError, "disk unavailable"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_contradictory_pending_identity_blocks_normal_helper_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": identity.helper_id,
                    "source_uuid": identity.source_uuid,
                    "source_kind": identity.source_kind,
                    "connection": {
                        "host": identity.connection_host,
                        "port": identity.connection_port,
                    },
                    "filter_name": identity.filter_name + "-wrong",
                    "filter_kind": identity.filter_kind,
                    "cleanup_action": "neutralize_disable",
                },
            )
        )
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "Obligation fade contradictoire"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

    def test_journal_written_then_precreate_inventory_failure_is_disarmed(self):
        class FilterInventoryFailureClient(FakeLayoutClient):
            fail_filter_inventory = False

            def send(self, request, data=None):
                if request == "GetSourceFilterList" and self.fail_filter_inventory:
                    self.calls.append((request, dict(data or {})))
                    raise RuntimeError("filter inventory unavailable")
                return super().send(request, data)

        client = FilterInventoryFailureClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        snapshots = []
        manager.set_pending_cleanup_changed(
            lambda: snapshots.append(manager.export_pending_fade_cleanup())
        )
        client.fail_filter_inventory = True
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "filter inventory unavailable"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertTrue(snapshots)
        self.assertNotEqual(snapshots[0], ())
        self.assertEqual(snapshots[-1], ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_prepared_manifest_never_adopts_preexisting_generated_filter(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        client.source_filters["[Webcam] Avatar"] = {
            identity.filter_name: {
                "kind": identity.filter_kind,
                "enabled": True,
                "settings": {"opacity": 0.4},
            }
        }
        manager = OBSLayoutManager(client, fade_helper_store=store)
        snapshots = []
        manager.set_pending_cleanup_changed(
            lambda: snapshots.append(manager.export_pending_fade_cleanup())
        )
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "existant non prouvé"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(manager.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 0.4)
        self.assertTrue(state["enabled"])
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )
        self.assertTrue(snapshots)
        self.assertNotEqual(snapshots[0], ())
        self.assertEqual(snapshots[-1], ())

    def test_recovery_never_mutates_unobserved_prepared_helper(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        client.source_filters["[Webcam] Avatar"] = {
            identity.filter_name: {
                "kind": identity.filter_kind,
                "enabled": True,
                "settings": {"opacity": 0.4},
            }
        }
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": identity.helper_id,
                    "source_uuid": identity.source_uuid,
                    "source_kind": identity.source_kind,
                    "connection": {
                        "host": identity.connection_host,
                        "port": identity.connection_port,
                    },
                    "filter_name": identity.filter_name,
                    "filter_kind": identity.filter_kind,
                    "cleanup_action": "neutralize_disable",
                },
            )
        )
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 0.4)
        self.assertTrue(state["enabled"])
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_create_response_lost_is_observed_without_second_create(self):
        class LostCreateResponseClient(FakeLayoutClient):
            def __init__(self):
                super().__init__()
                self.create_count = 0

            def send(self, request, data=None):
                if request == "CreateSourceFilter":
                    self.create_count += 1
                    super().send(request, data)
                    raise RuntimeError("response lost")
                return super().send(request, data)

        client = LostCreateResponseClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(lambda: None)

        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )

        self.assertEqual(client.create_count, 1)
        self.assertEqual(identity.state, "observed")

    def test_recovery_retires_prepared_obligation_even_if_source_disappeared(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager.import_pending_fade_cleanup(
            (
                {
                    "kind": "layout_fade",
                    "source": "[Webcam] Avatar",
                    "collection": "Collection A",
                    "helper_id": identity.helper_id,
                    "source_uuid": identity.source_uuid,
                    "source_kind": identity.source_kind,
                    "connection": {
                        "host": identity.connection_host,
                        "port": identity.connection_port,
                    },
                    "filter_name": identity.filter_name,
                    "filter_kind": identity.filter_kind,
                    "cleanup_action": "neutralize_disable",
                },
            )
        )
        client.source_kinds.pop("[Webcam] Avatar")
        client.input_uuids.pop("[Webcam] Avatar")
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertFalse(
            any(
                request in {
                    "GetInputList",
                    "GetSourceFilterList",
                    "GetSourceFilter",
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_cleanup_missing_owned_helper_is_terminal_without_create(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)

        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        manager._ensure_pending_fade(identity)
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_cleanup_neutralizes_then_disables_with_readback(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        snapshots = []
        manager.set_pending_cleanup_changed(
            lambda: snapshots.append(manager.export_pending_fade_cleanup())
        )

        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        manager._set_source_opacity("[Webcam] Avatar", 0.25)
        create_count = sum(
            1 for request, _ in client.calls if request == "CreateSourceFilter"
        )
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertEqual(
            sum(1 for request, _ in client.calls if request == "CreateSourceFilter"),
            0,
        )
        self.assertEqual(create_count, 1)
        self.assertEqual(snapshots[-1], ())

    def test_transition_opacity_response_loss_keeps_cleanup_and_recovers(self):
        class LostOpacityResponseClient(FakeLayoutClient):
            lose_opacity_response = False

            def send(self, request, data=None):
                payload = data or {}
                settings = payload.get("filterSettings")
                opacity = (
                    settings.get("opacity")
                    if isinstance(settings, dict)
                    else None
                )
                if (
                    request == "SetSourceFilterSettings"
                    and self.lose_opacity_response
                    and opacity == 0.25
                ):
                    self.lose_opacity_response = False
                    super().send(request, data)
                    raise RuntimeError("opacity response lost")
                return super().send(request, data)

        client = LostOpacityResponseClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        client.lose_opacity_response = True

        with self.assertRaisesRegex(RuntimeError, "opacity response lost"):
            manager._set_source_opacity("[Webcam] Avatar", 0.25)

        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 0.25)

        client.calls.clear()
        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_fade_hidden_to_visible_opacity_response_loss_keeps_final_visibility(self):
        class LostFadeInOpacityResponseClient(FakeLayoutClient):
            lose_zero_response = False

            def send(self, request, data=None):
                payload = data or {}
                settings = payload.get("filterSettings")
                opacity = (
                    settings.get("opacity")
                    if isinstance(settings, dict)
                    else None
                )
                if (
                    request == "SetSourceFilterSettings"
                    and self.lose_zero_response
                    and opacity == 0.0
                ):
                    self.lose_zero_response = False
                    super().send(request, data)
                    raise RuntimeError("fade-in opacity response lost")
                return super().send(request, data)

        client = LostFadeInOpacityResponseClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        profile = manager.capture_profile("Gameplay")
        profile["transition"] = {"mode": "fade", "duration_ms": 100, "steps": 2}
        client.enabled[2] = False
        client.lose_zero_response = True
        client.calls.clear()

        with patch("stream_state_router.obs.layouts.time.sleep"):
            result = manager.apply_profile(profile, record_undo=False)

        self.assertTrue(client.enabled[2])
        self.assertEqual(manager.pending_fade_cleanup(), ())
        identity = store.entries()[0]
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertTrue(
            any("Fondu d'apparition incertain" in item for item in result.warnings),
            result.warnings,
        )

    def test_move_fade_hidden_to_visible_opacity_response_loss_cleans_immediately(self):
        class LostMoveFadeInOpacityResponseClient(FakeLayoutClient):
            lose_zero_response = False

            def send(self, request, data=None):
                payload = data or {}
                settings = payload.get("filterSettings")
                opacity = (
                    settings.get("opacity")
                    if isinstance(settings, dict)
                    else None
                )
                if (
                    request == "SetSourceFilterSettings"
                    and self.lose_zero_response
                    and opacity == 0.0
                ):
                    self.lose_zero_response = False
                    super().send(request, data)
                    raise RuntimeError("move-fade opacity response lost")
                return super().send(request, data)

        client = LostMoveFadeInOpacityResponseClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        profile = manager.capture_profile("Gameplay")
        profile["transition"] = {"mode": "move_fade", "duration_ms": 100, "steps": 2}
        client.enabled[2] = False
        client.lose_zero_response = True
        client.calls.clear()

        with patch.object(
            manager,
            "_transition_progress",
            return_value=iter((1.0,)),
        ):
            result = manager.apply_profile(profile, record_undo=False)

        self.assertTrue(client.enabled[2])
        self.assertEqual(manager.pending_fade_cleanup(), ())
        identity = store.entries()[0]
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertTrue(
            any("Fondu indisponible" in item for item in result.warnings),
            result.warnings,
        )

    def test_cleanup_opacity_response_loss_uses_readback_before_ack(self):
        class LostNeutralizeResponseClient(FakeLayoutClient):
            lose_neutralize_response = False

            def send(self, request, data=None):
                payload = data or {}
                if (
                    request == "SetSourceFilterSettings"
                    and self.lose_neutralize_response
                    and float(
                        (payload.get("filterSettings") or {}).get(
                            "opacity",
                            -1.0,
                        )
                    )
                    == 1.0
                ):
                    self.lose_neutralize_response = False
                    super().send(request, data)
                    raise RuntimeError("neutralize response lost")
                return super().send(request, data)

        client = LostNeutralizeResponseClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        manager._set_source_opacity("[Webcam] Avatar", 0.25)
        client.lose_neutralize_response = True
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_cleanup_disable_response_loss_uses_readback_before_ack(self):
        class LostDisableResponseClient(FakeLayoutClient):
            lose_disable_response = False

            def send(self, request, data=None):
                payload = data or {}
                if (
                    request == "SetSourceFilterEnabled"
                    and self.lose_disable_response
                    and payload.get("filterEnabled") is False
                ):
                    self.lose_disable_response = False
                    super().send(request, data)
                    raise RuntimeError("disable response lost")
                return super().send(request, data)

        client = LostDisableResponseClient()
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        manager._set_source_opacity("[Webcam] Avatar", 0.25)
        client.lose_disable_response = True
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_detectable_helper_rename_keeps_obligation_without_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        filters = client.source_filters["[Webcam] Avatar"]
        state = filters.pop(identity.filter_name)
        renamed = identity.filter_name + "-renamed"
        filters[renamed] = state
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(any("helper-like" in item for item in warnings), warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_cleanup_missing_source_keeps_obligation_without_filter_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")
        client.source_kinds.pop("[Webcam] Avatar")
        client.input_uuids.pop("[Webcam] Avatar")
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_cleanup_missing_manifest_keeps_obligation_without_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")
        store._entries = []
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(
            any("manifeste helper absent" in item for item in warnings),
            warnings,
        )
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_restart_after_create_recovers_without_second_create(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        first = OBSLayoutManager(client, fade_helper_store=store)
        first.set_pending_cleanup_changed(lambda: None)
        identity = first._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        first._set_source_opacity("[Webcam] Avatar", 0.25)
        pending = first.export_pending_fade_cleanup()
        create_count = sum(
            1 for request, _ in client.calls if request == "CreateSourceFilter"
        )

        restarted = OBSLayoutManager(client, fade_helper_store=store)
        restarted.set_pending_cleanup_changed(lambda: None)
        restarted.import_pending_fade_cleanup(pending)
        client.calls.clear()

        self.assertEqual(restarted.retry_pending_fade_cleanup(), ())
        self.assertEqual(restarted.pending_fade_cleanup(), ())
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertFalse(state["enabled"])
        self.assertEqual(
            sum(
                1
                for request, _ in client.calls
                if request == "CreateSourceFilter"
            ),
            0,
        )
        self.assertEqual(create_count, 1)

    def test_retry_after_interruption_between_neutralize_and_disable_is_idempotent(self):
        class DisableFailureClient(FakeLayoutClient):
            fail_disable_once = False

            def send(self, request, data=None):
                payload = data or {}
                if (
                    request == "SetSourceFilterEnabled"
                    and self.fail_disable_once
                    and payload.get("filterEnabled") is False
                ):
                    self.fail_disable_once = False
                    self.calls.append((request, dict(payload)))
                    raise RuntimeError("crash before disable")
                return super().send(request, data)

        client = DisableFailureClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        manager._set_source_opacity("[Webcam] Avatar", 0.25)
        client.fail_disable_once = True

        first = manager.retry_pending_fade_cleanup()

        self.assertTrue(first)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        state = client.source_filters["[Webcam] Avatar"][identity.filter_name]
        self.assertAlmostEqual(float(state["settings"]["opacity"]), 1.0)
        self.assertTrue(state["enabled"])

        second = manager.retry_pending_fade_cleanup()

        self.assertEqual(second, ())
        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertFalse(state["enabled"])
        self.assertFalse(
            any(
                request == "CreateSourceFilter"
                for request, _ in client.calls
            )
        )

    def test_cleanup_contradictory_manifest_keeps_obligation_without_mutation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        store._entries = [
            replace(
                item,
                filter_name=identity.filter_name + "-contradictory",
            )
            if item.helper_id == identity.helper_id
            else item
            for item in store._entries
        ]
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(
            any("identité helper contradictoire" in item for item in warnings),
            warnings,
        )
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
        )

    def test_cleanup_same_name_new_uuid_keeps_obligation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)

        manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")
        client.input_uuids["[Webcam] Avatar"] = "uuid-recreated"
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {"SetSourceFilterSettings", "SetSourceFilterEnabled"}
                for request, _ in client.calls
            )
        )

    def test_cleanup_external_non_temporary_change_suspends_reuse(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)

        identity = manager._prepare_fade_filter(
            "[Webcam] Avatar",
            "Collection A",
        )
        client.source_filters["[Webcam] Avatar"][identity.filter_name][
            "settings"
        ]["contrast"] = 0.5
        client.calls.clear()

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(
            any("modifié extérieurement" in warning for warning in warnings)
        )
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(request == "SetSourceFilterEnabled" for request, _ in client.calls)
        )

    def test_obligation_removal_failure_restores_in_memory_obligation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        writes = 0

        def journal():
            nonlocal writes
            writes += 1
            if writes >= 2:
                raise OSError("cannot persist removal")

        manager.set_pending_cleanup_changed(journal)
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=1,
        )
        manager._ensure_pending_fade(identity)

        warnings = manager.retry_pending_fade_cleanup()

        self.assertTrue(warnings)
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))

    def test_foreign_collection_cleanup_never_mutates(self):
        client = FakeLayoutClient()
        client.scene_collection = "Collection A"
        store = MemoryFadeHelperManifestStore()
        manager = OBSLayoutManager(client, fade_helper_store=store)
        manager.set_pending_cleanup_changed(lambda: None)
        manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")
        client.scene_collection = "Collection B"
        client.calls.clear()

        self.assertEqual(manager.retry_pending_fade_cleanup(), ())
        self.assertEqual(manager.pending_fade_cleanup(), ("[Webcam] Avatar",))
        self.assertFalse(
            any(
                request in {
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                    "CreateSourceFilter",
                }
                for request, _ in client.calls
            )
        )

    def test_legacy_named_filter_is_never_adopted(self):
        client = FakeLayoutClient()
        client.source_filters["[Webcam] Avatar"] = {
            LEGACY_LAYOUT_FADE_FILTER: {
                "kind": "color_filter_v2",
                "enabled": True,
                "settings": {"opacity": 0.5},
            }
        }
        manager = OBSLayoutManager(
            client,
            fade_helper_store=MemoryFadeHelperManifestStore(),
        )
        manager.set_pending_cleanup_changed(lambda: None)
        client.calls.clear()

        with self.assertRaisesRegex(RuntimeError, "ambigu"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertFalse(
            any(request == "CreateSourceFilter" for request, _ in client.calls)
        )

    def test_incompatible_existing_generated_helper_disarms_new_obligation(self):
        client = FakeLayoutClient()
        store = MemoryFadeHelperManifestStore()
        identity = store.prepare_layout_fade(
            connection_host="127.0.0.1",
            connection_port=4455,
            collection="Collection A",
            source_uuid=client.input_uuids["[Webcam] Avatar"],
            source_alias="[Webcam] Avatar",
            source_kind="image_source",
            session_generation=0,
        )
        client.source_filters["[Webcam] Avatar"] = {
            identity.filter_name: {
                "kind": "unexpected_filter_kind",
                "enabled": False,
                "settings": {"opacity": 1.0},
            }
        }
        manager = OBSLayoutManager(client, fade_helper_store=store)
        snapshots = []
        manager.set_pending_cleanup_changed(
            lambda: snapshots.append(manager.export_pending_fade_cleanup())
        )

        with self.assertRaisesRegex(RuntimeError, "Kind du helper incompatible"):
            manager._prepare_fade_filter("[Webcam] Avatar", "Collection A")

        self.assertEqual(manager.pending_fade_cleanup(), ())
        self.assertTrue(snapshots)
        self.assertNotEqual(snapshots[0], ())
        self.assertEqual(snapshots[-1], ())
        self.assertFalse(
            any(
                request in {
                    "CreateSourceFilter",
                    "SetSourceFilterSettings",
                    "SetSourceFilterEnabled",
                }
                for request, _ in client.calls
            )
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

    def test_transition_steps_raise_historical_eight_steps_to_smooth_cadence(self):
        self.assertEqual(OBSLayoutManager._effective_transition_steps(1000, 8), 61)
        self.assertEqual(OBSLayoutManager._effective_transition_steps(2000, 8), 121)
        self.assertEqual(OBSLayoutManager._effective_transition_steps(3000, 8), 181)
        self.assertEqual(OBSLayoutManager._effective_transition_steps(6000, 8), 361)
        self.assertEqual(OBSLayoutManager._effective_transition_steps(1000, 45), 61)

    def test_transition_timeline_skips_frames_that_are_already_stale(self):
        manager = OBSLayoutManager(FakeLayoutClient())
        with (
            patch.object(manager, "_cooperative_sleep", return_value=None),
            patch(
                "stream_state_router.obs.layouts.time.monotonic",
                side_effect=[0.0, 0.0, 0.5, 0.5, 1.0],
            ),
        ):
            progress = list(manager._transition_progress(1000, 61))

        self.assertEqual(len(progress), 2)
        self.assertAlmostEqual(progress[0], 0.5, places=6)
        self.assertAlmostEqual(progress[1], 1.0, places=6)

    def test_fade_repositions_visible_item_only_while_fully_transparent(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["transition"] = {"mode": "fade", "duration_ms": 1000, "steps": 8}
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
        client.calls.clear()

        with patch("stream_state_router.obs.layouts.time.sleep"):
            result = manager.apply_profile(profile, record_undo=False)

        self.assertEqual(result.missing_sources, ())
        transform_index = next(
            index
            for index, (request, payload) in enumerate(client.calls)
            if request == "SetSceneItemTransform" and int(payload["sceneItemId"]) == 1
        )
        opacity_writes = [
            (index, float(payload["filterSettings"]["opacity"]))
            for index, (request, payload) in enumerate(client.calls)
            if request == "SetSourceFilterSettings"
            and payload.get("sourceName") == "[Webcam] Cadre"
        ]
        self.assertTrue(opacity_writes)
        before = [value for index, value in opacity_writes if index < transform_index]
        after = [value for index, value in opacity_writes if index > transform_index]
        self.assertTrue(before)
        self.assertTrue(after)
        self.assertAlmostEqual(before[-1], 0.0, places=6)
        self.assertAlmostEqual(after[0], 0.0, places=6)
        self.assertAlmostEqual(after[-1], 1.0, places=6)
    def test_move_fade_uses_fast_edge_fades_and_invisible_middle(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["transition"] = {"mode": "move_fade", "duration_ms": 1000, "steps": 8}
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] = 500.0
        client.calls.clear()

        with patch.object(
            manager,
            "_transition_progress",
            return_value=iter((0.075, 0.15, 0.50, 0.85, 0.925, 1.0)),
        ):
            result = manager.apply_profile(profile, record_undo=False)

        self.assertEqual(result.missing_sources, ())
        positions = [
            float(payload["sceneItemTransform"]["positionX"])
            for request, payload in client.calls
            if request == "SetSceneItemTransform" and int(payload["sceneItemId"]) == 1
        ]
        self.assertEqual(
            positions[:6],
            [130.0, 160.0, 300.0, 440.0, 470.0, 500.0],
        )

        opacities = [
            float(payload["filterSettings"]["opacity"])
            for request, payload in client.calls
            if request == "SetSourceFilterSettings"
            and payload.get("sourceName") == "[Webcam] Cadre"
        ]
        self.assertGreaterEqual(len(opacities), 4)
        self.assertAlmostEqual(opacities[0], 0.5, places=6)
        self.assertAlmostEqual(opacities[1], 0.0, places=6)
        self.assertAlmostEqual(opacities[2], 0.5, places=6)
        self.assertAlmostEqual(opacities[3], 1.0, places=6)

    def test_move_fade_opacity_curve_uses_fifteen_percent_edges(self):
        curve = OBSLayoutManager._move_fade_opacity

        self.assertAlmostEqual(curve("through", 0.0), 1.0, places=6)
        self.assertAlmostEqual(curve("through", 0.075), 0.5, places=6)
        self.assertAlmostEqual(curve("through", 0.15), 0.0, places=6)
        self.assertAlmostEqual(curve("through", 0.50), 0.0, places=6)
        self.assertAlmostEqual(curve("through", 0.85), 0.0, places=6)
        self.assertAlmostEqual(curve("through", 0.925), 0.5, places=6)
        self.assertAlmostEqual(curve("through", 1.0), 1.0, places=6)

        self.assertAlmostEqual(curve("in", 0.85), 0.0, places=6)
        self.assertAlmostEqual(curve("in", 0.925), 0.5, places=6)
        self.assertAlmostEqual(curve("in", 1.0), 1.0, places=6)

        self.assertAlmostEqual(curve("out", 0.075), 0.5, places=6)
        self.assertAlmostEqual(curve("out", 0.15), 0.0, places=6)
        self.assertAlmostEqual(curve("out", 1.0), 0.0, places=6)
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
        # A two-second transition targets ~60 FPS (120 intervals). All sources
        # still share the same wall-clock timeline.
        self.assertEqual(sleep_mock.call_count, 120)
        transform_calls = [
            payload for request, payload in client.calls if request == "SetSceneItemTransform"
        ]
        self.assertEqual(len(transform_calls), 240)

    def test_excluded_module_element_is_left_untouched(self):
        client = FakeLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("Gameplay")
        profile["modules"]["[Webcam] Avatar"]["elements"][0]["included"] = False
        profile["modules"]["[Webcam] Cadre"]["geometry"]["x"] += 50.0
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
        profile["modules"]["[Webcam] Avatar"]["geometry"]["x"] += 25.0

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
        module = profile["modules"]["[Webcam] Cadre"]
        module["geometry"]["width"] = float(module["geometry"]["width"]) + 2.0

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
