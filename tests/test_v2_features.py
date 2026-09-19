from __future__ import annotations

import json
import time
import unittest
from urllib.request import Request, urlopen

from stream_state_router.obs.dispatcher import OBSDispatcher, profile_map_from_raw
from stream_state_router.obs.layouts import (
    OBSLayoutManager,
    compact_layout_overrides,
    parse_module_source,
    resolve_layout_profile,
)
from stream_state_router.obs.models import OBSConnectionConfig
from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.models import ForegroundApp, StreamState
from stream_state_router.router.rules import AppRule, RuleSet
from stream_state_router.services.api import APIConfig, LocalControlAPI


class MutableLayoutClient:
    def __init__(self, canvas=(1920, 1080)):
        self.config = OBSConnectionConfig(enabled=True)
        self.canvas = canvas
        self.calls = []
        self.items = {"[Webcam] Cadre": 1, "[Webcam] Avatar": 2}
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
        self.enabled = {1: True, 2: True}
        self.filters = {}

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetVideoSettings":
            return {"baseWidth": self.canvas[0], "baseHeight": self.canvas[1]}
        if request == "GetSceneList":
            return {"currentProgramSceneName": "In Game", "scenes": [{"sceneName": "In Game"}]}
        if request == "GetSceneItemList":
            return {
                "sceneItems": [
                    {"sourceName": name, "sceneItemId": item_id, "sceneItemEnabled": self.enabled[item_id]}
                    for name, item_id in self.items.items()
                ]
            }
        if request == "GetSceneItemId":
            return {"sceneItemId": self.items.get(payload["sourceName"], 0)}
        if request == "GetSceneItemTransform":
            return {"sceneItemTransform": dict(self.transforms[int(payload["sceneItemId"])])}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": self.enabled[int(payload["sceneItemId"])]}
        if request == "SetSceneItemTransform":
            self.transforms[int(payload["sceneItemId"])].update(payload["sceneItemTransform"])
            return {}
        if request == "SetSceneItemEnabled":
            self.enabled[int(payload["sceneItemId"])] = bool(payload["sceneItemEnabled"])
            return {}
        if request == "GetSourceFilterList":
            source = payload["sourceName"]
            return {"filters": list(self.filters.get(source, {}).values())}
        if request == "CreateSourceFilter":
            source = payload["sourceName"]
            self.filters.setdefault(source, {})[payload["filterName"]] = {
                "filterName": payload["filterName"],
                "filterSettings": dict(payload.get("filterSettings") or {}),
            }
            return {}
        if request == "SetSourceFilterEnabled":
            return {}
        if request == "SetSourceFilterSettings":
            source = payload["sourceName"]
            self.filters.setdefault(source, {}).setdefault(payload["filterName"], {"filterName": payload["filterName"]})[
                "filterSettings"
            ] = dict(payload["filterSettings"])
            return {}
        raise AssertionError(request)


class GroupClient(MutableLayoutClient):
    def __init__(self):
        super().__init__()
        self.items["HUD"] = 10
        self.transforms[10] = self.transforms[1].copy()
        self.enabled[10] = True
        self.items["[Chat] Browser"] = 11
        self.transforms[11] = self.transforms[1].copy()
        self.enabled[11] = True

    def send(self, request, data=None):
        payload = dict(data or {})
        if request == "GetSceneItemList" and payload.get("sceneName") == "In Game":
            return {
                "sceneItems": [
                    {"sourceName": "HUD", "sceneItemId": 10, "sceneItemEnabled": True, "isGroup": True}
                ]
            }
        if request == "GetGroupSceneItemList" and payload.get("sceneName") == "HUD":
            return {
                "sceneItems": [
                    {"sourceName": "[Chat] Browser", "sceneItemId": 11, "sceneItemEnabled": True}
                ]
            }
        if request == "GetSceneItemTransform" and payload.get("sceneName") == "HUD":
            return {"sceneItemTransform": dict(self.transforms[int(payload["sceneItemId"])])}
        return super().send(request, data)


