from __future__ import annotations

import unittest

from stream_state_router.obs.dispatcher import OBSDispatcher, profile_map_from_raw
from stream_state_router.obs.models import OBSAction
from stream_state_router.router.models import StreamState


class FakeClient:
    def __init__(self):
        self.calls = []
        self.scene_item_ids = [42]

    def send(self, request, data=None):
        self.calls.append((request, data))
        if request == "GetSceneItemId":
            if len(self.scene_item_ids) > 1:
                return {"sceneItemId": self.scene_item_ids.pop(0)}
            return {"sceneItemId": self.scene_item_ids[0]}
        return {}


class OBSDispatcherTests(unittest.TestCase):
    def test_only_changed_profile_domains_are_dispatched(self):
        client = FakeClient()
        profiles = profile_map_from_raw(
            {
                "game": {
                    "Vanilla": {"actions": []},
                    "Overwatch": {"actions": [{"type": "set_program_scene", "params": {"scene": "OW"}}]},
                },
                "overlay": {
                    "FPS": {"actions": [{"type": "scene_item_enabled", "params": {"scene": "OW", "source": "FPS", "enabled": True}}]},
                },
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        state = StreamState("Overwatch", "FPS", "Default", "Default")
        first = dispatcher.dispatch_state(state)
        second = dispatcher.dispatch_state(state)
        self.assertEqual(first.executed, 2)
        self.assertEqual(second.executed, 0)
        self.assertEqual(len(client.calls), 3)  # scene change + lookup + scene item enabled

    def test_scene_item_id_is_resolved_fresh_for_each_visibility_action(self):
        client = FakeClient()
        client.scene_item_ids = [42, 99]
        dispatcher = OBSDispatcher(client, {})
        action = OBSAction("scene_item_enabled", {"scene": "A", "source": "B", "enabled": True})

        dispatcher.execute_action(action)
        dispatcher.execute_action(action)

        lookups = [call for call in client.calls if call[0] == "GetSceneItemId"]
        writes = [call for call in client.calls if call[0] == "SetSceneItemEnabled"]
        self.assertEqual(len(lookups), 2)
        self.assertEqual([call[1]["sceneItemId"] for call in writes], [42, 99])

    def test_supported_action_shapes(self):
        client = FakeClient()
        dispatcher = OBSDispatcher(client, {})
        actions = [
            OBSAction("set_program_scene", {"scene": "Game"}),
            OBSAction("source_filter_enabled", {"source": "Game", "filter": "HDR", "enabled": False}),
            OBSAction("input_mute", {"input": "Mic", "muted": True}),
            OBSAction("input_volume_db", {"input": "Music", "volume_db": -12.5}),
            OBSAction("set_input_settings", {"input": "Text", "settings": {"text": "Hello"}, "overlay": True}),
        ]
        for action in actions:
            dispatcher.execute_action(action)
        self.assertEqual([r for r, _ in client.calls], [
            "SetCurrentProgramScene",
            "SetSourceFilterEnabled",
            "SetInputMute",
            "SetInputVolume",
            "SetInputSettings",
        ])

    def test_layout_profile_is_dispatched_as_fifth_state_domain(self):
        client = FakeClient()
        layout = {
            "scene": "Gameplay",
            "modules": {
                "Webcam": {
                    "visible": True,
                    "lock_aspect": True,
                    "anchor": "top_left",
                    "base_bounds": {"x": 10, "y": 20, "width": 100, "height": 50},
                    "geometry": {"x": 20, "y": 40, "width": 200, "height": 100},
                    "elements": [
                        {
                            "source": "[Webcam] Cadre",
                            "included": True,
                            "enabled": True,
                            "transform": {
                                "positionX": 10,
                                "positionY": 20,
                                "scaleX": 1.0,
                                "scaleY": 1.0,
                                "boundsType": "OBS_BOUNDS_NONE",
                            },
                        }
                    ],
                }
            },
        }
        dispatcher = OBSDispatcher(client, {}, {"FPS": layout})
        state = StreamState(layout_profile="FPS")

        result = dispatcher.dispatch_state(state)

        self.assertIn("layout", result.changed_domains)
        self.assertEqual(result.executed, 1)
        requests = [request for request, _payload in client.calls]
        self.assertIn("SetSceneItemTransform", requests)
        self.assertIn("SetSceneItemEnabled", requests)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            OBSDispatcher(FakeClient(), {}).execute_action(OBSAction("nope", {}))


if __name__ == "__main__":
    unittest.main()
