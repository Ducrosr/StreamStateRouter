from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    channel: str
    type: str
    platform: str
    emitted_at: float
    payload: Mapping[str, Any]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "channel": self.channel,
            "type": self.type,
            "platform": self.platform,
            "emitted_at": self.emitted_at,
            "payload": dict(self.payload),
        }

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        channel: str,
        type: str,
        platform: str,
        emitted_at: float,
        payload: Mapping[str, Any] | None = None,
    ) -> "EventEnvelope":
        return cls(
            sequence=int(sequence),
            channel=str(channel),
            type=str(type),
            platform=str(platform),
            emitted_at=float(emitted_at),
            payload=MappingProxyType(dict(payload or {})),
        )
