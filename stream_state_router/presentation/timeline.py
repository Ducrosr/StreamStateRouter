from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping

from .models import Cue, CueAction


CUE_MAX_RUNTIME_MS = 60000


@dataclass(frozen=True, slots=True)
class CueExecutionResult:
    cue: str
    frames_executed: int
    actions_executed: int
    actions_skipped: int
    elapsed_ms: float


class CueExecutor:
    """Execute one cue serially while preserving timeline offsets.

    Actions sharing the same frame are intentionally serialized. A future OBS
    batch/bridge can optimize one frame without changing cue semantics.
    """

    def __init__(
        self,
        *,
        action_executor: Callable[
            [CueAction, Mapping[str, str] | None],
            None,
        ],
        cooperative_yield: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._action_executor = action_executor
        self._cooperative_yield = cooperative_yield
        self._clock = clock
        self._sleeper = sleeper

    def _yield(self) -> None:
        if self._cooperative_yield is not None:
            self._cooperative_yield()

    def _wait_until(self, deadline: float) -> None:
        while True:
            self._yield()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleeper(min(0.05, remaining))

    def execute(
        self,
        cue: Cue,
        *,
        variables: Mapping[str, str] | None = None,
    ) -> CueExecutionResult:
        started = self._clock()
        frames = 0
        executed = 0
        skipped = 0

        for frame in cue.frames:
            self._wait_until(started + frame.at_ms / 1000.0)
            frames += 1
            for action in frame.actions:
                self._yield()
                if not action.enabled:
                    skipped += 1
                    continue
                self._action_executor(action, variables)
                executed += 1

        return CueExecutionResult(
            cue=cue.name,
            frames_executed=frames,
            actions_executed=executed,
            actions_skipped=skipped,
            elapsed_ms=max(0.0, (self._clock() - started) * 1000.0),
        )

@dataclass(frozen=True, slots=True)
class CueTaskStepResult:
    execution_id: str
    cue: str
    phase: str
    status: str
    frames_executed: int
    actions_executed: int
    actions_skipped: int
    error: str = ""


@dataclass(slots=True)
class CueTask:
    """Non-blocking Cue execution state owned by the runtime worker.

    The cursor is advanced before a one-shot action is emitted. If an OBS
    request then fails or has an ambiguous outcome, the action is never retried
    automatically by this task.
    """

    cue: Cue
    variables: Mapping[str, str]
    execution_id: str
    target_profile: str
    phase: str
    started_at: float
    max_runtime_ms: int = CUE_MAX_RUNTIME_MS
    frame_index: int = 0
    action_index: int = 0
    blocked_until: float = 0.0
    frames_executed: int = 0
    actions_executed: int = 0
    actions_skipped: int = 0
    status: str = "active"
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "cancelled", "failed", "uncertain"}

    @property
    def next_deadline(self) -> float | None:
        if self.status != "active":
            return None
        if self.blocked_until > 0:
            return self.blocked_until
        if self.frame_index >= len(self.cue.frames):
            return self.started_at
        return self.started_at + (
            self.cue.frames[self.frame_index].at_ms / 1000.0
        )

    def cancel(self, reason: str = "replaced") -> None:
        if self.status != "active":
            return
        self.status = "cancelled"
        self.error = str(reason or "cancelled")

    def _result(
        self,
        *,
        actions_before: int,
        skipped_before: int,
        frames_before: int,
    ) -> CueTaskStepResult:
        return CueTaskStepResult(
            execution_id=self.execution_id,
            cue=self.cue.name,
            phase=self.phase,
            status=self.status,
            frames_executed=self.frames_executed - frames_before,
            actions_executed=self.actions_executed - actions_before,
            actions_skipped=self.actions_skipped - skipped_before,
            error=self.error,
        )

    def advance(
        self,
        *,
        action_executor: Callable[
            [CueAction, Mapping[str, str] | None],
            None,
        ],
        wait_resolver: Callable[
            [CueAction, Mapping[str, str] | None],
            float,
        ],
        clock: Callable[[], float] = time.monotonic,
        max_actions: int = 16,
    ) -> CueTaskStepResult:
        actions_before = self.actions_executed
        skipped_before = self.actions_skipped
        frames_before = self.frames_executed
        action_budget = max(1, int(max_actions))

        if self.status != "active":
            return self._result(
                actions_before=actions_before,
                skipped_before=skipped_before,
                frames_before=frames_before,
            )

        consumed = 0
        while self.status == "active" and consumed < action_budget:
            now = clock()
            if (
                (now - self.started_at) * 1000.0
                > max(1, int(self.max_runtime_ms))
            ):
                self.status = "failed"
                self.error = (
                    "Budget total du Cue dépassé "
                    f"({self.max_runtime_ms} ms)"
                )
                break

            if self.blocked_until > 0:
                if now < self.blocked_until:
                    break
                self.blocked_until = 0.0

            if self.frame_index >= len(self.cue.frames):
                self.status = "completed"
                break

            frame = self.cue.frames[self.frame_index]
            frame_deadline = (
                self.started_at + frame.at_ms / 1000.0
            )
            if now < frame_deadline:
                break

            if self.action_index == 0:
                self.frames_executed += 1

            if self.action_index >= len(frame.actions):
                self.frame_index += 1
                self.action_index = 0
                continue

            action = frame.actions[self.action_index]
            # Consume before executing. An exception after an external request
            # may mean the effect happened; retrying it would be unsafe.
            self.action_index += 1
            consumed += 1

            if not action.enabled:
                self.actions_skipped += 1
                continue

            if action.type.strip().casefold() == "wait_ms":
                try:
                    duration_ms = float(
                        wait_resolver(action, self.variables)
                    )
                except Exception as exc:
                    self.status = "failed"
                    self.error = str(exc)
                    break
                if duration_ms < 0:
                    self.status = "failed"
                    self.error = "wait_ms négatif"
                    break
                self.actions_executed += 1
                if duration_ms > 0:
                    self.blocked_until = (
                        clock() + duration_ms / 1000.0
                    )
                    break
                continue

            try:
                action_executor(action, self.variables)
            except Exception as exc:
                self.status = "uncertain"
                self.error = str(exc)
                break
            self.actions_executed += 1

        if (
            self.status == "active"
            and self.frame_index >= len(self.cue.frames)
        ):
            self.status = "completed"

        return self._result(
            actions_before=actions_before,
            skipped_before=skipped_before,
            frames_before=frames_before,
        )

