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
    reasons: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "profile": self.profile,
            "classification": self.classification,
            "enabled_actions": self.enabled_actions,
            "disabled_actions": self.disabled_actions,
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
        return "declarative_executable"
    priority = {
        "invalid": 5,
        "legacy_only": 4,
        "declarative_intent_only": 3,
        "declarative_plannable": 2,
        "declarative_executable": 1,
        "delegated": 0,
    }
    return max(actions, key=lambda item: priority[item.classification]).classification


def build_migration_coverage_report(
    config: Mapping[str, Any],
) -> MigrationCoverageReport:
    """Describe declarative migration maturity without OBS I/O or mutations."""

    action_rows: list[ActionCoverage] = []
    profile_rows: list[ProfileCoverage] = []

    profiles_root = config.get("profiles")
    profiles = profiles_root if isinstance(profiles_root, Mapping) else {}

    for domain in ACTION_PROFILE_DOMAINS:
        raw_domain = profiles.get(domain)
        domain_profiles = raw_domain if isinstance(raw_domain, Mapping) else {}
        for profile_name in sorted(domain_profiles, key=lambda value: str(value).casefold()):
            raw_profile = domain_profiles.get(profile_name)
            if not isinstance(raw_profile, Mapping):
                profile_rows.append(
                    ProfileCoverage(
                        domain,
                        str(profile_name),
                        "invalid",
                        0,
                        0,
                        ("profile is not an object",),
                    )
                )
                continue

            raw_actions = raw_profile.get("actions")
            actions = raw_actions if isinstance(raw_actions, list) else []
            profile_actions: list[ActionCoverage] = []
            disabled = 0
            for index, raw_action in enumerate(actions):
                if not isinstance(raw_action, Mapping):
                    row = ActionCoverage(
                        domain,
                        str(profile_name),
                        index,
                        "",
                        "invalid",
                        (),
                        "action is not an object",
                    )
                    action_rows.append(row)
                    profile_actions.append(row)
                    continue

                action = OBSAction.from_mapping(raw_action)
                if not action.enabled:
                    disabled += 1
                    continue

                provenance = f"{domain}:{profile_name}"
                try:
                    assignments = desired_assignments_from_actions(
                        (action,),
                        provenance=provenance,
                    )
                except UnsupportedIntentAction as exc:
                    row = ActionCoverage(
                        domain,
                        str(profile_name),
                        index,
                        action.type,
                        "legacy_only",
                        (),
                        str(exc),
                    )
                except (TypeError, ValueError) as exc:
                    row = ActionCoverage(
                        domain,
                        str(profile_name),
                        index,
                        action.type,
                        "invalid",
                        (),
                        str(exc),
                    )
                else:
                    kinds = {assignment.key.kind for assignment in assignments}
                    classification = _classification_for_property_kinds(kinds)
                    reason = (
                        ""
                        if assignments
                        else "action produced no stable managed property"
                    )
                    row = ActionCoverage(
                        domain,
                        str(profile_name),
                        index,
                        action.type,
                        classification,
                        tuple(sorted(kinds)),
                        reason,
                    )
                action_rows.append(row)
                profile_actions.append(row)

            reasons = tuple(
                dict.fromkeys(
                    item.reason
                    for item in profile_actions
                    if item.reason
                )
            )
            profile_rows.append(
                ProfileCoverage(
                    domain,
                    str(profile_name),
                    _profile_classification(profile_actions),
                    len(profile_actions),
                    disabled,
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
                ("owned by OBSLayoutManager",),
            )
        )

    return MigrationCoverageReport(
        actions=tuple(action_rows),
        profiles=tuple(profile_rows),
    )
