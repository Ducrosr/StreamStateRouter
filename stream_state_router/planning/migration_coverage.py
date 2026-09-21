from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..obs.models import OBSAction
from .intent import UnsupportedIntentAction, desired_assignments_from_actions


ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")
EXECUTABLE_PROPERTY_KINDS = frozenset({"scene_item_visibility", "input_mute"})
PLANNABLE_PROPERTY_KINDS = frozenset(
    {
        "program_scene",
        "input_volume_db",
        "input_setting",
        "filter_enabled",
        "filter_setting",
    }
)
CLASSIFICATIONS = (
    "empty",
    "declarative_executable",
    "declarative_plannable",
    "declarative_intent_only",
    "delegated",
    "legacy_only",
    "invalid",
)


@dataclass(frozen=True, slots=True)
class ActionCoverage:
    domain: str
    profile: str
    action_index: int
    action_type: str
    classification: str
    property_kinds: tuple[str, ...] = ()
    reason: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "profile": self.profile,
            "action_index": self.action_index,
            "action_type": self.action_type,
            "classification": self.classification,
            "property_kinds": list(self.property_kinds),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProfileCoverage:
    domain: str
    profile: str
    classification: str
    enabled_actions: int
    disabled_actions: int
    inherited_actions: int = 0
    reasons: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "profile": self.profile,
            "classification": self.classification,
            "enabled_actions": self.enabled_actions,
            "disabled_actions": self.disabled_actions,
            "inherited_actions": self.inherited_actions,
            "effective_actions": self.enabled_actions + self.inherited_actions,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class MigrationCoverageReport:
    actions: tuple[ActionCoverage, ...]
    profiles: tuple[ProfileCoverage, ...]

    @staticmethod
    def _counts(rows: tuple[object, ...]) -> dict[str, int]:
        result = {name: 0 for name in CLASSIFICATIONS}
        for row in rows:
            classification = str(getattr(row, "classification", ""))
            if classification in result:
                result[classification] += 1
        return result

    @staticmethod
    def _percentages(counts: Mapping[str, int], total: int) -> dict[str, float]:
        if total <= 0:
            return {name: 0.0 for name in CLASSIFICATIONS}
        return {
            name: round((counts.get(name, 0) * 100.0) / total, 2)
            for name in CLASSIFICATIONS
        }

    def as_mapping(self) -> dict[str, object]:
        action_counts = self._counts(self.actions)
        profile_counts = self._counts(self.profiles)
        action_total = len(self.actions)
        profile_total = len(self.profiles)
        return {
            "summary": {
                "actions": {
                    "total": action_total,
                    "counts": action_counts,
                    "percentages": self._percentages(action_counts, action_total),
                    "declarative_coverage_percent": round(
                        (
                            action_counts["declarative_executable"]
                            + action_counts["declarative_plannable"]
                            + action_counts["declarative_intent_only"]
                        )
                        * 100.0
                        / action_total,
                        2,
                    )
                    if action_total
                    else 100.0,
                    "executable_percent": round(
                        action_counts["declarative_executable"]
                        * 100.0
                        / action_total,
                        2,
                    )
                    if action_total
                    else 100.0,
                },
                "profiles": {
                    "total": profile_total,
                    "counts": profile_counts,
                    "percentages": self._percentages(profile_counts, profile_total),
                },
            },
            "profiles": [item.as_mapping() for item in self.profiles],
            "actions": [item.as_mapping() for item in self.actions],
        }


def _classification_for_property_kinds(kinds: set[str]) -> str:
    if not kinds:
        return "legacy_only"
    if kinds <= EXECUTABLE_PROPERTY_KINDS:
        return "declarative_executable"
    if kinds <= (EXECUTABLE_PROPERTY_KINDS | PLANNABLE_PROPERTY_KINDS):
        return "declarative_plannable"
    return "declarative_intent_only"


def _profile_classification(actions: list[ActionCoverage]) -> str:
    if not actions:
        return "empty"
    priority = {
        "invalid": 6,
        "legacy_only": 5,
        "declarative_intent_only": 4,
        "declarative_plannable": 3,
        "declarative_executable": 2,
        "delegated": 1,
        "empty": 0,
    }
    return max(actions, key=lambda item: priority[item.classification]).classification


def _classify_action(
    *,
    domain: str,
    profile: str,
    action_index: int,
    raw_action: Mapping[str, Any],
) -> ActionCoverage | None:
    action = OBSAction.from_mapping(raw_action)
    if not action.enabled:
        return None

    provenance = f"{domain}:{profile}"
    try:
        assignments = desired_assignments_from_actions(
            (action,),
            provenance=provenance,
        )
    except UnsupportedIntentAction as exc:
        return ActionCoverage(
            domain,
            profile,
            action_index,
            action.type,
            "legacy_only",
            (),
            str(exc),
        )
    except (TypeError, ValueError) as exc:
        return ActionCoverage(
            domain,
            profile,
            action_index,
            action.type,
            "invalid",
            (),
            str(exc),
        )

    kinds = {assignment.key.kind for assignment in assignments}
    return ActionCoverage(
        domain,
        profile,
        action_index,
        action.type,
        _classification_for_property_kinds(kinds),
        tuple(sorted(kinds)),
        "" if assignments else "action produced no stable managed property",
    )


