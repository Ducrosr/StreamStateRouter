from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..obs.dispatcher import DispatchResult, OBSDispatcher
from ..router.engine import StateChange, StateRouterEngine
from ..router.foreground import WindowsForegroundProvider
from ..router.models import ForegroundApp, StreamState


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: str
    message: str


class RoutingService:
    """Background foreground observer + state router + OBS dispatcher."""

    def __init__(
        self,
        engine: StateRouterEngine,
        dispatcher: OBSDispatcher,
        *,
        poll_ms: int = 50,
        provider=None,
        logger: logging.Logger | None = None,
        obs_probe_seconds: float = 2.0,
    ) -> None:
        self.engine = engine
        self.dispatcher = dispatcher
        self.poll_seconds = max(0.02, poll_ms / 1000.0)
        self.provider = provider or WindowsForegroundProvider()
        self.logger = logger or logging.getLogger("stream_state_router")
        self.obs_probe_seconds = max(0.5, float(obs_probe_seconds))

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._paused = False
        self._last_app: ForegroundApp | None = None
        self._dispatch_generation = 0
        self._last_obs_probe = 0.0
        self._last_obs_connected: bool | None = None

        self.on_foreground: Callable[[ForegroundApp | None], None] | None = None
        self.on_change: Callable[[StateChange], None] | None = None
        self.on_dispatch: Callable[[DispatchResult], None] | None = None
        self.on_event: Callable[[RuntimeEvent], None] | None = None

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def last_app(self) -> ForegroundApp | None:
        with self._lock:
            return self._last_app

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SSR-Router", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._dispatch_generation += 1
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def pause(self, paused: bool = True) -> None:
        with self._lock:
            self._paused = bool(paused)
        self._wake.set()
        self._emit(RuntimeEvent("pause", "Routage suspendu" if paused else "Routage repris"))

    def set_manual_override(
        self,
        state: StreamState,
        *,
        duration_seconds: float | None = None,
    ) -> StateChange | None:
        with self._lock:
            change = self.engine.set_manual_override(state, duration_seconds=duration_seconds)
        if change:
            self._apply_change(change)
        return change

    def clear_manual_override(self) -> StateChange | None:
        with self._lock:
            app = self._last_app
            change = self.engine.clear_manual_override(app)
        if change:
            self._apply_change(change)
        return change

    def force_reapply(self) -> DispatchResult | None:
        with self._lock:
            state = self.engine.current_state
        if state is None:
            return None
        result = self.dispatcher.dispatch_state(state, force=True)
        if self.on_dispatch:
            self.on_dispatch(result)
        return result

    def _run(self) -> None:
        self.logger.info("Routing service started")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._probe_obs_if_due()
                app = self.provider.get()
                with self._lock:
                    changed_app = app != self._last_app
                    self._last_app = app
                    paused = self._paused
                if changed_app:
                    self.logger.info(
                        "Foreground -> %s | %s",
                        app.exe_name if app else "<none>",
                        app.window_title if app else "",
                    )
                    if self.on_foreground:
                        self.on_foreground(app)
                if not paused:
                    with self._lock:
                        change = self.engine.observe(app)
                    if change:
                        self._apply_change(change)
            except Exception as exc:
                self.logger.exception("Routing loop error")
                self._emit(RuntimeEvent("error", str(exc)))

            elapsed = time.monotonic() - started
            wait_for = max(0.0, self.poll_seconds - elapsed)
            self._wake.wait(wait_for)
            self._wake.clear()
        self.logger.info("Routing service stopped")

    def _probe_obs_if_due(self) -> None:
        client = getattr(self.dispatcher, "client", None)
        config = getattr(client, "config", None)
        if client is None or config is None or not bool(getattr(config, "enabled", False)):
            self._last_obs_connected = None
            return

        now = time.monotonic()
        if self._last_obs_probe and now - self._last_obs_probe < self.obs_probe_seconds:
            return
        self._last_obs_probe = now

        ok, message = client.probe()
        if ok:
            if self._last_obs_connected is not True:
                self.logger.info("OBS connection established: %s", message)
                self._emit(RuntimeEvent("obs_connected", message))
            self._last_obs_connected = True
            return

        if self._last_obs_connected is not False:
            self.logger.warning("OBS connection unavailable: %s", message)
            self._emit(RuntimeEvent("obs_disconnected", message))
        self._last_obs_connected = False

    def _apply_change(self, change: StateChange) -> None:
        self.logger.info(
            "State decision [%s] -> %s (OBS delay %d ms)",
            change.rule_name,
            change.current.as_variables(),
            change.apply_delay_ms,
        )
        if self.on_change:
            self.on_change(change)
        with self._lock:
            self._dispatch_generation += 1
            generation = self._dispatch_generation
        if change.apply_delay_ms > 0:
            self._emit(
                RuntimeEvent(
                    "pending",
                    f"{change.rule_name}: application OBS dans {change.apply_delay_ms} ms",
                )
            )
            timer = threading.Timer(
                change.apply_delay_ms / 1000.0,
                self._dispatch_if_current,
                args=(change, generation),
            )
            timer.daemon = True
            timer.start()
        else:
            self._dispatch_if_current(change, generation)

    def _dispatch_if_current(self, change: StateChange, generation: int) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            if generation != self._dispatch_generation:
                return
            if self.engine.current_state != change.current:
                return
        try:
            result = self.dispatcher.dispatch_change(change)
            if self.on_dispatch:
                self.on_dispatch(result)
            if result.executed:
                self.logger.info(
                    "OBS dispatch: %d action(s), domains=%s",
                    result.executed,
                    ",".join(result.changed_domains),
                )
            for warning in result.warnings:
                self.logger.warning("OBS: %s", warning)
        except Exception as exc:
            self.logger.error("OBS dispatch failed: %s", exc)
            self._emit(RuntimeEvent("obs_error", str(exc)))

    def _emit(self, event: RuntimeEvent) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                self.logger.exception("Runtime event callback failed")