class NestedLayoutClient:
    """Fake OBS hierarchy: In Game -> Webcam scene -> Avatar scene.

    Scene sources use the OBS base canvas as their intrinsic source size. The
    raw scene-item scale is initially preserved across a canvas change, so their
    rendered width/height grow with the canvas until SSR compensates.
    """

    def __init__(self, canvas=(1920, 1080)):
        self.config = OBSConnectionConfig(enabled=True)
        self.canvas = canvas
        self.calls = []
        cw, ch = canvas
        root_scale_x = 600.0 / 1920.0
        root_scale_y = 450.0 / 1080.0
        avatar_scale_x = 300.0 / 1920.0
        avatar_scale_y = 360.0 / 1080.0
        self.transforms = {
            ("In Game", 1): {
                "positionX": 1200.0, "positionY": 500.0,
                "sourceWidth": float(cw), "sourceHeight": float(ch),
                "width": cw * root_scale_x, "height": ch * root_scale_y,
                "scaleX": root_scale_x, "scaleY": root_scale_y,
                "alignment": 5, "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_NONE",
            },
            ("[Module] Webcam", 2): {
                "positionX": 100.0, "positionY": 80.0,
                "sourceWidth": float(cw), "sourceHeight": float(ch),
                "width": cw * avatar_scale_x, "height": ch * avatar_scale_y,
                "scaleX": avatar_scale_x, "scaleY": avatar_scale_y,
                "alignment": 5, "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_NONE",
            },
            ("[Module] Avatar", 3): {
                "positionX": 20.0, "positionY": 20.0,
                "sourceWidth": 260.0, "sourceHeight": 320.0,
                "width": 260.0, "height": 320.0,
                "scaleX": 1.0, "scaleY": 1.0,
                "alignment": 5, "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_NONE",
            },
        }
        self.enabled = {key: True for key in self.transforms}

    def set_canvas(self, canvas):
        self.canvas = canvas
        cw, ch = canvas
        for key in (("In Game", 1), ("[Module] Webcam", 2)):
            transform = self.transforms[key]
            transform["sourceWidth"] = float(cw)
            transform["sourceHeight"] = float(ch)
            transform["width"] = abs(float(transform["scaleX"])) * cw
            transform["height"] = abs(float(transform["scaleY"])) * ch

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetVideoSettings":
            return {"baseWidth": self.canvas[0], "baseHeight": self.canvas[1]}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "In Game",
                "scenes": [
                    {"sceneName": "In Game"},
                    {"sceneName": "[Module] Webcam"},
                    {"sceneName": "[Module] Avatar"},
                ],
            }
        if request == "GetSceneItemList":
            scene = str(payload.get("sceneName") or "")
            if scene == "In Game":
                return {"sceneItems": [{
                    "sourceName": "[Module] Webcam",
                    "sceneItemId": 1, "sceneItemEnabled": True,
                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                }]}
            if scene == "[Module] Webcam":
                return {"sceneItems": [{
                    "sourceName": "[Module] Avatar",
                    "sceneItemId": 2, "sceneItemEnabled": True,
                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                }]}
            if scene == "[Module] Avatar":
                return {"sceneItems": [{
                    "sourceName": "Avatar Dynamic",
                    "sceneItemId": 3, "sceneItemEnabled": True,
                }]}
            return {"sceneItems": []}
        if request == "GetSceneItemTransform":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            return {"sceneItemTransform": dict(self.transforms[key])}
        if request == "GetSceneItemId":
            scene = str(payload["sceneName"])
            source = str(payload["sourceName"])
            mapping = {
                ("In Game", "[Module] Webcam"): 1,
                ("[Module] Webcam", "[Module] Avatar"): 2,
                ("[Module] Avatar", "Avatar Dynamic"): 3,
            }
            return {"sceneItemId": mapping.get((scene, source), 0)}
        if request == "GetSceneItemEnabled":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            return {"sceneItemEnabled": self.enabled[key]}
        if request == "SetSceneItemTransform":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            transform = self.transforms[key]
            transform.update(dict(payload["sceneItemTransform"]))
            if "scaleX" in payload["sceneItemTransform"] and "sourceWidth" in transform:
                transform["width"] = abs(float(transform["scaleX"])) * float(transform["sourceWidth"])
            if "scaleY" in payload["sceneItemTransform"] and "sourceHeight" in transform:
                transform["height"] = abs(float(transform["scaleY"])) * float(transform["sourceHeight"])
            return {}
        if request == "SetSceneItemEnabled":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            self.enabled[key] = bool(payload["sceneItemEnabled"])
            return {}
        raise AssertionError(request)


