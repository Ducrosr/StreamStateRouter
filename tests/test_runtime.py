from __future__ import annotations

import queue
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
from stream_state_router.router.rules import AppRule, ResolutionKind, RuleSet
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


    def cached_obs_context(self):
        return {
            "obs_enabled": True,
            "streaming": False,
            "recording": False,
            "program_scene": "Gameplay",
        }

    def plan_state(self, state, *, context=None):
        return {
            "state": state.as_variables(),
            "context": dict(context or {}),
            "domains": [{"domain": "game", "status": "planned"}],
        }


class CommandDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.profile_threads = []
        self.layout_threads = []

    def execute_profile(self, domain, profile_name):
        self.profile_threads.append((domain, profile_name, threading.current_thread().name))
        return DispatchResult(1, 0, (domain,))

    def execute_layout_profile(self, profile_name, preview=False):
        self.layout_threads.append(
            (profile_name, bool(preview), threading.current_thread().name)
        )
        return SimpleNamespace(warnings=(), missing_sources=())


class BlockingLayoutDispatcher(CommandDispatcher):
    def __init__(self):
        super().__init__()
        self.layout_entered = threading.Event()
        self.release_layout = threading.Event()

    def execute_layout_profile(self, profile_name, preview=False):
        self.layout_entered.set()
        if not self.release_layout.wait(2.0):
            raise RuntimeError("layout barrier timed out")
        return super().execute_layout_profile(profile_name, preview=preview)


class CooperativeLayoutManager:
    def __init__(self):
        self._yield = None

    def set_cooperative_yield(self, callback):
        self._yield = callback

    def checkpoint(self):
        if self._yield is not None:
            self._yield()


class CooperativeLayoutDispatcher(CommandDispatcher):
    def __init__(self):
        super().__init__()
        self.layout_manager = CooperativeLayoutManager()
        self.layout_entered = threading.Event()
        self.in_layout = False

    def execute_layout_profile(self, profile_name, preview=False):
        self.in_layout = True
        self.layout_entered.set()
        try:
            while True:
                self.layout_manager.checkpoint()
                time.sleep(0.01)
        finally:
            self.in_layout = False


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


class CooperativeBackgroundDispatcher(FakeDispatcher):
    """Model a long automatic OBS reconciliation, not an explicit command."""

    def __init__(self):
        super().__init__()
        self._yield = None
        self.reconcile_entered = threading.Event()
        self.reconcile_exited = threading.Event()

    def set_cooperative_yield(self, callback):
        self._yield = callback

    def pending_domains(self, _state=None):
        return ("game",)

    def dispatch_state(self, state, force=False):
        del state, force
        self.reconcile_entered.set()
        try:
            while True:
                if self._yield is not None:
                    self._yield()
                time.sleep(0.01)
        finally:
            self.reconcile_exited.set()


class NonCooperativeBackgroundDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.reconcile_entered = threading.Event()
        self.release_reconcile = threading.Event()

    def pending_domains(self, _state=None):
        return ("game",)

    def dispatch_state(self, state, force=False):
        del state, force
        self.reconcile_entered.set()
        if not self.release_reconcile.wait(2.0):
            raise RuntimeError("background reconcile barrier timed out")
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
        self.registered_events = []
        self.thread_names = []

    def reconcile(self):
        self.reconcile_calls += 1
        return ()

    def export_pending_hides(self):
        return ()

    def pending_hides(self, _policy_name=None):
        return ()

    def pending_hides_for_current_collection(self):
        return ()

    def register_hide_obligation(self, event):
        self.registered_events.append(event)

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


class SlowReconcileController(FakeActivationController):
    def __init__(self, delay=0.4):
        super().__init__()
        self.delay = delay

    def reconcile(self):
        self.reconcile_calls += 1
        time.sleep(self.delay)
        return ()


class ActiveShutdownScheduler(FakeActivationScheduler):
    def reset_all(self):
        self.reset_all_calls += 1
        return [
            ActivationEvent(
                "hide",
                "egg",
                time.monotonic(),
                source="Cloud",
                container="[Module] EasterEgg",
                reason="reset",
            )
        ]


