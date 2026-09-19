from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from stream_state_router.activation import ActivationEvent
from stream_state_router.obs.dispatcher import DispatchResult
from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.models import ForegroundApp, StreamState
from stream_state_router.router.rules import AppRule, RuleSet
from stream_state_router.services.runtime import RoutingService


class FakeProvider:
    def __init__(self, app):
        self.app = app

    def get(self):
        return self.app


class FakeDispatcher:
    def __init__(self):
        self.changes = []

    def dispatch_change(self, change):
        self.changes.append(change)
        return DispatchResult(0, 0, ("game",))

    def dispatch_state(self, state, force=False):
        return DispatchResult(0, 0, ("game",))




class FakeOBSHeartbeatClient:
    def __init__(self):
        self.config = SimpleNamespace(enabled=True)
        self.connected = False
        self.ok = True
        self.probes = 0

    def probe(self):
        self.probes += 1
        self.connected = self.ok
        return self.ok, "connected" if self.ok else "offline"


class FakeHeartbeatDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.client = FakeOBSHeartbeatClient()

class FakeActivationScheduler:
    def __init__(self):
        self.reset_all_calls = 0
        self.reset_policy_calls = []
        self.tick_calls = 0
        self.policies = {}

    def reset_all(self):
        self.reset_all_calls += 1
        return []

    def reset_policy(self, policy_name, *, now=None):
        self.reset_policy_calls.append((policy_name, now))
        return []

    def tick(self, eligibility, *, now=None):
        self.tick_calls += 1
        return []


class FakeActivationController:
    def __init__(self):
        self.reconcile_calls = 0
        self.changed = False
        self.events = []

    def reconcile(self):
        self.reconcile_calls += 1
        return ()

    def scene_collection_changed(self):
        value = self.changed
        self.changed = False
        return value

    def is_eligible(self, _name, _policy):
        return True

    def apply_event(self, event):
        self.events.append(event)


class RuntimeTests(unittest.TestCase):
    def test_service_routes_foreground_in_background(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", state, exe="game.exe")]),
            debounce_ms=0,
        )
        dispatcher = FakeDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        seen = threading.Event()
        service.on_change = lambda _change: seen.set()
        try:
            service.start()
            self.assertTrue(seen.wait(1.0))
            self.assertEqual(dispatcher.changes[0].current.game, "Game")
        finally:
            service.stop()

    def test_obs_heartbeat_connects_and_reports_disconnect_without_dispatch(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeHeartbeatDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            obs_probe_seconds=0.05,
            provider=FakeProvider(app),
        )
        events = []
        service.on_event = events.append
        try:
            service.start()
            deadline = time.monotonic() + 1.0
            while not any(event.kind == "obs_connected" for event in events) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(any(event.kind == "obs_connected" for event in events))
            self.assertTrue(dispatcher.client.connected)

            dispatcher.client.ok = False
            deadline = time.monotonic() + 1.0
            while not any(event.kind == "obs_disconnected" for event in events) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(any(event.kind == "obs_disconnected" for event in events))
            self.assertFalse(dispatcher.client.connected)
        finally:
            service.stop()

    def test_pause_prevents_routing_until_resumed(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", state, exe="game.exe")]),
            debounce_ms=0,
        )
        dispatcher = FakeDispatcher()
        service = RoutingService(engine, dispatcher, poll_ms=20, provider=FakeProvider(app))
        service.pause(True)
        try:
            service.start()
            time.sleep(0.08)
            self.assertEqual(dispatcher.changes, [])
            service.pause(False)
            deadline = time.monotonic() + 1.0
            while not dispatcher.changes and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(dispatcher.changes)
        finally:
            service.stop()

    def test_obs_reconnect_reconciles_activation_fail_safe(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeHeartbeatDispatcher()
        scheduler = FakeActivationScheduler()
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        service._probe_obs_if_due()

        self.assertEqual(scheduler.reset_all_calls, 1)
        self.assertEqual(controller.reconcile_calls, 1)

    def test_scene_collection_change_resets_activation(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        controller = FakeActivationController()
        controller.changed = True
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        service._tick_activation(paused=False)

        self.assertEqual(scheduler.reset_all_calls, 1)
        self.assertEqual(controller.reconcile_calls, 1)
        self.assertEqual(scheduler.tick_calls, 0)

    def test_activation_show_event_is_sent_to_controller(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        event = ActivationEvent(
            "show",
            "egg",
            10.0,
            source="Cloud",
            container="[Module] EasterEgg",
            duration_seconds=5.0,
        )

        service._handle_activation_event(event, now=10.0)

        self.assertEqual(controller.events, [event])

    def test_activation_status_reports_remaining_deadlines(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        scheduler.policies = {"egg": SimpleNamespace()}
        scheduler.state = lambda _name: SimpleNamespace(
            phase=SimpleNamespace(value="visible"),
            active_source="Cloud",
            next_roll_at=None,
            visible_until=time.monotonic() + 5.0,
            cooldown_until=None,
        )
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        status = service.activation_status("egg")

        self.assertTrue(status["available"])
        self.assertEqual(status["phase"], "visible")
        self.assertEqual(status["active_source"], "Cloud")
        self.assertGreater(status["visible_seconds"], 0.0)

    def test_activation_manual_trigger_is_applied_to_controller(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        scheduler.policies = {"egg": SimpleNamespace()}
        scheduler.trigger_now = lambda *args, **kwargs: [
            ActivationEvent(
                "show",
                "egg",
                kwargs.get("now", 0.0),
                source="Cloud",
                container="[Module] EasterEgg",
                duration_seconds=5.0,
            )
        ]
        controller = FakeActivationController()
        controller.is_eligible = lambda _name, _policy: True
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        events = service.activation_trigger_now("egg", target_source="Cloud")

        self.assertEqual(len(events), 1)
        self.assertEqual(controller.events, events)

    def test_manual_trigger_respects_cooldown_by_default(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        scheduler.policies = {"egg": SimpleNamespace()}
        seen = {}

        def trigger_now(*args, **kwargs):
            seen.update(kwargs)
            return []

        scheduler.trigger_now = trigger_now
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        service.activation_trigger_now("egg")

        self.assertFalse(seen["ignore_cooldown"])

    def test_manual_reset_all_uses_fail_safe_reconciliation(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        service.activation_reset_all()

        self.assertEqual(scheduler.reset_all_calls, 1)
        self.assertEqual(controller.reconcile_calls, 1)
        self.assertTrue(
            any("réinitialisation manuelle" in row for row in service.activation_diagnostics())
        )

    def test_activation_status_includes_eligibility_and_diagnostics(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        scheduler.policies = {"egg": SimpleNamespace()}
        scheduler.state = lambda _name: SimpleNamespace(
            phase=SimpleNamespace(value="eligible"),
            active_source="",
            next_roll_at=None,
            visible_until=None,
            cooldown_until=None,
        )
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        service._record_activation_diagnostic("egg", "test", "diagnostic visible")
        service._activation_eligibility_cache["egg"] = (True, "test éligible")

        status = service.activation_status("egg")

        self.assertTrue(status["eligible"])
        self.assertIn("diagnostic visible", status["last_event"])
        self.assertTrue(status["diagnostics"])


if __name__ == "__main__":
    unittest.main()
