from __future__ import annotations

import copy
from dataclasses import dataclass

from .models import DesiredProperty, DesiredState, ObservedState, ResourceKey, values_equal


@dataclass(frozen=True, slots=True)
class PlanDiagnostic:
    level: str
    code: str
    message: str
    resource: ResourceKey | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "resource": self.resource.as_mapping() if self.resource is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PlannedOperation:
    operation_type: str
    resource: ResourceKey
    observed_known: bool
    observed_value: object
    desired_value: object
    provenance: tuple[str, ...]
    reason: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "type": self.operation_type,
            "resource": self.resource.as_mapping(),
            "observed": {
                "known": self.observed_known,
                "value": copy.deepcopy(self.observed_value),
            },
            "desired": copy.deepcopy(self.desired_value),
            "provenance": list(self.provenance),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    desired_state: DesiredState
    observed_state: ObservedState
    operations: tuple[PlannedOperation, ...] = ()
    diagnostics: tuple[PlanDiagnostic, ...] = ()

    @property
    def converged(self) -> bool:
        return not self.operations and not any(
            diagnostic.level == "error" for diagnostic in self.diagnostics
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "desired_state": self.desired_state.as_mapping(),
            "observed_state": self.observed_state.as_mapping(),
            "operations": [operation.as_mapping() for operation in self.operations],
            "diagnostics": [diagnostic.as_mapping() for diagnostic in self.diagnostics],
            "expected_final_state": self.desired_state.as_mapping(),
            "converged": self.converged,
        }


class DeclarativePlanner:
    """Pure DesiredState vs ObservedState planner.

    The planner deliberately performs no I/O. It only emits intent-level
    operations; a later executor may translate those operations to the existing
    serialized OBS writer.
    """

    def plan(self, desired: DesiredState, observed: ObservedState) -> ExecutionPlan:
        operations: list[PlannedOperation] = []
        diagnostics: list[PlanDiagnostic] = []
        for item in desired.properties:
            current = observed.lookup(item.key)
            if current.known and values_equal(current.value, item.value):
                continue
            if current.known:
                reason = "observed_value_differs"
            else:
                reason = "observed_value_unknown"
                diagnostics.append(
                    PlanDiagnostic(
                        level="warning",
                        code="observation_unknown",
                        message=(
                            f"Valeur observée inconnue pour {item.key.label()} ; "
                            "une affectation explicite serait nécessaire pour converger."
                        ),
                        resource=item.key,
                    )
                )
            operations.append(
                PlannedOperation(
                    operation_type=self._operation_type(item),
                    resource=item.key,
                    observed_known=current.known,
                    observed_value=copy.deepcopy(current.value),
                    desired_value=copy.deepcopy(item.value),
                    provenance=item.provenance,
                    reason=reason,
                )
            )
        return ExecutionPlan(
            desired_state=desired,
            observed_state=observed,
            operations=tuple(operations),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _operation_type(item: DesiredProperty) -> str:
        key = item.key
        if key.kind == "scene_item" and key.property_name == "visible":
            return "set_scene_item_visibility"
        if key.kind == "input" and key.property_name.startswith("settings."):
            return "set_input_setting"
        if key.kind == "filter" and key.property_name == "enabled":
            return "set_filter_enabled"
        if key.kind == "filter" and key.property_name.startswith("settings."):
            return "set_filter_setting"
        if key.kind == "layout" and key.property_name == "profile":
            return "apply_layout"
        return "set_property"
