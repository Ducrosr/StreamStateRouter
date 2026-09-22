from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Any

from ..obs.models import OBSAction
from .intent import UnsupportedIntentAction, desired_assignments_from_actions


DECLARATIVE_EXECUTABLE = "declarative_executable"
DECLARATIVE_PLANNABLE = "declarative_plannable"
DECLARATIVE_INTENT_ONLY = "declarative_intent_only"
DELEGATED = "delegated"
LEGACY_ONLY = "legacy_only"
INVALID = "invalid"
DISABLED = "disabled"

_ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")


@dataclass(frozen=True, slots=True)
class ActionCoverage:
    domain: str
    profile: str
    index: int
    action_type: str
    classification: str
    property_kinds: tuple[str, ...] = ()
    reason: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "profile": self.profile,
            "index": self.index,
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
    actions: tuple[ActionCoverage, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "profile": self.profile,
            "classification": self.classification,
            "actions": [item.as_mapping() for item in self.actions],
        }


@dataclass(frozen=True, slots=True)
class MigrationCoverageReport:
    profiles: tuple[ProfileCoverage, ...]

    @property
    def action_rows(self) -> tuple[ActionCoverage, ...]:
        return tuple(
            action
            for profile in self.profiles
            for action in profile.actions
        )

    def _action_counts(self) -> Counter[str]:
        return Counter(
            row.classification
            for row in self.action_rows
            if row.classification != DISABLED
        )

    def _profile_counts(self) -> Counter[str]:
        return Counter(profile.classification for profile in self.profiles)

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if total <= 0:
            return 100.0
        return round((value / total) * 100.0, 1)

    def summary(self) -> dict[str, object]:
        action_counts = self._action_counts()
        profile_counts = self._profile_counts()
        active_actions = sum(action_counts.values())
        represented = sum(
            action_counts.get(name, 0)
            for name in (
                DECLARATIVE_EXECUTABLE,
                DECLARATIVE_PLANNABLE,
                DECLARATIVE_INTENT_ONLY,
            )
        )
        executable = action_counts.get(DECLARATIVE_EXECUTABLE, 0)
        action_profiles = sum(
            count
            for name, count in profile_counts.items()
            if name != DELEGATED
        )
        executable_profiles = profile_counts.get(DECLARATIVE_EXECUTABLE, 0)
        return {
            "actions": {
                "active": active_actions,
                "counts": dict(sorted(action_counts.items())),
                "represented_percent": self._percent(represented, active_actions),
                "executable_percent": self._percent(executable, active_actions),
            },
            "profiles": {
                "total": len(self.profiles),
                "action_profiles": action_profiles,
                "counts": dict(sorted(profile_counts.items())),
                "executable_percent": self._percent(
                    executable_profiles,
                    action_profiles,
                ),
            },
        }

    def as_mapping(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "profiles": [item.as_mapping() for item in self.profiles],
        }


@dataclass(frozen=True, slots=True)
class _EffectiveAction:
    action: OBSAction


def _effective_actions(
    profiles: Mapping[str, object],
    name: str,
    *,
    stack: tuple[str, ...] = (),
) -> tuple[_EffectiveAction, ...]:
    if name in stack:
        raise ValueError(
            "Circular profile inheritance: " + " -> ".join((*stack, name))
        )
    raw = profiles.get(name)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Profile '{name}' is missing")

    rows: list[_EffectiveAction] = []
    parent = str(raw.get("extends") or "").strip()
    if parent:
        rows.extend(
            _effective_actions(
                profiles,
                parent,
                stack=(*stack, name),
            )
        )

    actions = raw.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError(f"Profile '{name}' actions are not a list")
    rows.extend(
        _EffectiveAction(OBSAction.from_mapping(item))
        for item in actions
        if isinstance(item, Mapping)
    )
    return tuple(rows)


def _classify_action(
    *,
    domain: str,
    profile: str,
    index: int,
    action: OBSAction,
    executable_kinds: frozenset[str],
) -> ActionCoverage:
    if not action.enabled:
        return ActionCoverage(
            domain=domain,
            profile=profile,
            index=index,
            action_type=action.type,
            classification=DISABLED,
            reason="Action disabled",
        )

    try:
        assignments = desired_assignments_from_actions(
            (action,),
            provenance=f"{domain}:{profile}",
        )
    except UnsupportedIntentAction as exc:
        return ActionCoverage(
            domain=domain,
            profile=profile,
            index=index,
            action_type=action.type,
            classification=LEGACY_ONLY,
            reason=str(exc),
        )
    except (TypeError, ValueError) as exc:
        return ActionCoverage(
            domain=domain,
            profile=profile,
            index=index,
            action_type=action.type,
            classification=INVALID,
            reason=str(exc),
        )

    kinds = tuple(sorted({item.key.kind for item in assignments}))
    if not assignments:
        return ActionCoverage(
            domain=domain,
            profile=profile,
            index=index,
            action_type=action.type,
            classification=DECLARATIVE_INTENT_ONLY,
            reason="Action produces no managed property",
        )

    if all(kind in executable_kinds for kind in kinds):
        classification = DECLARATIVE_EXECUTABLE
        reason = ""
    else:
        classification = DECLARATIVE_PLANNABLE
        reason = (
            "Managed property is not enabled in the guarded executor: "
            + ", ".join(kind for kind in kinds if kind not in executable_kinds)
        )
    return ActionCoverage(
        domain=domain,
        profile=profile,
        index=index,
        action_type=action.type,
        classification=classification,
        property_kinds=kinds,
        reason=reason,
    )


