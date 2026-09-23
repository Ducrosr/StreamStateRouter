from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .audio import SoundVolumeViewAudioRouter
from .hdr import WindowsHDRController


@dataclass(frozen=True, slots=True)
class HostControlConfig:
    soundvolumeview_path: str = ""
    audio_timeout_seconds: float = 5.0


class HostControlController:
    """Execute Windows-host actions requested by existing SSR action profiles."""

    def __init__(
        self,
        config: HostControlConfig | None = None,
        *,
        audio_router: SoundVolumeViewAudioRouter | None = None,
        hdr_controller: WindowsHDRController | None = None,
    ) -> None:
        self.config = config or HostControlConfig()
        self.audio_router = audio_router or SoundVolumeViewAudioRouter(
            self.config.soundvolumeview_path,
            timeout_seconds=self.config.audio_timeout_seconds,
        )
        self.hdr_controller = hdr_controller or WindowsHDRController()

    def execute(self, kind: str, params: Mapping[str, object]) -> bool:
        action = str(kind or "").strip().casefold()
        if action == "app_audio_output":
            self.audio_router.set_app_default(
                device=str(params.get("device") or ""),
                process=str(params.get("process") or ""),
                roles=str(params.get("roles") or "all"),
            )
            return True
        if action == "windows_hdr":
            if "enabled" not in params or not isinstance(params.get("enabled"), bool):
                raise ValueError("windows_hdr requiert params.enabled booléen")
            scope = str(params.get("display") or "primary").strip().casefold()
            if scope not in {"primary", "all"}:
                raise ValueError(
                    "windows_hdr params.display doit être primary ou all"
                )
            self.hdr_controller.set_enabled(
                bool(params["enabled"]),
                scope=scope,
            )
            return True
        return False