class NestedGroupLayoutClient(NestedLayoutClient):
    """Realistic module hierarchy with a group in the Webcam scene.

    In Game -> [Module] Webcam scene -> WebCam group -> [Module] Avatar
    scene -> Avatar Dynamic PNG.
    """

    def __init__(self, canvas=(1920, 1080)):
        super().__init__(canvas)
        self.transforms[("[Module] Webcam", 4)] = {
            "positionX": 100.0, "positionY": 80.0,
            "sourceWidth": 400.0, "sourceHeight": 360.0,
            "width": 400.0, "height": 360.0,
            "scaleX": 1.0, "scaleY": 1.0,
            "alignment": 5, "rotation": 0.0,
            "boundsType": "OBS_BOUNDS_NONE",
        }
        self.transforms[("WebCam", 2)] = self.transforms.pop(("[Module] Webcam", 2))
        self.transforms[("WebCam", 5)] = {
            "positionX": 0.0, "positionY": 0.0,
            "sourceWidth": 400.0, "sourceHeight": 300.0,
            "width": 400.0, "height": 300.0,
            "scaleX": 1.0, "scaleY": 1.0,
            "alignment": 5, "rotation": 0.0,
            "boundsType": "OBS_BOUNDS_NONE",
        }
        self.enabled = {key: True for key in self.transforms}

    def set_canvas(self, canvas):
        self.canvas = canvas
        cw, ch = canvas
        root = self.transforms[("In Game", 1)]
        root["sourceWidth"] = float(cw)
        root["sourceHeight"] = float(ch)
        root["width"] = abs(float(root["scaleX"])) * cw
        root["height"] = abs(float(root["scaleY"])) * ch
        avatar = self.transforms[("WebCam", 2)]
        avatar["sourceWidth"] = float(cw)
        avatar["sourceHeight"] = float(ch)
        avatar["width"] = abs(float(avatar["scaleX"])) * cw
        avatar["height"] = abs(float(avatar["scaleY"])) * ch

    def send(self, request, data=None):
        payload = dict(data or {})
        self.calls.append((request, payload))
        if request == "GetVideoSettings":
            return {"baseWidth": self.canvas[0], "baseHeight": self.canvas[1]}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "In Game",
                "scenes": [
                    {"sceneName": "In Game"},
                    {"sceneName": "[Module] Webcam"},
                    {"sceneName": "[Module] Avatar"},
                ],
            }
        if request == "GetSceneItemList":
            scene = str(payload.get("sceneName") or "")
            if scene == "In Game":
                return {"sceneItems": [{
                    "sourceName": "[Module] Webcam",
                    "sceneItemId": 1, "sceneItemEnabled": True,
                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                }]}
            if scene == "[Module] Webcam":
                return {"sceneItems": [{
                    "sourceName": "WebCam",
                    "sceneItemId": 4, "sceneItemEnabled": True,
                    "isGroup": True,
                    # Real OBS groups may still advertise themselves as
                    # scene-like sources. GetSceneItemList must not be used.
                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                }]}
            if scene == "WebCam":
                raise RuntimeError(
                    "602: The specified source is not a scene. (Is group)"
                )
            if scene == "[Module] Avatar":
                return {"sceneItems": [{
                    "sourceName": "Avatar Dynamic",
                    "sceneItemId": 3, "sceneItemEnabled": True,
                }]}
            return {"sceneItems": []}
        if request == "GetGroupSceneItemList" and payload.get("sceneName") == "WebCam":
            return {"sceneItems": [
                {
                    "sourceName": "Cadre Webcam",
                    "sceneItemId": 5, "sceneItemEnabled": True,
                },
                {
                    "sourceName": "[Module] Avatar",
                    "sceneItemId": 2, "sceneItemEnabled": True,
                    "sourceType": "OBS_SOURCE_TYPE_SCENE",
                },
            ]}
        if request == "GetSceneItemTransform":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            return {"sceneItemTransform": dict(self.transforms[key])}
        if request == "GetSceneItemId":
            mapping = {
                ("In Game", "[Module] Webcam"): 1,
                ("[Module] Webcam", "WebCam"): 4,
                ("WebCam", "Cadre Webcam"): 5,
                ("WebCam", "[Module] Avatar"): 2,
                ("[Module] Avatar", "Avatar Dynamic"): 3,
            }
            return {"sceneItemId": mapping.get((str(payload["sceneName"]), str(payload["sourceName"])), 0)}
        if request == "GetSceneItemEnabled":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            return {"sceneItemEnabled": self.enabled[key]}
        if request == "SetSceneItemTransform":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            transform = self.transforms[key]
            transform.update(dict(payload["sceneItemTransform"]))
            if "scaleX" in payload["sceneItemTransform"] and "sourceWidth" in transform:
                transform["width"] = abs(float(transform["scaleX"])) * float(transform["sourceWidth"])
            if "scaleY" in payload["sceneItemTransform"] and "sourceHeight" in transform:
                transform["height"] = abs(float(transform["scaleY"])) * float(transform["sourceHeight"])
            return {}
        if request == "SetSceneItemEnabled":
            key = (str(payload["sceneName"]), int(payload["sceneItemId"]))
            self.enabled[key] = bool(payload["sceneItemEnabled"])
            return {}
        raise AssertionError(request)