class ReentrancyDetectingController(FakeActivationController):
    def __init__(self, dispatcher):
        super().__init__()
        self.dispatcher = dispatcher
        self.reconcile_during_layout = False
        self.apply_during_layout = False

    def reconcile(self):
        self.reconcile_during_layout = self.reconcile_during_layout or self.dispatcher.in_layout
        return super().reconcile()

    def apply_event(self, event):
        self.apply_during_layout = self.apply_during_layout or self.dispatcher.in_layout
        return super().apply_event(event)


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


class ExplodingCleanupController(FakeActivationController):
    def pending_hides_for_current_collection(self):
        raise RuntimeError("cleanup exploded")


class CleanupRetryController(FakeActivationController):
    def __init__(self):
        super().__init__()
        self.pending = [SimpleNamespace(policy="legacy")]
        self.retry_calls = 0

    def pending_hides(self, _policy_name=None):
        return tuple(self.pending)

    def pending_hides_for_current_collection(self):
        return tuple(self.pending)

    def retry_pending_hides(self, *, now=None):
        self.retry_calls += 1
        self.pending.clear()
        return ("cleanup ack",)


class PrearmShutdownScheduler(FakeActivationScheduler):
    def __init__(self):
        super().__init__()
        self.reset_happened = False

    def states(self):
        return {
            "egg": SimpleNamespace(
                phase=SimpleNamespace(value="visible"),
                active_source="Cloud",
                active_container="[Module] EasterEgg",
                active_container_kind="scene",
            )
        }

    def reset_all(self):
        self.reset_all_calls += 1
        self.reset_happened = True
        return [
            ActivationEvent(
                "hide",
                "egg",
                time.monotonic(),
                source="Cloud",
                container="[Module] EasterEgg",
                container_kind="scene",
                reason="reset",
            )
        ]


class PrearmShutdownController(FakeActivationController):
    def __init__(self, scheduler):
        super().__init__()
        self.scheduler = scheduler
        self.registered_before_reset = False

    def register_hide_obligation(self, event):
        self.registered_before_reset = not self.scheduler.reset_happened
        super().register_hide_obligation(event)


class CleanupLayoutManager:
    def __init__(self):
        self._yield = None
        self.imported = []
        self.exported = []

    def set_cooperative_yield(self, callback):
        self._yield = callback

    def import_pending_fade_cleanup(self, items):
        self.imported.extend(dict(item) for item in items)
        self.exported.extend(dict(item) for item in items)
        return len(self.imported)

    def export_pending_fade_cleanup(self):
        return tuple(dict(item) for item in self.exported)


class CleanupDispatcher(FakeDispatcher):
    def __init__(self):
        super().__init__()
        self.layout_manager = CleanupLayoutManager()


