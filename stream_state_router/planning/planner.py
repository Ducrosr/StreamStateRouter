from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping

from .models import DesiredAssignment, DesiredState, ObservedState, PropertyKey


_OPERATION_TYPES = {
    "program_scene": "SetProgramScene",
    "scene_item_visibility": "SetSceneItemVisibility",
    "input_mute": "SetInputMute",
    "input_volume_db": "SetInputVolumeDb",
    "input_setting": "SetInputSetting",
    "filter_enabled": "SetFilterEnabled",
    "filter_setting": "SetFilterSetting",
    "layout_profile": "ApplyLayout",
}


@dataclass(frozen=True, slots=True)
class DiffEntry:
    key: PropertyKey
    status: str
    observed_known: bool
    observed: Any
    desired: Any
    provenance: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "property": self.key.as_mapping(),
            "status": self.status,
            "observed_known": self.observed_known,
            "observed": self.observed,
            "desired": self.desired,
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class PlanOperation:
    operation: str
    key: PropertyKey
    observed: Any
    target: Any
    provenance: tuple[str, ...] = ()
    reason: str = "value_differs"

    def as_mapping(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "property": self.key.as_mapping(),
            "observed": self.observed,
            "target": self.target,
            "provenance": list(self.provenance),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PlanDiagnostic:
    level: str
    code: str
    message: str
    key: PropertyKey | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }
        if self.key is not None:
            result["property"] = self.key.as_mapping()
        return result


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    target_signature: str
    diff: tuple[DiffEntry, ...]
    operations: tuple[PlanOperation, ...]
    diagnostics: tuple[PlanDiagnostic, ...]

    @property
    def converged(self) -> bool:
        return not self.operations and all(item.status == "converged" for item in self.diff)

    @property
    def blocked(self) -> bool:
        return any(
            item.status in {"unknown", "unsupported", "blocked"}
            for item in self.diff
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "target_signature": self.target_signature,
            "converged": self.converged,
            "blocked": self.blocked,
            "diff": [item.as_mapping() for item in self.diff],
            "operations": [item.as_mapping() for item in self.operations],
            "diagnostics": [item.as_mapping() for item in self.diagnostics],
        }


def _operation_for(assignment: DesiredAssignment) -> str | None:
    return _OPERATION_TYPES.get(assignment.key.kind)


