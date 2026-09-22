from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping
import uuid

from ..obs.client import OBSClientManager, OBSRequestError, OBSUnavailableError
from ..obs.observed import (
    ExecutionBinding,
    SceneItemBinding,
)
from ..planning.models import DesiredState, ObservedState, ObservedValue, PropertyKey
from ..planning.planner import ExecutionPlan, build_execution_plan
from .declarative import DeclarativePlanningService


_ALLOWED_KINDS = frozenset({"scene_item_visibility", "input_mute"})


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    plan_id: str
    desired: DesiredState
    plan: ExecutionPlan
    collection: str
    session_generation: int
    catalog_epoch: int
    config_revision: str
    dispatch_generation: int
    resume_generation: int
    bindings: tuple[ExecutionBinding, ...]

    @classmethod
    def create(
        cls,
        *,
        desired: DesiredState,
        plan: ExecutionPlan,
        collection: str,
        session_generation: int,
        catalog_epoch: int,
        config_revision: str,
        dispatch_generation: int,
        resume_generation: int,
        bindings: tuple[ExecutionBinding, ...],
    ) -> "PreparedExecution":
        return cls(
            plan_id=uuid.uuid4().hex,
            desired=desired,
            plan=plan,
            collection=str(collection),
            session_generation=int(session_generation),
            catalog_epoch=int(catalog_epoch),
            config_revision=str(config_revision),
            dispatch_generation=int(dispatch_generation),
            resume_generation=int(resume_generation),
            bindings=tuple(bindings),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "target_signature": self.plan.target_signature,
            "collection": self.collection,
            "session_generation": self.session_generation,
            "catalog_epoch": self.catalog_epoch,
            "config_revision": self.config_revision,
            "desired": self.desired.as_mapping(diagnostic=True),
            "plan": self.plan.as_mapping(),
            "bindings": [
                _binding_diagnostic(binding)
                for binding in self.bindings
            ],
        }


@dataclass(frozen=True, slots=True)
class ExecutionStepResult:
    key: PropertyKey
    operation: str
    write_attempted: bool
    write_accepted: bool | None
    ack_known: bool
    ack_value: bool | None
    status: str
    code: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "property": self.key.as_mapping(),
            "operation": self.operation,
            "write_attempted": self.write_attempted,
            "write_accepted": self.write_accepted,
            "ack_known": self.ack_known,
            "ack_value": self.ack_value,
            "status": self.status,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    plan_id: str
    target_signature: str
    collection: str
    session_generation: int
    status: str
    converged: bool
    replan_required: bool
    steps: tuple[ExecutionStepResult, ...]
    diagnostics: tuple[str, ...] = ()
    final_plan: ExecutionPlan | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "plan_id": self.plan_id,
            "target_signature": self.target_signature,
            "collection": self.collection,
            "session_generation": self.session_generation,
            "status": self.status,
            "converged": self.converged,
            "replan_required": self.replan_required,
            "steps": [step.as_mapping() for step in self.steps],
            "diagnostics": list(self.diagnostics),
        }
        if self.final_plan is not None:
            result["final_plan"] = self.final_plan.as_mapping()
        return result


class _ReplanRequired(RuntimeError):
    pass


class _ExecutionBlocked(RuntimeError):
    pass


def _binding_diagnostic(binding: ExecutionBinding) -> dict[str, object]:
    if isinstance(binding, SceneItemBinding):
        return {
            "kind": "scene_item_visibility",
            "property": binding.key.as_mapping(),
            "container_uuid": binding.container_uuid,
            "source_uuid": binding.source_uuid,
            "scene_item_id": binding.scene_item_id,
            "occurrences": len(binding.occurrence_fingerprint),
        }
    return {
        "kind": "input_mute",
        "property": binding.key.as_mapping(),
        "input_uuid": binding.input_uuid,
    }


