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

    def matches(self, app: ForegroundApp, context: Mapping[str, Any] | None = None) -> bool:
        if not self.enabled:
            return False

        matched_any_selector = False
        if self.exe:
            matched_any_selector = True
            if not fnmatch.fnmatchcase(app.normalized_exe, self.exe.casefold().strip()):
                return False
        if self.path:
            matched_any_selector = True
            candidate = app.normalized_path
            wanted = str(Path(self.path)).casefold()
            if not candidate or not fnmatch.fnmatchcase(candidate, wanted):
                return False
        if self.title_regex:
            matched_any_selector = True
            try:
                matched = re.search(self.title_regex, app.window_title, flags=re.IGNORECASE)
            except re.error:
                return False
            if matched is None:
                return False

        if not matched_any_selector:
            return False
        return self._conditions_match(context or {})

    def _conditions_match(self, context: Mapping[str, Any]) -> bool:
        if not self.conditions:
            return True
        if "streaming" in self.conditions:
            if context.get("streaming") is None or bool(context.get("streaming")) != bool(self.conditions["streaming"]):
                return False
        if "recording" in self.conditions:
            if context.get("recording") is None or bool(context.get("recording")) != bool(self.conditions["recording"]):
                return False
        wanted_scene = str(self.conditions.get("program_scene") or "").strip()
        if wanted_scene and str(context.get("program_scene") or "") != wanted_scene:
            return False
        if bool(self.conditions.get("obs_enabled", False)) and not bool(context.get("obs_enabled", False)):
            return False
        return True


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

    def resolve(
        self,
        app: ForegroundApp | None,
        context: Mapping[str, Any] | None = None,
    ) -> RuleResolution:
        if app is None:
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
        return RuleResolution(ResolutionKind.FALLBACK, self.fallback, "fallback")
