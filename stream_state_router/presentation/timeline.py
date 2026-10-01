from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping

from .models import Cue, CueAction


class CueEffectUncertainError(RuntimeError):
    """A one-shot cue effect may have happened and must not be replayed."""


@dataclass(frozen=True, slots=True)
class CueExecutionResult:
    cue: str
    frames_executed: int
    actions_executed: int
    actions_skipped: int
    elapsed_ms: float


@dataclass(slots=True)
class _CueProgress:
    cue_name: str
    started_at: float
    frame_index: int = 0
    action_index: int = 0
    frame_counted: bool = False
    frames_executed: int = 0
    actions_executed: int = 0
    actions_skipped: int = 0
    uncertain_error: str = ""


class CueExecutor:
    """Execute one cue serially while preserving timeline offsets.

    When an execution_id is supplied, successful actions are checkpointed. A
    retry after a later failure resumes at the first unfinished action rather
    than replaying already-accomplished one-shot effects.
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
        self._progress: dict[str, _CueProgress] = {}

    def cancel_all(self) -> None:
        self._progress.clear()

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
        execution_id: str = "",
    ) -> CueExecutionResult:
        key = str(execution_id or "").strip()
        progress = self._progress.get(key) if key else None
        if progress is not None and progress.cue_name != cue.name:
            raise RuntimeError(
                "Cue execution identity reused for a different cue"
            )
        if progress is None:
            progress = _CueProgress(
                cue_name=cue.name,
                started_at=self._clock(),
            )
            if key:
                self._progress[key] = progress
        if progress.uncertain_error:
            raise CueEffectUncertainError(progress.uncertain_error)

        try:
            while progress.frame_index < len(cue.frames):
                frame = cue.frames[progress.frame_index]
                self._wait_until(
                    progress.started_at + frame.at_ms / 1000.0
                )
                if not progress.frame_counted:
                    progress.frames_executed += 1
                    progress.frame_counted = True

                while progress.action_index < len(frame.actions):
                    action = frame.actions[progress.action_index]
                    self._yield()
                    if not action.enabled:
                        progress.actions_skipped += 1
                        progress.action_index += 1
                        continue
                    try:
                        self._action_executor(action, variables)
                    except CueEffectUncertainError as exc:
                        progress.uncertain_error = str(exc)
                        raise
                    progress.actions_executed += 1
                    progress.action_index += 1

                progress.frame_index += 1
                progress.action_index = 0
                progress.frame_counted = False
        except Exception:
            if not key:
                # A non-keyed execution retains the historical stateless
                # behavior. Keyed executions intentionally preserve progress.
                progress = _CueProgress(cue.name, self._clock())
            raise

        if key:
            self._progress.pop(key, None)
        return CueExecutionResult(
            cue=cue.name,
            frames_executed=progress.frames_executed,
            actions_executed=progress.actions_executed,
            actions_skipped=progress.actions_skipped,
            elapsed_ms=max(
                0.0,
                (self._clock() - progress.started_at) * 1000.0,
            ),
        )
