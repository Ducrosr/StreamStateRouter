from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import ForegroundApp, StreamState
from .rules import ResolutionKind, RuleResolution, RuleSet


@dataclass(frozen=True, slots=True)
class StateChange:
    previous: StreamState | None
    current: StreamState
    reason: str
    rule_name: str
    app: ForegroundApp | None
    apply_delay_ms: int = 0


class StateRouterEngine:
    """Resolve foreground applications into stable logical stream states."""

    def __init__(
        self,
        rules: RuleSet,
        *,
        debounce_ms: int = 150,
        fallback_debounce_ms: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        context_provider: Callable[[], Mapping[str, Any]] | None = None,
    ):
        if debounce_ms < 0:
            raise ValueError("debounce_ms must be >= 0")
        if fallback_debounce_ms is not None and fallback_debounce_ms < 0:
            raise ValueError("fallback_debounce_ms must be >= 0")
        self._rules = rules
        self._debounce_seconds = debounce_ms / 1000.0
        self._fallback_debounce_seconds = (
            self._debounce_seconds
            if fallback_debounce_ms is None
            else fallback_debounce_ms / 1000.0
        )
        self._clock = clock
        self._context_provider = context_provider

        self._current_state: StreamState | None = None
        self._current_rule = ""
        self._candidate: RuleResolution | None = None
        self._candidate_app: ForegroundApp | None = None
        self._candidate_since = 0.0
        self._manual_override: StreamState | None = None
        self._manual_override_until: float | None = None

    @property
    def current_state(self) -> StreamState | None:
        return self._current_state

    @property
    def current_rule(self) -> str:
        return self._current_rule

    @property
    def manual_override(self) -> StreamState | None:
        return self._manual_override

    @property
    def manual_override_until(self) -> float | None:
        return self._manual_override_until

    def set_manual_override(
        self,
        state: StreamState,
        *,
        duration_seconds: float | None = None,
    ) -> StateChange | None:
        self._manual_override = state
        self._manual_override_until = (
            None
            if duration_seconds is None or duration_seconds <= 0
            else self._clock() + float(duration_seconds)
        )
        self._reset_candidate()
        return self._commit(state, "manual_override", "manual", None, 0)

    def clear_manual_override(self, app: ForegroundApp | None = None) -> StateChange | None:
        self._manual_override = None
        self._manual_override_until = None
        self._reset_candidate()
        return self.observe(app, force=True)

    def observe(
        self,
        app: ForegroundApp | None,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> StateChange | None:
        timestamp = self._clock() if now is None else now
        if self._manual_override is not None:
            if self._manual_override_until is None or timestamp < self._manual_override_until:
                return None
            self._manual_override = None
            self._manual_override_until = None
            force = True

        context: Mapping[str, Any] = {}
        if self._rules.needs_context and self._context_provider is not None:
            try:
                context = self._context_provider() or {}
            except Exception:
                context = {}
        resolution = self._rules.resolve(app, context)

        if resolution.kind is ResolutionKind.IGNORE:
            self._reset_candidate()
            return None

        assert resolution.state is not None
        state = resolution.state
        if state == self._current_state:
            self._reset_candidate()
            return None

        delay = (
            self._fallback_debounce_seconds
            if resolution.kind is ResolutionKind.FALLBACK
            else self._debounce_seconds
        )

        if force or delay == 0:
            self._reset_candidate()
            return self._commit(
                state,
                "foreground",
                resolution.rule_name,
                app,
                resolution.apply_delay_ms,
            )

        if self._candidate != resolution:
            self._candidate = resolution
            self._candidate_app = app
            self._candidate_since = timestamp
            return None

        if timestamp - self._candidate_since < delay:
            return None

        candidate_app = self._candidate_app
        candidate = self._candidate
        self._reset_candidate()
        return self._commit(
            state,
            "foreground",
            resolution.rule_name,
            candidate_app,
            candidate.apply_delay_ms if candidate else 0,
        )

    def _reset_candidate(self) -> None:
        self._candidate = None
        self._candidate_app = None
        self._candidate_since = 0.0

    def _commit(
        self,
        state: StreamState,
        reason: str,
        rule_name: str,
        app: ForegroundApp | None,
        apply_delay_ms: int,
    ) -> StateChange | None:
        if state == self._current_state:
            return None
        previous = self._current_state
        self._current_state = state
        self._current_rule = rule_name
        return StateChange(previous, state, reason, rule_name, app, max(0, int(apply_delay_ms)))