def _is_json_compatible(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _assignment_validation_error(
    assignment: DesiredAssignment,
) -> tuple[str, str] | None:
    key = assignment.key
    value = assignment.value

    if key.kind == "program_scene":
        if not isinstance(value, str) or not value.strip():
            return "invalid_desired_value", "Program scene must be a non-empty string"
        return None

    if key.kind == "scene_item_visibility":
        if not key.container or not key.source:
            return (
                "invalid_property_key",
                "Scene-item visibility requires container and source",
            )
        if not isinstance(value, bool):
            return "invalid_desired_value", "Scene-item visibility must be boolean"
        return None

    if key.kind == "input_mute":
        if not key.source:
            return "invalid_property_key", "Input mute requires an input name"
        if not isinstance(value, bool):
            return "invalid_desired_value", "Input mute must be boolean"
        return None

    if key.kind == "input_volume_db":
        if not key.source:
            return "invalid_property_key", "Input volume requires an input name"
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return "invalid_desired_value", "Input volume dB must be a finite number"
        return None

    if key.kind == "input_setting":
        if not key.source or not key.setting:
            return (
                "invalid_property_key",
                "Input setting requires input name and setting key",
            )
        if not _is_json_compatible(value):
            return (
                "invalid_desired_value",
                "Input setting value must be JSON-compatible",
            )
        return None

    if key.kind == "filter_enabled":
        if not key.source or not key.filter_name:
            return (
                "invalid_property_key",
                "Filter enable state requires source and filter name",
            )
        if not isinstance(value, bool):
            return "invalid_desired_value", "Filter enable state must be boolean"
        return None

    if key.kind == "filter_setting":
        if not key.source or not key.filter_name or not key.setting:
            return (
                "invalid_property_key",
                "Filter setting requires source, filter name and setting key",
            )
        if not _is_json_compatible(value):
            return (
                "invalid_desired_value",
                "Filter setting value must be JSON-compatible",
            )
        return None

    if key.kind == "layout_profile":
        if not isinstance(value, str) or not value.strip():
            return "invalid_desired_value", "Layout profile must be a non-empty string"
        return None

    return None


def _values_equal(left: Any, right: Any) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        try:
            return math.isclose(
                float(left),
                float(right),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        except (TypeError, ValueError, OverflowError):
            return False
    return left == right


def build_execution_plan(
    desired: DesiredState,
    observed: ObservedState,
    *,
    preflight: Mapping[PropertyKey, PlanDiagnostic] | None = None,
) -> ExecutionPlan:
    """Compute a deterministic, side-effect-free dry-run plan.

    Unknown physical values are intentionally not converted into blind writes.
    A future observer/executor must first resolve them or explicitly adopt a
    policy for an idempotent assignment.
    """
    diff: list[DiffEntry] = []
    operations: list[PlanOperation] = []
    diagnostics: list[PlanDiagnostic] = []
    preflight = preflight or {}

    for assignment in desired.assignments:
        key = assignment.key
        current = observed.get(key)
        operation_type = _operation_for(assignment)

        validation_error = _assignment_validation_error(assignment)
        if validation_error is not None:
            code, message = validation_error
            diff.append(
                DiffEntry(
                    key=key,
                    status="blocked",
                    observed_known=current.known,
                    observed=current.value,
                    desired=assignment.value,
                    provenance=assignment.provenance,
                )
            )
            diagnostics.append(
                PlanDiagnostic("error", code, message, key)
            )
            continue

        blocked = preflight.get(key)
        if blocked is not None:
            diff.append(
                DiffEntry(
                    key=key,
                    status="blocked",
                    observed_known=current.known,
                    observed=current.value,
                    desired=assignment.value,
                    provenance=assignment.provenance,
                )
            )
            diagnostics.append(blocked)
            continue

        if operation_type is None:
            diff.append(
                DiffEntry(
                    key=key,
                    status="unsupported",
                    observed_known=current.known,
                    observed=current.value,
                    desired=assignment.value,
                    provenance=assignment.provenance,
                )
            )
            diagnostics.append(
                PlanDiagnostic(
                    "error",
                    "unsupported_property",
                    f"Unsupported managed property kind: {key.kind}",
                    key,
                )
            )
            continue

        if not current.known:
            diff.append(
                DiffEntry(
                    key=key,
                    status="unknown",
                    observed_known=False,
                    observed=None,
                    desired=assignment.value,
                    provenance=assignment.provenance,
                )
            )
            diagnostics.append(
                PlanDiagnostic(
                    "warning",
                    "observed_value_unknown",
                    "Observed value is unknown; no write is planned until it is resolved.",
                    key,
                )
            )
            continue

        if _values_equal(current.value, assignment.value):
            diff.append(
                DiffEntry(
                    key=key,
                    status="converged",
                    observed_known=True,
                    observed=current.value,
                    desired=assignment.value,
                    provenance=assignment.provenance,
                )
            )
            continue

        diff.append(
            DiffEntry(
                key=key,
                status="change",
                observed_known=True,
                observed=current.value,
                desired=assignment.value,
                provenance=assignment.provenance,
            )
        )
        operations.append(
            PlanOperation(
                operation=operation_type,
                key=key,
                observed=current.value,
                target=assignment.value,
                provenance=assignment.provenance,
            )
        )

    return ExecutionPlan(
        target_signature=desired.target_signature(),
        diff=tuple(diff),
        operations=tuple(operations),
        diagnostics=tuple(diagnostics),
    )


def _display_value(key: PropertyKey, value: Any) -> str:
    if key.kind == "input_setting":
        return "<redacted>"
    return repr(value)


def render_execution_plan(plan: ExecutionPlan) -> str:
    """Render a compact deterministic dry-run report for diagnostics/UI."""

    lines = [
        "Declarative dry-run",
        f"target={plan.target_signature}",
        f"converged={str(plan.converged).lower()} blocked={str(plan.blocked).lower()}",
        "diff:",
    ]
    if not plan.diff:
        lines.append("  (empty)")
    for item in plan.diff:
        label = item.key.kind
        if item.key.container:
            label += f" {item.key.container}"
        if item.key.source:
            label += f"/{item.key.source}"
        if item.key.filter_name:
            label += f"/{item.key.filter_name}"
        if item.key.setting:
            label += f".{item.key.setting}"
        observed = (
            _display_value(item.key, item.observed)
            if item.observed_known
            else "<unknown>"
        )
        desired = _display_value(item.key, item.desired)
        lines.append(
            f"  - {label}: {item.status} {observed} -> {desired}"
        )

    lines.append("operations:")
    if not plan.operations:
        lines.append("  (none)")
    for item in plan.operations:
        lines.append(
            f"  - {item.operation}: "
            f"{_display_value(item.key, item.observed)} -> "
            f"{_display_value(item.key, item.target)}"
        )

    if plan.diagnostics:
        lines.append("diagnostics:")
        for item in plan.diagnostics:
            lines.append(f"  - {item.level}:{item.code}: {item.message}")
    return "\n".join(lines)
