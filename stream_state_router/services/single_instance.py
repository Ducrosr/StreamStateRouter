from __future__ import annotations

import ctypes
import os


class SingleInstanceGuard:
    def __init__(self, name: str = "Local\\StreamStateRouter-v1"):
        self._handle = None
        self.already_running = False
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == 183
        self._kernel32 = kernel32

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
