from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Protocol

from ..events import EventBus
from .models import MediaState, MediaStateStore


class MediaProvider(Protocol):
    name: str

    def poll(self) -> MediaState: ...

    def artwork(self, key: str) -> tuple[bytes, str] | None: ...


@dataclass(frozen=True, slots=True)
class MediaEngineConfig:
    enabled: bool = True
    poll_interval_seconds: float = 1.0


class MediaEngine:
    def __init__(
        self,
        config: MediaEngineConfig,
        store: MediaStateStore,
        *,
        providers: tuple[MediaProvider, ...] = (),
        event_bus: EventBus | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.store = store
        self.providers = tuple(providers)
        self.event_bus = event_bus
        self._sleeper = sleeper
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_provider: MediaProvider | None = None
        self._last_error = ""

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def last_error(self) -> str:
        return self._last_error

    def _publish_change(
        self,
        previous: MediaState,
        current: MediaState,
    ) -> None:
        bus = self.event_bus
        if bus is None or previous == current:
            return
        event_type = "state"
        if previous.track_id != current.track_id and current.track_id:
            event_type = "track_changed"
        elif previous.playback != current.playback:
            event_type = "playback_changed"
        bus.publish(
            channel="media",
            type=event_type,
            platform=current.provider,
            payload=current.as_mapping(),
        )

    def poll_once(self) -> MediaState:
        previous = self.store.snapshot().state
        selected = MediaState()
        selected_provider: MediaProvider | None = None
        errors: list[str] = []
        for provider in self.providers:
            try:
                state = provider.poll()
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if state.track_id or state.playback in {"playing", "paused"}:
                selected = state
                selected_provider = provider
                break
            if not selected.provider:
                selected = state
                selected_provider = provider
        self._last_error = "; ".join(errors)
        self._active_provider = selected_provider
        self.store.update(selected)
        self._publish_change(previous, selected)
        return selected

    def artwork(self, key: str) -> tuple[bytes, str] | None:
        provider = self._active_provider
        if provider is None:
            return None
        return provider.artwork(key)

    def _run(self) -> None:
        interval = max(
            0.25,
            float(self.config.poll_interval_seconds),
        )
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        self._stop.clear()
        self.poll_once()
        thread = threading.Thread(
            target=self._run,
            name="SSR-MediaEngine",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
