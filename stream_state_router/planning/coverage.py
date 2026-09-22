from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import DesiredState, PropertyKey


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    state: str
    key: PropertyKey
    message: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "state": self.state,
            "property": self.key.as_mapping(),
            "message": self.message,
        }


def validate_state_coverage(
    states: Mapping[str, DesiredState],
    *,
    required: Iterable[PropertyKey] | None = None,
    delegated: Mapping[str, Iterable[PropertyKey]] | None = None,
) -> tuple[CoverageIssue, ...]:
    """Find managed properties accidentally left undefined by selectable states.

    The helper is intentionally policy-light.  A caller may supply an explicit
    required property set, or omit it to use the union of all properties managed
    by the compared states.  Explicit delegation suppresses a missing-property
    issue and represents ownership by another subsystem.
    """

    if required is None:
        required_keys = {
            assignment.key
            for state in states.values()
            for assignment in state.assignments
        }
    else:
        required_keys = set(required)

    delegated_sets = {
        str(name): set(keys)
        for name, keys in (delegated or {}).items()
    }

    issues: list[CoverageIssue] = []
    for state_name in sorted(states, key=str.casefold):
        state = states[state_name]
        present = set(state.by_key())
        allowed_missing = delegated_sets.get(state_name, set())
        for key in sorted(required_keys):
            if key in present or key in allowed_missing:
                continue
            issues.append(
                CoverageIssue(
                    state=state_name,
                    key=key,
                    message=(
                        f"State '{state_name}' does not define managed property "
                        f"'{key.kind}' and does not delegate it"
                    ),
                )
            )
    return tuple(issues)
