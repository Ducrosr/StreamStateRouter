from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
