# OBS WebSocket animation benchmark lab

This lab is deliberately outside the production runtime. It exists to answer one
question before any OBS plugin/bridge work: how much of the layout-animation
cost can be removed by using obs-websocket RequestBatch SerialFrame?

The protocol documents SerialFrame as a serial batch mode synchronized with the
graphics thread and intended for animation workloads. The current production
SSR path is not changed by this experiment.

## Offline plan

No OBS connection or mutation:

    python scripts/benchmark_obs_websocket_animation.py --plan --duration-ms 1000 --fps 60

The report separates WebSocket messages from OBS requests. SerialFrame reduces
network round-trips; it does not make the contained Set/Sleep operations
disappear.

## Live laboratory run

The live runner uses the optional third-party package `simpleobsws` only inside
the script. It is intentionally not added to SSR's production dependency
closure.

Use a disposable scene/item and make the OBS WebSocket password available in
the environment rather than on the command line:

    $env:OBS_WEBSOCKET_PASSWORD = "..."
    python scripts/benchmark_obs_websocket_animation.py \
      --scene "TEST SSR" \
      --scene-item-id 42 \
      --duration-ms 1000 \
      --fps 60 \
      --repeat 5 \
      --confirm-live-mutation

The benchmark changes only `positionX` of the selected Scene Item and attempts
to restore the original value in a `finally` block. It must never be run
against an irreplaceable live layout.

## Measurements to record on the real OBS machine

Record at least:

- total duration for sequential round-trip animation;
- per-request sequential latency distribution;
- total SerialFrame batch duration;
- visible jitter/stutter;
- OBS CPU/GPU impact;
- SSR/Python CPU impact;
- failures or divergent final position;
- behavior while OBS is under representative streaming load.

Run the same target/duration repeatedly and keep the raw JSON outputs.

## Decision gate

A native OBS bridge is not justified by "batching" alone. Before a bridge is
considered for animation performance, SerialFrame must first be tested on the
real OBS 32.x setup. A bridge becomes interesting only if it still provides a
material, repeatable improvement or solves a safety/identity problem that the
WebSocket path cannot solve cleanly.
