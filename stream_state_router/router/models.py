from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ForegroundApp:
    hwnd: int
    pid: int
    exe_name: str
    process_path: str = ""
    window_title: str = ""

    @property
    def normalized_exe(self) -> str:
        return self.exe_name.casefold().strip()

    @property
    def normalized_path(self) -> str:
        return str(Path(self.process_path)).casefold() if self.process_path else ""


@dataclass(frozen=True, slots=True)
class StreamState:
    game: str = "Vanilla"
    overlay_profile: str = "Vanilla"
    capture_profile: str = "Default"
    audio_profile: str = "Default"
    layout_profile: str = "Vanilla"
    metadata: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "StreamState":
        values = values or {}
        metadata = values.get("metadata")
        return cls(
            game=str(values.get("Game", values.get("game", "Vanilla"))),
            overlay_profile=str(
                values.get("OverlayProfile", values.get("overlay_profile", "Vanilla"))
            ),
            capture_profile=str(
                values.get("CaptureProfile", values.get("capture_profile", "Default"))
            ),
            audio_profile=str(
                values.get("AudioProfile", values.get("audio_profile", "Default"))
            ),
            layout_profile=str(
                values.get("LayoutProfile", values.get("layout_profile", "Vanilla"))
            ),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def as_variables(self) -> dict[str, str]:
        return {
            "Game": self.game,
            "OverlayProfile": self.overlay_profile,
            "CaptureProfile": self.capture_profile,
            "AudioProfile": self.audio_profile,
            "LayoutProfile": self.layout_profile,
            **dict(self.metadata),
        }

    def profile_name(self, domain: str) -> str:
        mapping = {
            "game": self.game,
            "overlay": self.overlay_profile,
            "capture": self.capture_profile,
            "audio": self.audio_profile,
            "layout": self.layout_profile,
        }
        return mapping[domain]
