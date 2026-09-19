from __future__ import annotations

import unittest

from stream_state_router.activation import (
    ActivationPhase,
    ActivationScheduler,
    TriggerPolicyConfig,
    TriggerTargetConfig,
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


if __name__ == "__main__":
    unittest.main()
