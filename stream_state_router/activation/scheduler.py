from __future__ import annotations

import hashlib
import json
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
    SimulationResult,
    TriggerPolicyConfig,
    TriggerTargetConfig,
    TriggerTargetIdentity,
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
        for name, policy in self._policies.items():
            self._validate_policy(name, policy)
        self._validate_ownership(self._policies)
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
        candidate = dict(policies)
        for name, policy in candidate.items():
            self._validate_policy(name, policy)
        self._validate_ownership(candidate)
        self._policies = candidate
        self._states = {name: ActivationRuntimeState() for name in self._policies}

    def state(self, policy_name: str) -> ActivationRuntimeState:
        return replace(self._state(policy_name))

    def set_active_collection(self, policy_name: str, collection: str) -> None:
        state = self._state(policy_name)
        if state.phase is ActivationPhase.VISIBLE:
            state.active_collection = str(collection or "").strip()

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
            is_eligible = (
                bool(eligibility(name, policy))
                if eligibility is not None
                else bool(policy.enabled)
            )
            events.extend(self._tick_policy(name, policy, timestamp, is_eligible))
        return events

    def test_roll(self, policy_name: str) -> RollTestResult:
        policy = self._policy(policy_name)
        state = self._state(policy_name)
        roll = self._rng.random()
        triggered = roll < self._chance(policy)
        source = ""
        identity = None
        if triggered:
            target = self._choose_target(policy, state)
            source = target.source if target is not None else ""
            identity = target.identity if target is not None else None
            triggered = target is not None
        return RollTestResult(
            policy_name,
            roll,
            self._chance(policy),
            triggered,
            source,
            identity,
        )

    def simulate(
        self,
        policy_name: str,
        *,
        trials: int = 1000,
        seed: int = 12345,
    ) -> SimulationResult:
        """Run deterministic dry rolls without mutating runtime state or the live RNG."""
        policy = self._policy(policy_name)
        sample_count = max(1, min(1_000_000, int(trials)))
        sample_seed = int(seed)
        rng = random.Random(sample_seed)
        chance = self._chance(policy)
        last_identity: TriggerTargetIdentity | None = None
        hit_count = 0
        trigger_count = 0
        miss_count = 0
        blocked_count = 0
        counts: dict[str, int] = {}

        for _ in range(sample_count):
            if rng.random() >= chance:
                miss_count += 1
                continue
            hit_count += 1
            target = self._choose_target_with_rng(policy, last_identity, rng)
            if target is None:
                blocked_count += 1
                continue
            trigger_count += 1
            label = (
                f"{target.container_kind}:{target.container}/{target.source}"
                if target.container
                else target.source
            )
            counts[label] = counts.get(label, 0) + 1
            last_identity = target.identity

        fingerprint = hashlib.sha256(
            json.dumps(
                policy.to_mapping(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        return SimulationResult(
            policy=policy_name,
            trials=sample_count,
            seed=sample_seed,
            config_fingerprint=fingerprint,
            chance_hit_count=hit_count,
            trigger_count=trigger_count,
            miss_count=miss_count,
            blocked_count=blocked_count,
            target_counts=tuple(sorted(counts.items())),
        )

    def trigger_now(
        self,
        policy_name: str,
        *,
        target_identity: TriggerTargetIdentity | None = None,
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

        target = self._manual_target(
            policy,
            state,
            target_identity=target_identity,
            target_source=target_source,
        )
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

    def reset_policy(
        self,
        policy_name: str,
        *,
        now: float | None = None,
    ) -> list[ActivationEvent]:
        """Reset one policy to Idle and return a best-effort hide event if needed."""
        timestamp = self._clock() if now is None else float(now)
        state = self._state(policy_name)
        events: list[ActivationEvent] = []
        if state.phase is ActivationPhase.VISIBLE and state.active_source:
            events.append(
                ActivationEvent(
                    "hide",
                    policy_name,
                    timestamp,
                    source=state.active_source,
                    container=state.active_container,
                    container_kind=state.active_container_kind,
                    collection=state.active_collection,
                    reason="reset",
                )
            )
        self._reset_to_idle(state)
        return events

    def reset_all(self, *, now: float | None = None) -> list[ActivationEvent]:
        """Return hide events for active sources and reset every policy to Idle."""
        timestamp = self._clock() if now is None else float(now)
        events: list[ActivationEvent] = []
        for name in tuple(self._states):
            events.extend(self.reset_policy(name, now=timestamp))
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
                        container=state.active_container,
                        container_kind=state.active_container_kind,
                        collection=state.active_collection,
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
        hit = roll < chance
        events = [
            ActivationEvent(
                "roll",
                name,
                now,
                roll=roll,
                chance=chance,
                reason="chance_hit" if hit else "chance_miss",
            )
        ]
        if not hit:
            return events

        target = self._choose_target(policy, state)
        if target is None:
            events.append(ActivationEvent("blocked", name, now, reason="no_target"))
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
        state.active_container = target.container
        state.active_container_kind = target.container_kind
        state.active_collection = ""
        state.visible_until = now + duration
        state.cooldown_until = None
        state.next_roll_at = None
        state.last_source = target.source
        state.last_identity = target.identity
        state.last_trigger_at = now
        return [
            ActivationEvent(
                "show",
                name,
                now,
                source=target.source,
                container=target.container,
                container_kind=target.container_kind,
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
        container = state.active_container
        container_kind = state.active_container_kind
        collection = state.active_collection
        events = [
            ActivationEvent(
                "hide",
                name,
                now,
                source=source,
                container=container,
                container_kind=container_kind,
                collection=collection,
                reason=reason,
            )
        ]
        state.active_source = ""
        state.active_container = ""
        state.active_container_kind = "scene"
        state.active_collection = ""
        state.active_collection = ""
        state.visible_until = None
        cooldown = self._cooldown(policy)
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
        *,
        target_identity: TriggerTargetIdentity | None,
        target_source: str | None,
    ) -> TriggerTargetConfig | None:
        if target_identity is not None:
            matches = [
                target
                for target in policy.targets
                if target.enabled and target.identity == target_identity
            ]
            if len(matches) > 1:
                raise RuntimeError(
                    "Configuration invalide : cible d'activation exacte dupliquée"
                )
            return matches[0] if matches else None
        if target_source is None:
            return self._choose_target(policy, state)
        wanted = str(target_source).strip()
        matches = [
            target
            for target in policy.targets
            if target.enabled and target.source == wanted
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"Cible ambiguë '{wanted}' : précisez conteneur et type de conteneur"
            )
        return matches[0] if matches else None

    def _choose_target(
        self,
        policy: TriggerPolicyConfig,
        state: ActivationRuntimeState,
    ) -> TriggerTargetConfig | None:
        return self._choose_target_with_rng(policy, state.last_identity, self._rng)

    @staticmethod
    def _choose_target_with_rng(
        policy: TriggerPolicyConfig,
        last_identity: TriggerTargetIdentity | None,
        rng: random.Random,
    ) -> TriggerTargetConfig | None:
        weighted: list[tuple[TriggerTargetConfig, float]] = []
        for target in policy.targets:
            if not target.enabled:
                continue
            weight = float(target.weight)
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(
                    f"Poids d'activation invalide pour {target.source}: {target.weight!r}"
                )
            if weight > 0.0:
                weighted.append((target, weight))
        if not weighted:
            return None

        if policy.avoid_immediate_repeat and last_identity is not None and len(weighted) > 1:
            alternatives = [
                item for item in weighted if item[0].identity != last_identity
            ]
            if alternatives:
                weighted = alternatives

        max_weight = max(weight for _target, weight in weighted)
        if max_weight <= 0.0 or not math.isfinite(max_weight):
            return None
        scaled = [(target, weight / max_weight) for target, weight in weighted]
        total = math.fsum(weight for _target, weight in scaled)
        if not math.isfinite(total) or total <= 0.0:
            raise ValueError("Somme des poids d'activation invalide")
        needle = rng.random() * total
        cumulative = 0.0
        for target, weight in scaled:
            cumulative += weight
            if needle < cumulative:
                return target
        return scaled[-1][0]

    @staticmethod
    def _validate_ownership(policies: Mapping[str, TriggerPolicyConfig]) -> None:
        owners: dict[TriggerTargetIdentity, str] = {}
        for policy_name, policy in policies.items():
            for target in policy.targets:
                identity = target.identity
                previous = owners.get(identity)
                if previous is not None and previous != policy_name:
                    raise ValueError(
                        "Cible d'activation possédée par plusieurs politiques : "
                        f"{identity.container_kind}:{identity.container}/{identity.source} "
                        f"({previous}, {policy_name})"
                    )
                owners[identity] = policy_name

    def _policy(self, policy_name: str) -> TriggerPolicyConfig:
        try:
            return self._policies[policy_name]
        except KeyError as exc:
            raise KeyError(f"Politique d'activation introuvable : {policy_name}") from exc

    def _state(self, policy_name: str) -> ActivationRuntimeState:
        self._policy(policy_name)
        return self._states.setdefault(policy_name, ActivationRuntimeState())

    @classmethod
    def _validate_policy(
        cls,
        name: str,
        policy: TriggerPolicyConfig,
    ) -> None:
        cls._chance(policy)
        cls._interval(policy)
        cls._cooldown(policy)
        default_duration = float(policy.default_duration_seconds)
        if not math.isfinite(default_duration) or default_duration <= 0.0:
            raise ValueError(
                f"{name}: default_duration_seconds doit être un nombre fini > 0"
            )
        identities: set[TriggerTargetIdentity] = set()
        for target in policy.targets:
            if target.identity in identities:
                raise ValueError(
                    f"{name}: cible d'activation exacte dupliquée "
                    f"{target.container}/{target.source}"
                )
            identities.add(target.identity)
            weight = float(target.weight)
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(
                    f"{name}: poids invalide pour {target.container}/{target.source}"
                )
            if target.duration_seconds is not None:
                duration = float(target.duration_seconds)
                if not math.isfinite(duration) or duration <= 0.0:
                    raise ValueError(
                        f"{name}: durée invalide pour {target.container}/{target.source}"
                    )

    @staticmethod
    def _chance(policy: TriggerPolicyConfig) -> float:
        value = float(policy.chance)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("chance doit être un nombre fini compris entre 0 et 1")
        return value

    @staticmethod
    def _interval(policy: TriggerPolicyConfig) -> float:
        value = float(policy.interval_seconds)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("interval_seconds doit être un nombre fini > 0")
        return max(0.001, value)

    @staticmethod
    def _cooldown(policy: TriggerPolicyConfig) -> float:
        value = float(policy.cooldown_seconds)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("cooldown_seconds doit être un nombre fini >= 0")
        return value

    @staticmethod
    def _duration(policy: TriggerPolicyConfig, target: TriggerTargetConfig) -> float:
        value = target.duration_seconds
        if value is None:
            value = policy.default_duration_seconds
        number = float(value)
        if not math.isfinite(number) or number <= 0.0:
            raise ValueError("duration_seconds doit être un nombre fini > 0")
        return max(0.001, number)

    @staticmethod
    def _reset_to_idle(state: ActivationRuntimeState) -> None:
        state.phase = ActivationPhase.IDLE
        state.next_roll_at = None
        state.visible_until = None
        state.cooldown_until = None
        state.active_source = ""
        state.active_container = ""
        state.active_container_kind = "scene"