class DeferredGroupResizeClient(NestedGroupLayoutClient):
    """Model OBS group bounds updating one render frame after child changes."""

    def __init__(self, canvas=(1920, 1080)):
        super().__init__(canvas)
        self.group_settles = 0

    def set_canvas(self, canvas):
        super().set_canvas(canvas)
        # The Avatar scene source grows immediately with the base canvas. OBS
        # groups can still report/reuse the enlarged group bounds until the next
        # render-frame resize pass has absorbed the child compensation.
        if tuple(canvas) == (2560, 1440):
            group = self.transforms[("[Module] Webcam", 4)]
            group["sourceWidth"] = 500.0
            group["sourceHeight"] = 450.0
            group["width"] = abs(float(group["scaleX"])) * 500.0
            group["height"] = abs(float(group["scaleY"])) * 450.0

    def settle_groups(self):
        self.group_settles += 1
        group = self.transforms[("[Module] Webcam", 4)]
        # Once child transforms have settled, the group's intrinsic box returns
        # to the captured local composition before the outer 4/3 scaling.
        group["sourceWidth"] = 400.0
        group["sourceHeight"] = 360.0
        group["width"] = abs(float(group["scaleX"])) * 400.0
        group["height"] = abs(float(group["scaleY"])) * 360.0


class ActionClient:
    def __init__(self):
        self.calls = []
        self.config = OBSConnectionConfig(enabled=True)

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetStreamStatus":
            return {"outputActive": True}
        if request == "GetRecordStatus":
            return {"outputActive": False}
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "In Game"}
        return {}