class OneShotBlockingQueue:
    def __init__(self):
        self.inner = queue.Queue()
        self.put_entered = threading.Event()
        self.release_first_put = threading.Event()
        self._blocked = False

    def put(self, item):
        if not self._blocked:
            self._blocked = True
            self.put_entered.set()
            if not self.release_first_put.wait(2.0):
                raise RuntimeError("queue put barrier timed out")
        self.inner.put(item)

    def get_nowait(self):
        return self.inner.get_nowait()

    def qsize(self):
        return self.inner.qsize()


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
    def test_cooperative_checkpoint_does_not_start_probe_or_activation_tick(self):
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
        service._thread = threading.current_thread()

        service._cooperative_obs_yield()

        self.assertEqual(dispatcher.client.probes, 0)
        self.assertEqual(scheduler.tick_calls, 0)
        self.assertEqual(controller.reconcile_calls, 0)

    def test_cleanup_exception_cannot_lose_consumed_shutdown(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        controller = ExplodingCleanupController()
        service = RoutingService(
            engine,
            FakeDispatcher(),
            poll_ms=20,
            provider=FakeProvider(app),
            activation_controller=controller,
        )
        service.start()
        try:
            result = service.stop(timeout=1.0)
            self.assertTrue(result, result.diagnostic_summary())
            self.assertFalse(service._thread.is_alive())
            self.assertTrue(service._stop.is_set())
        finally:
            service.stop()

    def test_cleanup_retries_without_activation_scheduler(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        controller = CleanupRetryController()
        service = RoutingService(
            engine,
            FakeDispatcher(),
            provider=FakeProvider(app),
            activation_scheduler=None,
            activation_controller=controller,
        )

        service._tick_activation(paused=False)

        self.assertEqual(controller.retry_calls, 1)
        self.assertEqual(controller.pending_hides(), ())

    def test_shutdown_prearms_visible_cleanup_before_scheduler_reset(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        scheduler = PrearmShutdownScheduler()
        controller = PrearmShutdownController(scheduler)
        service = RoutingService(
            engine,
            FakeDispatcher(),
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        service.start()
        try:
            result = service.stop(timeout=1.0)
            self.assertTrue(result, result.diagnostic_summary())
            self.assertTrue(controller.registered_before_reset)
            self.assertEqual(len(controller.registered_events), 1)
            self.assertEqual(controller.registered_events[0].source, "Cloud")
            self.assertEqual(len(controller.events), 1)
            self.assertEqual(controller.events[0].kind, "hide")
        finally:
            service.stop()

    def test_runtime_imports_and_exports_contextual_fade_cleanup(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = CleanupDispatcher()
        fade = {
            "kind": "layout_fade",
            "source": "[Webcam] Avatar",
            "collection": "Collection A",
            "created_at": 1.0,
            "attempts": 2,
            "last_error": "offline",
        }

        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            pending_cleanup=(fade,),
        )

        self.assertEqual(dispatcher.layout_manager.imported, [fade])
        self.assertEqual(service.pending_cleanup_snapshot(), (fade,))

    def test_shutdown_snapshot_combines_activation_and_fade_obligations(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = CleanupDispatcher()
        controller = FakeActivationController()
        activation = {
            "kind": "activation_hide",
            "policy": "egg",
            "collection": "Collection A",
            "target": {"container": "Egg", "source": "Cloud"},
        }
        fade = {
            "kind": "layout_fade",
            "source": "[Webcam] Avatar",
            "collection": "Collection A",
        }
        controller.export_pending_hides = lambda: (activation,)
        dispatcher.layout_manager.exported = [fade]
        service = RoutingService(
            engine,
            dispatcher,
            provider=FakeProvider(app),
            activation_controller=controller,
        )

        snapshot = service.pending_cleanup_snapshot()

        self.assertEqual(snapshot, (activation, fade))

    def test_command_admission_is_atomic_with_shutdown_boundary(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        service = RoutingService(
            engine,
            CommandDispatcher(),
            poll_ms=20,
            provider=FakeProvider(app),
        )
        service.start()
        blocker = OneShotBlockingQueue()
        service._runtime_commands = blocker
        request_done = threading.Event()
        stop_done = threading.Event()
        stop_result = {}

        def submit():
            try:
                service.request_layout("apply", "Test A")
            finally:
                request_done.set()

        def stop():
            stop_result["value"] = service.stop(timeout=1.5)
            stop_done.set()

        submitter = threading.Thread(target=submit)
        stopper = threading.Thread(target=stop)
        try:
            submitter.start()
            self.assertTrue(blocker.put_entered.wait(1.0))
            stopper.start()
            time.sleep(0.05)

            # submit_obs_command still owns _lock while its accepted command is
            # inserted, so stop() cannot close admission in the middle.
            self.assertFalse(service._stopping)
            self.assertFalse(stop_done.is_set())

            blocker.release_first_put.set()
            self.assertTrue(request_done.wait(1.0))
            submitter.join(1.0)
            stopper.join(2.0)
            self.assertFalse(stopper.is_alive())
            self.assertTrue(stop_result["value"], stop_result["value"].diagnostic_summary())
        finally:
            blocker.release_first_put.set()
            service.stop()

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

    def test_completed_layout_apply_stops_before_replacement_runtime_starts(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine_a = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher_a = CommandDispatcher()
        service_a = RoutingService(
            engine_a,
            dispatcher_a,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        collector = OBSResultCollector()
        service_a.on_event = collector.callback
        service_a.start()
        service_b = None
        try:
            request_id = service_a.request_layout("apply", "Test A")
            command_result = collector.wait(request_id)
            self.assertTrue(command_result.success, command_result.error)
            self.assertEqual(
                dispatcher_a.layout_threads,
                [("Test A", False, "SSR-Router")],
            )

            shutdown = service_a.stop(timeout=1.0)
            self.assertTrue(shutdown, shutdown.diagnostic_summary())
            self.assertIsNotNone(service_a._thread)
            self.assertFalse(service_a._thread.is_alive())

            engine_b = StateRouterEngine(RuleSet([]), debounce_ms=0)
            service_b = RoutingService(
                engine_b,
                CommandDispatcher(),
                poll_ms=20,
                provider=FakeProvider(app),
            )
            # Runtime B is started only after A has proved fully stopped.
            service_b.start()
            self.assertTrue(service_b._thread.is_alive())
            self.assertFalse(service_a._thread.is_alive())
        finally:
            service_a.stop()
            if service_b is not None:
                self.assertTrue(service_b.stop())

    def test_shutdown_interrupts_cooperative_background_reconciliation(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        engine.set_manual_override(state)
        dispatcher = CooperativeBackgroundDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        service.start()
        try:
            self.assertTrue(dispatcher.reconcile_entered.wait(1.0))

            started = time.monotonic()
            shutdown = service.stop(timeout=0.5)
            elapsed = time.monotonic() - started

            self.assertTrue(shutdown, shutdown.diagnostic_summary())
            self.assertTrue(dispatcher.reconcile_exited.is_set())
            self.assertFalse(service._thread.is_alive())
            self.assertEqual(shutdown.pending_commands, 0)
            self.assertLess(elapsed, 0.5)
        finally:
            service.stop()

    def test_stop_timeout_reports_real_dispatch_quiescence(self):
        app = ForegroundApp(1, 1, "game.exe")
        state = StreamState(game="Game")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        engine.set_manual_override(state)
        dispatcher = NonCooperativeBackgroundDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        service.start()
        try:
            self.assertTrue(dispatcher.reconcile_entered.wait(1.0))

            first = service.stop(timeout=0.05)

            self.assertFalse(first)
            self.assertFalse(first.worker_stopped)
            self.assertTrue(first.dispatch_quiescent)
            self.assertIn("obs_dispatch=quiescent", first.diagnostic_summary())
        finally:
            dispatcher.release_reconcile.set()
            self.assertTrue(service.stop(timeout=1.0))

    def test_stop_waits_for_inflight_layout_command_then_stops_cleanly(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = BlockingLayoutDispatcher()
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
        )
        collector = OBSResultCollector()
        service.on_event = collector.callback
        service.start()
        stopped = {}
        try:
            request_id = service.request_layout("apply", "Test A")
            self.assertTrue(dispatcher.layout_entered.wait(1.0))

            def stop_service():
                stopped["value"] = service.stop(timeout=1.0)

            stopper = threading.Thread(target=stop_service)
            stopper.start()
            time.sleep(0.05)
            self.assertTrue(stopper.is_alive())

            # Non-cooperative OBS work already in flight is allowed to finish;
            # the replacement runtime remains forbidden until it does.
            dispatcher.release_layout.set()
            stopper.join(1.0)
            self.assertFalse(stopper.is_alive())
            self.assertTrue(stopped["value"], stopped["value"].diagnostic_summary())
            self.assertFalse(service._thread.is_alive())

            command_result = collector.wait(request_id)
            self.assertTrue(command_result.success, command_result.error)
        finally:
            dispatcher.release_layout.set()
            service.stop()

    def test_cooperative_layout_shutdown_unwinds_before_activation_cleanup(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = CooperativeLayoutDispatcher()
        scheduler = ActiveShutdownScheduler()
        controller = ReentrancyDetectingController(dispatcher)
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        collector = OBSResultCollector()
        service.on_event = collector.callback
        service.start()
        try:
            request_id = service.request_layout("apply", "Test A")
            self.assertTrue(dispatcher.layout_entered.wait(1.0))

            shutdown = service.stop(timeout=1.0)
            self.assertTrue(shutdown, shutdown.diagnostic_summary())
            self.assertFalse(service._thread.is_alive())

            command_result = collector.wait(request_id)
            self.assertFalse(command_result.success)
            self.assertIn("Arrêt du runtime demandé", command_result.error)
            self.assertFalse(controller.reconcile_during_layout)
            self.assertFalse(controller.apply_during_layout)
            self.assertEqual(len(controller.events), 1)
            self.assertEqual(controller.events[0].kind, "hide")
        finally:
            service.stop()

    def test_shutdown_hides_scheduler_owned_visible_activation(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        scheduler = ActiveShutdownScheduler()
        controller = FakeActivationController()
        service = RoutingService(
            engine,
            FakeDispatcher(),
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        service.start()
        try:
            shutdown = service.stop(timeout=1.0)
            self.assertTrue(shutdown, shutdown.diagnostic_summary())
            self.assertEqual(scheduler.reset_all_calls, 1)
            self.assertEqual(len(controller.events), 1)
            self.assertEqual(controller.events[0].kind, "hide")
            self.assertEqual(controller.events[0].source, "Cloud")
            self.assertEqual(controller.reconcile_calls, 0)
        finally:
            service.stop()

    def test_shutdown_does_not_run_full_activation_reconcile(self):
        app = ForegroundApp(1, 1, "terminal.exe")
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        dispatcher = FakeDispatcher()
        scheduler = FakeActivationScheduler()
        controller = SlowReconcileController(delay=0.4)
        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=FakeProvider(app),
            activation_scheduler=scheduler,
            activation_controller=controller,
        )
        service.start()
        try:
            # The old shutdown path called controller.reconcile() here. That
            # operation can hide every configured target with synchronous OBS
            # requests and could outlive the whole stop budget.
            shutdown = service.stop(timeout=0.2)
            self.assertTrue(shutdown, shutdown.diagnostic_summary())
            self.assertEqual(controller.reconcile_calls, 0)
            self.assertFalse(service._thread.is_alive())
        finally:
            service.stop()

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

    def test_explain_decision_is_read_only_and_matches_router_resolution(self):
        app = ForegroundApp(1, 1, "game.exe", window_title="Gameplay")
        wanted = StreamState(game="Game")
        engine = StateRouterEngine(
            RuleSet([AppRule("Game", wanted, exe="game.exe")]),
            debounce_ms=0,
        )
        dispatcher = DiagnosticDispatcher()
        service = RoutingService(engine, dispatcher, provider=FakeProvider(app))
        service._last_app = app
        before = dispatcher.client.request_count

        explanation = service.explain_decision()

        self.assertEqual(dispatcher.client.request_count, before)
        self.assertEqual(explanation["routing"]["kind"], "match")
        self.assertEqual(explanation["routing"]["rule_name"], "Game")
        self.assertEqual(explanation["routing"]["effective_state"]["Game"], "Game")
        self.assertEqual(explanation["obs_plan"]["domains"][0]["status"], "planned")

    def test_explain_ignore_preserves_current_state_without_obs_plan(self):
        app = ForegroundApp(1, 1, "launcher.exe")
        engine = StateRouterEngine(
            RuleSet([AppRule("Launcher", priority=100, exe="launcher.exe", behavior=ResolutionKind.IGNORE)]),
            debounce_ms=0,
        )
        dispatcher = DiagnosticDispatcher()
        service = RoutingService(engine, dispatcher, provider=FakeProvider(app))
        service._last_app = app
        before = dispatcher.client.request_count

        explanation = service.explain_decision()

        self.assertEqual(dispatcher.client.request_count, before)
        self.assertEqual(explanation["routing"]["kind"], "ignore")
        self.assertEqual(explanation["obs_plan"]["domains"], [])
        self.assertIn("IGNORE", explanation["obs_plan"]["reason"])

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
            self.assertEqual(status["domain_details"][0]["domain"], "game")
            self.assertEqual(status["domain_details"][0]["message"], "test")
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
