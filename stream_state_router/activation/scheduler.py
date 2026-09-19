from __future__ import annotations

import math
import random
import time
from dataclasses import replace
from typing import Callable, Mapping

from .models import (
    ActivationEvent,
    ActivationPhase,
    ActivationRuntimeState,
    RollTestResult,
    TriggerPolicyConfig,
    TriggerTargetConfig,
)

EligibilityProvider = Callable[[str, TriggerPolicyConfig], bool]


class ActivationScheduler:
    """Pure monotonic scheduler for temporary source activations.

    The scheduler deliberately knows nothing about OBS or Qt. It emits small
    show/hide/cooldown events; the runtime layer will translate those events to
    OBS WebSocket requests in the next integration step.
    """

    def __init__(
        self,
        policies: Mapping[str, TriggerPolicyConfig] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._rng = rng or random.Random()
        self._policies: dict[str, TriggerPolicyConfig] = dict(policies or {})
        self._states: dict[str, ActivationRuntimeState] = {
            name: ActivationRuntimeState() for name in self._policies
        }

    @property
    def policies(self) -> Mapping[str, TriggerPolicyConfig]:
        return self._policies

    def configure(self, policies: Mapping[str, TriggerPolicyConfig]) -> None:
        """Replace policies and reset transient timing state.

        Configuration reloads must never preserve an in-flight timer from an
        older policy definition. Reconciliation with OBS is handled by the
        runtime integration layer.
        """
        self._policies = dict(policies)
        self._states = {name: ActivationRuntimeState() for name in self._policies}

    def state(self, policy_name: str) -> ActivationRuntimeState:
        return replace(self._state(policy_name))

    def states(self) -> dict[str, ActivationRuntimeState]:
        return {name: replace(state) for name, state in self._states.items()}

    def tick(
        self,
        eligibility: EligibilityProvider | None = None,
        *,
        now: float | None = None,
    ) -> list[ActivationEvent]:
        timestamp = self._clock() if now is None else float(now)
        events: list[ActivationEvent] = []
        for name, policy in self._policies.items():
            is_eligible = bool(policy.enabled)
            if is_eligible and eligibility is not None:
                is_eligible = bool(eligibility(name, policy))
            events.extend(self._tick_policy(name, policy, timestamp, is_eligible))
        return events

    def test_roll(self, policy_name: str) -> RollTestResult:
        policy = self._policy(policy_name)
        state = self._state(policy_name)
        roll = self._rng.random()
        triggered = roll < self._chance(policy)
        source = ""
        if triggered:
            target = self._choose_target(policy, state)
            source = target.source if target is not None else ""
            triggered = target is not None
        return RollTestResult(policy_name, roll, self._chance(policy), triggered, source)

    def trigger_now(
        self,
        policy_name: str,
        *,
        target_source: str | None = None,
        eligible: bool = True,
        ignore_cooldown: bool = False,
        now: float | None = None,
    ) -> list[ActivationEvent]:
        timestamp = self._clock() if now is None else float(now)
        policy = self._policy(policy_name)
        state = self._state(policy_name)
        if not policy.enabled:
            raise RuntimeError(f"Politique désactivée : {policy_name}")
        if not eligible:
            raise RuntimeError(f"Politique non éligible : {policy_name}")
        if state.phase is ActivationPhase.VISIBLE:
            raise RuntimeError(f"Une source est déjà active pour {policy_name}")
        if state.phase is ActivationPhase.COOLDOWN and not ignore_cooldown:
            raise RuntimeError(f"Cooldown actif pour {policy_name}")

        target = self._manual_target(policy, state, target_source)
        if target is None:
            raise RuntimeError(f"Aucune source éligible pour {policy_name}")
        return self._activate(policy_name, policy, state, target, timestamp, reason="manual")

    def stop(
        self,
        policy_name: str,
        *,
        enter_cooldown: bool = True,
        now: float | None = None,
    ) -> list[ActivationEvent]:
        timestamp = self._clock() if now is None else float(now)
        policy = self._policy(policy_name)
        state = self._state(policy_name)
        if state.phase is not ActivationPhase.VISIBLE or not state.active_source:
            return []
        return self._hide_visible(
            policy_name,
            policy,
            state,
            timestamp,
            enter_cooldown=enter_cooldown,
            reason="manual_stop",
        )

    def reset_cooldown(
        self,
        policy_name: str,
        *,
        eligible: bool = True,
        now: float | None = None,
    ) -> list[ActivationEvent]:
        timestamp = self._clock() if now is None else float(now)
        policy = self._policy(policy_name)
        state = self._state(policy_name)
        if state.phase is not ActivationPhase.COOLDOWN:
            return []
        state.cooldown_until = None
        if eligible and policy.enabled:
            state.phase = ActivationPhase.ELIGIBLE
            state.next_roll_at = timestamp + self._interval(policy)
        else:
            state.phase = ActivationPhase.IDLE
            state.next_roll_at = None
        return [
            ActivationEvent(
                "cooldown_complete",
                policy_name,
                timestamp,
                reason="manual_reset",
            )
        ]

    def reset_all(self, *, now: float | None = None) -> list[ActivationEvent]:
        """Return hide events for active sources and reset every policy to Idle."""
        timestamp = self._clock() if now is None else float(now)
        events: list[ActivationEvent] = []
        for name, state in self._states.items():
            if state.phase is ActivationPhase.VISIBLE and state.active_source:
                events.append(
                    ActivationEvent(
                        "hide",
                        name,
                        timestamp,
                        source=state.active_source,
                        reason="reset",
                    )
                )
            self._reset_to_idle(state)
        return events

    def _tick_policy(
        self,
        name: str,
        policy: TriggerPolicyConfig,
        now: float,
        eligible: bool,
    ) -> list[ActivationEvent]:
        state = self._state(name)
        if not eligible:
            events: list[ActivationEvent] = []
            if state.phase is ActivationPhase.VISIBLE and state.active_source:
                events.append(
                    ActivationEvent(
                        "hide",
                        name,
                        now,
                        source=state.active_source,
                        reason="ineligible",
                    )
                )
            self._reset_to_idle(state)
            return events

        if state.phase is ActivationPhase.IDLE:
            state.phase = ActivationPhase.ELIGIBLE
            state.next_roll_at = now + self._interval(policy)
            return []

        if state.phase is ActivationPhase.VISIBLE:
            if state.visible_until is None or now < state.visible_until:
                return []
            return self._hide_visible(
                name,
                policy,
                state,
                now,
                enter_cooldown=True,
                reason="duration_elapsed",
            )

        if state.phase is ActivationPhase.COOLDOWN:
            if state.cooldown_until is None or now < state.cooldown_until:
                return []
            state.phase = ActivationPhase.ELIGIBLE
            state.cooldown_until = None
            state.next_roll_at = now + self._interval(policy)
            return [ActivationEvent("cooldown_complete", name, now)]

        if state.next_roll_at is None:
            state.next_roll_at = now + self._interval(policy)
            return []
        if now < state.next_roll_at:
            return []

        due = state.next_roll_at
        interval = self._interval(policy)
        skipped_slots = max(0, math.floor((now - due) / interval))
        state.next_roll_at = due + (skipped_slots + 1) * interval

        roll = self._rng.random()
        chance = self._chance(policy)
        events = [ActivationEvent("roll", name, now, roll=roll, chance=chance)]
        if roll >= chance:
            return events

        target = self._choose_target(policy, state)
        if target is None:
            return events
        events.extend(self._activate(name, policy, state, target, now, reason="random"))
        return events

    def _activate(
        self,
        name: str,
        policy: TriggerPolicyConfig,
        state: ActivationRuntimeState,
        target: TriggerTargetConfig,
        now: float,
        *,
        reason: str,
    ) -> list[ActivationEvent]:
        duration = self._duration(policy, target)
        state.phase = ActivationPhase.VISIBLE
        state.active_source = target.source
        state.visible_until = now + duration
        state.cooldown_until = None
        state.next_roll_at = None
        state.last_source = target.source
        state.last_trigger_at = now
        return [
            ActivationEvent(
                "show",
                name,
                now,
                source=target.source,
                duration_seconds=duration,
                reason=reason,
            )
        ]

    def _hide_visible(
        self,
        name: str,
        policy: TriggerPolicyConfig,
        state: ActivationRuntimeState,
        now: float,
        *,
        enter_cooldown: bool,
        reason: str,
    ) -> list[ActivationEvent]:
        source = state.active_source
        events = [ActivationEvent("hide", name, now, source=source, reason=reason)]
        state.active_source = ""
        state.visible_until = None
        cooldown = max(0.0, float(policy.cooldown_seconds))
        if enter_cooldown and cooldown > 0:
            state.phase = ActivationPhase.COOLDOWN
            state.cooldown_until = now + cooldown
            state.next_roll_at = None
            events.append(
                ActivationEvent(
                    "cooldown_started",
                    name,
                    now,
                    cooldown_seconds=cooldown,
                )
            )
        else:
            state.phase = ActivationPhase.ELIGIBLE
            state.cooldown_until = None
            state.next_roll_at = now + self._interval(policy)
        return events

    def _manual_target(
        self,
        policy: TriggerPolicyConfig,
        state: ActivationRuntimeState,
        target_source: str | None,
    ) -> TriggerTargetConfig | None:
        if target_source is None:
            return self._choose_target(policy, state)
        wanted = str(target_source).strip()
        for target in policy.targets:
            if target.source == wanted and target.enabled:
                return target
        return None

    def _choose_target(
        self,
        policy: TriggerPolicyConfig,
        state: ActivationRuntimeState,
    ) -> TriggerTargetConfig | None:
        candidates = [
            target
            for target in policy.targets
            if target.enabled and float(target.weight) > 0
        ]
        if not candidates:
            return None
        if policy.avoid_immediate_repeat and state.last_source and len(candidates) > 1:
            alternatives = [target for target in candidates if target.source != state.last_source]
            if alternatives:
                candidates = alternatives

        total = sum(float(target.weight) for target in candidates)
        if total <= 0:
            return None
        needle = self._rng.random() * total
        cumulative = 0.0
        for target in candidates:
            cumulative += float(target.weight)
            if needle < cumulative:
                return target
        return candidates[-1]

    def _policy(self, policy_name: str) -> TriggerPolicyConfig:
        try:
            return self._policies[policy_name]
        except KeyError as exc:
            raise KeyError(f"Politique d'activation introuvable : {policy_name}") from exc

    def _state(self, policy_name: str) -> ActivationRuntimeState:
        self._policy(policy_name)
        return self._states.setdefault(policy_name, ActivationRuntimeState())

    @staticmethod
    def _chance(policy: TriggerPolicyConfig) -> float:
        return min(1.0, max(0.0, float(policy.chance)))

    @staticmethod
    def _interval(policy: TriggerPolicyConfig) -> float:
        return max(0.001, float(policy.interval_seconds))

    @staticmethod
    def _duration(policy: TriggerPolicyConfig, target: TriggerTargetConfig) -> float:
        value = target.duration_seconds
        if value is None:
            value = policy.default_duration_seconds
        return max(0.001, float(value))

    @staticmethod
    def _reset_to_idle(state: ActivationRuntimeState) -> None:
        state.phase = ActivationPhase.IDLE
        state.next_roll_at = None
        state.visible_until = None
        state.cooldown_until = None
        state.active_source = ""
