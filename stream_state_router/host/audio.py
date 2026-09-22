from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence


_ALLOWED_ROLES = {"0", "1", "2", "all"}


class SoundVolumeViewAudioRouter:
    """Route one application's default audio endpoint through SoundVolumeView.

    Windows exposes per-application routing in Settings but does not publish a
    stable desktop API for assigning that preference. SoundVolumeView is kept
    behind this narrow adapter so SSR can replace the backend later without
    changing profile/action semantics.
    """

    def __init__(
        self,
        executable_path: str = "",
        *,
        timeout_seconds: float = 5.0,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable_path = str(executable_path or "").strip()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._runner = runner

    def resolve_executable(self) -> str:
        configured = os.path.expandvars(os.path.expanduser(self.executable_path))
        if configured:
            path = Path(configured)
            if path.is_file():
                return str(path)
            raise FileNotFoundError(
                f"SoundVolumeView introuvable : {path}"
            )
        discovered = shutil.which("SoundVolumeView.exe") or shutil.which(
            "SoundVolumeView"
        )
        if discovered:
            return discovered
        raise FileNotFoundError(
            "SoundVolumeView est introuvable. Configurez son chemin dans "
            "Paramètres > Contrôle Windows."
        )

    def set_app_default(
        self,
        *,
        device: str,
        process: str,
        roles: str = "all",
    ) -> None:
        endpoint = str(device or "").strip()
        process_name = str(process or "").strip()
        role = str(roles or "all").strip().casefold()
        if not endpoint:
            raise ValueError("app_audio_output requiert params.device")
        if not process_name:
            raise ValueError("app_audio_output requiert params.process")
        if role not in _ALLOWED_ROLES:
            raise ValueError(
                "app_audio_output params.roles doit être 0, 1, 2 ou all"
            )

        command: Sequence[str] = (
            self.resolve_executable(),
            "/SetAppDefault",
            endpoint,
            role,
            process_name,
        )
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        completed = self._runner(
            list(command),
            check=False,
            timeout=self.timeout_seconds,
            creationflags=creationflags,
            capture_output=True,
            text=True,
        )
        if int(getattr(completed, "returncode", 0)) != 0:
            stderr = str(getattr(completed, "stderr", "") or "").strip()
            stdout = str(getattr(completed, "stdout", "") or "").strip()
            detail = stderr or stdout or f"code {completed.returncode}"
            raise RuntimeError(f"SoundVolumeView /SetAppDefault a échoué : {detail}")
