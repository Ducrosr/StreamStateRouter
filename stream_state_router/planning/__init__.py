"""Declarative OBS planning primitives.

The planning package is deliberately free of OBS/Windows/Qt I/O.  It models
managed properties and computes dry-run execution plans from frozen inputs.
"""

from .coverage import CoverageIssue, validate_state_coverage
from .migration_coverage import (
    ActionCoverage,
    MigrationCoverageReport,
    ProfileCoverage,
    build_migration_coverage,
    render_migration_coverage,
)
from .intent import (
    DesiredOwnershipConflict,
    UnsupportedIntentAction,
    desired_assignments_from_actions,
    desired_state_from_action_sets,
)
from .models import (
    DesiredAssignment,
    DesiredState,
    DesiredStateConflict,
    ObservedState,
    ObservedValue,
    PropertyKey,
)
from .planner import (
    DiffEntry,
    ExecutionPlan,
    PlanDiagnostic,
    PlanOperation,
    build_execution_plan,
    render_execution_plan,
)

__all__ = [
    "ActionCoverage",
    "CoverageIssue",
    "MigrationCoverageReport",
    "ProfileCoverage",
    "DesiredAssignment",
    "DesiredOwnershipConflict",
    "UnsupportedIntentAction",
    "desired_assignments_from_actions",
    "desired_state_from_action_sets",
    "DesiredState",
    "DesiredStateConflict",
    "DiffEntry",
    "ExecutionPlan",
    "ObservedState",
    "ObservedValue",
    "PlanDiagnostic",
    "PlanOperation",
    "PropertyKey",
    "build_execution_plan",
    "build_migration_coverage",
    "validate_state_coverage",
    "render_execution_plan",
    "render_migration_coverage",
]
