from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Mapping

from .paths import control_state_path


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {
    "Game",
    "OverlayProfile",
    "CaptureProfile",
    "AudioProfile",
    "LayoutProfile",
}


def validate_control_variable_name(name: str) -> str:
    value = str(name or "").strip()
    if not _NAME_RE.fullmatch(value):
        raise ValueError(
            "Nom de variable invalide : utilisez lettres, chiffres et underscore, "
            "sans commencer par un chiffre."
        )
    if value in _RESERVED:
        raise ValueError(
            f"Variable réservée par l'état logique SSR : {value}"
        )
    return value


class ControlVariableStore:
    """Small persistent string-variable store for local runtime controls."""

    def __init__(
        self,
        defaults: Mapping[str, object] | None = None,
        *,
        path: str | Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else None
        self._values: dict[str, str] = {}
        for raw_name, raw_value in (defaults or {}).items():
            name = validate_control_variable_name(str(raw_name))
            self._values[name] = str(raw_value)
        self._load()

    @classmethod
    def persistent(
        cls,
        defaults: Mapping[str, object] | None = None,
    ) -> "ControlVariableStore":
        return cls(defaults, path=control_state_path())

    def _load(self) -> None:
        path = self._path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        raw = payload.get("variables") if isinstance(payload, Mapping) else None
        if not isinstance(raw, Mapping):
            return
        with self._lock:
            for raw_name, raw_value in raw.items():
                try:
                    name = validate_control_variable_name(str(raw_name))
                except ValueError:
                    continue
                if isinstance(raw_value, str):
                    self._values[name] = raw_value

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._values)

    def set(self, name: str, value: object) -> bool:
        key = validate_control_variable_name(name)
        text = str(value)
        if len(text) > 4096:
            raise ValueError("Valeur de variable trop longue (4096 caractères max).")
        with self._lock:
            changed = self._values.get(key) != text
            self._values[key] = text
            if changed:
                self._save_locked()
            return changed

    def _save_locked(self) -> None:
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(
                json.dumps(
                    {"variables": self._values},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
