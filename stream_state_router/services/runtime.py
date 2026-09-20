from __future__ import annotations

import copy
import logging
import queue
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Callable, Mapping

from ..activation import (
    ActivationBlocked,
    ActivationCollectionChanged,
    ActivationEvent,
    ActivationScheduler,
    ActivationTargetMissing,
    ActivationVisibilityUncertain,
    OBSActivationController,
    TriggerPolicyConfig,
    TriggerTargetIdentity,
)
from ..obs.dispatcher import DispatchResult, OBSDispatcher
from ..router.engine import StateChange, StateRouterEngine
from ..router.foreground import WindowsForegroundProvider
from ..router.models import ForegroundApp, StreamState


class _RuntimeShutdownRequested(BaseException):
    """Internal cooperative-cancellation signal for the runtime worker."""


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: str
    message: str
    request_id: str = ""
    payload: object | None = None
    success: bool = True


@dataclass(frozen=True, slots=True)
class ActivationCommandResult:
    request_id: str
    action: str
    policy: str
    success: bool
    result: object | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _ActivationCommand:
    request_id: str
    generation: int
    action: str
    policy: str = ""
    target_identity: TriggerTargetIdentity | None = None
    target_source: str | None = None
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OBSCommandResult:
    request_id: str
    action: str
    success: bool
    result: object | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class RoutingDecisionStatus:
    decision_id: str
    origin: str
    generation: int
    config_revision: str
    rule_name: str
    requested_domains: tuple[str, ...]
    applied_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    failed_domains: tuple[str, ...]
    pending_domains: tuple[str, ...]
    domain_details: tuple[dict[str, str], ...]
    duration_ms: float
    obs_requests: int
    success: bool
    message: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "origin": self.origin,
            "generation": self.generation,
            "config_revision": self.config_revision,
            "rule_name": self.rule_name,
            "requested_domains": list(self.requested_domains),
            "applied_domains": list(self.applied_domains),
            "blocked_domains": list(self.blocked_domains),
            "failed_domains": list(self.failed_domains),
            "pending_domains": list(self.pending_domains),
            "domain_details": [dict(item) for item in self.domain_details],
            "duration_ms": round(float(self.duration_ms), 3),
            "obs_requests": int(self.obs_requests),
            "success": bool(self.success),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class _OBSCommand:
    request_id: str
    generation: int
    action: str
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _PendingDispatch:
    deadline: float
    generation: int
    change: StateChange
    decision_id: str
    origin: str


@dataclass(frozen=True, slots=True)
class RuntimeShutdownResult:
    worker_stopped: bool
    dispatch_quiescent: bool
    cleanup_complete: bool
    pending_cleanup: tuple[dict[str, object], ...] = ()
    elapsed_ms: float = 0.0
    pending_commands: int = 0
    shutdown_phase: str = ""
    active_operation: str = ""
    worker_phase: str = ""

    @property
    def success(self) -> bool:
        return self.worker_stopped and self.dispatch_quiescent

    def __bool__(self) -> bool:
        return self.success

    def diagnostic_summary(self) -> str:
        activation_pending = sum(
            1
            for item in self.pending_cleanup
            if str(item.get("kind") or "activation_hide").casefold() != "layout_fade"
        )
        fade_pending = sum(
            1
            for item in self.pending_cleanup
            if str(item.get("kind") or "").casefold() == "layout_fade"
        )
        return (
            f"routing_thread={'stopped' if self.worker_stopped else 'alive'}; "
            f"obs_dispatch={'quiescent' if self.dispatch_quiescent else 'active'}; "
            f"cleanup={'complete' if self.cleanup_complete else f'pending({len(self.pending_cleanup)})'}; "
            f"activation_pending={activation_pending}; "
            f"fade_pending={fade_pending}; "
            f"pending_commands={self.pending_commands}; "
            f"phase={self.shutdown_phase or 'unknown'}; "
            f"active_operation={self.active_operation or 'none'}; "
            f"worker_phase={self.worker_phase or 'unknown'}; "
            f"elapsed_ms={self.elapsed_ms:.1f}"
        )


