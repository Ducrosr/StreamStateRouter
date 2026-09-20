from __future__ import annotations

import unittest

from stream_state_router.activation import (
    ActivationPhase,
    ActivationScheduler,
    TriggerPolicyConfig,
    TriggerTargetConfig,
    TriggerTargetIdentity,
)


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class SequenceRng:
    def __init__(self, *values: float):
        self.values = list(values)

    def random(self) -> float:
        if not self.values:
            raise AssertionError("Sequence RNG exhausted")
        return self.values.pop(0)


class ActivationSchedulerTests(unittest.TestCase):
    def policy(self, **overrides) -> TriggerPolicyConfig:
        values = {
            "module_source": "[Module] EasterEgg",
            "chance": 1.0,
            "interval_seconds": 10.0,
            "cooldown_seconds": 0.0,
            "default_duration_seconds": 5.0,
            "targets": (
                TriggerTargetConfig("[Module] EasterEgg", "A", weight=1.0),
                TriggerTargetConfig("[Module] EasterEgg", "B", weight=1.0),
            ),
        }
        values.update(overrides)
        return TriggerPolicyConfig(**values)

    def test_random_trigger_uses_interval_and_fixed_duration(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy()},
            clock=clock,
            rng=SequenceRng(0.0, 0.0),
        )

        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.ELIGIBLE)

        clock.value = 9.9
        self.assertEqual(scheduler.tick(), [])
        clock.value = 10.0
        events = scheduler.tick()
        self.assertEqual([event.kind for event in events], ["roll", "show"])
        self.assertEqual(events[-1].source, "A")
        self.assertEqual(events[-1].container, "[Module] EasterEgg")
        self.assertEqual(scheduler.state("egg").active_container, "[Module] EasterEgg")
        self.assertEqual(scheduler.state("egg").visible_until, 15.0)

        clock.value = 15.0
        events = scheduler.tick()
        self.assertEqual([event.kind for event in events], ["hide"])
        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.ELIGIBLE)

    def test_zero_weight_target_is_never_randomly_selected(self):
        policy = self.policy(
            targets=(
                TriggerTargetConfig("[Module] EasterEgg", "A", weight=1.0),
                TriggerTargetConfig("[Module] EasterEgg", "B", weight=0.0),
            )
        )
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": policy},
            clock=clock,
            rng=SequenceRng(0.0, 0.999999),
        )
        scheduler.tick()
        clock.value = 10.0
        events = scheduler.tick()
        self.assertEqual(events[-1].source, "A")

    def test_cooldown_blocks_new_trigger_until_complete(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy(cooldown_seconds=20.0)},
            clock=clock,
            rng=SequenceRng(0.0, 0.0, 0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        scheduler.tick()
        clock.value = 15.0
        events = scheduler.tick()
        self.assertEqual([event.kind for event in events], ["hide", "cooldown_started"])
        self.assertEqual(scheduler.state("egg").cooldown_until, 35.0)

        clock.value = 34.9
        self.assertEqual(scheduler.tick(), [])
        clock.value = 35.0
        self.assertEqual([event.kind for event in scheduler.tick()], ["cooldown_complete"])
        self.assertEqual(scheduler.state("egg").next_roll_at, 45.0)
        clock.value = 45.0
        self.assertEqual([event.kind for event in scheduler.tick()], ["roll", "show"])

    def test_late_tick_skips_missed_slots_without_burst(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy(chance=0.0)},
            clock=clock,
            rng=SequenceRng(0.4, 0.5),
        )
        scheduler.tick()
        clock.value = 35.0
        events = scheduler.tick()
        self.assertEqual([event.kind for event in events], ["roll"])
        self.assertEqual(scheduler.state("egg").next_roll_at, 40.0)
        self.assertEqual(scheduler.tick(), [])
        clock.value = 40.0
        self.assertEqual([event.kind for event in scheduler.tick()], ["roll"])

    def test_avoid_immediate_repeat_uses_other_available_target(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy()},
            clock=clock,
            rng=SequenceRng(0.0, 0.0, 0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        self.assertEqual(scheduler.tick()[-1].source, "A")
        clock.value = 15.0
        scheduler.tick()
        clock.value = 25.0
        self.assertEqual(scheduler.tick()[-1].source, "B")

    def test_avoid_immediate_repeat_uses_exact_target_identity(self):
        policy = self.policy(
            targets=(
                TriggerTargetConfig("Scene A", "Same", container_kind="scene", weight=1.0),
                TriggerTargetConfig("Scene B", "Same", container_kind="scene", weight=1.0),
            )
        )
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": policy},
            clock=clock,
            rng=SequenceRng(0.0, 0.0, 0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        first = scheduler.tick()[-1]
        self.assertEqual(first.container, "Scene A")
        clock.value = 15.0
        scheduler.tick()
        clock.value = 25.0
        second = scheduler.tick()[-1]
        self.assertEqual(second.container, "Scene B")
        self.assertEqual(second.source, "Same")

    def test_scheduler_rejects_cross_policy_target_ownership_conflict(self):
        target = TriggerTargetConfig("Egg", "Cloud")
        with self.assertRaisesRegex(ValueError, "plusieurs politiques"):
            ActivationScheduler(
                {
                    "one": self.policy(targets=(target,)),
                    "two": self.policy(targets=(target,)),
                }
            )

    def test_manual_specific_target_can_test_zero_weight_source(self):
        policy = self.policy(
            targets=(
                TriggerTargetConfig("[Module] EasterEgg", "A", weight=1.0),
                TriggerTargetConfig("[Module] EasterEgg", "B", weight=0.0, duration_seconds=2.0),
            )
        )
        clock = FakeClock()
        scheduler = ActivationScheduler({"egg": policy}, clock=clock)
        events = scheduler.trigger_now("egg", target_source="B")
        self.assertEqual([event.kind for event in events], ["show"])
        self.assertEqual(events[0].source, "B")
        self.assertEqual(events[0].duration_seconds, 2.0)

    def test_test_roll_does_not_mutate_runtime_state(self):
        scheduler = ActivationScheduler(
            {"egg": self.policy()},
            rng=SequenceRng(0.0, 0.8),
        )
        before = scheduler.state("egg")
        result = scheduler.test_roll("egg")
        after = scheduler.state("egg")
        self.assertTrue(result.triggered)
        self.assertEqual(result.source, "B")
        self.assertEqual(before, after)

    def test_becoming_ineligible_hides_active_source_and_resets_idle(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy()},
            clock=clock,
            rng=SequenceRng(0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        scheduler.tick()
        clock.value = 11.0
        events = scheduler.tick(lambda _name, _policy: False)
        self.assertEqual([event.kind for event in events], ["hide"])
        self.assertEqual(events[0].reason, "ineligible")
        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.IDLE)

    def test_reset_cooldown_reschedules_next_roll(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy(cooldown_seconds=100.0)},
            clock=clock,
            rng=SequenceRng(0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        scheduler.tick()
        clock.value = 15.0
        scheduler.tick()
        clock.value = 20.0
        events = scheduler.reset_cooldown("egg")
        self.assertEqual([event.kind for event in events], ["cooldown_complete"])
        self.assertEqual(scheduler.state("egg").next_roll_at, 30.0)

    def test_simulation_is_deterministic_and_does_not_mutate_runtime(self):
        scheduler = ActivationScheduler({"egg": self.policy()})
        before = scheduler.state("egg")

        first = scheduler.simulate("egg", trials=1000, seed=42)
        second = scheduler.simulate("egg", trials=1000, seed=42)

        self.assertEqual(first, second)
        self.assertEqual(first.trigger_count, 1000)
        self.assertEqual(first.miss_count, 0)
        self.assertEqual(first.blocked_count, 0)
        self.assertEqual(sum(count for _name, count in first.target_counts), 1000)
        self.assertEqual(before, scheduler.state("egg"))

    def test_simulation_reports_hits_blocked_by_missing_targets(self):
        policy = self.policy(targets=())
        scheduler = ActivationScheduler({"egg": policy})

        result = scheduler.simulate("egg", trials=25, seed=7)

        self.assertEqual(result.chance_hit_count, 25)
        self.assertEqual(result.trigger_count, 0)
        self.assertEqual(result.blocked_count, 25)
        self.assertEqual(result.miss_count, 0)

    def test_successful_roll_without_target_emits_blocked_diagnostic_event(self):
        policy = self.policy(targets=())
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": policy},
            clock=clock,
            rng=SequenceRng(0.0),
        )
        scheduler.tick()
        clock.value = 10.0

        events = scheduler.tick()

        self.assertEqual([event.kind for event in events], ["roll", "blocked"])
        self.assertEqual(events[-1].reason, "no_target")

    def test_manual_target_identity_disambiguates_same_source_name(self):
        policy = self.policy(
            targets=(
                TriggerTargetConfig("Scene A", "Cloud", container_kind="scene"),
                TriggerTargetConfig("Group B", "Cloud", container_kind="group"),
            )
        )
        scheduler = ActivationScheduler({"egg": policy})

        with self.assertRaisesRegex(RuntimeError, "ambiguë"):
            scheduler.trigger_now("egg", target_source="Cloud")

        events = scheduler.trigger_now(
            "egg",
            target_identity=TriggerTargetIdentity("Group B", "Cloud", "group"),
        )
        self.assertEqual(events[0].container, "Group B")
        self.assertEqual(events[0].container_kind, "group")

    def test_non_finite_direct_scheduler_values_are_rejected(self):
        invalid_policies = (
            self.policy(chance=float("nan")),
            self.policy(interval_seconds=float("inf")),
            self.policy(default_duration_seconds=float("nan")),
            self.policy(cooldown_seconds=float("inf")),
            self.policy(
                targets=(
                    TriggerTargetConfig(
                        "[Module] EasterEgg",
                        "A",
                        weight=float("inf"),
                    ),
                )
            ),
            self.policy(
                targets=(
                    TriggerTargetConfig(
                        "[Module] EasterEgg",
                        "A",
                        duration_seconds=float("nan"),
                    ),
                )
            ),
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    ActivationScheduler({"egg": policy})

    def test_large_finite_weights_do_not_overflow_sum(self):
        policy = self.policy(
            targets=(
                TriggerTargetConfig("[Module] EasterEgg", "A", weight=1e308),
                TriggerTargetConfig("[Module] EasterEgg", "B", weight=1e308),
            )
        )
        scheduler = ActivationScheduler(
            {"egg": policy},
            rng=SequenceRng(0.0, 0.75),
        )

        result = scheduler.test_roll("egg")

        self.assertTrue(result.triggered)
        self.assertIn(result.source, {"A", "B"})

    def test_losing_eligibility_characterization_clears_cooldown(self):
        clock = FakeClock()
        scheduler = ActivationScheduler(
            {"egg": self.policy(cooldown_seconds=100.0)},
            clock=clock,
            rng=SequenceRng(0.0, 0.0),
        )
        scheduler.tick()
        clock.value = 10.0
        scheduler.tick()
        clock.value = 15.0
        scheduler.tick()
        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.COOLDOWN)

        clock.value = 16.0
        scheduler.tick(lambda _name, _policy: False)

        state = scheduler.state("egg")
        self.assertEqual(state.phase, ActivationPhase.IDLE)
        self.assertIsNone(state.cooldown_until)



if __name__ == "__main__":
    unittest.main()