def _profile_classification(actions: tuple[ActionCoverage, ...]) -> str:
    active = [
        item.classification
        for item in actions
        if item.classification != DISABLED
    ]
    if not active:
        return DECLARATIVE_EXECUTABLE
    if INVALID in active:
        return INVALID
    if LEGACY_ONLY in active:
        return LEGACY_ONLY
    if DECLARATIVE_INTENT_ONLY in active:
        return DECLARATIVE_INTENT_ONLY
    if DECLARATIVE_PLANNABLE in active:
        return DECLARATIVE_PLANNABLE
    return DECLARATIVE_EXECUTABLE


def build_migration_coverage(
    config: Mapping[str, Any],
    *,
    executable_kinds: Iterable[str],
) -> MigrationCoverageReport:
    """Classify configured OBS profiles without performing any OBS I/O.

    The report reuses the declarative intent adapter to decide whether a legacy
    action has a stable DesiredState representation. Executor support is supplied
    by the caller so planning remains independent from the mutation service.
    """

    executable = frozenset(str(item) for item in executable_kinds)
    profiles_raw = config.get("profiles", {})
    profiles = profiles_raw if isinstance(profiles_raw, Mapping) else {}

    rows: list[ProfileCoverage] = []
    for domain in _ACTION_PROFILE_DOMAINS:
        domain_raw = profiles.get(domain, {})
        domain_profiles = (
            domain_raw
            if isinstance(domain_raw, Mapping)
            else {}
        )
        for profile_name in sorted(domain_profiles, key=lambda value: str(value).casefold()):
            name = str(profile_name)
            try:
                effective = _effective_actions(domain_profiles, name)
                actions = tuple(
                    _classify_action(
                        domain=domain,
                        profile=name,
                        index=index,
                        action=row.action,
                        executable_kinds=executable,
                    )
                    for index, row in enumerate(effective)
                )
                classification = _profile_classification(actions)
            except (TypeError, ValueError) as exc:
                actions = ()
                classification = INVALID
                actions = (
                    ActionCoverage(
                        domain=domain,
                        profile=name,
                        index=0,
                        action_type="",
                        classification=INVALID,
                        reason=str(exc),
                    ),
                )
            rows.append(
                ProfileCoverage(
                    domain=domain,
                    profile=name,
                    classification=classification,
                    actions=actions,
                )
            )

    layouts_raw = config.get("layout_profiles", {})
    layouts = layouts_raw if isinstance(layouts_raw, Mapping) else {}
    for name in sorted(layouts, key=lambda value: str(value).casefold()):
        rows.append(
            ProfileCoverage(
                domain="layout",
                profile=str(name),
                classification=DELEGATED,
                actions=(),
            )
        )

    return MigrationCoverageReport(tuple(rows))


def render_migration_coverage(report: MigrationCoverageReport) -> str:
    summary = report.summary()
    actions = summary["actions"]
    profiles = summary["profiles"]
    assert isinstance(actions, Mapping)
    assert isinstance(profiles, Mapping)

    lines = [
        "Couverture de migration déclarative",
        (
            "Actions actives : "
            f"{actions['active']} · représentées "
            f"{actions['represented_percent']}% · exécutables "
            f"{actions['executable_percent']}%"
        ),
        (
            "Profils : "
            f"{profiles['total']} · profils d'actions "
            f"{profiles['action_profiles']} · exécutables "
            f"{profiles['executable_percent']}%"
        ),
    ]

    action_counts = actions.get("counts", {})
    if isinstance(action_counts, Mapping) and action_counts:
        lines.append(
            "Actions par statut : "
            + ", ".join(
                f"{name}={count}"
                for name, count in action_counts.items()
            )
        )

    profile_counts = profiles.get("counts", {})
    if isinstance(profile_counts, Mapping) and profile_counts:
        lines.append(
            "Profils par statut : "
            + ", ".join(
                f"{name}={count}"
                for name, count in profile_counts.items()
            )
        )

    notable = [
        row
        for row in report.action_rows
        if row.classification
        not in {DECLARATIVE_EXECUTABLE, DISABLED}
    ]
    if notable:
        lines.append("")
        lines.append("Actions non exécutables actuellement :")
        for row in notable:
            properties = (
                " [" + ", ".join(row.property_kinds) + "]"
                if row.property_kinds
                else ""
            )
            reason = f" — {row.reason}" if row.reason else ""
            lines.append(
                f"- {row.domain}/{row.profile} #{row.index + 1} "
                f"{row.action_type}: {row.classification}"
                f"{properties}{reason}"
            )

    delegated = [
        row
        for row in report.profiles
        if row.classification == DELEGATED
    ]
    if delegated:
        lines.append("")
        lines.append(
            "Délégués à un sous-système spécialisé : "
            + ", ".join(
                f"{row.domain}/{row.profile}"
                for row in delegated
            )
        )

    return "\n".join(lines)
