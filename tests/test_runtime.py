from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stream_state_router.activation import (
    ActivationEvent,
    ActivationPhase,
    ActivationScheduler,
    TriggerPolicyConfig,
    TriggerTargetConfig,
)
from stream_state_router.obs.dispatcher import DispatchResult, DomainDispatchStatus
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


class DiagnosticDispatcher(FakeDispatcher):
    def __init__(self, *, incomplete: bool = False):
        super().__init__()
        self.client = SimpleNamespace(request_count=0)
        self.incomplete = incomplete
        self._pending = ("game",)

    def pending_domains(self, _state=None):
        return self._pending

    def dispatch_change(self, change):
        self.changes.append(change)
        self.client.request_count += 3
        if self.incomplete:
            return DispatchResult(
                0,
                1,
                ("game",),
                ("blocked for test",),
                (DomainDispatchStatus("game", change.current.game, "", "blocked", "test"),),
            )
        self._pending = ()
        return DispatchResult(
            1,
            0,
            ("game",),
            (),
            (DomainDispatchStatus("game", change.current.game, change.current.game, "applied"),),
        )

    def dispatch_state(self, state, force=False):
        change = SimpleNamespace(current=state)
        return self.dispatch_change(change)


class CommandDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.profile_threads = []

    def execute_profile(self, domain, profile_name):
        self.profile_threads.append((domain, profile_name, threading.current_thread().name))
        return DispatchResult(1, 0, (domain,))


class BlockingDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.dispatch_entered = threading.Event()
        self.release_dispatch = threading.Event()

    def dispatch_change(self, change):
        self.dispatch_entered.set()
        if not self.release_dispatch.wait(2.0):
            raise RuntimeError("dispatch barrier timed out")
        return super().dispatch_change(change)


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
        self.thread_names = []

    def reconcile(self):
        self.reconcile_calls += 1
        return ()

    def pending_hides(self, _policy_name=None):
        return ()

    def policy_cleanup_status(self, _policy_name):
        return False, ""

    def retry_pending_hides(self, *, now=None):
        return ()

    def scene_collection_changed(self):
        value = self.changed
        self.changed = False
        return value

    def eligibility(self, _name, _policy):
        return True, "test éligible"

    def is_eligible(self, _name, _policy):
        return True

    def apply_event(self, event):
        self.thread_names.append(threading.current_thread().name)
        self.events.append(event)


class CleanupBlockingController(FakeActivationController):
    def __init__(self):
        super().__init__()
        self.blocked = True

    def policy_cleanup_status(self, _policy_name):
        if self.blocked:
            return True, "nettoyage OBS en attente : Egg/Cloud"
        return False, ""


class BlockingActivationController(FakeActivationController):
    def __init__(self):
        super().__init__()
        self.show_entered = threading.Event()
        self.release_show = threading.Event()
        self.hide_seen = threading.Event()

    def apply_event(self, event):
        self.thread_names.append(threading.current_thread().name)
        self.events.append(event)
        if event.kind == "show":
            self.show_entered.set()
            if not self.release_show.wait(2.0):
                raise RuntimeError("test barrier timed out")
        elif event.kind == "hide":
            self.hide_seen.set()


class ResultCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._results = {}
        self._events = {}

    def callback(self, event):
        if event.kind != "activation_command_result" or event.payload is None:
            return
        request_id = event.request_id
        with self._lock:
            self._results[request_id] = event.payload
            waiter = self._events.setdefault(request_id, threading.Event())
            waiter.set()

    def wait(self, request_id, timeout=2.0):
        with self._lock:
            if request_id in self._results:
                return self._results[request_id]
            waiter = self._events.setdefault(request_id, threading.Event())
        if not waiter.wait(timeout):
            raise AssertionError(f"activation result timeout: {request_id}")
        with self._lock:
            return self._results[request_id]


class OBSResultCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._results = {}
        self._events = {}

    def callback(self, event):
        if event.kind != "obs_command_result" or event.payload is None:
            return
        with self._lock:
            self._results[event.request_id] = event.payload
            waiter = self._events.setdefault(event.request_id, threading.Event())
            waiter.set()

    def wait(self, request_id, timeout=2.0):
        with self._lock:
            if request_id in self._results:
                return self._results[request_id]
            waiter = self._events.setdefault(request_id, threading.Event())
        if not waiter.wait(timeout):
            raise AssertionError(f"OBS command result timeout: {request_id}")
        with self._lock:
            return self._results[request_id]


