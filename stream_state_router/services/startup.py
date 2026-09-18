from __future__ import annotations

import os
import sys
from pathlib import Path

APP_RUN_NAME = "StreamStateRouter"


def startup_command() -> str:
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return f'"{exe}" --minimized'
    main = Path(__file__).resolve().parents[2] / "main.py"
    return f'"{exe}" "{main}" --minimized'


def is_startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            value, _ = winreg.QueryValueEx(key, APP_RUN_NAME)
            return bool(value)
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("Le démarrage Windows n'est disponible que sous Windows")
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_RUN_NAME)
            except FileNotFoundError:
                pass
