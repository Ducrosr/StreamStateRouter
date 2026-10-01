from __future__ import annotations

from dataclasses import dataclass, field
import math
import queue
import threading
import time
from typing import Mapping
import uuid

from ..events import EventBus
from .base import MediaProvider
from .models import MediaState
from .state import MediaStateStore


@dataclass(frozen=True, slots=True)
class MediaRuntimeConfig:
    enabled: bool = False
    poll_seconds: float = 0.5

    def __post_init__(self) -> None:
        poll = float(self.poll_seconds)
        if not math.isfinite(poll) or poll < 0.1:
            raise ValueError("media.poll_seconds doit être >= 0.1")


@dataclass(frozen=True, slots=True)
class MediaCommandResult:
    request_id: str
    action: str
    success: bool
    error: str = ""
    state: Mapping[str, object] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "success": self.success,
            "error": self.error,
            "state": dict(self.state),
        }


@dataclass(frozen=True, slots=True)
class _MediaCommand:
    request_id: str
    action: str
    options: Mapping[str, object] = field(default_factory=dict)


class MediaRuntime:
    """Owns all provider I/O on one worker and publishes normalized state."""

    _SUPPORTED_ACTIONS = frozenset(
        {
            "play",
            "pause",
            "stop",
            "next",
            "previous",
            "seek",
            "set_volume",
            "play_uri",
        }
    )

    def __init__(
        self,
        config: MediaRuntimeConfig,
        provider: MediaProvider,
        *,
        state_store: MediaStateStore | None = None,
        event_bus: EventBus | None = None,
        clock=time.monotonic,
    ):
        self.config = config
        self.provider = provider
        self.state_store = state_store or MediaStateStore(provider.name)
        self.event_bus = event_bus
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._commands: queue.Queue[_MediaCommand] = queue.Queue()
        self._lock = threading.RLock()
        self._results: dict[str, MediaCommandResult] = {}
        self._last_semantic_key: tuple[object, ...] | None = None
        self._last_state: MediaState | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run,
            name="SSR-MediaRuntime",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        thread = self._thread
        self._stop.set()
        self._wake.set()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not bool(thread and thread.is_alive())
        if stopped:
            self._thread = None
        return stopped

    def request(
        self,
        action: str,
        **options: object,
    ) -> str:
        normalized = str(action or "").strip().casefold()
        if normalized not in self._SUPPORTED_ACTIONS:
            raise ValueError(f"Action média inconnue : {action}")
        if not self.running:
            raise RuntimeError("Media Runtime indisponible")
        request_id = uuid.uuid4().hex
        self._commands.put(
            _MediaCommand(
                request_id=request_id,
                action=normalized,
                options=dict(options),
            )
        )
        self._wake.set()
        return request_id

    def command_status(
        self,
        request_id: str,
    ) -> dict[str, object] | None:
        with self._lock:
            result = self._results.get(str(request_id or ""))
        return result.as_mapping() if result is not None else None

    def state(self) -> dict[str, object]:
        return self.state_store.snapshot()

    def poll_once(self) -> MediaState:
        try:
            state = self.provider.state()
        except Exception as exc:
            state = MediaState(
                provider=self.provider.name,
                connected=False,
                playback_state="unknown",
                error=str(exc),
            )
        self.state_store.update(state)
        semantic_key = state.semantic_key()
        if (
            self.event_bus is not None
            and semantic_key != self._last_semantic_key
        ):
            self.event_bus.publish(
                channel="media",
                type="state_changed",
                platform=self.provider.name,
                payload=state.as_mapping(),
            )
        self._last_semantic_key = semantic_key
        self._last_state = state
        return state

    def _execute(self, command: _MediaCommand) -> MediaCommandResult:
        try:
            action = command.action
            if action == "play":
                self.provider.play()
            elif action == "pause":
                self.provider.pause()
            elif action == "stop":
                self.provider.stop()
            elif action == "next":
                self.provider.next()
            elif action == "previous":
                self.provider.previous()
            elif action == "seek":
                self.provider.seek(
                    float(command.options.get("seconds", 0.0))
                )
            elif action == "set_volume":
                self.provider.set_volume(
                    float(command.options.get("percent", 0.0))
                )
            elif action == "play_uri":
                self.provider.play_uri(
                    str(command.options.get("uri") or "")
                )
            else:
                raise ValueError(
                    f"Action média inconnue : {command.action}"
                )
            state = self.poll_once()
            result = MediaCommandResult(
                request_id=command.request_id,
                action=command.action,
                success=True,
                state=self.state_store.snapshot(),
            )
            if self.event_bus is not None:
                self.event_bus.publish(
                    channel="media",
                    type="command",
                    platform=self.provider.name,
                    payload={
                        "request_id": command.request_id,
                        "action": command.action,
                        "success": True,
                        "state": state.as_mapping(),
                    },
                )
            return result
        except Exception as exc:
            self.poll_once()
            return MediaCommandResult(
                request_id=command.request_id,
                action=command.action,
                success=False,
                error=str(exc),
                state=self.state_store.snapshot(),
            )

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            now = self._clock()
            if now >= next_poll:
                self.poll_once()
                next_poll = now + float(self.config.poll_seconds)

            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                timeout = max(
                    0.01,
                    min(
                        0.25,
                        next_poll - self._clock(),
                    ),
                )
                self._wake.wait(timeout)
                self._wake.clear()
                continue

            result = self._execute(command)
            with self._lock:
                self._results[result.request_id] = result
                # Bound command diagnostics; callers only need recent requests.
                if len(self._results) > 250:
                    for key in tuple(self._results)[:50]:
                        self._results.pop(key, None)
            next_poll = self._clock() + float(self.config.poll_seconds)
