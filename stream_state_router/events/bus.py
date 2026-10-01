from __future__ import annotations

from collections import defaultdict, deque
import threading
import time
from typing import Callable, Mapping
import uuid

from .models import EventEnvelope


class EventBus:
    """Thread-safe normalized event bus with bounded per-channel history."""

    def __init__(
        self,
        *,
        history_limit: int = 250,
        clock: Callable[[], float] = time.time,
    ):
        self._history_limit = max(1, int(history_limit))
        self._clock = clock
        self._lock = threading.RLock()
        self._sequence = 0
        self._stream_id = uuid.uuid4().hex
        self._dropped_through: dict[str, int] = {}
        self._history: dict[str, deque[EventEnvelope]] = defaultdict(
            lambda: deque(maxlen=self._history_limit)
        )
        self._subscribers: dict[
            str,
            tuple[str, Callable[[EventEnvelope], None]],
        ] = {}

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def stream_id(self) -> str:
        return self._stream_id

    def publish(
        self,
        *,
        channel: str,
        type: str,
        platform: str = "",
        payload: Mapping[str, object] | None = None,
    ) -> EventEnvelope:
        normalized_channel = str(channel or "").strip().casefold()
        normalized_type = str(type or "").strip()
        if not normalized_channel:
            raise ValueError("channel requis")
        if not normalized_type:
            raise ValueError("type requis")

        with self._lock:
            self._sequence += 1
            event = EventEnvelope.create(
                sequence=self._sequence,
                channel=normalized_channel,
                type=normalized_type,
                platform=str(platform or "").strip().casefold(),
                emitted_at=self._clock(),
                payload=payload or {},
            )
            history = self._history[normalized_channel]
            if len(history) == self._history_limit and history:
                self._dropped_through[normalized_channel] = max(
                    self._dropped_through.get(normalized_channel, 0),
                    int(history[0].sequence),
                )
            history.append(event)
            subscribers = tuple(self._subscribers.values())

        for wanted_channel, callback in subscribers:
            if wanted_channel and wanted_channel != normalized_channel:
                continue
            try:
                callback(event)
            except Exception:
                # One consumer must never break routing/event delivery.
                continue
        return event

    def subscribe(
        self,
        callback: Callable[[EventEnvelope], None],
        *,
        channel: str = "",
    ) -> str:
        if not callable(callback):
            raise TypeError("callback doit être appelable")
        token = uuid.uuid4().hex
        normalized = str(channel or "").strip().casefold()
        with self._lock:
            self._subscribers[token] = (normalized, callback)
        return token

    def unsubscribe(self, token: str) -> None:
        with self._lock:
            self._subscribers.pop(str(token), None)

    def events(
        self,
        channel: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[EventEnvelope, ...]:
        normalized = str(channel or "").strip().casefold()
        if not normalized:
            return ()
        wanted_after = max(0, int(after))
        wanted_limit = max(1, min(500, int(limit)))
        with self._lock:
            values = tuple(self._history.get(normalized, ()))
        selected = [
            event
            for event in values
            if event.sequence > wanted_after
        ]
        # Continuation cursors must consume the oldest pending page first.
        # Returning the newest page makes the client advance past events that
        # were still retained but never delivered.
        return tuple(selected[:wanted_limit])

    def snapshot(
        self,
        channel: str,
        *,
        after: int = 0,
        limit: int = 100,
        stream_id: str = "",
    ) -> dict[str, object]:
        normalized = str(channel or "").strip().casefold()
        requested_after = max(0, int(after))
        expected_stream = str(stream_id or "").strip()
        reset_required = bool(
            expected_stream and expected_stream != self._stream_id
        )
        effective_after = 0 if reset_required else requested_after
        wanted_limit = max(1, min(500, int(limit)))

        with self._lock:
            values = tuple(self._history.get(normalized, ()))
            dropped_through = int(
                self._dropped_through.get(normalized, 0)
            )
            latest = self._sequence

        pending = [
            event for event in values if event.sequence > effective_after
        ]
        page = pending[:wanted_limit]
        mode = (
            "reset"
            if reset_required
            else "incremental"
            if expected_stream
            else "initial"
        )
        gap = bool(
            mode == "incremental"
            and effective_after < dropped_through
        )
        next_after = (
            int(page[-1].sequence)
            if page
            else effective_after
        )
        return {
            "channel": normalized,
            "stream_id": self._stream_id,
            "mode": mode,
            "reset_required": reset_required,
            "requested_after": requested_after,
            "after": effective_after,
            "next_after": next_after,
            "latest_sequence": latest,
            "has_more": len(pending) > len(page),
            "gap": gap,
            "dropped_through": dropped_through if gap else 0,
            "initial_history_truncated": bool(
                mode in {"initial", "reset"} and dropped_through
            ),
            "events": [event.as_mapping() for event in page],
        }

    def clear(self, channel: str = "") -> None:
        normalized = str(channel or "").strip().casefold()
        with self._lock:
            if normalized:
                self._history.pop(normalized, None)
                self._dropped_through.pop(normalized, None)
            else:
                self._history.clear()
                self._dropped_through.clear()