def activation_policy(*, enabled=True, cooldown=20.0):
    return TriggerPolicyConfig(
        module_source="[Module] EasterEgg",
        enabled=enabled,
        active_when="always",
        chance=0.0,
        interval_seconds=9999.0,
        cooldown_seconds=cooldown,
        default_duration_seconds=30.0,
        exclusive=False,
        targets=(
            TriggerTargetConfig(
                "[Module] EasterEgg",
                "Cloud",
                weight=1.0,
            ),
        ),
    )


class RuntimeTests(unittest.TestCase):
    def test_live_obs_profile_command_runs_on_runtime_worker(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = CommandDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        collector = OBSResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            request_id = service.request_profile("game", "Vanilla")
            result = collector.wait(request_id)
            self.assertTrue(result.success, result.error)
            self.assertEqual(
                dispatcher.profile_threads,
                [("game", "Vanilla", "SSR-Router")],
            )
        finally:
            self.assertTrue(service.stop())

    def test_delayed_dispatch_uses_runtime_deadline_not_timer_thread(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", state, exe="game.exe", apply_delay_ms=60)]),
            debounce_ms=0,
        )
        dispatcher = FakeDispatcher()
        service = RoutingService(engine, dispatcher, poll_ms=10, provider=FakeProvider(app))
        seen = threading.Event()
        service.on_dispatch = lambda _result: seen.set()
        service.start()
        try:
            self.assertFalse(seen.wait(0.02))
            self.assertTrue(seen.wait(1.0))
            self.assertEqual(len(dispatcher.changes), 1)
        finally:
            self.assertTrue(service.stop())

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
            self.assertTrue(service.stop())

    def test_routing_diagnostic_keeps_decision_id_through_result(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", state, exe="game.exe")]),
            debounce_ms=0,
        )
        dispatcher = DiagnosticDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=10,
            provider=FakeProvider(app),
            config_revision="rev-safe",
        )
        events = []
        completed = threading.Event()

        def on_event(event):
            events.append(event)
            if event.kind == "routing_result":
                completed.set()

        service.on_event = on_event
        service.start()
        try:
            self.assertTrue(completed.wait(1.0))
            decision = next(event for event in events if event.kind == "routing_decision")
            result = next(event for event in events if event.kind == "routing_result")
            self.assertEqual(decision.request_id, result.request_id)
            self.assertTrue(result.success)
            self.assertEqual(result.payload["config_revision"], "rev-safe")
            self.assertEqual(result.payload["requested_domains"], ["game"])
            self.assertEqual(result.payload["applied_domains"], ["game"])
            self.assertEqual(result.payload["obs_requests"], 3)
        finally:
            self.assertTrue(service.stop())

    def test_routing_diagnostic_classifies_blocked_domain_as_incomplete(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", state, exe="game.exe")]),
            debounce_ms=0,
        )
        dispatcher = DiagnosticDispatcher(incomplete=True)
        service = RoutingService(engine, dispatcher, poll_ms=10, provider=FakeProvider(app))
        completed = threading.Event()
        service.on_event = lambda event: completed.set() if event.kind == "routing_result" else None
        service.start()
        try:
            self.assertTrue(completed.wait(1.0))
            status = service.routing_status()
            before = dispatcher.client.request_count
            snapshots = service.routing_diagnostics(limit=5)
            self.assertEqual(dispatcher.client.request_count, before)
            self.assertFalse(status["success"])
            self.assertEqual(status["blocked_domains"], ["game"])
            self.assertEqual(status["pending_domains"], ["game"])
            self.assertTrue(snapshots)
            serialized = repr(status).casefold()
            self.assertNotIn("password", serialized)
            self.assertNotIn("token", serialized)
        finally:
            self.assertTrue(service.stop())

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
        connected = threading.Event()
        disconnected = threading.Event()

        def on_event(event):
            events.append(event)
            if event.kind == "obs_connected":
                connected.set()
            if event.kind == "obs_disconnected":
                disconnected.set()

        service.on_event = on_event
        try:
            service.start()
            self.assertTrue(connected.wait(1.0))
            self.assertTrue(dispatcher.client.connected)

            dispatcher.client.ok = False
            service._wake.set()
            self.assertTrue(disconnected.wait(1.0))
            self.assertFalse(dispatcher.client.connected)
        finally:
            self.assertTrue(service.stop())

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
        routed = threading.Event()
        service.on_change = lambda _change: routed.set()
        try:
            service.start()
            self.assertFalse(routed.wait(0.1))
            service.pause(False)
            self.assertTrue(routed.wait(1.0))
        finally:
            self.assertTrue(service.stop())

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

    def test_activation_status_reports_remaining_deadlines_from_snapshot(self):
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
        service._activation_eligibility_cache["egg"] = (True, "cached")

        status = service.activation_status("egg")

        self.assertTrue(status["available"])
        self.assertEqual(status["phase"], "visible")
        self.assertEqual(status["active_source"], "Cloud")
        self.assertEqual(status["eligibility_reason"], "cached")
        self.assertGreater(status["visible_seconds"], 0.0)

    def test_show_and_stop_are_serialized_on_runtime_worker(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy(cooldown=20.0)
        scheduler = ActivationScheduler({"egg": policy})
        controller = BlockingActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            trigger_id = service.activation_trigger_now("egg", target_source="Cloud")
            self.assertTrue(controller.show_entered.wait(1.0))

            stop_id = service.activation_stop("egg")
            self.assertFalse(controller.hide_seen.is_set())

            controller.release_show.set()
            trigger_result = collector.wait(trigger_id)
            stop_result = collector.wait(stop_id)

            self.assertTrue(trigger_result.success, trigger_result.error)
            self.assertTrue(stop_result.success, stop_result.error)
            self.assertTrue(controller.hide_seen.wait(1.0))
            self.assertEqual(
                [event.kind for event in controller.events[:2]],
                ["show", "hide"],
            )
            self.assertTrue(
                all(name == "SSR-Router" for name in controller.thread_names)
            )
            self.assertEqual(
                scheduler.state("egg").phase,
                ActivationPhase.COOLDOWN,
            )
        finally:
            controller.release_show.set()
            self.assertTrue(service.stop())

    def test_disabled_policy_reports_disabled_reason_and_rejects_manual_trigger(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy(enabled=False)
        scheduler = ActivationScheduler({"egg": policy})
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            deadline = time.monotonic() + 1.0
            status = service.activation_status("egg")
            while (
                status.get("eligibility_reason") != "politique désactivée"
                and time.monotonic() < deadline
            ):
                service._wake.set()
                threading.Event().wait(0.01)
                status = service.activation_status("egg")
            self.assertFalse(status["eligible"])
            self.assertEqual(status["eligibility_reason"], "politique désactivée")

            request_id = service.activation_trigger_now("egg")
            result = collector.wait(request_id)
            self.assertFalse(result.success)
            self.assertIn("Politique", result.error)
        finally:
            self.assertTrue(service.stop())

    def test_pause_reason_is_used_for_tick_command_and_status(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy()
        scheduler = ActivationScheduler({"egg": policy})
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            service.pause(True)
            service._wake.set()
            deadline = time.monotonic() + 1.0
            status = service.activation_status("egg")
            while (
                status.get("eligibility_reason") != "routage suspendu"
                and time.monotonic() < deadline
            ):
                threading.Event().wait(0.01)
                status = service.activation_status("egg")
            self.assertEqual(status["eligibility_reason"], "routage suspendu")

            request_id = service.activation_trigger_now("egg")
            result = collector.wait(request_id)
            self.assertFalse(result.success)
            self.assertIn("non éligible", result.error)
        finally:
            self.assertTrue(service.stop())

    def test_simulation_does_not_block_live_activation_worker(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy(cooldown=0.0)
        scheduler = ActivationScheduler({"egg": policy})
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        simulation_entered = threading.Event()
        release_simulation = threading.Event()
        original_simulate = ActivationScheduler.simulate

        def blocked_simulate(self, policy_name, *, trials=1000, seed=12345):
            simulation_entered.set()
            if not release_simulation.wait(2.0):
                raise RuntimeError("simulation barrier timed out")
            return original_simulate(self, policy_name, trials=trials, seed=seed)

        with patch(
            "stream_state_router.services.runtime.ActivationScheduler.simulate",
            blocked_simulate,
        ):
            service.start()
            try:
                simulation_id = service.activation_simulate("egg", trials=10, seed=7)
                self.assertTrue(simulation_entered.wait(1.0))

                trigger_id = service.activation_trigger_now("egg", target_source="Cloud")
                trigger_result = collector.wait(trigger_id)
                self.assertTrue(trigger_result.success, trigger_result.error)

                stop_id = service.activation_stop("egg")
                stop_result = collector.wait(stop_id)
                self.assertTrue(stop_result.success, stop_result.error)
                self.assertEqual(
                    [event.kind for event in controller.events[-2:]],
                    ["show", "hide"],
                )

                # Simulation is still deliberately blocked while live hide has
                # already completed on SSR-Router.
                self.assertNotIn(simulation_id, collector._results)
                release_simulation.set()
                simulation_result = collector.wait(simulation_id)
                self.assertTrue(simulation_result.success, simulation_result.error)
                self.assertTrue(simulation_result.result.config_fingerprint)
            finally:
                release_simulation.set()
                self.assertTrue(service.stop())

    def test_stop_rejects_new_commands_and_invalidates_queued_commands(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy()
        scheduler = ActivationScheduler({"egg": policy})
        controller = BlockingActivationController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        service.start()
        trigger_id = service.activation_trigger_now("egg")
        self.assertTrue(controller.show_entered.wait(1.0))
        queued_id = service.activation_stop("egg")

        stopped = {}

        def stop_service():
            stopped["value"] = service.stop(timeout=2.0)

        stopper = threading.Thread(target=stop_service)
        stopper.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "arrêt"):
                service.activation_stop("egg")
            controller.release_show.set()
            stopper.join(2.0)
            self.assertTrue(stopped.get("value"))
            cancelled = collector.wait(queued_id)
            self.assertFalse(cancelled.success)
            self.assertIn("annulée", cancelled.error)
            self.assertTrue(trigger_id)
        finally:
            controller.release_show.set()
            if stopper.is_alive():
                stopper.join(1.0)
            service.stop()

    def test_pending_cleanup_blocks_tick_status_and_manual_trigger(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        policy = activation_policy()
        scheduler = ActivationScheduler({"egg": policy})
        controller = CleanupBlockingController()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = ResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            deadline = time.monotonic() + 1.0
            status = service.activation_status("egg")
            while (
                "nettoyage OBS en attente" not in str(status.get("eligibility_reason"))
                and time.monotonic() < deadline
            ):
                service._wake.set()
                threading.Event().wait(0.01)
                status = service.activation_status("egg")

            self.assertFalse(status["eligible"])
            self.assertIn("nettoyage OBS en attente", status["eligibility_reason"])
            self.assertEqual(scheduler.state("egg").phase, ActivationPhase.IDLE)

            request_id = service.activation_trigger_now("egg")
            result = collector.wait(request_id)
            self.assertFalse(result.success)
            self.assertIn("non éligible", result.error)
            self.assertEqual(controller.events, [])
        finally:
            self.assertTrue(service.stop())

    def test_losing_eligibility_clears_cooldown_characterization_is_preserved(self):
        policy = activation_policy(cooldown=100.0)
        scheduler = ActivationScheduler({"egg": policy})
        scheduler.trigger_now("egg", now=0.0)
        scheduler.stop("egg", now=1.0)
        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.COOLDOWN)

        scheduler.tick(lambda _name, _policy: False, now=2.0)

        self.assertEqual(scheduler.state("egg").phase, ActivationPhase.IDLE)
        self.assertIsNone(scheduler.state("egg").cooldown_until)

    def test_shutdown_result_preserves_unacknowledged_cleanup(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        controller = FakeActivationController()
        controller.export_pending_hides = lambda: (
            {
                "policy": "egg",
                "collection": "Collection A",
                "target": {"container": "Egg", "source": "Cloud"},
            },
        )
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )

        result = service.stop()

        self.assertTrue(result)
        self.assertFalse(result.cleanup_complete)
        self.assertEqual(len(result.pending_cleanup), 1)

    def test_stop_waits_for_inflight_old_obs_dispatch(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet(
                [
                    AppRule(
                        "Game",
                        state,
                        exe="game.exe",
                        apply_delay_ms=10,
                    )
                ]
            ),
            debounce_ms=0,
        )
        dispatcher = BlockingDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        service.start()
        try:
            self.assertTrue(dispatcher.dispatch_entered.wait(1.0))
            result = {}
            stop_started = threading.Event()

            def stop_service():
                stop_started.set()
                result["stopped"] = service.stop(timeout=2.0)

            stopper = threading.Thread(target=stop_service)
            stopper.start()
            self.assertTrue(stop_started.wait(1.0))
            self.assertTrue(stopper.is_alive())

            dispatcher.release_dispatch.set()
            stopper.join(2.0)
            self.assertTrue(result.get("stopped"))
        finally:
            dispatcher.release_dispatch.set()
            service.stop()



if __name__ == "__main__":
    unittest.main()
