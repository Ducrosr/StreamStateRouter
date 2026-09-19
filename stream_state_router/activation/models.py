from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ActivationPhase(str, Enum):
    IDLE = "idle"
    ELIGIBLE = "eligible"
    VISIBLE = "visible"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True)
class TriggerTargetConfig:
    container: str
    source: str
    enabled: bool = True
    weight: float = 1.0
    duration_seconds: float | None = None
    container_kind: str = "scene"
    path: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TriggerTargetConfig":
        duration_raw = raw.get("duration_seconds")
        duration = None if duration_raw in (None, "") else float(duration_raw)
        path_raw = raw.get("path")
        path = tuple(str(item) for item in path_raw) if isinstance(path_raw, (list, tuple)) else ()
        return cls(
            container=str(raw.get("container") or "").strip(),
            source=str(raw.get("source") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            weight=float(raw.get("weight", 1.0)),
            duration_seconds=duration,
            container_kind=str(raw.get("container_kind") or "scene").strip() or "scene",
            path=path,
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "container": self.container,
            "source": self.source,
            "enabled": self.enabled,
            "weight": self.weight,
            "container_kind": self.container_kind,
            "path": list(self.path),
        }
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


@dataclass(frozen=True, slots=True)
class TriggerPolicyConfig:
    module_source: str
    enabled: bool = True
    type: str = "random"
    active_when: str = "module_in_program_scene"
    chance: float = 0.01
    interval_seconds: float = 60.0
    cooldown_seconds: float = 600.0
    default_duration_seconds: float = 10.0
    exclusive: bool = True
    avoid_immediate_repeat: bool = True
    targets: tuple[TriggerTargetConfig, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        module_source: str,
        raw: Mapping[str, Any],
    ) -> "TriggerPolicyConfig":
        targets_raw = raw.get("targets")
        targets = tuple(
            TriggerTargetConfig.from_mapping(item)
            for item in targets_raw or ()
            if isinstance(item, Mapping)
        )
        return cls(
            module_source=str(module_source).strip(),
            enabled=bool(raw.get("enabled", True)),
            type=str(raw.get("type") or "random").strip().casefold(),
            active_when=str(raw.get("active_when") or "module_in_program_scene").strip().casefold(),
            chance=float(raw.get("chance", 0.01)),
            interval_seconds=float(raw.get("interval_seconds", 60.0)),
            cooldown_seconds=float(raw.get("cooldown_seconds", 600.0)),
            default_duration_seconds=float(raw.get("default_duration_seconds", 10.0)),
            exclusive=bool(raw.get("exclusive", True)),
            avoid_immediate_repeat=bool(raw.get("avoid_immediate_repeat", True)),
            targets=targets,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "type": self.type,
            "active_when": self.active_when,
            "chance": self.chance,
            "interval_seconds": self.interval_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "default_duration_seconds": self.default_duration_seconds,
            "exclusive": self.exclusive,
            "avoid_immediate_repeat": self.avoid_immediate_repeat,
            "targets": [target.to_mapping() for target in self.targets],
        }


@dataclass(slots=True)
class ActivationRuntimeState:
    phase: ActivationPhase = ActivationPhase.IDLE
    next_roll_at: float | None = None
    visible_until: float | None = None
    cooldown_until: float | None = None
    active_source: str = ""
    last_source: str = ""
    last_trigger_at: float | None = None


@dataclass(frozen=True, slots=True)
class ActivationEvent:
    kind: str
    policy: str
    at: float
    source: str = ""
    roll: float | None = None
    chance: float | None = None
    duration_seconds: float | None = None
    cooldown_seconds: float | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RollTestResult:
    policy: str
    roll: float
    chance: float
    triggered: bool
    source: str = ""
