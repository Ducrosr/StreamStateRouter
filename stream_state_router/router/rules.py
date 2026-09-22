from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ForegroundApp, StreamState


class ResolutionKind(str, Enum):
    MATCH = "match"
    IGNORE = "ignore"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class RuleResolution:
    kind: ResolutionKind
    state: StreamState | None
    rule_name: str
    apply_delay_ms: int = 0


@dataclass(frozen=True, slots=True)
class RuleCheck:
    name: str
    priority: int
    behavior: str
    matched: bool
    reason: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "priority": self.priority,
            "behavior": self.behavior,
            "matched": self.matched,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RuleExplanation:
    resolution: RuleResolution
    checks: tuple[RuleCheck, ...]

    def as_mapping(self) -> dict[str, object]:
        state = self.resolution.state
        return {
            "kind": self.resolution.kind.value,
            "rule_name": self.resolution.rule_name,
            "apply_delay_ms": self.resolution.apply_delay_ms,
            "state": state.as_variables() if state is not None else None,
            "checks": [item.as_mapping() for item in self.checks],
        }


@dataclass(frozen=True, slots=True)
class AppRule:
    name: str
    state: StreamState | None = None
    priority: int = 0
    exe: str = ""
    path: str = ""
    title_regex: str = ""
    enabled: bool = True
    behavior: ResolutionKind = ResolutionKind.MATCH
    conditions: Mapping[str, Any] = field(default_factory=dict)
    apply_delay_ms: int = 0

    def matches(
        self,
        app: ForegroundApp | None,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        return self.match_details(app, context)[0]

    def match_details(
        self,
        app: ForegroundApp | None,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if not self.enabled:
            return False, "règle désactivée"

        matched_any_selector = False
        if self.exe:
            matched_any_selector = True
            if app is None:
                return False, "aucune fenêtre foreground pour tester exe"
            wanted_exe = self.exe.casefold().strip()
            if not fnmatch.fnmatchcase(app.normalized_exe, wanted_exe):
                return False, f"exe ne correspond pas à {self.exe}"
        if self.path:
            matched_any_selector = True
            if app is None:
                return False, "aucune fenêtre foreground pour tester le chemin"
            candidate = app.normalized_path
            wanted = str(Path(self.path)).casefold()
            if not candidate or not fnmatch.fnmatchcase(candidate, wanted):
                return False, f"chemin ne correspond pas à {self.path}"
        if self.title_regex:
            matched_any_selector = True
            if app is None:
                return False, "aucune fenêtre foreground pour tester le titre"
            try:
                matched = re.search(self.title_regex, app.window_title, flags=re.IGNORECASE)
            except re.error as exc:
                return False, f"regex titre invalide : {exc}"
            if matched is None:
                return False, f"titre ne correspond pas à /{self.title_regex}/"

        process_selector = str(
            self.conditions.get("process_running") or ""
        ).strip()
        if not matched_any_selector and not process_selector:
            return False, "aucun sélecteur exe/chemin/titre/process_running"
        conditions_ok, condition_reason = self._conditions_result(context or {})
        if not conditions_ok:
            return False, condition_reason
        return True, "sélecteurs et conditions satisfaits"

    def _conditions_match(self, context: Mapping[str, Any]) -> bool:
        return self._conditions_result(context)[0]

    def _conditions_result(self, context: Mapping[str, Any]) -> tuple[bool, str]:
        if not self.conditions:
            return True, "aucune condition OBS"
        if "streaming" in self.conditions:
            actual = context.get("streaming")
            wanted = bool(self.conditions["streaming"])
            if actual is None:
                return False, "état streaming inconnu"
            if bool(actual) != wanted:
                return False, f"streaming={bool(actual)} attendu={wanted}"
        if "recording" in self.conditions:
            actual = context.get("recording")
            wanted = bool(self.conditions["recording"])
            if actual is None:
                return False, "état enregistrement inconnu"
            if bool(actual) != wanted:
                return False, f"recording={bool(actual)} attendu={wanted}"
        wanted_scene = str(self.conditions.get("program_scene") or "").strip()
        if wanted_scene and str(context.get("program_scene") or "") != wanted_scene:
            return False, f"scène programme différente de {wanted_scene}"
        wanted_process = str(
            self.conditions.get("process_running") or ""
        ).strip()
        if wanted_process:
            running = context.get("running_processes")
            if running is None or isinstance(running, (str, bytes)):
                return False, "liste des processus actifs inconnue"
            try:
                names = {
                    str(item).strip().casefold()
                    for item in running
                    if str(item).strip()
                }
            except TypeError:
                return False, "liste des processus actifs invalide"
            if wanted_process.casefold() not in names:
                return False, f"processus absent : {wanted_process}"
        if bool(self.conditions.get("obs_enabled", False)) and not bool(context.get("obs_enabled", False)):
            return False, "pilotage OBS requis"
        return True, "conditions OBS satisfaites"


class RuleSet:
    def __init__(self, rules: Iterable[AppRule], fallback: StreamState | None = None):
        self._rules = tuple(sorted(rules, key=lambda item: item.priority, reverse=True))
        self.fallback = fallback or StreamState()

    @property
    def rules(self) -> tuple[AppRule, ...]:
        return self._rules

    @property
    def needs_context(self) -> bool:
        return any(bool(rule.conditions) for rule in self._rules if rule.enabled)

    @property
    def needs_process_context(self) -> bool:
        return any(
            bool(str(rule.conditions.get("process_running") or "").strip())
            for rule in self._rules
            if rule.enabled
        )

    def resolve(
        self,
        app: ForegroundApp | None,
        context: Mapping[str, Any] | None = None,
    ) -> RuleResolution:
        if app is None and not self.needs_process_context:
            return RuleResolution(ResolutionKind.IGNORE, None, "no_foreground")
        for rule in self._rules:
            if not rule.matches(app, context):
                continue
            if rule.behavior is ResolutionKind.IGNORE:
                return RuleResolution(ResolutionKind.IGNORE, None, rule.name)
            return RuleResolution(
                ResolutionKind.MATCH,
                rule.state or self.fallback,
                rule.name,
                max(0, int(rule.apply_delay_ms)),
            )
        if app is None:
            return RuleResolution(ResolutionKind.IGNORE, None, "no_foreground")
        return RuleResolution(ResolutionKind.FALLBACK, self.fallback, "fallback")

    def explain(
        self,
        app: ForegroundApp | None,
        context: Mapping[str, Any] | None = None,
    ) -> RuleExplanation:
        """Explain resolution using the same AppRule matcher as live routing."""
        if app is None and not self.needs_process_context:
            return RuleExplanation(
                RuleResolution(ResolutionKind.IGNORE, None, "no_foreground"),
                (),
            )
        checks: list[RuleCheck] = []
        for rule in self._rules:
            matched, reason = rule.match_details(app, context)
            checks.append(
                RuleCheck(
                    name=rule.name,
                    priority=rule.priority,
                    behavior=rule.behavior.value,
                    matched=matched,
                    reason=reason,
                )
            )
            if not matched:
                continue
            if rule.behavior is ResolutionKind.IGNORE:
                resolution = RuleResolution(ResolutionKind.IGNORE, None, rule.name)
            else:
                resolution = RuleResolution(
                    ResolutionKind.MATCH,
                    rule.state or self.fallback,
                    rule.name,
                    max(0, int(rule.apply_delay_ms)),
                )
            return RuleExplanation(resolution, tuple(checks))
        if app is None:
            return RuleExplanation(
                RuleResolution(ResolutionKind.IGNORE, None, "no_foreground"),
                tuple(checks),
            )
        return RuleExplanation(
            RuleResolution(ResolutionKind.FALLBACK, self.fallback, "fallback"),
            tuple(checks),
        )