def _resolve_effective_profile_actions(
    name: str,
    *,
    declared_by_profile: Mapping[str, list[ActionCoverage]],
    parent_by_profile: Mapping[str, str],
    invalid_profile_reason: Mapping[str, str],
    resolved_cache: dict[str, tuple[list[ActionCoverage], int, str]],
    stack: tuple[str, ...] = (),
) -> tuple[list[ActionCoverage], int, str]:
    cached = resolved_cache.get(name)
    if cached is not None:
        return cached
    if name in stack:
        return [], 0, "circular profile inheritance: " + " -> ".join((*stack, name))
    if name in invalid_profile_reason:
        return [], 0, invalid_profile_reason[name]

    parent = parent_by_profile.get(name, "")
    inherited: list[ActionCoverage] = []
    inherited_count = 0
    if parent:
        if parent not in declared_by_profile:
            return [], 0, f"missing parent profile: {parent}"
        parent_actions, _parent_inherited, error = _resolve_effective_profile_actions(
            parent,
            declared_by_profile=declared_by_profile,
            parent_by_profile=parent_by_profile,
            invalid_profile_reason=invalid_profile_reason,
            resolved_cache=resolved_cache,
            stack=(*stack, name),
        )
        if error:
            return [], 0, error
        inherited = list(parent_actions)
        inherited_count = len(parent_actions)

    effective = [*inherited, *declared_by_profile.get(name, [])]
    result = (effective, inherited_count, "")
    resolved_cache[name] = result
    return result


def build_migration_coverage_report(
    config: Mapping[str, Any],
) -> MigrationCoverageReport:
    """Describe declarative migration maturity without OBS I/O or mutations.

    Action totals count declarations once. Profile classifications use effective
    inherited actions, so an empty child cannot hide a legacy-only parent.
    """

    action_rows: list[ActionCoverage] = []
    profile_rows: list[ProfileCoverage] = []

    profiles_root = config.get("profiles")
    profiles = profiles_root if isinstance(profiles_root, Mapping) else {}

    for domain in ACTION_PROFILE_DOMAINS:
        raw_domain = profiles.get(domain)
        domain_profiles = raw_domain if isinstance(raw_domain, Mapping) else {}
        declared_by_profile: dict[str, list[ActionCoverage]] = {}
        disabled_by_profile: dict[str, int] = {}
        parent_by_profile: dict[str, str] = {}
        invalid_profile_reason: dict[str, str] = {}

        for raw_name in sorted(domain_profiles, key=lambda value: str(value).casefold()):
            name = str(raw_name)
            raw_profile = domain_profiles.get(raw_name)
            if not isinstance(raw_profile, Mapping):
                declared_by_profile[name] = []
                disabled_by_profile[name] = 0
                parent_by_profile[name] = ""
                invalid_profile_reason[name] = "profile is not an object"
                continue

            parent_by_profile[name] = str(raw_profile.get("extends") or "").strip()
            raw_actions = raw_profile.get("actions")
            actions = raw_actions if isinstance(raw_actions, list) else []
            declared: list[ActionCoverage] = []
            disabled = 0
            for index, raw_action in enumerate(actions):
                if not isinstance(raw_action, Mapping):
                    row = ActionCoverage(
                        domain,
                        name,
                        index,
                        "",
                        "invalid",
                        (),
                        "action is not an object",
                    )
                    declared.append(row)
                    action_rows.append(row)
                    continue
                parsed = OBSAction.from_mapping(raw_action)
                if not parsed.enabled:
                    disabled += 1
                    continue
                row = _classify_action(
                    domain=domain,
                    profile=name,
                    action_index=index,
                    raw_action=raw_action,
                )
                assert row is not None
                declared.append(row)
                action_rows.append(row)
            declared_by_profile[name] = declared
            disabled_by_profile[name] = disabled

        resolved_cache: dict[str, tuple[list[ActionCoverage], int, str]] = {}

        for raw_name in sorted(domain_profiles, key=lambda value: str(value).casefold()):
            name = str(raw_name)
            effective, inherited_count, resolution_error = _resolve_effective_profile_actions(
                name,
                declared_by_profile=declared_by_profile,
                parent_by_profile=parent_by_profile,
                invalid_profile_reason=invalid_profile_reason,
                resolved_cache=resolved_cache,
            )
            if resolution_error:
                profile_rows.append(
                    ProfileCoverage(
                        domain,
                        name,
                        "invalid",
                        len(declared_by_profile.get(name, [])),
                        disabled_by_profile.get(name, 0),
                        0,
                        (resolution_error,),
                    )
                )
                continue

            reasons = tuple(
                dict.fromkeys(
                    item.reason
                    for item in effective
                    if item.reason
                )
            )
            profile_rows.append(
                ProfileCoverage(
                    domain,
                    name,
                    _profile_classification(effective),
                    len(declared_by_profile.get(name, [])),
                    disabled_by_profile.get(name, 0),
                    inherited_count,
                    reasons,
                )
            )

    layouts_root = config.get("layout_profiles")
    layouts = layouts_root if isinstance(layouts_root, Mapping) else {}
    for profile_name in sorted(layouts, key=lambda value: str(value).casefold()):
        profile_rows.append(
            ProfileCoverage(
                "layout",
                str(profile_name),
                "delegated",
                0,
                0,
                0,
                ("owned by OBSLayoutManager",),
            )
        )

    return MigrationCoverageReport(
        actions=tuple(action_rows),
        profiles=tuple(profile_rows),
    )
