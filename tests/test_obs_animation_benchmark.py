from __future__ import annotations

import unittest

from scripts.benchmark_obs_websocket_animation import (
    benchmark_plan,
    build_multi_item_frame_requests,
    build_serial_frame_requests,
    build_serial_frame_requests_for_items,
    build_transform_requests,
    frame_progress,
)


class OBSAnimationBenchmarkPlanTests(unittest.TestCase):
    def test_progress_has_exact_endpoints_and_render_cadence(self) -> None:
        points = frame_progress(1000, 60.0)
        self.assertEqual(points[0], 0.0)
        self.assertEqual(points[-1], 1.0)
        self.assertEqual(len(points), 61)

    def test_serial_frame_workload_inserts_one_frame_sleeps(self) -> None:
        batch = build_serial_frame_requests(
            scene="In Game",
            scene_item_id=42,
            start_x=100.0,
            delta_x=20.0,
            duration_ms=100,
            fps=10.0,
        )
        self.assertEqual(
            [item["requestType"] for item in batch],
            ["SetSceneItemTransform"],
        )
        self.assertEqual(
            batch[-1]["requestData"]["sceneItemTransform"]["positionX"],
            120.0,
        )

        longer = build_serial_frame_requests(
            scene="In Game",
            scene_item_id=42,
            start_x=100.0,
            delta_x=20.0,
            duration_ms=1000,
            fps=2.0,
        )
        self.assertEqual(
            [item["requestType"] for item in longer],
            [
                "SetSceneItemTransform",
                "Sleep",
                "SetSceneItemTransform",
            ],
        )
        self.assertEqual(
            longer[1]["requestData"],
            {"sleepFrames": 1},
        )

    def test_plan_separates_websocket_messages_from_obs_requests(self) -> None:
        plan = benchmark_plan(
            duration_ms=1000,
            fps=60.0,
            items=1,
        )
        self.assertEqual(plan["visual_frames"], 60)
        self.assertEqual(
            plan["sequential"]["websocket_messages"],
            60,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["websocket_messages"],
            1,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["set_requests"],
            60,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["sleep_requests"],
            59,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["obs_requests"],
            119,
        )

    def test_plan_scales_set_operations_for_multiple_items(self) -> None:
        plan = benchmark_plan(
            duration_ms=1000,
            fps=60.0,
            items=3,
        )
        self.assertEqual(plan["visual_frames"], 60)
        self.assertEqual(
            plan["sequential"]["set_requests"],
            180,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["set_requests"],
            180,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["sleep_requests"],
            59,
        )
        self.assertEqual(
            plan["serial_frame_batch"]["obs_requests"],
            239,
        )

    def test_transform_workload_uses_explicit_scene_item_id(self) -> None:
        requests = build_transform_requests(
            scene="In Game",
            scene_item_id=99,
            start_x=0.0,
            delta_x=10.0,
            duration_ms=500,
            fps=2.0,
        )
        self.assertEqual(len(requests), 1)
        data = requests[0]["requestData"]
        self.assertEqual(data["sceneName"], "In Game")
        self.assertEqual(data["sceneItemId"], 99)
        self.assertEqual(
            data["sceneItemTransform"]["positionX"],
            10.0,
        )

    def test_multi_item_frames_group_sets_before_sleep(self) -> None:
        frames = build_multi_item_frame_requests(
            scene="In Game",
            scene_item_ids=(10, 20),
            start_x={10: 0.0, 20: 100.0},
            delta_x=20.0,
            duration_ms=1000,
            fps=2.0,
        )
        self.assertEqual(len(frames), 2)
        self.assertEqual(
            [
                item["requestData"]["sceneItemId"]
                for item in frames[0]
            ],
            [10, 20],
        )

        batch = build_serial_frame_requests_for_items(
            scene="In Game",
            scene_item_ids=(10, 20),
            start_x={10: 0.0, 20: 100.0},
            delta_x=20.0,
            duration_ms=1000,
            fps=2.0,
        )
        self.assertEqual(
            [item["requestType"] for item in batch],
            [
                "SetSceneItemTransform",
                "SetSceneItemTransform",
                "Sleep",
                "SetSceneItemTransform",
                "SetSceneItemTransform",
            ],
        )
        self.assertEqual(
            batch[2]["requestData"],
            {"sleepFrames": 1},
        )
        self.assertEqual(
            batch[-1]["requestData"]["sceneItemTransform"]["positionX"],
            120.0,
        )


if __name__ == "__main__":
    unittest.main()
