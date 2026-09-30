from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Any, Iterable


def frame_progress(duration_ms: int, fps: float) -> tuple[float, ...]:
    duration = max(1, int(duration_ms))
    rate = max(1.0, float(fps))
    intervals = max(1, int(math.ceil((duration / 1000.0) * rate)))
    return tuple(index / intervals for index in range(intervals + 1))


def build_multi_item_frame_requests(
    *,
    scene: str,
    scene_item_ids: Iterable[int],
    start_x: dict[int, float],
    delta_x: float,
    duration_ms: int,
    fps: float,
) -> list[list[dict[str, Any]]]:
    item_ids = tuple(int(item) for item in scene_item_ids)
    points = frame_progress(duration_ms, fps)
    return [
        [
            {
                "requestType": "SetSceneItemTransform",
                "requestData": {
                    "sceneName": str(scene),
                    "sceneItemId": item_id,
                    "sceneItemTransform": {
                        "positionX": float(start_x[item_id])
                        + float(delta_x) * progress,
                    },
                },
            }
            for item_id in item_ids
        ]
        for progress in points[1:]
    ]


def build_transform_requests(
    *,
    scene: str,
    scene_item_id: int,
    start_x: float,
    delta_x: float,
    duration_ms: int,
    fps: float,
) -> list[dict[str, Any]]:
    frames = build_multi_item_frame_requests(
        scene=scene,
        scene_item_ids=(scene_item_id,),
        start_x={int(scene_item_id): float(start_x)},
        delta_x=delta_x,
        duration_ms=duration_ms,
        fps=fps,
    )
    return [frame[0] for frame in frames]


def build_serial_frame_requests_for_items(
    *,
    scene: str,
    scene_item_ids: Iterable[int],
    start_x: dict[int, float],
    delta_x: float,
    duration_ms: int,
    fps: float,
) -> list[dict[str, Any]]:
    frames = build_multi_item_frame_requests(
        scene=scene,
        scene_item_ids=scene_item_ids,
        start_x=start_x,
        delta_x=delta_x,
        duration_ms=duration_ms,
        fps=fps,
    )
    batch: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        batch.extend(frame)
        if frame_index + 1 < len(frames):
            batch.append(
                {
                    "requestType": "Sleep",
                    "requestData": {"sleepFrames": 1},
                }
            )
    return batch


def build_serial_frame_requests(**kwargs) -> list[dict[str, Any]]:
    scene_item_id = int(kwargs["scene_item_id"])
    return build_serial_frame_requests_for_items(
        scene=str(kwargs["scene"]),
        scene_item_ids=(scene_item_id,),
        start_x={scene_item_id: float(kwargs["start_x"])},
        delta_x=float(kwargs["delta_x"]),
        duration_ms=int(kwargs["duration_ms"]),
        fps=float(kwargs["fps"]),
    )


def benchmark_plan(
    *,
    duration_ms: int,
    fps: float,
    items: int = 1,
) -> dict[str, object]:
    item_count = max(1, int(items))
    frame_count = len(frame_progress(duration_ms, fps)) - 1
    sequential_sets = frame_count * item_count
    batch_requests = sequential_sets + max(0, frame_count - 1)
    return {
        "duration_ms": int(duration_ms),
        "fps": float(fps),
        "items": item_count,
        "visual_frames": frame_count,
        "sequential": {
            "websocket_messages": sequential_sets,
            "obs_requests": sequential_sets,
            "set_requests": sequential_sets,
        },
        "serial_frame_batch": {
            "websocket_messages": 1,
            "obs_requests": batch_requests,
            "set_requests": sequential_sets,
            "sleep_requests": max(0, frame_count - 1),
        },
    }


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


async def _call_checked(
    ws,
    obs,
    request_type: str,
    request_data: dict[str, Any] | None = None,
):
    response = await ws.call(obs.Request(request_type, request_data))
    if not response.ok():
        status = getattr(response, "requestStatus", None)
        raise RuntimeError(f"{request_type} failed: {status}")
    return response.responseData or {}


async def _read_x(
    ws,
    obs,
    *,
    scene: str,
    scene_item_id: int,
) -> float:
    response = await _call_checked(
        ws,
        obs,
        "GetSceneItemTransform",
        {
            "sceneName": scene,
            "sceneItemId": scene_item_id,
        },
    )
    transform = response.get("sceneItemTransform") or {}
    raw_x = transform.get("positionX")
    if isinstance(raw_x, bool) or not isinstance(raw_x, (int, float)):
        raise RuntimeError(
            f"OBS n'a pas renvoyé positionX pour le Scene Item {scene_item_id}"
        )
    return float(raw_x)


async def _restore_x(
    ws,
    obs,
    *,
    scene: str,
    original_x: dict[int, float],
) -> None:
    for scene_item_id, x in original_x.items():
        await _call_checked(
            ws,
            obs,
            "SetSceneItemTransform",
            {
                "sceneName": scene,
                "sceneItemId": scene_item_id,
                "sceneItemTransform": {"positionX": float(x)},
            },
        )


