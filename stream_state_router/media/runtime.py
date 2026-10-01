from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import queue
import threading
import time
from typing import Mapping
import uuid

from ..events import EventBus
from .base import MediaProvider
from .models import MediaState, media_artwork_identity
from .state import MediaArtworkStore, MediaCommandStore, MediaStateStore


@dataclass(frozen=True, slots=True)
class MediaRuntimeConfig:
    enabled: bool = False
    poll_seconds: float = 0.5

    def __post_init__(self) -> None:
        poll = float(self.poll_seconds)
        if not math.isfinite(poll) or poll < 0.1:
            raise ValueError("media.poll_seconds doit être >= 0.1")
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "poll_seconds", poll)


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
            "status": "completed" if self.success else "failed",
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
            "enqueue_uri",
            "clear_queue",
        }
    )

    def __init__(
        self,
        config: MediaRuntimeConfig,
        provider: MediaProvider,
        *,
        state_store: MediaStateStore | None = None,
        artwork_store: MediaArtworkStore | None = None,
        command_store: MediaCommandStore | None = None,
        event_bus: EventBus | None = None,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        self.config = config
        self.provider = provider
        self.state_store = state_store or MediaStateStore(provider.name)
        self.state_store.set_expected_poll(config.poll_seconds)
        self.artwork_store = artwork_store or MediaArtworkStore()
        self.command_store = command_store or MediaCommandStore()
        self.event_bus = event_bus
        self._clock = clock
        self._wall_clock = wall_clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._commands: queue.Queue[_MediaCommand] = queue.Queue(
            maxsize=64
        )
        self._lock = threading.RLock()
        self._stopping = False
        self._last_semantic_key: tuple[object, ...] | None = None
        self._last_state: MediaState | None = None
        self._last_artwork_identity: tuple[str, ...] | None = None
        self._next_artwork_retry_at = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
            stopping = self._stopping
        return bool(
            thread is not None
            and thread.is_alive()
            and not stopping
        )

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stopping = False
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="SSR-MediaRuntime",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
            self._stopping = True
            self._stop.set()
            cancelled: list[_MediaCommand] = []
            while True:
                try:
                    cancelled.append(self._commands.get_nowait())
                except queue.Empty:
                    break
            state = self.state_store.public_snapshot()
            for command in cancelled:
                self.command_store.finish(
                    command.request_id,
                    success=False,
                    error="Media Runtime en arrêt",
                    state=state,
                )
        self._wake.set()
        if timeout is None:
            provider_config = getattr(self.provider, "config", None)
            provider_timeout = getattr(
                provider_config,
                "timeout_seconds",
                0.0,
            )
            try:
                provider_timeout_value = max(
                    0.0,
                    float(provider_timeout),
                )
            except (TypeError, ValueError, OverflowError):
                provider_timeout_value = 0.0
            timeout_value = max(2.0, provider_timeout_value + 0.5)
        else:
            timeout_value = max(0.0, float(timeout))
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout_value)
        stopped = not bool(thread and thread.is_alive())
        if stopped:
            with self._lock:
                if self._thread is thread:
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
        capabilities = {
            str(item).strip().casefold()
            for item in (
                getattr(self.provider, "capabilities", ()) or ()
            )
            if str(item).strip()
        }
        if normalized not in capabilities:
            raise ValueError(
                "Action média non supportée par "
                f"{self.provider.name} : {normalized}"
            )
        request_id = uuid.uuid4().hex
        command = _MediaCommand(
            request_id=request_id,
            action=normalized,
            options=dict(options),
        )
        with self._lock:
            thread = self._thread
            if (
                self._stopping
                or thread is None
                or not thread.is_alive()
            ):
                raise RuntimeError("Media Runtime indisponible")
            self.command_store.reserve(
                request_id,
                normalized,
                state=self.state_store.public_snapshot(),
            )
            try:
                self._commands.put_nowait(command)
            except queue.Full as exc:
                self.command_store.discard(request_id)
                raise RuntimeError(
                    "File des commandes média saturée"
                ) from exc
        self._wake.set()
        return request_id

    def command_status(
        self,
        request_id: str,
    ) -> dict[str, object] | None:
        return self.command_store.get(str(request_id or ""))

    def state(self) -> dict[str, object]:
        return self.state_store.snapshot()

    def _refresh_artwork(self, state: MediaState) -> None:
        identity = media_artwork_identity(
            provider=state.provider,
            track_id=state.track_id,
            uri=state.uri,
            title=state.title,
            artwork_url=state.artwork_url,
        )
        now = self._clock()
        if not state.connected or not identity:
            self._last_artwork_identity = identity
            self._next_artwork_retry_at = 0.0
            self.artwork_store.clear()
            return
        if (
            identity == self._last_artwork_identity
            and now < self._next_artwork_retry_at
        ):
            return

        # Never expose artwork from the previous track while the new artwork
        # is still being resolved.
        if identity != self._last_artwork_identity:
            self.artwork_store.clear()
        self._last_artwork_identity = identity

        capabilities = {
            str(item).strip().casefold()
            for item in (
                getattr(self.provider, "capabilities", ()) or ()
            )
            if str(item).strip()
        }
        loader = getattr(self.provider, "artwork", None)
        if "artwork" not in capabilities or not callable(loader):
            self._next_artwork_retry_at = float("inf")
            self.artwork_store.clear()
            return
        try:
            content, content_type = loader(state.track_id)
            with self._lock:
                if self._stopping:
                    return
            self.artwork_store.update(
                content,
                content_type=content_type,
                identity=identity,
            )
            self._next_artwork_retry_at = float("inf")
        except Exception:
            # Artwork is optional metadata: never make the player appear
            # disconnected because a cover cannot be loaded.
            self._next_artwork_retry_at = now + 5.0
            self.artwork_store.clear()

    def poll_once(self) -> MediaState:
        try:
            state = self.provider.state()
            if not isinstance(state, MediaState):
                raise TypeError(
                    "Le provider média doit retourner MediaState"
                )
        except Exception as exc:
            state = MediaState(
                provider=self.provider.name,
                connected=False,
                playback_state="unknown",
                error=str(exc),
            )
        state = replace(
            state,
            observed_at_unix=float(self._wall_clock()),
            capabilities=tuple(
                getattr(self.provider, "capabilities", ()) or ()
            ),
        )
        with self._lock:
            stopping = self._stopping
        if stopping:
            return state
        self.state_store.update(state)
        self._refresh_artwork(state)
        semantic_key = state.semantic_key()
        with self._lock:
            stopping = self._stopping
        if (
            self.event_bus is not None
            and not stopping
            and semantic_key != self._last_semantic_key
        ):
            self.event_bus.publish(
                channel="media",
                type="state_changed",
                platform=self.provider.name,
                payload=state.as_public_mapping(),
            )
        self._last_semantic_key = semantic_key
        return state

    def _execute(self, command: _MediaCommand) -> MediaCommandResult:
        with self._lock:
            if self._stopping:
                return MediaCommandResult(
                    request_id=command.request_id,
                    action=command.action,
                    success=False,
                    error="Media Runtime en arrêt",
                    state=self.state_store.public_snapshot(),
                )
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
            elif action == "enqueue_uri":
                self.provider.enqueue_uri(
                    str(command.options.get("uri") or "")
                )
            elif action == "clear_queue":
                self.provider.clear_queue()
            else:
                raise ValueError(
                    f"Action média inconnue : {command.action}"
                )
            with self._lock:
                stopping = self._stopping
            state = None if stopping else self.poll_once()
            with self._lock:
                stopping_after_poll = self._stopping
            result = MediaCommandResult(
                request_id=command.request_id,
                action=command.action,
                success=True,
                state=self.state_store.public_snapshot(),
            )
            if (
                self.event_bus is not None
                and not stopping_after_poll
                and state is not None
            ):
                self.event_bus.publish(
                    channel="media",
                    type="command",
                    platform=self.provider.name,
                    payload={
                        "request_id": command.request_id,
                        "action": command.action,
                        "success": True,
                        "state": state.as_public_mapping(),
                    },
                )
            return result
        except Exception as exc:
            with self._lock:
                stopping = self._stopping
            if not stopping:
                self.poll_once()
            return MediaCommandResult(
                request_id=command.request_id,
                action=command.action,
                success=False,
                error=str(exc),
                state=self.state_store.public_snapshot(),
            )

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            # Commands have priority over the periodic observer. This keeps
            # controls responsive when a poll is due at the same instant.
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                command = None

            if command is not None:
                self.command_store.mark_running(command.request_id)
                result = self._execute(command)
                self.command_store.finish(
                    result.request_id,
                    success=result.success,
                    error=result.error,
                    state=dict(result.state),
                )
                next_poll = self._clock() + self.config.poll_seconds
                continue

            now = self._clock()
            if now >= next_poll:
                with self._lock:
                    stopping = self._stopping
                if stopping:
                    break
                self.poll_once()
                next_poll = self._clock() + self.config.poll_seconds
                continue

            timeout = max(
                0.01,
                min(
                    0.25,
                    next_poll - self._clock(),
                ),
            )
            self._wake.wait(timeout)
            self._wake.clear()
