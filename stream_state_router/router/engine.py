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
    def needs_context(self) -> bool:
        return bool(self._rules.needs_context)

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

    def explain(
        self,
        app: ForegroundApp | None,
        *,
        context: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, object]:
        """Explain the current routing decision without mutating engine state.

        The caller supplies a frozen context. This method never invokes the
        context provider, so diagnostics cannot accidentally poll OBS.
        """
        timestamp = self._clock() if now is None else float(now)
        if self._manual_override is not None:
            remaining = None
            if self._manual_override_until is not None:
                remaining = max(0.0, self._manual_override_until - timestamp)
            return {
                "kind": "manual_override",
                "rule_name": "manual",
                "state": self._manual_override.as_variables(),
                "effective_state": self._manual_override.as_variables(),
                "current_state": (
                    self._current_state.as_variables() if self._current_state is not None else None
                ),
                "would_change": self._manual_override != self._current_state,
                "debounce_ms": 0,
                "apply_delay_ms": 0,
                "override_remaining_seconds": remaining,
                "checks": [],
            }

        explanation = self._rules.explain(app, context or {})
        resolution = explanation.resolution
        if resolution.kind is ResolutionKind.IGNORE:
            effective = self._current_state
            debounce_ms = 0
        else:
            effective = resolution.state
            debounce_ms = int(
                round(
                    1000.0
                    * (
                        self._fallback_debounce_seconds
                        if resolution.kind is ResolutionKind.FALLBACK
                        else self._debounce_seconds
                    )
                )
            )
        candidate_matches = (
            self._candidate is not None
            and self._candidate == resolution
        )
        candidate_elapsed_ms = (
            max(0.0, (timestamp - self._candidate_since) * 1000.0)
            if candidate_matches and self._candidate_since
            else 0.0
        )
        would_change = effective is not None and effective != self._current_state
        return {
            **explanation.as_mapping(),
            "effective_state": effective.as_variables() if effective is not None else None,
            "current_state": (
                self._current_state.as_variables() if self._current_state is not None else None
            ),
            "would_change": would_change,
            "debounce_ms": debounce_ms,
            "candidate_active": candidate_matches,
            "candidate_elapsed_ms": round(candidate_elapsed_ms, 3),
            "would_commit_now": (
                bool(would_change)
                and (
                    debounce_ms == 0
                    or (
                        candidate_matches
                        and candidate_elapsed_ms >= float(debounce_ms)
                    )
                )
            ),
        }

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

    def clear_manual_override(
        self,
        app: ForegroundApp | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        use_context_provider: bool = True,
    ) -> StateChange | None:
        self._manual_override = None
        self._manual_override_until = None
        self._reset_candidate()
        return self.observe(
            app,
            force=True,
            context=context,
            use_context_provider=use_context_provider,
        )

    def observe(
        self,
        app: ForegroundApp | None,
        *,
        now: float | None = None,
        force: bool = False,
        context: Mapping[str, Any] | None = None,
        use_context_provider: bool = True,
    ) -> StateChange | None:
        timestamp = self._clock() if now is None else now
        if self._manual_override is not None:
            if self._manual_override_until is None or timestamp < self._manual_override_until:
                return None
            self._manual_override = None
            self._manual_override_until = None
            force = True

        frozen_context: Mapping[str, Any] = context or {}
        if (
            self._rules.needs_context
            and context is None
            and use_context_provider
            and self._context_provider is not None
        ):
            try:
                frozen_context = self._context_provider() or {}
            except Exception:
                frozen_context = {}
        resolution = self._rules.resolve(app, frozen_context)

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