async def run_live(args: argparse.Namespace) -> dict[str, object]:
    try:
        import simpleobsws as obs
    except ImportError as exc:
        raise RuntimeError(
            "Le benchmark live requiert le paquet optionnel simpleobsws. "
            "Installez requirements/lab-obs-websocket.txt dans un "
            "environnement de laboratoire séparé."
        ) from exc

    password = os.environ.get(args.password_env, "")
    ws = obs.WebSocketClient(
        url=f"ws://{args.host}:{args.port}",
        password=password,
    )
    await ws.connect()
    await ws.wait_until_identified()

    original_x: dict[int, float] = {}
    try:
        version_payload = await _call_checked(ws, obs, "GetVersion")
        for scene_item_id in args.scene_item_id:
            original_x[scene_item_id] = await _read_x(
                ws,
                obs,
                scene=args.scene,
                scene_item_id=scene_item_id,
            )

        frames = build_multi_item_frame_requests(
            scene=args.scene,
            scene_item_ids=args.scene_item_id,
            start_x=original_x,
            delta_x=args.delta_x,
            duration_ms=args.duration_ms,
            fps=args.fps,
        )
        batch_requests = build_serial_frame_requests_for_items(
            scene=args.scene,
            scene_item_ids=args.scene_item_id,
            start_x=original_x,
            delta_x=args.delta_x,
            duration_ms=args.duration_ms,
            fps=args.fps,
        )
        interval = (args.duration_ms / 1000.0) / max(1, len(frames))

        sequential_totals: list[float] = []
        sequential_call_ms: list[float] = []
        sequential_frame_ms: list[float] = []
        batch_totals: list[float] = []

        for _ in range(args.repeat):
            await _restore_x(
                ws,
                obs,
                scene=args.scene,
                original_x=original_x,
            )
            started = time.perf_counter()
            for frame in frames:
                frame_started = time.perf_counter()
                for request in frame:
                    call_started = time.perf_counter()
                    await _call_checked(
                        ws,
                        obs,
                        request["requestType"],
                        request["requestData"],
                    )
                    sequential_call_ms.append(
                        (time.perf_counter() - call_started) * 1000.0
                    )
                frame_elapsed = time.perf_counter() - frame_started
                sequential_frame_ms.append(frame_elapsed * 1000.0)
                await asyncio.sleep(max(0.0, interval - frame_elapsed))
            sequential_totals.append(
                (time.perf_counter() - started) * 1000.0
            )

            await _restore_x(
                ws,
                obs,
                scene=args.scene,
                original_x=original_x,
            )
            requests = [
                obs.Request(
                    item["requestType"],
                    item.get("requestData"),
                )
                for item in batch_requests
            ]
            started = time.perf_counter()
            responses = await ws.call_batch(
                requests,
                halt_on_failure=True,
                execution_type=obs.RequestBatchExecutionType.SerialFrame,
            )
            batch_totals.append(
                (time.perf_counter() - started) * 1000.0
            )
            failures = [
                response
                for response in responses
                if not response.ok()
            ]
            if failures:
                raise RuntimeError(
                    "SerialFrame batch contains "
                    f"{len(failures)} failed request(s)"
                )

        def summary(values: list[float]) -> dict[str, float]:
            return {
                "min_ms": min(values),
                "median_ms": statistics.median(values),
                "max_ms": max(values),
            }

        return {
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "simpleobsws": _package_version("simpleobsws"),
                "obs_version": str(version_payload.get("obsVersion") or ""),
                "obs_websocket_version": str(
                    version_payload.get("obsWebSocketVersion") or ""
                ),
                "rpc_version": version_payload.get("rpcVersion"),
            },
            "target": {
                "scene": args.scene,
                "scene_item_ids": list(args.scene_item_id),
                "delta_x": args.delta_x,
            },
            "plan": benchmark_plan(
                duration_ms=args.duration_ms,
                fps=args.fps,
                items=len(args.scene_item_id),
            ),
            "repeat": args.repeat,
            "sequential_total": summary(sequential_totals),
            "sequential_request_latency": summary(
                sequential_call_ms
            ),
            "sequential_frame_write_time": summary(
                sequential_frame_ms
            ),
            "serial_frame_total": summary(batch_totals),
        }
    finally:
        if original_x:
            try:
                await _restore_x(
                    ws,
                    obs,
                    scene=args.scene,
                    original_x=original_x,
                )
            except Exception:
                pass
        await ws.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Laboratoire SSR : comparer des SetSceneItemTransform séquentiels "
            "à un RequestBatch SerialFrame. Ne fait partie d'aucun chemin runtime."
        )
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Afficher seulement la charge théorique",
    )
    parser.add_argument(
        "--items",
        type=int,
        default=1,
        help="Nombre d'items pour le plan théorique",
    )
    parser.add_argument("--duration-ms", type=int, default=1000)
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4455)
    parser.add_argument(
        "--password-env",
        default="OBS_WEBSOCKET_PASSWORD",
    )
    parser.add_argument("--scene", default="")
    parser.add_argument(
        "--scene-item-id",
        type=int,
        action="append",
        default=[],
        help=(
            "ID d'un Scene Item de laboratoire. Répétez l'option pour "
            "tester plusieurs items par frame."
        ),
    )
    parser.add_argument("--delta-x", type=float, default=20.0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--confirm-live-mutation",
        action="store_true",
        help=(
            "Obligatoire pour exécuter le benchmark qui déplace "
            "temporairement les Scene Items."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.plan:
        payload = benchmark_plan(
            duration_ms=args.duration_ms,
            fps=args.fps,
            items=args.items,
        )
    else:
        if not args.confirm_live_mutation:
            raise SystemExit(
                "Refusé : ajoutez --confirm-live-mutation "
                "pour le benchmark live."
            )
        if (
            not args.scene.strip()
            or not args.scene_item_id
            or any(item <= 0 for item in args.scene_item_id)
        ):
            raise SystemExit(
                "--scene et au moins un --scene-item-id > 0 "
                "sont requis en mode live."
            )
        if len(set(args.scene_item_id)) != len(args.scene_item_id):
            raise SystemExit("--scene-item-id ne doit pas contenir de doublon.")
        if args.repeat <= 0:
            raise SystemExit("--repeat doit être > 0.")
        payload = asyncio.run(run_live(args))

    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    print(rendered)
    if args.output:
        Path(args.output).write_text(
            rendered + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
