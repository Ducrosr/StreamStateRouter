from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "StreamStateRouter"


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return user_data_dir() / "config.json"


def control_state_path() -> Path:
    return user_data_dir() / "control-state.json"


def logs_dir() -> Path:
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir() -> Path:
    path = user_data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return bundled_root() / "config" / "default.json"