class V2FeatureTests(unittest.TestCase):
    def test_enriched_module_flags_are_parsed(self):
        parsed = parse_module_source("[Webcam:nomove,noresize] Avatar")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.module, "Webcam")
        self.assertEqual(parsed.element, "Avatar")
        self.assertEqual(parsed.flags, frozenset({"nomove", "noresize"}))

    def test_layout_inheritance_and_compaction(self):
        base = {
            "scene": "In Game",
            "modules": {"Webcam": {"geometry": {"x": 10}}},
            "transition": {"mode": "instant"},
        }
        child = {
            "extends": "Base",
            "scene": "",
            "modules": {
                "Webcam": {"geometry": {"x": 10}},
                "Chat": {"geometry": {"x": 20}},
            },
        }
        resolved = resolve_layout_profile("Child", {"Base": base, "Child": child})
        self.assertEqual(resolved["scene"], "In Game")
        self.assertIn("Chat", resolved["modules"])
        compact = compact_layout_overrides(child, base)
        self.assertIn("Chat", compact["modules"])
        self.assertNotIn("Webcam", compact["modules"])

    def test_action_profile_inheritance_and_conditions(self):
        client = ActionClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Base": {
                        "actions": [{"type": "set_program_scene", "params": {"scene": "In Game"}}]
                    },
                    "Child": {
                        "extends": "Base",
                        "conditions": {"streaming": True, "program_scene": "In Game"},
                        "actions": [{"type": "input_mute", "params": {"input": "Mic", "muted": False}}],
                    },
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        result = dispatcher.execute_profile("game", "Child")
        self.assertEqual(result.executed, 2)
        requests = [name for name, _ in client.calls]
        self.assertIn("SetCurrentProgramScene", requests)
        self.assertIn("SetInputMute", requests)

    def test_rule_conditions_and_obs_delay(self):
        state = StreamState(game="Overwatch")
        rules = RuleSet(
            [
                AppRule(
                    "OW live",
                    state=state,
                    exe="Overwatch.exe",
                    conditions={"streaming": True},
                    apply_delay_ms=120,
                )
            ]
        )
        app = ForegroundApp(1, 2, "Overwatch.exe")
        no_match = rules.resolve(app, {"streaming": False})
        self.assertEqual(no_match.rule_name, "fallback")
        match = rules.resolve(app, {"streaming": True})
        self.assertEqual(match.rule_name, "OW live")
        self.assertEqual(match.apply_delay_ms, 120)

    def test_temporary_override_expires(self):
        now = [10.0]
        rules = RuleSet([AppRule("Game", state=StreamState(game="Game"), exe="game.exe")])
        engine = StateRouterEngine(rules, debounce_ms=0, clock=lambda: now[0])
        app = ForegroundApp(1, 2, "game.exe")
        engine.observe(app)
        engine.set_manual_override(StreamState(game="Manual"), duration_seconds=2)
        now[0] = 11.0
        self.assertIsNone(engine.observe(app))
        now[0] = 12.1
        change = engine.observe(app)
        self.assertIsNotNone(change)
        self.assertEqual(change.current.game, "Game")

    def test_normalized_layout_scales_to_new_canvas(self):
        capture_client = MutableLayoutClient(canvas=(1920, 1080))
        manager = OBSLayoutManager(capture_client)
        profile = manager.capture_profile("In Game")
        profile["modules"]["[Webcam] Cadre"]["geometry"] = {
            "x": 960.0,
            "y": 540.0,
            "width": 480.0,
            "height": 270.0,
        }
        profile["modules"]["[Webcam] Cadre"]["normalized_geometry"] = {
            "x": 0.5,
            "y": 0.5,
            "width": 0.25,
            "height": 0.25,
        }

        client = MutableLayoutClient(canvas=(3840, 2160))
        manager = OBSLayoutManager(client)
        manager.apply_profile(profile, record_undo=False)
        # First element starts at the module origin and follows the scaled geometry.
        self.assertAlmostEqual(client.transforms[1]["positionX"], 1920.0)
        self.assertAlmostEqual(client.transforms[1]["positionY"], 1080.0)


    def test_nested_scene_modules_and_internal_png_follow_canvas_change(self):
        capture_client = NestedLayoutClient(canvas=(1920, 1080))
        manager = OBSLayoutManager(capture_client)
        profile = manager.capture_profile("In Game")

        webcam = profile["modules"]["[Module] Webcam"]
        avatar = profile["modules"]["[Module] Avatar"]
        self.assertEqual(webcam["coordinate_space"], "root_canvas")
        self.assertEqual(avatar["coordinate_space"], "root_canvas")
        self.assertIn("normalized_geometry", webcam)
        self.assertIn("normalized_geometry", avatar)
        self.assertEqual(
            [item["source"] for item in profile.get("support_items", [])],
            ["Avatar Dynamic"],
        )

        client = NestedLayoutClient(canvas=(2560, 1440))
        manager = OBSLayoutManager(client)
        manager.apply_profile(profile, record_undo=False)

        # Both scene items live in OBS scene-canvas coordinates and therefore
        # keep the same relative position/size on the 4/3 larger canvas.
        self.assertAlmostEqual(client.transforms[("In Game", 1)]["positionX"], 1600.0)
        self.assertAlmostEqual(client.transforms[("In Game", 1)]["width"], 800.0)
        self.assertAlmostEqual(client.transforms[("[Module] Webcam", 2)]["positionX"], 133.3333333)
        self.assertAlmostEqual(client.transforms[("[Module] Webcam", 2)]["width"], 400.0)

        # The ordinary PNG inside the managed Avatar scene is captured as an
        # internal support item and follows the same canvas ratio. Without this
        # step the nested scene source would move correctly while its actual
        # avatar artwork stayed at its old 1080p size/offset.
        png = client.transforms[("[Module] Avatar", 3)]
        self.assertAlmostEqual(png["positionX"], 26.6666667)
        self.assertAlmostEqual(png["positionY"], 26.6666667)
        self.assertAlmostEqual(png["scaleX"], 4.0 / 3.0)
        self.assertAlmostEqual(png["scaleY"], 4.0 / 3.0)
        self.assertAlmostEqual(png["width"], 260.0 * 4.0 / 3.0)
        self.assertAlmostEqual(png["height"], 320.0 * 4.0 / 3.0)

    def test_group_local_avatar_stays_local_while_owned_scene_internals_scale(self):
        capture_client = NestedGroupLayoutClient(canvas=(1920, 1080))
        profile = OBSLayoutManager(capture_client).capture_profile("In Game")

        avatar = profile["modules"]["[Module] Avatar"]
        self.assertEqual(avatar["coordinate_space"], "container_local")
        support = {(item["container"], item["source"]): item for item in profile["support_items"]}
        self.assertIn(("[Module] Webcam", "WebCam"), support)
        self.assertEqual(support[("[Module] Webcam", "WebCam")]["source_type"], "group")
        self.assertIn(("WebCam", "Cadre Webcam"), support)
        self.assertIn(("[Module] Avatar", "Avatar Dynamic"), support)
        self.assertFalse(
            any(
                request == "GetSceneItemList" and payload.get("sceneName") == "WebCam"
                for request, payload in capture_client.calls
            )
        )

        client = NestedGroupLayoutClient(canvas=(2560, 1440))
        OBSLayoutManager(client).apply_profile(profile, record_undo=False)

        # Avatar is group-local: keep its 300x360 local rectangle by
        # compensating the intrinsic 4/3 growth of its scene source.
        avatar_item = client.transforms[("WebCam", 2)]
        self.assertAlmostEqual(avatar_item["positionX"], 100.0)
        self.assertAlmostEqual(avatar_item["positionY"], 80.0)
        self.assertAlmostEqual(avatar_item["width"], 300.0)
        self.assertAlmostEqual(avatar_item["height"], 360.0)

        # The PNG inside the Avatar scene scales 4/3. After the 3/4 scene-source
        # compensation above, it keeps the same local visual size. The enclosing
        # WebCam group itself scales 4/3 on the scene canvas, so frame and avatar
        # finally grow together and remain aligned.
        png = client.transforms[("[Module] Avatar", 3)]
        self.assertAlmostEqual(png["scaleX"], 4.0 / 3.0)
        self.assertAlmostEqual(png["scaleY"], 4.0 / 3.0)
        group = client.transforms[("[Module] Webcam", 4)]
        self.assertAlmostEqual(group["positionX"], 133.3333333)
        self.assertAlmostEqual(group["positionY"], 106.6666667)
        self.assertAlmostEqual(group["width"], 400.0 * 4.0 / 3.0)
        self.assertAlmostEqual(group["height"], 360.0 * 4.0 / 3.0)

    def test_group_transform_is_resolved_after_obs_group_resize_settles(self):
        capture_client = DeferredGroupResizeClient(canvas=(1920, 1080))
        profile = OBSLayoutManager(capture_client).capture_profile("In Game")

        client = DeferredGroupResizeClient(canvas=(1920, 1080))
        client.set_canvas((2560, 1440))
        manager = OBSLayoutManager(client)
        manager._wait_group_resize_settle = client.settle_groups
        manager.apply_profile(profile, record_undo=False)

        # SSR must wait until the group has recomputed its intrinsic bounds
        # before resolving the group's final scale. Otherwise 533/500 would be
        # used instead of the correct 533/400 and the whole module would shrink.
        self.assertGreaterEqual(client.group_settles, 2)
        group = client.transforms[("[Module] Webcam", 4)]
        self.assertAlmostEqual(group["scaleX"], 4.0 / 3.0)
        self.assertAlmostEqual(group["scaleY"], 4.0 / 3.0)
        self.assertAlmostEqual(group["width"], 400.0 * 4.0 / 3.0)
        self.assertAlmostEqual(group["height"], 360.0 * 4.0 / 3.0)

        avatar = client.transforms[("WebCam", 2)]
        self.assertAlmostEqual(avatar["width"], 300.0)
        self.assertAlmostEqual(avatar["height"], 360.0)


    def test_legacy_mislabeled_group_is_inferred_from_profile_topology(self):
        capture_client = NestedGroupLayoutClient(canvas=(1920, 1080))
        profile = OBSLayoutManager(capture_client).capture_profile("In Game")
        group_support = next(
            item
            for item in profile["support_items"]
            if item["container"] == "[Module] Webcam" and item["source"] == "WebCam"
        )
        # Reproduce the metadata bug from SSR 2.0.5/2.0.6.
        group_support["source_type"] = "scene"

        client = DeferredGroupResizeClient(canvas=(1920, 1080))
        client.set_canvas((2560, 1440))
        manager = OBSLayoutManager(client)
        manager._wait_group_resize_settle = client.settle_groups
        manager.apply_profile(profile, record_undo=False)

        # Topology inference must still route WebCam through the group
        # stabilization path, so old layouts remain usable after update.
        self.assertGreaterEqual(client.group_settles, 2)
        group = client.transforms[("[Module] Webcam", 4)]
        self.assertAlmostEqual(group["width"], 400.0 * 4.0 / 3.0)
        self.assertAlmostEqual(group["height"], 360.0 * 4.0 / 3.0)

    def test_nested_scene_source_preserves_relative_size_after_canvas_growth(self):
        capture_client = NestedLayoutClient(canvas=(1920, 1080))
        profile = OBSLayoutManager(capture_client).capture_profile("In Game")

        client = NestedLayoutClient(canvas=(2560, 1440))
        before = client.transforms[("[Module] Webcam", 2)]
        self.assertAlmostEqual(before["width"], 400.0)
        self.assertAlmostEqual(before["height"], 480.0)

        OBSLayoutManager(client).apply_profile(profile, record_undo=False)
        after = client.transforms[("[Module] Webcam", 2)]
        self.assertAlmostEqual(after["width"], 400.0)
        self.assertAlmostEqual(after["height"], 480.0)

    def test_preview_can_be_cancelled(self):
        client = MutableLayoutClient()
        manager = OBSLayoutManager(client)
        profile = manager.capture_profile("In Game")
        profile["coordinate_mode"] = "absolute"
        profile["modules"]["[Webcam] Cadre"]["geometry"] = {
            "x": 500.0,
            "y": 500.0,
            "width": 300.0,
            "height": 100.0,
        }
        original_x = client.transforms[1]["positionX"]
        manager.preview_profile(profile)
        self.assertNotEqual(client.transforms[1]["positionX"], original_x)
        manager.cancel_preview()
        self.assertAlmostEqual(client.transforms[1]["positionX"], original_x)

    def test_group_scene_items_are_discovered(self):
        manager = OBSLayoutManager(GroupClient())
        modules = manager.discover_scene("In Game")
        self.assertIn("[Chat] Browser", modules)
        self.assertEqual(modules["[Chat] Browser"][0].container, "HUD")

    def test_local_api_routes_status_and_actions(self):
        calls = []
        api = LocalControlAPI(
            APIConfig(enabled=True, host="127.0.0.1", port=0),
            status=lambda: {"paused": False},
            action=lambda name, payload: calls.append((name, payload)) or {"done": True},
        )
        api.start()
        try:
            with urlopen(f"http://127.0.0.1:{api.bound_port}/status", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            req = Request(
                f"http://127.0.0.1:{api.bound_port}/reapply",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["done"])
            self.assertEqual(calls[0][0], "reapply")
        finally:
            api.stop()

    def test_runtime_owned_support_visibility_is_not_captured_or_reapplied(self):
        capture_client = NestedLayoutClient(canvas=(1920, 1080))
        manager = OBSLayoutManager(capture_client)
        manager.set_runtime_visibility_owners({
            ("[Module] Avatar", "Avatar Dynamic")
        })

        # Simulate capture while the temporary animation is currently visible.
        capture_client.enabled[("[Module] Avatar", 3)] = True
        profile = manager.capture_profile("In Game")
        support = next(
            item for item in profile["support_items"]
            if item["source"] == "Avatar Dynamic"
        )
        self.assertFalse(support["enabled"])
        self.assertEqual(support["visibility_owner"], "runtime")

        # Applying the layout still owns/scales geometry but must not alter the
        # temporary visibility state.
        client = NestedLayoutClient(canvas=(2560, 1440))
        client.enabled[("[Module] Avatar", 3)] = True
        manager = OBSLayoutManager(client)
        manager.set_runtime_visibility_owners({
            ("[Module] Avatar", "Avatar Dynamic")
        })
        client.calls.clear()
        manager.apply_profile(profile, record_undo=False)

        self.assertTrue(client.enabled[("[Module] Avatar", 3)])
        png_visibility_calls = [
            payload
            for request, payload in client.calls
            if request == "SetSceneItemEnabled"
            and payload.get("sceneName") == "[Module] Avatar"
            and payload.get("sceneItemId") == 3
        ]
        self.assertEqual(png_visibility_calls, [])
        self.assertAlmostEqual(
            client.transforms[("[Module] Avatar", 3)]["scaleX"],
            4.0 / 3.0,
        )

    def test_preview_snapshot_does_not_restore_runtime_owned_visibility(self):
        client = MutableLayoutClient()
        manager = OBSLayoutManager(client)
        manager.set_runtime_visibility_owners({("In Game", "[Webcam] Avatar")})
        profile = manager.capture_profile("In Game")
        profile["coordinate_mode"] = "absolute"
        profile["modules"]["[Webcam] Avatar"]["geometry"]["x"] = 700.0

        # Runtime turns the source off immediately before the preview.
        client.enabled[2] = False
        manager.preview_profile(profile)
        self.assertFalse(client.enabled[2])

        # Runtime changes it while preview is open. Cancel must restore geometry
        # only and leave the runtime visibility untouched.
        client.enabled[2] = True
        manager.cancel_preview()
        self.assertTrue(client.enabled[2])


if __name__ == "__main__":
    unittest.main()
