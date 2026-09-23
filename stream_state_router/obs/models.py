from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class OBSAction:
    type: str
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    name: str = ""
    preapply_on_launcher: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OBSAction":
        params = raw.get("params")
        return cls(
            type=str(raw.get("type") or "").strip(),
            params=dict(params) if isinstance(params, Mapping) else {},
            enabled=bool(raw.get("enabled", True)),
            name=str(raw.get("name") or ""),
            preapply_on_launcher=bool(
                raw.get("preapply_on_launcher", False)
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "enabled": self.enabled,
            "preapply_on_launcher": self.preapply_on_launcher,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class OBSProfile:
    name: str
    actions: tuple[OBSAction, ...] = ()
    extends: str = ""
    conditions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OBSConnectionConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 4455
    password: str = ""
    timeout_seconds: float = 2.0
    reconnect_seconds: float = 3.0