class RoutingService:
    """Background foreground observer + state router + OBS dispatcher."""

    def __init__(
        self,
        engine: StateRouterEngine,
        dispatcher: OBSDispatcher,
        *,
        poll_ms: int = 50,
        provider=None,
        logger: logging.Logger | None = None,
        obs_probe_seconds: float = 2.0,
        state_reconcile_seconds: float = 0.5,
        activation_policies: Mapping[str, TriggerPolicyConfig] | None = None,
        activation_scheduler: ActivationScheduler | None = None,
        activation_controller: OBSActivationController | None = None,
        pending_activation_cleanup=(),
        pending_cleanup=(),
        config_revision: str = "",
    ) -> None:
        self.engine = engine
        self.dispatcher = dispatcher
        self.config_revision = str(config_revision or "")
        self.poll_seconds = max(0.02, poll_ms / 1000.0)
        self.provider = provider or WindowsForegroundProvider()
        self.logger = logger or logging.getLogger("stream_state_router")
        self.obs_probe_seconds = max(0.5, float(obs_probe_seconds))
        self.state_reconcile_seconds = max(0.1, float(state_reconcile_seconds))
        policies = dict(activation_policies or {})
        transferred_cleanup = tuple(pending_activation_cleanup or ()) + tuple(pending_cleanup or ())
        activation_cleanup: list[Mapping[str, object]] = []
        fade_cleanup: list[Mapping[str, object]] = []
        for item in transferred_cleanup:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "activation_hide").strip().casefold()
            if kind == "layout_fade":
                fade_cleanup.append(item)
            else:
                # Backward compatibility: pre-v2 cleanup markers did not carry
                # a kind and always represented activation hides.
                activation_cleanup.append(item)

        self.activation_scheduler = activation_scheduler
        self.activation_controller = activation_controller
        if self.activation_scheduler is None and policies:
            self.activation_scheduler = ActivationScheduler(policies)
        if self.activation_controller is None and (policies or activation_cleanup):
            self.activation_controller = OBSActivationController(dispatcher, policies)
        if self.activation_controller is not None and activation_cleanup:
            self.activation_controller.import_pending_hides(activation_cleanup)

        layout_manager = getattr(self.dispatcher, "layout_manager", None)
        if (
            layout_manager is not None
            and fade_cleanup
            and hasattr(layout_manager, "import_pending_fade_cleanup")
        ):
            layout_manager.import_pending_fade_cleanup(fade_cleanup)

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._paused = False
        self._last_app: ForegroundApp | None = None
        self._dispatch_generation = 0
        self._last_obs_probe = 0.0
        self._last_obs_connected: bool | None = None
        self._last_state_reconcile = 0.0
        self._activation_diagnostics: deque[tuple[float, str, str, str]] = deque(maxlen=250)
        self._routing_diagnostics: deque[RoutingDecisionStatus] = deque(maxlen=100)
        self._last_routing_status: RoutingDecisionStatus | None = None
        self._activation_eligibility_cache: dict[str, tuple[bool, str]] = {}
        self._activation_cleanup_cache: dict[str, int] = {}
        self._runtime_commands: queue.Queue[_ActivationCommand | _OBSCommand] = queue.Queue()
        self._command_generation = 0
        self._accept_activation_commands = False
        self._accept_obs_commands = False
        self._pending_dispatch: _PendingDispatch | None = None
        self._runtime_operational = False
        self._stopping = False
        self._shutdown_complete = threading.Event()
        self._shutdown_phase = "idle"
        self._active_operation = ""
        self._worker_phase = "idle"
        self._shutdown_cleanup_active = False
        self._shutdown_result = RuntimeShutdownResult(True, True, True, ())
        self._command_status: dict[str, dict[str, object]] = {}
        self._simulation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="SSR-Activation-Sim",
        )

        self.on_foreground: Callable[[ForegroundApp | None], None] | None = None
        self.on_change: Callable[[StateChange], None] | None = None
        self.on_dispatch: Callable[[DispatchResult], None] | None = None
        self.on_event: Callable[[RuntimeEvent], None] | None = None
        if hasattr(self.dispatcher, "set_cooperative_yield"):
            self.dispatcher.set_cooperative_yield(self._cooperative_obs_yield)
        elif layout_manager is not None and hasattr(layout_manager, "set_cooperative_yield"):
            layout_manager.set_cooperative_yield(self._cooperative_obs_yield)
        if self.activation_controller is not None and hasattr(
            self.activation_controller, "set_cooperative_yield"
        ):
            self.activation_controller.set_cooperative_yield(self._cooperative_obs_yield)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def last_app(self) -> ForegroundApp | None:
        with self._lock:
            return self._last_app

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            self._stop.clear()
            self._shutdown_complete.clear()
            self._stopping = False
            self._shutdown_phase = "running"
            self._active_operation = ""
            self._worker_phase = "starting"
            self._shutdown_cleanup_active = False
            self._runtime_operational = True
            self._accept_activation_commands = True
            self._accept_obs_commands = True
        self._thread = threading.Thread(target=self._run, name="SSR-Router", daemon=True)
        self._thread.start()

    @property
    def shutdown_result(self) -> RuntimeShutdownResult:
        return self._shutdown_result

    def pending_cleanup_snapshot(self) -> tuple[dict[str, object], ...]:
        items: list[dict[str, object]] = []
        controller = self.activation_controller
        if controller is not None:
            exporter = getattr(controller, "export_pending_hides", None)
            if callable(exporter):
                items.extend(dict(item) for item in exporter())

        layout_manager = getattr(self.dispatcher, "layout_manager", None)
        if layout_manager is not None:
            exporter = getattr(layout_manager, "export_pending_fade_cleanup", None)
            if callable(exporter):
                items.extend(dict(item) for item in exporter())
        return tuple(items)

    def _build_shutdown_result(
        self,
        *,
        worker_stopped: bool,
        dispatch_quiescent: bool,
        pending_cleanup: tuple[dict[str, object], ...],
        started_at: float,
    ) -> RuntimeShutdownResult:
        with self._lock:
            phase = self._shutdown_phase
            active_operation = self._active_operation
        worker_phase = self._worker_phase
        result = RuntimeShutdownResult(
            worker_stopped=worker_stopped,
            dispatch_quiescent=dispatch_quiescent,
            cleanup_complete=not pending_cleanup,
            pending_cleanup=pending_cleanup,
            elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
            pending_commands=max(0, self._runtime_commands.qsize()),
            shutdown_phase=phase,
            active_operation=active_operation,
            worker_phase=worker_phase,
        )
        self._shutdown_result = result
        log = self.logger.info if result else self.logger.error
        log("runtime_stop: %s", result.diagnostic_summary())
        return result

    def stop(self, timeout: float = 5.0) -> RuntimeShutdownResult:
        started_at = time.monotonic()
        thread = self._thread
        if thread is None or not thread.is_alive():
            with self._lock:
                self._accept_activation_commands = False
                self._accept_obs_commands = False
                self._runtime_operational = False
                self._stopping = True
                self._shutdown_phase = "already_stopped"
            quiescent = self._wait_for_dispatch_quiescence(timeout)
            pending = self.pending_cleanup_snapshot()
            return self._build_shutdown_result(
                worker_stopped=True,
                dispatch_quiescent=quiescent,
                pending_cleanup=pending,
                started_at=started_at,
            )

        with self._lock:
            if not self._stopping:
                self._accept_activation_commands = False
                self._accept_obs_commands = False
                self._runtime_operational = False
                self._stopping = True
                self._shutdown_phase = "requested"
                self._dispatch_generation += 1
                self._pending_dispatch = None
                self._command_generation += 1
                generation = self._command_generation
                command = _ActivationCommand(
                    request_id=f"shutdown-{uuid.uuid4().hex}",
                    generation=generation,
                    action="shutdown",
                )
                self._runtime_commands.put(command)
        self._wake.set()

        deadline = time.monotonic() + max(0.1, float(timeout))
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_stopped = not thread.is_alive()
        if not worker_stopped:
            self._log_worker_stack(thread)
            self.logger.error(
                "Routing service did not stop within %.1f s; refusing replacement runtime",
                timeout,
            )
            # The worker being alive is already sufficient to reject a replacement.
            # Probe the dispatch lock only for an accurate diagnostic instead of
            # reporting it active unconditionally.
            quiescent = self._wait_for_dispatch_quiescence(0.0)
            pending = self.pending_cleanup_snapshot()
            return self._build_shutdown_result(
                worker_stopped=False,
                dispatch_quiescent=quiescent,
                pending_cleanup=pending,
                started_at=started_at,
            )

        remaining = max(0.0, deadline - time.monotonic())
        quiescent = self._wait_for_dispatch_quiescence(remaining)
        if not quiescent:
            self.logger.error(
                "An OBS dispatch from the previous runtime is still active; "
                "refusing replacement runtime"
            )
        pending = self.pending_cleanup_snapshot()
        return self._build_shutdown_result(
            worker_stopped=True,
            dispatch_quiescent=quiescent,
            pending_cleanup=pending,
            started_at=started_at,
        )

    def _log_worker_stack(self, thread: threading.Thread) -> None:
        """Capture the worker stack without requiring cooperation from it."""
        ident = thread.ident
        if ident is None:
            self.logger.error("SSR-Router stack unavailable: thread has no ident")
            return
        frame = sys._current_frames().get(ident)
        if frame is None:
            self.logger.error("SSR-Router stack unavailable: no current frame")
            return
        try:
            stack = "".join(traceback.format_stack(frame))
        except Exception as exc:
            self.logger.error("SSR-Router stack capture failed: %s", exc)
            return
        self.logger.error(
            "SSR-Router stack at stop timeout (phase=%s, active_operation=%s):\n%s",
            self._worker_phase or "unknown",
            self._active_operation or "none",
            stack,
        )

    def pause(self, paused: bool = True) -> None:
        with self._lock:
            self._paused = bool(paused)
        self._wake.set()
        self._emit(RuntimeEvent("pause", "Routage suspendu" if paused else "Routage repris"))

    def set_manual_override(
        self,
        state: StreamState,
        *,
        duration_seconds: float | None = None,
    ) -> StateChange | None:
        with self._lock:
            change = self.engine.set_manual_override(state, duration_seconds=duration_seconds)
        if change:
            self._apply_change(change)
        return change

    def clear_manual_override(self) -> StateChange | None:
        with self._lock:
            app = self._last_app
            change = self.engine.clear_manual_override(app)
        if change:
            self._apply_change(change)
        return change

    def force_reapply(self) -> DispatchResult | None:
        with self._dispatch_lock:
            with self._lock:
                if self._stopping:
                    return None
                state = self.engine.current_state
            if state is None:
                return None
            result = self.dispatcher.dispatch_state(state, force=True)
        if self.on_dispatch:
            self.on_dispatch(result)
        return result

    def activation_status(self, policy_name: str) -> dict[str, object]:
        scheduler = self.activation_scheduler
        controller = self.activation_controller
        if scheduler is None or controller is None or policy_name not in scheduler.policies:
            return {"available": False, "phase": "idle", "diagnostics": []}
        now = time.monotonic()
        with self._lock:
            state = scheduler.state(policy_name)
            eligible, eligibility_reason = self._activation_eligibility_cache.get(
                policy_name,
                (False, "en attente du prochain cycle runtime"),
            )
            operational = self._runtime_operational and not self._stopping
            cleanup_pending = self._activation_cleanup_cache.get(policy_name, 0)
        diagnostics = self.activation_diagnostics(policy_name, limit=16)
        return {
            "available": True,
            "phase": state.phase.value,
            "active_source": state.active_source,
            "eligible": eligible,
            "eligibility_reason": eligibility_reason,
            "operational": operational,
            "cleanup_pending": cleanup_pending,
            "config_revision": self.config_revision,
            "next_roll_seconds": (
                max(0.0, state.next_roll_at - now) if state.next_roll_at is not None else None
            ),
            "visible_seconds": (
                max(0.0, state.visible_until - now) if state.visible_until is not None else None
            ),
            "cooldown_seconds": (
                max(0.0, state.cooldown_until - now) if state.cooldown_until is not None else None
            ),
            "last_event": diagnostics[-1] if diagnostics else "",
            "diagnostics": diagnostics,
        }

    def activation_test_roll(self, policy_name: str) -> str:
        return self.submit_activation_command("test_roll", policy_name)

    def activation_trigger_now(
        self,
        policy_name: str,
        *,
        target_identity: TriggerTargetIdentity | None = None,
        target_source: str | None = None,
        ignore_cooldown: bool = False,
    ) -> str:
        return self.submit_activation_command(
            "trigger",
            policy_name,
            target_identity=target_identity,
            target_source=target_source,
            options={"ignore_cooldown": bool(ignore_cooldown)},
        )

    def activation_stop(self, policy_name: str) -> str:
        return self.submit_activation_command("stop", policy_name)

    def activation_reset_cooldown(self, policy_name: str) -> str:
        return self.submit_activation_command("reset_cooldown", policy_name)

    def activation_simulate(
        self,
        policy_name: str,
        *,
        trials: int = 1000,
        seed: int = 12345,
    ) -> str:
        return self.submit_activation_command(
            "simulate",
            policy_name,
            options={"trials": int(trials), "seed": int(seed)},
        )

    def activation_reset_all(self) -> str:
        return self.submit_activation_command("reset_all", "*")

    def submit_activation_command(
        self,
        action: str,
        policy_name: str,
        *,
        target_identity: TriggerTargetIdentity | None = None,
        target_source: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        with self._lock:
            thread = self._thread
            if (
                not self._accept_activation_commands
                or self._stopping
                or thread is None
                or not thread.is_alive()
            ):
                raise RuntimeError("Runtime d'activation indisponible ou en arrêt")
            generation = self._command_generation
            # Admission and queue insertion are one atomic lifecycle decision:
            # stop() cannot close admission/increment generation between them.
            self._set_command_status(request_id, action=str(action), status="accepted")
            self._runtime_commands.put(
                _ActivationCommand(
                    request_id=request_id,
                    generation=generation,
                    action=str(action),
                    policy=str(policy_name),
                    target_identity=target_identity,
                    target_source=target_source,
                    options=dict(options or {}),
                )
            )
        self._wake.set()
        return request_id

    def submit_obs_command(
        self,
        action: str,
        *,
        options: Mapping[str, object] | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        with self._lock:
            thread = self._thread
            if (
                not self._accept_obs_commands
                or self._stopping
                or thread is None
                or not thread.is_alive()
            ):
                raise RuntimeError("Runtime OBS indisponible ou en arrêt")
            generation = self._command_generation
            self._set_command_status(request_id, action=str(action), status="accepted")
            self._runtime_commands.put(
                _OBSCommand(
                    request_id=request_id,
                    generation=generation,
                    action=str(action),
                    options=dict(options or {}),
                )
            )
        self._wake.set()
        return request_id

    def _set_command_status(
        self,
        request_id: str,
        *,
        action: str,
        status: str,
        error: str = "",
        result: object | None = None,
    ) -> None:
        row = {
            "request_id": str(request_id),
            "action": str(action),
            "status": str(status),
            "error": str(error),
            "updated_at": time.time(),
        }
        if result is not None:
            if is_dataclass(result):
                row["result"] = asdict(result)
            elif isinstance(result, Mapping):
                row["result"] = dict(result)
            elif hasattr(result, "__dict__"):
                row["result"] = dict(result.__dict__)
            else:
                row["result"] = str(result)
        with self._lock:
            self._command_status[str(request_id)] = row
            while len(self._command_status) > 200:
                self._command_status.pop(next(iter(self._command_status)))

    def command_status(self, request_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._command_status.get(str(request_id))
            return dict(row) if row is not None else None

    def request_force_reapply(self) -> str:
        return self.submit_obs_command("reapply")

    def request_profile(self, domain: str, profile_name: str) -> str:
        return self.submit_obs_command(
            "profile",
            options={"domain": str(domain), "name": str(profile_name)},
        )

    def request_layout(self, action: str, profile_name: str = "") -> str:
        options: dict[str, object] = {}
        if profile_name:
            options["name"] = str(profile_name)
        return self.submit_obs_command(f"layout.{action}", options=options)

    def explain_decision(
        self,
        app: ForegroundApp | None = None,
    ) -> dict[str, object]:
        """Explain routing and the resulting OBS plan without performing OBS I/O."""
        with self._lock:
            target_app = self._last_app if app is None else app
            paused = self._paused
        if hasattr(self.dispatcher, "cached_obs_context"):
            context = self.dispatcher.cached_obs_context()
        else:
            context = {}
        routing = self.engine.explain(target_app, context=context)
        kind = str(routing.get("kind") or "")
        effective_raw = routing.get("effective_state")
        if kind == "ignore":
            obs_plan: dict[str, object] = {
                "state": effective_raw,
                "context": dict(context),
                "domains": [],
                "reason": "IGNORE conserve l'état logique courant ; aucune nouvelle mutation OBS n'est demandée.",
            }
        elif isinstance(effective_raw, Mapping):
            state = StreamState.from_mapping(effective_raw)
            if hasattr(self.dispatcher, "plan_state"):
                obs_plan = self.dispatcher.plan_state(state, context=context)
            else:
                obs_plan = {"state": state.as_variables(), "context": dict(context), "domains": []}
        else:
            obs_plan = {"state": None, "context": dict(context), "domains": []}
        return {
            "config_revision": self.config_revision,
            "paused": paused,
            "foreground": {
                "exe": target_app.exe_name if target_app else "",
                "path": target_app.process_path if target_app else "",
                "title": target_app.window_title if target_app else "",
            },
            "routing": routing,
            "obs_plan": obs_plan,
        }

    def routing_status(self) -> dict[str, object]:
        """Return the latest routing diagnostic snapshot without OBS I/O."""
        with self._lock:
            status = self._last_routing_status
        return status.as_mapping() if status is not None else {}

    def routing_diagnostics(self, *, limit: int = 20) -> list[dict[str, object]]:
        """Return recent routing outcomes; snapshots contain no configuration secrets."""
        with self._lock:
            rows = list(self._routing_diagnostics)[-max(1, int(limit)) :]
        return [row.as_mapping() for row in rows]

    def _obs_request_count(self) -> int:
        client = getattr(self.dispatcher, "client", None)
        value = getattr(client, "request_count", 0) if client is not None else 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _publish_routing_result(
        self,
        *,
        decision_id: str,
        origin: str,
        generation: int,
        rule_name: str,
        state: StreamState,
        result: DispatchResult | None,
        started_at: float,
        obs_requests_before: int,
        error: str = "",
    ) -> RoutingDecisionStatus:
        statuses = tuple(getattr(result, "domain_statuses", ()) or ()) if result is not None else ()
        requested = tuple(getattr(result, "changed_domains", ()) or ()) if result is not None else ()
        if not requested and hasattr(self.dispatcher, "pending_domains"):
            try:
                requested = tuple(self.dispatcher.pending_domains(state))
            except Exception:
                requested = ()

        applied = tuple(
            str(getattr(item, "domain", ""))
            for item in statuses
            if str(getattr(item, "status", "")) == "applied"
        )
        blocked = tuple(
            str(getattr(item, "domain", ""))
            for item in statuses
            if str(getattr(item, "status", "")) == "blocked"
        )
        failed = tuple(
            str(getattr(item, "domain", ""))
            for item in statuses
            if str(getattr(item, "status", "")) in {"failed", "missing", "partial"}
        )
        pending: tuple[str, ...] = ()
        if hasattr(self.dispatcher, "pending_domains"):
            try:
                pending = tuple(self.dispatcher.pending_domains(state))
            except Exception:
                pending = ()

        if result is not None and not statuses and not error:
            # Compatibility for lightweight dispatchers used by integrations/tests.
            applied = tuple(requested)

        if error and not failed:
            failed = tuple(requested or pending)
        success = not error and not blocked and not failed and not pending
        if success:
            message = "Application OBS complète"
        else:
            parts = []
            if blocked:
                parts.append("bloqué=" + ",".join(blocked))
            if failed:
                parts.append("échec=" + ",".join(failed))
            if pending:
                parts.append("en attente=" + ",".join(pending))
            if error:
                parts.append(error)
            message = "Application OBS incomplète" + (": " + " · ".join(parts) if parts else "")

        status = RoutingDecisionStatus(
            decision_id=str(decision_id),
            origin=str(origin),
            generation=int(generation),
            config_revision=self.config_revision,
            rule_name=str(rule_name),
            requested_domains=tuple(dict.fromkeys(requested)),
            applied_domains=tuple(dict.fromkeys(applied)),
            blocked_domains=tuple(dict.fromkeys(blocked)),
            failed_domains=tuple(dict.fromkeys(failed)),
            pending_domains=tuple(dict.fromkeys(pending)),
            domain_details=tuple(
                {
                    "domain": str(getattr(item, "domain", "")),
                    "status": str(getattr(item, "status", "")),
                    "desired_profile": str(getattr(item, "desired_profile", "")),
                    "applied_profile": str(getattr(item, "applied_profile", "")),
                    "message": str(getattr(item, "message", "")),
                }
                for item in statuses
            ),
            duration_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
            obs_requests=max(0, self._obs_request_count() - obs_requests_before),
            success=success,
            message=message,
        )
        with self._lock:
            self._last_routing_status = status
            self._routing_diagnostics.append(status)
        self._emit(
            RuntimeEvent(
                "routing_result",
                status.message,
                request_id=status.decision_id,
                payload=status.as_mapping(),
                success=status.success,
            )
        )
        return status

    def activation_diagnostics(
        self,
        policy_name: str | None = None,
        *,
        limit: int = 20,
    ) -> list[str]:
        wanted = str(policy_name or "")
        with self._lock:
            rows = [
                row
                for row in self._activation_diagnostics
                if not wanted or row[1] in {wanted, "*"}
            ]
        rows = rows[-max(1, int(limit)) :]
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(wall_time))} [{kind}] {message}"
            for wall_time, _policy, kind, message in rows
        ]

    def _run(self) -> None:
        self.logger.info("Routing service started")
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self._worker_phase = "commands"
                    if self._drain_runtime_commands():
                        break
                    self._worker_phase = "probe"
                    self._probe_obs_if_due()
                    with self._lock:
                        if self._stopping:
                            continue
                    self._worker_phase = "foreground"
                    app = self.provider.get()
                    with self._lock:
                        changed_app = app != self._last_app
                        self._last_app = app
                        paused = self._paused
                    if changed_app:
                        self.logger.info(
                            "Foreground -> %s | %s",
                            app.exe_name if app else "<none>",
                            app.window_title if app else "",
                        )
                        if self.on_foreground:
                            self.on_foreground(app)
                    if not paused:
                        self._worker_phase = "observe"
                        with self._lock:
                            change = self.engine.observe(app)
                        if change:
                            self._apply_change(change)
                    self._worker_phase = "due_dispatch"
                    self._process_due_dispatch()
                    with self._lock:
                        if self._stopping:
                            continue
                    self._worker_phase = "reconcile"
                    self._reconcile_desired_state_if_due()
                    with self._lock:
                        if self._stopping:
                            continue
                    self._worker_phase = "activation_tick"
                    self._tick_activation(paused=paused)
                    self._worker_phase = "idle"
                except _RuntimeShutdownRequested:
                    if self._stopping:
                        self._worker_phase = "shutdown_cleanup"
                        self._perform_orderly_shutdown()
                        break
                    raise
                except Exception as exc:
                    self.logger.exception("Routing loop error")
                    self._emit(RuntimeEvent("error", str(exc)))

                elapsed = time.monotonic() - started
                wait_for = max(0.0, self.poll_seconds - elapsed)
                with self._lock:
                    pending = self._pending_dispatch
                if pending is not None:
                    wait_for = min(wait_for, max(0.0, pending.deadline - time.monotonic()))
                self._wake.wait(wait_for)
                self._wake.clear()
        finally:
            with self._lock:
                self._runtime_operational = False
                self._accept_activation_commands = False
                self._accept_obs_commands = False
                self._shutdown_phase = "stopped"
                self._active_operation = ""
                self._worker_phase = "stopped"
            self._shutdown_complete.set()
            self.logger.info("Routing service stopped")

    def _probe_obs_if_due(self) -> None:
        client = getattr(self.dispatcher, "client", None)
        config = getattr(client, "config", None)
        if client is None or config is None or not bool(getattr(config, "enabled", False)):
            self._last_obs_connected = None
            return

        now = time.monotonic()
        if self._last_obs_probe and now - self._last_obs_probe < self.obs_probe_seconds:
            return
        self._last_obs_probe = now

        ok, message = client.probe()
        if ok:
            manager = getattr(self.dispatcher, "layout_manager", None)
            if self._last_obs_connected is not True:
                self.logger.info("OBS connection established: %s", message)
                if hasattr(self.dispatcher, "invalidate_applied_state"):
                    self.dispatcher.invalidate_applied_state()
                if manager is not None and hasattr(manager, "invalidate_session"):
                    manager.invalidate_session()
                self._last_state_reconcile = 0.0
                self._reconcile_activation("connexion OBS")
                self._emit(RuntimeEvent("obs_connected", message))
            if manager is not None and hasattr(manager, "retry_pending_fade_cleanup"):
                cleanup_warnings = manager.retry_pending_fade_cleanup()
                if cleanup_warnings:
                    detail = "; ".join(cleanup_warnings)
                    self.logger.warning("Layout fade cleanup pending: %s", detail)
                    self._emit(
                        RuntimeEvent(
                            "layout_cleanup_pending",
                            detail,
                            success=False,
                        )
                    )
                elif hasattr(manager, "pending_fade_cleanup") and not manager.pending_fade_cleanup():
                    # No event on the common clean path; this branch simply
                    # confirms that an earlier obligation has been discharged.
                    pass
            self._last_obs_connected = True
            return

        if self._last_obs_connected is not False:
            self.logger.warning("OBS connection unavailable: %s", message)
            self._emit(RuntimeEvent("obs_disconnected", message))
        self._last_obs_connected = False

    def _reconcile_desired_state_if_due(self) -> None:
        if self._last_obs_connected is False:
            return
        now = time.monotonic()
        if self._last_state_reconcile and now - self._last_state_reconcile < self.state_reconcile_seconds:
            return
        with self._lock:
            if self._stopping or self._pending_dispatch is not None:
                return
            state = self.engine.current_state
            generation = self._dispatch_generation
            rule_name = self.engine.current_rule
        if state is None or not hasattr(self.dispatcher, "pending_domains"):
            return
        pending = self.dispatcher.pending_domains(state)
        if not pending:
            return
        self._last_state_reconcile = now
        decision_id = f"reconcile-{uuid.uuid4().hex}"
        started_at = time.monotonic()
        obs_before = self._obs_request_count()
        try:
            result = self.dispatcher.dispatch_state(state)
        except Exception as exc:
            self.logger.error("OBS reconciliation failed: %s", exc)
            self._publish_routing_result(
                decision_id=decision_id,
                origin="reconcile",
                generation=generation,
                rule_name=rule_name,
                state=state,
                result=None,
                started_at=started_at,
                obs_requests_before=obs_before,
                error=str(exc),
            )
            self._emit(RuntimeEvent("obs_error", str(exc), request_id=decision_id))
            return
        if self.on_dispatch:
            self.on_dispatch(result)
        self._publish_routing_result(
            decision_id=decision_id,
            origin="reconcile",
            generation=generation,
            rule_name=rule_name,
            state=state,
            result=result,
            started_at=started_at,
            obs_requests_before=obs_before,
        )

    def _reconcile_activation(self, reason: str) -> bool:
        scheduler = self.activation_scheduler
        controller = self.activation_controller
        if controller is None:
            return True
        if scheduler is not None:
            with self._lock:
                scheduler.reset_all()
                self._activation_eligibility_cache.clear()
                self._activation_cleanup_cache.clear()
        try:
            warnings = controller.reconcile()
            for message in controller.retry_pending_hides(now=time.monotonic()):
                self._record_activation_diagnostic("*", "nettoyage", message)
        except Exception as exc:
            self.logger.warning("Activation reconciliation failed (%s): %s", reason, exc)
            self._record_activation_diagnostic(
                "*",
                "erreur",
                f"réconciliation impossible ({reason}) : {exc}",
            )
            self._emit(RuntimeEvent("activation_error", str(exc)))
            return False

        for warning in warnings:
            self.logger.warning("Activation reconcile: %s", warning)
            self._record_activation_diagnostic("*", "warning", warning)
        pending = controller.pending_hides()
        current_pending_provider = getattr(
            controller,
            "pending_hides_for_current_collection",
            None,
        )
        active_pending = (
            tuple(current_pending_provider())
            if callable(current_pending_provider)
            else tuple(pending)
        )
        cleanup_counts: dict[str, int] = {}
        for item in active_pending:
            cleanup_counts[item.policy] = cleanup_counts.get(item.policy, 0) + 1
        with self._lock:
            self._activation_cleanup_cache.clear()
            self._activation_cleanup_cache.update(cleanup_counts)
        if warnings or active_pending:
            message = (
                f"Nettoyage activation incomplet — {reason} "
                f"({len(active_pending)} masquage(s) actif(s) en attente)"
            )
            self._record_activation_diagnostic("*", "nettoyage", message)
            self._emit(RuntimeEvent("activation_cleanup_pending", message, success=False))
            return False
        suspended = max(0, len(pending) - len(active_pending))
        if suspended:
            self._record_activation_diagnostic(
                "*",
                "nettoyage",
                f"{suspended} obligation(s) contextualisée(s) suspendue(s) dans une autre Scene Collection",
            )

        message = f"Déclenchements réinitialisés — {reason}"
        self._record_activation_diagnostic("*", "fail-safe", message)
        self._emit(RuntimeEvent("activation_reconciled", message))
        return True

    def _tick_activation(self, *, paused: bool) -> None:
        scheduler = self.activation_scheduler
        controller = self.activation_controller
        if controller is None:
            return

        collection_changed = controller.scene_collection_changed()
        if collection_changed and scheduler is not None:
            self._reconcile_activation("changement de Scene Collection")
            return

        # Imported/leftover obligations are recovery work in their own right.
        # They must continue to progress even when the current configuration no
        # longer contains an activation scheduler/policy.
        now = time.monotonic()
        for message in controller.retry_pending_hides(now=now):
            self._record_activation_diagnostic("*", "nettoyage", message)

        if scheduler is None:
            return

        def eligibility(name: str, policy: TriggerPolicyConfig) -> bool:
            result = self._effective_activation_eligibility(name, policy)
            cleanup_count = len(controller.pending_hides(name))
            with self._lock:
                self._activation_eligibility_cache[name] = result
                self._activation_cleanup_cache[name] = cleanup_count
            return result[0]

        events = scheduler.tick(eligibility, now=now)
        for event in events:
            self._handle_activation_event(event, now=now)

    def _handle_activation_event(self, event: ActivationEvent, *, now: float) -> None:
        controller = self.activation_controller
        scheduler = self.activation_scheduler
        if controller is None or scheduler is None:
            return

        if event.kind == "roll":
            roll = float(event.roll or 0.0)
            chance = float(event.chance or 0.0)
            verdict = "succès" if roll < chance else "échec"
            self._record_activation_diagnostic(
                event.policy,
                "tirage",
                f"{roll * 100.0:.3f} % / seuil {chance * 100.0:.3f} % → {verdict}",
            )
            return
        if event.kind == "blocked":
            self._record_activation_diagnostic(
                event.policy,
                "bloqué",
                "tirage réussi mais aucune source participante éligible",
            )
            return
        if event.kind == "cooldown_started":
            self._record_activation_diagnostic(
                event.policy,
                "cooldown",
                f"démarré pour {event.cooldown_seconds or 0.0:.1f} s",
            )
            return
        if event.kind == "cooldown_complete":
            self._record_activation_diagnostic(
                event.policy,
                "cooldown",
                "terminé ; politique de nouveau éligible",
            )
            return
        if event.kind not in {"show", "hide"}:
            return

        try:
            applied_collection = controller.apply_event(event)
        except ActivationCollectionChanged as exc:
            self._record_activation_diagnostic(event.policy, "collection", str(exc))
            with self._lock:
                scheduler.reset_policy(event.policy, now=now)
            self._reconcile_activation("changement de Scene Collection détecté pendant activation")
            return
        except (
            ActivationVisibilityUncertain,
            ActivationTargetMissing,
            ActivationBlocked,
            RuntimeError,
        ) as exc:
            self.logger.warning(
                "Activation OBS failed [%s/%s]: %s",
                event.policy,
                event.source,
                exc,
            )
            self._record_activation_diagnostic(
                event.policy,
                "erreur",
                f"{event.kind} {event.source} : {exc}",
            )
            self._emit(RuntimeEvent("activation_error", f"{event.policy}/{event.source}: {exc}"))
            if event.kind == "show":
                with self._lock:
                    scheduler.reset_policy(event.policy, now=now)
            return

        if event.kind == "show":
            if applied_collection:
                setter = getattr(scheduler, "set_active_collection", None)
                if callable(setter):
                    with self._lock:
                        setter(event.policy, str(applied_collection))
            self._record_activation_diagnostic(
                event.policy,
                "déclenché",
                (
                    f"{event.source} visible {event.duration_seconds or 0.0:.1f} s "
                    f"({event.reason or 'runtime'})"
                ),
            )
            self._emit(
                RuntimeEvent(
                    "activation_show",
                    f"{event.policy}: {event.source} ({event.duration_seconds or 0.0:.1f} s)",
                )
            )
        else:
            self._record_activation_diagnostic(
                event.policy,
                "masqué",
                f"{event.source} ({event.reason or 'runtime'})",
            )
            self._emit(RuntimeEvent("activation_hide", f"{event.policy}: {event.source}"))

    def _activation_eligibility(
        self,
        policy_name: str,
        policy: TriggerPolicyConfig,
    ) -> tuple[bool, str]:
        return self._effective_activation_eligibility(policy_name, policy)

    def _record_activation_diagnostic(
        self,
        policy_name: str,
        kind: str,
        message: str,
    ) -> None:
        row = (time.time(), str(policy_name or "*"), str(kind), str(message))
        with self._lock:
            self._activation_diagnostics.append(row)
        self.logger.info(
            "Activation diagnostic [%s/%s] %s",
            row[1],
            row[2],
            row[3],
        )

    def _effective_activation_eligibility(
        self,
        policy_name: str,
        policy: TriggerPolicyConfig,
    ) -> tuple[bool, str]:
        controller = self.activation_controller
        if not policy.enabled:
            return False, "politique désactivée"
        with self._lock:
            if not self._runtime_operational or self._stopping:
                return False, "service runtime non opérationnel"
            if self._paused:
                return False, "routage suspendu"
        if controller is None:
            return False, "contrôleur OBS indisponible"
        blocked, reason = controller.policy_cleanup_status(policy_name)
        if blocked:
            return False, reason
        try:
            return controller.eligibility(policy_name, policy)
        except Exception as exc:
            return False, f"évaluation impossible : {exc}"

    def _drain_runtime_commands(self, *, allow_obs: bool = True) -> bool:
        deferred: list[_ActivationCommand | _OBSCommand] = []
        should_stop = False
        while True:
            try:
                command = self._runtime_commands.get_nowait()
            except queue.Empty:
                break

            with self._lock:
                current_generation = self._command_generation
            if command.generation != current_generation:
                if isinstance(command, _ActivationCommand):
                    self._emit_activation_result(
                        command,
                        success=False,
                        error="Commande annulée par arrêt/reconfiguration du runtime",
                    )
                else:
                    self._emit_obs_result(
                        command,
                        success=False,
                        error="Commande annulée par arrêt/reconfiguration du runtime",
                    )
                continue

            if isinstance(command, _OBSCommand):
                if not allow_obs:
                    deferred.append(command)
                    continue
                self._execute_obs_command(command)
                continue

            if command.action == "shutdown":
                if not allow_obs:
                    # A cooperative checkpoint may run while a layout command
                    # owns _dispatch_lock. Defer shutdown until that command has
                    # unwound so cleanup never re-enters OBS from inside an OBS
                    # mutation.
                    deferred.append(command)
                    should_stop = True
                    break
                self._perform_orderly_shutdown()
                should_stop = True
                break
            if command.action == "simulate":
                self._start_simulation(command)
                continue
            self._execute_activation_command(command)

        for command in deferred:
            self._runtime_commands.put(command)
        return should_stop

    def _cooperative_obs_yield(self) -> None:
        """Pure cancellation checkpoint used inside potentially long OBS work.

        It must never start probes, scheduler ticks, cleanup or other OBS I/O:
        doing so makes the checkpoint re-entrant and can recursively re-enter the
        operation that called it. Orderly cleanup is performed only by the outer
        runtime loop after the interrupted operation has unwound.
        """
        if self._thread is None or threading.current_thread() is not self._thread:
            return
        if self._shutdown_cleanup_active:
            return
        with self._lock:
            stopping = self._stopping
        if stopping or self._stop.is_set():
            raise _RuntimeShutdownRequested(
                "Arrêt du runtime demandé pendant une opération OBS"
            )

    def _execute_obs_command(self, command: _OBSCommand) -> None:
        with self._lock:
            self._active_operation = command.action
        try:
            with self._dispatch_lock:
                if command.action == "reapply":
                    with self._lock:
                        state = self.engine.current_state
                        rule_name = self.engine.current_rule
                        generation = self._dispatch_generation
                    if state is None:
                        result = None
                    else:
                        started_at = time.monotonic()
                        obs_before = self._obs_request_count()
                        result = self.dispatcher.dispatch_state(state, force=True)
                        if self.on_dispatch:
                            self.on_dispatch(result)
                        self._publish_routing_result(
                            decision_id=command.request_id,
                            origin="command:reapply",
                            generation=generation,
                            rule_name=rule_name,
                            state=state,
                            result=result,
                            started_at=started_at,
                            obs_requests_before=obs_before,
                        )
                elif command.action == "profile":
                    result = self.dispatcher.execute_profile(
                        str(command.options.get("domain") or ""),
                        str(command.options.get("name") or ""),
                    )
                elif command.action == "layout.apply":
                    result = self.dispatcher.execute_layout_profile(
                        str(command.options.get("name") or "")
                    )
                elif command.action == "layout.preview":
                    result = self.dispatcher.execute_layout_profile(
                        str(command.options.get("name") or ""),
                        preview=True,
                    )
                elif command.action == "layout.cancel-preview":
                    result = self.dispatcher.layout_manager.cancel_preview()
                elif command.action == "layout.undo":
                    result = self.dispatcher.layout_manager.undo_last()
                else:
                    raise ValueError(f"Commande OBS inconnue : {command.action}")
            warnings = tuple(getattr(result, "warnings", ()) or ()) if result is not None else ()
            missing = tuple(getattr(result, "missing_sources", ()) or ()) if result is not None else ()
            incomplete = bool(warnings or missing)
            if incomplete:
                details = [
                    *(f"source manquante: {name}" for name in missing),
                    *warnings,
                ]
                self._emit_obs_result(
                    command,
                    success=False,
                    result=result,
                    error="Application OBS incomplète: " + "; ".join(details),
                )
            else:
                self._emit_obs_result(command, success=True, result=result)
        except _RuntimeShutdownRequested as exc:
            self.logger.info("OBS command interrupted [%s]: %s", command.action, exc)
            self._emit_obs_result(command, success=False, error=str(exc))
            raise
        except Exception as exc:
            self.logger.error("OBS command failed [%s]: %s", command.action, exc)
            self._emit_obs_result(command, success=False, error=str(exc))
        finally:
            with self._lock:
                if self._active_operation == command.action:
                    self._active_operation = ""

    def _emit_obs_result(
        self,
        command: _OBSCommand,
        *,
        success: bool,
        result: object | None = None,
        error: str = "",
    ) -> None:
        self._set_command_status(
            command.request_id,
            action=command.action,
            status="completed" if success else "failed",
            error=error,
            result=result,
        )
        payload = OBSCommandResult(
            request_id=command.request_id,
            action=command.action,
            success=bool(success),
            result=result,
            error=str(error),
        )
        self._emit(
            RuntimeEvent(
                "obs_command_result",
                error or f"{command.action} terminé",
                request_id=command.request_id,
                payload=payload,
                success=bool(success),
            )
        )

    def _execute_activation_command(self, command: _ActivationCommand) -> None:
        scheduler = self.activation_scheduler
        controller = self.activation_controller
        if scheduler is None or controller is None:
            self._emit_activation_result(
                command,
                success=False,
                error="Aucune politique de déclenchement active",
            )
            return

        now = time.monotonic()
        try:
            if command.action == "test_roll":
                result = scheduler.test_roll(command.policy)
                verdict = "succès" if result.triggered else "échec"
                source = f" → {result.source}" if result.source else ""
                self._record_activation_diagnostic(
                    command.policy,
                    "test",
                    (
                        f"test tirage {result.roll * 100.0:.3f} % / "
                        f"{result.chance * 100.0:.3f} % : {verdict}{source}"
                    ),
                )
                self._emit_activation_result(command, success=True, result=result)
                return

            if command.action == "reset_all":
                clean = self._reconcile_activation("réinitialisation manuelle")
                self._emit_activation_result(
                    command,
                    success=clean,
                    result=clean,
                    error="" if clean else "Nettoyage OBS incomplet",
                )
                return

            policy = scheduler.policies.get(command.policy)
            if policy is None:
                raise KeyError(f"Politique d'activation introuvable : {command.policy}")
            eligible, reason = self._effective_activation_eligibility(
                command.policy,
                policy,
            )
            with self._lock:
                self._activation_eligibility_cache[command.policy] = (eligible, reason)

            if command.action == "trigger":
                events = scheduler.trigger_now(
                    command.policy,
                    target_identity=command.target_identity,
                    target_source=command.target_source,
                    eligible=eligible,
                    ignore_cooldown=bool(command.options.get("ignore_cooldown", False)),
                    now=now,
                )
                for event in events:
                    self._handle_activation_event(event, now=now)
                state = scheduler.state(command.policy)
                if state.phase.value != "visible":
                    raise RuntimeError(
                        "Déclenchement non acquitté ; consulter le diagnostic runtime"
                    )
                self._emit_activation_result(command, success=True, result=events)
                return

            if command.action == "stop":
                events = scheduler.stop(command.policy, enter_cooldown=True, now=now)
                if not events:
                    self._record_activation_diagnostic(
                        command.policy,
                        "commande",
                        "arrêt manuel ignoré : aucune source visible",
                    )
                for event in events:
                    self._handle_activation_event(event, now=now)
                blocked, cleanup_reason = controller.policy_cleanup_status(command.policy)
                if blocked:
                    raise RuntimeError(cleanup_reason)
                self._emit_activation_result(command, success=True, result=events)
                return

            if command.action == "reset_cooldown":
                events = scheduler.reset_cooldown(
                    command.policy,
                    eligible=eligible,
                    now=now,
                )
                if not events:
                    self._record_activation_diagnostic(
                        command.policy,
                        "commande",
                        "réinitialisation cooldown ignorée : aucun cooldown actif",
                    )
                else:
                    self._record_activation_diagnostic(
                        command.policy,
                        "commande",
                        "cooldown réinitialisé manuellement",
                    )
                self._emit_activation_result(command, success=True, result=events)
                return

            raise ValueError(f"Commande de déclenchement inconnue : {command.action}")
        except Exception as exc:
            self._record_activation_diagnostic(
                command.policy or "*",
                "commande",
                f"{command.action} refusée : {exc}",
            )
            self._emit_activation_result(command, success=False, error=str(exc))

    def _start_simulation(self, command: _ActivationCommand) -> None:
        scheduler = self.activation_scheduler
        if scheduler is None:
            self._emit_activation_result(
                command,
                success=False,
                error="Aucune politique de déclenchement active",
            )
            return
        with self._lock:
            policy = scheduler.policies.get(command.policy)
            policy_snapshot = copy.deepcopy(policy) if policy is not None else None
        if policy_snapshot is None:
            self._emit_activation_result(
                command,
                success=False,
                error=f"Politique d'activation introuvable : {command.policy}",
            )
            return
        trials = int(command.options.get("trials", 1000))
        seed = int(command.options.get("seed", 12345))

        def run_simulation():
            isolated = ActivationScheduler({command.policy: policy_snapshot})
            return isolated.simulate(command.policy, trials=trials, seed=seed)

        future = self._simulation_executor.submit(run_simulation)
        future.add_done_callback(
            lambda completed, cmd=command: self._finish_simulation(cmd, completed)
        )

    def _finish_simulation(
        self,
        command: _ActivationCommand,
        future: Future,
    ) -> None:
        with self._lock:
            if command.generation != self._command_generation or self._stopping:
                return
        try:
            result = future.result()
        except Exception as exc:
            self._emit_activation_result(command, success=False, error=str(exc))
            return
        self._record_activation_diagnostic(
            command.policy,
            "simulation",
            (
                f"{result.trials} tirages seed={result.seed} "
                f"config={result.config_fingerprint} : "
                f"{result.trigger_count} déclenchements, "
                f"{result.miss_count} échecs chance, {result.blocked_count} bloqués"
            ),
        )
        self._emit_activation_result(command, success=True, result=result)

    def _emit_activation_result(
        self,
        command: _ActivationCommand,
        *,
        success: bool,
        result: object | None = None,
        error: str = "",
    ) -> None:
        self._set_command_status(
            command.request_id,
            action=command.action,
            status="completed" if success else "failed",
            error=error,
            result=result,
        )
        payload = ActivationCommandResult(
            request_id=command.request_id,
            action=command.action,
            policy=command.policy,
            success=bool(success),
            result=result,
            error=str(error),
        )
        self._emit(
            RuntimeEvent(
                "activation_command_result",
                error or f"{command.action} terminé",
                request_id=command.request_id,
                payload=payload,
                success=bool(success),
            )
        )

    def _perform_orderly_shutdown(self) -> None:
        """Run cleanup once and always finish the worker shutdown protocol."""
        self._shutdown_cleanup_active = True
        try:
            self._perform_activation_shutdown()
        except Exception as exc:
            # Cleanup failure must preserve the fail-closed lifecycle: do not
            # resurrect normal work and do not lose an already-consumed shutdown.
            self.logger.exception("Runtime cleanup failed during shutdown")
            self._record_activation_diagnostic(
                "*",
                "warning",
                f"nettoyage runtime interrompu : {exc}",
            )
        finally:
            self._finalize_shutdown_signal()

    def _finalize_shutdown_signal(self) -> None:
        with self._lock:
            self._shutdown_phase = "simulation_shutdown"
        try:
            self._simulation_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._stop.set()
        with self._lock:
            self._shutdown_phase = "stop_signalled"
        self._shutdown_cleanup_active = False

    def _perform_activation_shutdown(self) -> None:
        self._record_activation_diagnostic("*", "arrêt", "nettoyage runtime en cours")
        scheduler = self.activation_scheduler
        controller = self.activation_controller

        # Full activation reconciliation is deliberately reserved for OBS
        # connect/reconnect. During orderly stop, capture scheduler-owned visible
        # state as cleanup obligations before reset_all() destroys that transient
        # state, then issue only the corresponding hides.
        hide_events: list[ActivationEvent] = []
        if scheduler is not None:
            with self._lock:
                self._shutdown_phase = "activation_snapshot"
            states = getattr(scheduler, "states", None)
            register = getattr(controller, "register_hide_obligation", None)
            if callable(states) and callable(register):
                try:
                    now = time.monotonic()
                    for policy_name, state in states().items():
                        phase = str(getattr(getattr(state, "phase", None), "value", ""))
                        source = str(getattr(state, "active_source", "") or "")
                        if phase != "visible" or not source:
                            continue
                        event = ActivationEvent(
                            "hide",
                            str(policy_name),
                            now,
                            source=source,
                            container=str(getattr(state, "active_container", "") or ""),
                            container_kind=str(
                                getattr(state, "active_container_kind", "scene") or "scene"
                            ),
                            collection=str(getattr(state, "active_collection", "") or ""),
                            reason="shutdown",
                        )
                        register(event)
                except Exception as exc:
                    # Do not invent a context if OBS cannot establish one. The
                    # scheduler event is still retained locally below and its
                    # normal apply path will either acknowledge or record it.
                    self.logger.warning(
                        "Activation cleanup pre-arm failed during shutdown: %s",
                        exc,
                    )
                    self._record_activation_diagnostic(
                        "*",
                        "warning",
                        f"pré-enregistrement cleanup impossible : {exc}",
                    )

            with self._lock:
                self._shutdown_phase = "activation_reset"
            try:
                hide_events = [
                    event
                    for event in scheduler.reset_all()
                    if getattr(event, "kind", "") == "hide"
                ]
            except Exception as exc:
                self.logger.warning("Activation scheduler reset failed during shutdown: %s", exc)
                self._record_activation_diagnostic(
                    "*",
                    "warning",
                    f"réinitialisation scheduler impossible : {exc}",
                )
            with self._lock:
                self._activation_eligibility_cache.clear()
                self._activation_cleanup_cache.clear()

        if controller is not None:
            for event in hide_events:
                with self._lock:
                    self._shutdown_phase = (
                        f"activation_hide:{getattr(event, 'policy', '*')}/"
                        f"{getattr(event, 'source', '')}"
                    )
                try:
                    controller.apply_event(event)
                except Exception as exc:
                    self.logger.warning("Activation hide during shutdown failed: %s", exc)
                    self._record_activation_diagnostic(
                        getattr(event, "policy", "*"),
                        "nettoyage",
                        f"masquage de sortie non acquitté : {exc}",
                    )

        with self._lock:
            self._shutdown_phase = "activation_pending_cleanup"
        current_pending = getattr(controller, "pending_hides_for_current_collection", None)
        pending_for_retry = (
            current_pending
            if callable(current_pending)
            else (lambda: controller.pending_hides() if controller is not None else ())
        )
        deadline = time.monotonic() + 1.5
        while (
            controller is not None
            and pending_for_retry()
            and time.monotonic() < deadline
        ):
            now = time.monotonic()
            for message in controller.retry_pending_hides(now=now):
                self._record_activation_diagnostic("*", "nettoyage", message)
            if pending_for_retry():
                time.sleep(0.05)

        if controller is not None and controller.pending_hides():
            pending_count = len(controller.pending_hides())
            self._record_activation_diagnostic(
                "*",
                "warning",
                f"arrêt avec {pending_count} masquage(s) non acquitté(s)",
            )
            self._emit(
                RuntimeEvent(
                    "activation_cleanup_persisted",
                    f"{pending_count} masquage(s) devront être repris par le prochain runtime",
                    success=False,
                )
            )
    def _apply_change(self, change: StateChange) -> None:
        self.logger.info(
            "State decision [%s] -> %s (OBS delay %d ms)",
            change.rule_name,
            change.current.as_variables(),
            change.apply_delay_ms,
        )
        if self.on_change:
            self.on_change(change)
        superseded: _PendingDispatch | None = None
        with self._lock:
            superseded = self._pending_dispatch
            self._dispatch_generation += 1
            generation = self._dispatch_generation
            decision_id = uuid.uuid4().hex
            origin = str(change.reason or "router")
            self._pending_dispatch = _PendingDispatch(
                deadline=time.monotonic() + max(0, change.apply_delay_ms) / 1000.0,
                generation=generation,
                change=change,
                decision_id=decision_id,
                origin=origin,
            )

        if superseded is not None:
            pending_domains = ()
            if hasattr(self.dispatcher, "pending_domains"):
                try:
                    pending_domains = tuple(self.dispatcher.pending_domains(superseded.change.current))
                except Exception:
                    pending_domains = ()
            status = RoutingDecisionStatus(
                decision_id=superseded.decision_id,
                origin=superseded.origin,
                generation=superseded.generation,
                config_revision=self.config_revision,
                rule_name=superseded.change.rule_name,
                requested_domains=pending_domains,
                applied_domains=(),
                blocked_domains=(),
                failed_domains=(),
                pending_domains=pending_domains,
                domain_details=(),
                duration_ms=0.0,
                obs_requests=0,
                success=False,
                message="Décision OBS remplacée avant application",
            )
            with self._lock:
                self._last_routing_status = status
                self._routing_diagnostics.append(status)
            self._emit(
                RuntimeEvent(
                    "routing_result",
                    status.message,
                    request_id=status.decision_id,
                    payload=status.as_mapping(),
                    success=False,
                )
            )

        self._emit(
            RuntimeEvent(
                "routing_decision",
                f"{change.rule_name}: décision OBS génération {generation}",
                request_id=decision_id,
                payload={
                    "decision_id": decision_id,
                    "origin": origin,
                    "generation": generation,
                    "config_revision": self.config_revision,
                    "rule_name": change.rule_name,
                    "state": change.current.as_variables(),
                    "apply_delay_ms": change.apply_delay_ms,
                },
            )
        )
        if change.apply_delay_ms > 0:
            self._emit(
                RuntimeEvent(
                    "pending",
                    f"{change.rule_name}: application OBS dans {change.apply_delay_ms} ms",
                    request_id=decision_id,
                )
            )
        self._wake.set()

    def _process_due_dispatch(self) -> None:
        with self._lock:
            pending = self._pending_dispatch
            if pending is None or pending.deadline > time.monotonic():
                return
            self._pending_dispatch = None
        self._dispatch_if_current(pending)

    def _dispatch_if_current(self, pending: _PendingDispatch) -> None:
        change = pending.change
        with self._dispatch_lock:
            if self._stop.is_set():
                return
            with self._lock:
                if self._stopping:
                    return
                if pending.generation != self._dispatch_generation:
                    return
                if self.engine.current_state != change.current:
                    return
            started_at = time.monotonic()
            obs_before = self._obs_request_count()
            try:
                result = self.dispatcher.dispatch_change(change)
                if self.on_dispatch:
                    self.on_dispatch(result)
                if result.executed:
                    self.logger.info(
                        "OBS dispatch: %d action(s), domains=%s",
                        result.executed,
                        ",".join(result.changed_domains),
                    )
                for warning in result.warnings:
                    self.logger.warning("OBS: %s", warning)
                self._publish_routing_result(
                    decision_id=pending.decision_id,
                    origin=pending.origin,
                    generation=pending.generation,
                    rule_name=change.rule_name,
                    state=change.current,
                    result=result,
                    started_at=started_at,
                    obs_requests_before=obs_before,
                )
            except Exception as exc:
                self.logger.error("OBS dispatch failed: %s", exc)
                self._publish_routing_result(
                    decision_id=pending.decision_id,
                    origin=pending.origin,
                    generation=pending.generation,
                    rule_name=change.rule_name,
                    state=change.current,
                    result=None,
                    started_at=started_at,
                    obs_requests_before=obs_before,
                    error=str(exc),
                )
                self._emit(RuntimeEvent("obs_error", str(exc), request_id=pending.decision_id))

    def _wait_for_dispatch_quiescence(self, timeout: float) -> bool:
        acquired = self._dispatch_lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            return False
        self._dispatch_lock.release()
        return True

    def _emit(self, event: RuntimeEvent) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                self.logger.exception("Runtime event callback failed")
