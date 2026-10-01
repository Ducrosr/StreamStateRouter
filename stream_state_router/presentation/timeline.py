from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping

from .models import Cue, CueAction


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