class DeclarativeExecutor:
    """Strict opt-in executor for the first declarative mutation MVP."""

    def __init__(
        self,
        client: OBSClientManager,
        planning: DeclarativePlanningService,
        *,
        cooperative_yield: Callable[[], None] | None = None,
    ):
        self.client = client
        self.planning = planning
        self._cooperative_yield = cooperative_yield

    def _yield(self) -> None:
        if self._cooperative_yield is not None:
            self._cooperative_yield()

    def _context_check(self, prepared: PreparedExecution) -> None:
        if prepared.session_generation <= 0:
            raise _ReplanRequired("prepared OBS session generation is invalid")
        if self.planning.catalog_epoch != prepared.catalog_epoch:
            raise _ReplanRequired("OBS catalog epoch changed since preparation")
        status = self.planning.catalog_status()
        if bool(status.get("stale", False)):
            raise _ReplanRequired("OBS catalog became stale since preparation")
        try:
            collection, generation = self.planning.context_identity()
        except RuntimeError as exc:
            raise _ReplanRequired(str(exc)) from exc
        if collection != prepared.collection or generation != prepared.session_generation:
            raise _ReplanRequired("OBS session or Scene Collection changed")

    def _capability_check(self, prepared: PreparedExecution) -> None:
        catalog = self.planning.catalog
        if catalog is None:
            raise _ReplanRequired("OBS catalog is unavailable")
        if catalog.warnings or catalog.unreadable_containers:
            raise _ExecutionBlocked("OBS catalog is partial")
        required: set[str] = set()
        for assignment in prepared.desired.assignments:
            if assignment.key.kind == "scene_item_visibility":
                required.update(
                    {
                        "GetSceneList",
                        "GetSceneItemList",
                        "GetSceneItemEnabled",
                        "SetSceneItemEnabled",
                    }
                )
            elif assignment.key.kind == "input_mute":
                required.update({"GetInputList", "GetInputMute", "SetInputMute"})
            else:
                raise _ExecutionBlocked(
                    f"property kind {assignment.key.kind!r} is outside executor allowlist"
                )
        missing = sorted(name for name in required if catalog.supports(name) is not True)
        if missing:
            raise _ExecutionBlocked(
                "OBS capabilities are not established for: " + ", ".join(missing)
            )

    @staticmethod
    def _binding_map(
        prepared: PreparedExecution,
    ) -> dict[PropertyKey, ExecutionBinding]:
        result = {binding.key: binding for binding in prepared.bindings}
        desired_keys = {assignment.key for assignment in prepared.desired.assignments}
        if set(result) != desired_keys:
            raise _ExecutionBlocked("prepared physical bindings are incomplete")
        return result

    def _guarded_send(
        self,
        prepared: PreparedExecution,
        request: str,
        data: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._yield()
        return self.client.send(
            request,
            data,
            expected_session_generation=prepared.session_generation,
        )

    def _read_binding(
        self,
        prepared: PreparedExecution,
        binding: ExecutionBinding,
    ) -> bool:
        key = binding.key
        if isinstance(binding, SceneItemBinding):
            scenes = self._guarded_send(prepared, "GetSceneList").get("scenes", []) or []
            scene_matches = [
                row
                for row in scenes
                if isinstance(row, Mapping)
                and str(row.get("sceneName") or "").strip() == key.container
            ]
            if len(scene_matches) != 1:
                raise _ReplanRequired("scene container identity changed")
            scene_uuid = str(scene_matches[0].get("sceneUuid") or "").strip()
            if not scene_uuid or scene_uuid != binding.container_uuid:
                raise _ReplanRequired("scene container UUID changed")

            rows_raw = self._guarded_send(
                prepared,
                "GetSceneItemList",
                {"sceneUuid": binding.container_uuid},
            ).get("sceneItems", []) or []
            rows = [
                row
                for row in rows_raw
                if isinstance(row, Mapping)
                and not bool(
                    row.get("groupItemBackup", False)
                    or row.get("group_item_backup", False)
                )
                and str(row.get("sourceName") or "").strip() == key.source
            ]
            fingerprint: list[tuple[int, str]] = []
            for row in rows:
                if "sceneItemId" not in row:
                    raise _ExecutionBlocked("OBS omitted sceneItemId")
                raw_id = row.get("sceneItemId")
                if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                    raise _ExecutionBlocked("OBS returned invalid sceneItemId")
                item_id = raw_id
                source_uuid = str(row.get("sourceUuid") or "").strip()
                if not source_uuid:
                    raise _ExecutionBlocked("OBS omitted sourceUuid")
                fingerprint.append((item_id, source_uuid))
            if tuple(fingerprint) != binding.occurrence_fingerprint:
                raise _ReplanRequired("scene-item occurrence binding changed")
            if key.occurrence >= len(fingerprint):
                raise _ReplanRequired("scene-item occurrence disappeared")
            item_id, source_uuid = fingerprint[key.occurrence]
            if item_id != binding.scene_item_id or source_uuid != binding.source_uuid:
                raise _ReplanRequired("scene-item physical identity changed")

            response = self._guarded_send(
                prepared,
                "GetSceneItemEnabled",
                {
                    "sceneUuid": binding.container_uuid,
                    "sceneItemId": binding.scene_item_id,
                },
            )
            value = response.get("sceneItemEnabled")
            if not isinstance(value, bool):
                raise _ExecutionBlocked("OBS returned non-boolean sceneItemEnabled")
            return value

        inputs = self._guarded_send(prepared, "GetInputList").get("inputs", []) or []
        matches = [
            row
            for row in inputs
            if isinstance(row, Mapping)
            and str(row.get("inputName") or "").strip() == key.source
        ]
        if len(matches) != 1:
            raise _ReplanRequired("input identity changed")
        input_uuid = str(matches[0].get("inputUuid") or "").strip()
        if not input_uuid or input_uuid != binding.input_uuid:
            raise _ReplanRequired("input UUID changed")
        response = self._guarded_send(
            prepared,
            "GetInputMute",
            {"inputUuid": binding.input_uuid},
        )
        value = response.get("inputMuted")
        if not isinstance(value, bool):
            raise _ExecutionBlocked("OBS returned non-boolean inputMuted")
        return value

    def _write_binding(
        self,
        prepared: PreparedExecution,
        binding: ExecutionBinding,
        target: bool,
    ) -> None:
        if isinstance(binding, SceneItemBinding):
            self.client.send(
                "SetSceneItemEnabled",
                {
                    "sceneUuid": binding.container_uuid,
                    "sceneItemId": binding.scene_item_id,
                    "sceneItemEnabled": target,
                },
                expected_session_generation=prepared.session_generation,
            )
            return
        self.client.send(
            "SetInputMute",
            {
                "inputUuid": binding.input_uuid,
                "inputMuted": target,
            },
            expected_session_generation=prepared.session_generation,
        )

    def execute(
        self,
        prepared: PreparedExecution,
        *,
        validate_target: Callable[[], tuple[bool, str]],
        progress: Callable[[tuple[ExecutionStepResult, ...]], None] | None = None,
    ) -> ExecutionResult:
        steps: list[ExecutionStepResult] = []
        operation_by_key = {operation.key: operation for operation in prepared.plan.operations}

        def publish_progress() -> None:
            if progress is not None:
                progress(tuple(steps))

        def replace_step(step: ExecutionStepResult) -> None:
            nonlocal steps
            steps = [item for item in steps if item.key != step.key]
            steps.append(step)
            publish_progress()

        def finish(
            status: str,
            *,
            converged: bool = False,
            replan_required: bool = False,
            diagnostic: str = "",
            final_plan: ExecutionPlan | None = None,
        ) -> ExecutionResult:
            return ExecutionResult(
                plan_id=prepared.plan_id,
                target_signature=prepared.plan.target_signature,
                collection=prepared.collection,
                session_generation=prepared.session_generation,
                status=status,
                converged=converged,
                replan_required=replan_required,
                steps=tuple(steps),
                diagnostics=((diagnostic,) if diagnostic else ()),
                final_plan=final_plan,
            )

        try:
            if prepared.plan.blocked:
                return finish("blocked", diagnostic="prepared plan is blocked")
            if any(
                assignment.key.kind not in _ALLOWED_KINDS
                for assignment in prepared.desired.assignments
            ):
                return finish("blocked", diagnostic="desired state is outside MVP allowlist")
            self._context_check(prepared)
            self._capability_check(prepared)
            valid, reason = validate_target()
            if not valid:
                return finish("replan_required", replan_required=True, diagnostic=reason)

            bindings = self._binding_map(prepared)
            candidates: list[tuple[PropertyKey, ExecutionBinding, bool]] = []

            # Global physical preflight before the first mutation.
            for assignment in prepared.desired.assignments:
                binding = bindings[assignment.key]
                current = self._read_binding(prepared, binding)
                target = assignment.value
                if not isinstance(target, bool):
                    return finish(
                        "blocked",
                        diagnostic="MVP executable values must be boolean",
                    )
                operation = operation_by_key.get(assignment.key)
                if current is target:
                    steps.append(
                        ExecutionStepResult(
                            assignment.key,
                            operation.operation if operation else "Verify",
                            False,
                            None,
                            True,
                            current,
                            "already_converged",
                        )
                    )
                    continue
                if operation is None:
                    return finish(
                        "replan_required",
                        replan_required=True,
                        diagnostic="previously converged property drifted after preparation",
                    )
                if operation.observed is not current:
                    return finish(
                        "replan_required",
                        replan_required=True,
                        diagnostic="physical value no longer matches prepared observation",
                    )
                steps.append(
                    ExecutionStepResult(
                        assignment.key,
                        operation.operation,
                        False,
                        None,
                        False,
                        None,
                        "not_run",
                    )
                )
                candidates.append((assignment.key, binding, target))

            for key, binding, target in candidates:
                self._context_check(prepared)
                valid, reason = validate_target()
                if not valid:
                    return finish("replan_required", replan_required=True, diagnostic=reason)

                current = self._read_binding(prepared, binding)
                operation = operation_by_key[key]
                if current is target:
                    steps = [step for step in steps if step.key != key]
                    steps.append(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            False,
                            None,
                            True,
                            current,
                            "already_converged",
                        )
                    )
                    publish_progress()
                    continue
                if operation.observed is not current:
                    return finish(
                        "replan_required",
                        replan_required=True,
                        diagnostic="physical value changed immediately before mutation",
                    )

                # Last admission check immediately before the mutation.  This is
                # deliberately repeated after the fresh physical read so a
                # resume/override that raced with preflight cannot authorize a
                # write from the old target.
                # Revalidate the executor-owned context first, then let
                # the runtime callback perform its own fresh condition/target
                # resolution and final runtime-generation check.
                self._context_check(prepared)
                valid, reason = validate_target()
                if not valid:
                    return finish(
                        "replan_required",
                        replan_required=True,
                        diagnostic=reason,
                    )

                # Pure cooperative checkpoint immediately before the write.
                # Once the request is handed to obs-websocket it cannot be
                # rolled back or safely retried by this executor.
                self._yield()
                replace_step(
                    ExecutionStepResult(
                        key,
                        operation.operation,
                        True,
                        None,
                        False,
                        None,
                        "unacknowledged",
                        "write_inflight",
                    )
                )
                try:
                    self._write_binding(prepared, binding, target)
                except OBSUnavailableError as exc:
                    replace_step(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            True,
                            None,
                            False,
                            None,
                            "unacknowledged",
                            "transport_failure",
                        )
                    )
                    return finish(
                        "failed",
                        replan_required=True,
                        diagnostic=str(exc),
                    )
                except OBSRequestError as exc:
                    replace_step(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            True,
                            False,
                            False,
                            None,
                            "failed",
                            "obs_request_failed",
                        )
                    )
                    return finish(
                        "failed",
                        replan_required=True,
                        diagnostic=str(exc),
                    )

                replace_step(
                    ExecutionStepResult(
                        key,
                        operation.operation,
                        True,
                        True,
                        False,
                        None,
                        "unacknowledged",
                        "awaiting_readback",
                    )
                )
                try:
                    self._context_check(prepared)
                    ack = self._read_binding(prepared, binding)
                    # The readback is only authoritative while the prepared
                    # OBS session and Scene Collection are still current.
                    self._context_check(prepared)
                except _ReplanRequired as exc:
                    replace_step(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            True,
                            True,
                            False,
                            None,
                            "unacknowledged",
                            "context_changed_after_write",
                        )
                    )
                    return finish(
                        "replan_required",
                        replan_required=True,
                        diagnostic=str(exc),
                    )
                except (OBSUnavailableError, _ExecutionBlocked) as exc:
                    replace_step(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            True,
                            True,
                            False,
                            None,
                            "unacknowledged",
                            "ack_unavailable",
                        )
                    )
                    return finish(
                        "failed",
                        replan_required=True,
                        diagnostic=str(exc),
                    )

                if ack is target:
                    replace_step(
                        ExecutionStepResult(
                            key,
                            operation.operation,
                            True,
                            True,
                            True,
                            ack,
                            "applied",
                        )
                    )
                    continue
                replace_step(
                    ExecutionStepResult(
                        key,
                        operation.operation,
                        True,
                        True,
                        True,
                        ack,
                        "divergent",
                        "readback_differs",
                    )
                )
                return finish(
                    "divergent",
                    replan_required=True,
                    diagnostic="OBS readback differs from target",
                )

            self._context_check(prepared)
            valid, reason = validate_target()
            if not valid:
                return finish("replan_required", replan_required=True, diagnostic=reason)

            final_values: dict[PropertyKey, ObservedValue] = {}
            for assignment in prepared.desired.assignments:
                try:
                    current = self._read_binding(prepared, bindings[assignment.key])
                except _ExecutionBlocked as exc:
                    return finish(
                        "failed",
                        replan_required=True,
                        diagnostic=f"final observation unavailable: {exc}",
                    )
                final_values[assignment.key] = ObservedValue.known_value(current)
            final_plan = build_execution_plan(
                prepared.desired,
                ObservedState(final_values),
            )
            self._context_check(prepared)
            valid, reason = validate_target()
            if not valid:
                return finish(
                    "replan_required",
                    replan_required=True,
                    diagnostic=reason,
                    final_plan=final_plan,
                )
            if final_plan.converged:
                return finish(
                    "converged",
                    converged=True,
                    final_plan=final_plan,
                )
            return finish(
                "divergent",
                replan_required=True,
                diagnostic="final observation is not converged",
                final_plan=final_plan,
            )
        except _ReplanRequired as exc:
            return finish("replan_required", replan_required=True, diagnostic=str(exc))
        except _ExecutionBlocked as exc:
            return finish("blocked", diagnostic=str(exc))
        except OBSUnavailableError as exc:
            return finish("failed", replan_required=True, diagnostic=str(exc))
        except OBSRequestError as exc:
            return finish(
                "failed",
                replan_required=True,
                diagnostic=str(exc),
            )
