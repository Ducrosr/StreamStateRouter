# OBS WebSocket animation benchmark lab

This lab is deliberately outside the production runtime. It exists to answer one
question before any OBS plugin/bridge work: how much of the layout-animation
cost can be removed by using obs-websocket RequestBatch SerialFrame?

The current production SSR path is not changed by this experiment.

## Frozen laboratory dependency

The live runner uses the laboratory-only dependency closure:

    python -m pip install -r requirements/lab-obs-websocket.txt

It is intentionally excluded from SSR's production dependency closure. The lab
currently pins `simpleobsws==1.4.3` so two benchmark sessions can be compared
without silently changing the client implementation.

## Offline plan

No OBS connection or mutation:

    python scripts/benchmark_obs_websocket_animation.py --plan --duration-ms 1000 --fps 60 --items 3

The report separates:

- requested visual frames;
- SetSceneItemTransform operations;
- total OBS requests;
- WebSocket messages.

SerialFrame reduces network round-trips; it does not make the contained
Set/Sleep operations disappear.

## Live laboratory run

Use a disposable scene and make the OBS WebSocket password available in the
environment rather than on the command line:

    $env:OBS_WEBSOCKET_PASSWORD = "..."
    python scripts/benchmark_obs_websocket_animation.py \
      --scene "TEST SSR" \
      --scene-item-id 42 \
      --scene-item-id 43 \
      --scene-item-id 44 \
      --duration-ms 1000 \
      --fps 60 \
      --repeat 5 \
      --output ".\benchmark-3-items.json" \
      --confirm-live-mutation

Repeat `--scene-item-id` to test several items in the same visual frame. The
sequential baseline writes all selected items for a frame and then waits for the
next frame. The SerialFrame batch places all Set requests for one visual frame
before a one-frame Sleep.

The benchmark changes only `positionX` of the selected Scene Items and attempts
to restore every original value in a `finally` block. It must never be run
against an irreplaceable live layout.

## Environment metadata

Every live JSON result records:

- Python version and platform;
- simpleobsws version;
- OBS version;
- obs-websocket version;
- negotiated RPC version;
- scene and exact Scene Item IDs;
- duration, target FPS, item count and operation counts.

Keep this metadata with every raw result. Performance numbers from different
OBS/client versions must not be compared as though they were one population.

## Measurements to record on the real OBS machine

Record at least:

- total duration for sequential round-trip animation;
- per-request sequential latency distribution;
- per-frame sequential write time;
- total SerialFrame batch duration;
- visible jitter/stutter;
- OBS CPU/GPU impact;
- SSR/Python CPU impact;
- failures or divergent final positions;
- behavior while OBS is under representative streaming load.

Run at least the same 1-item and 3-item target/duration repeatedly and keep the
raw JSON outputs.

## Production-path baseline

The lab deliberately does not instrument `OBSLayoutManager` while A1 is frozen.
After A1 is approved and integrated, add a third baseline using the real SSR
layout path (resolution, collection/session barriers, revalidation, mutation and
readback). That baseline is required before concluding that SerialFrame or a
native bridge materially improves the complete SSR workload.

## Decision gate

A native OBS bridge is not justified by "batching" alone. Before a bridge is
considered for animation performance, SerialFrame must first be tested on the
real OBS 32.x setup. A bridge becomes interesting only if it still provides a
material, repeatable improvement or solves a safety/identity problem that the
WebSocket path cannot solve cleanly.
