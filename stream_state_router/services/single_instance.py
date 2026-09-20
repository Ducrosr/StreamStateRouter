from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183


def _configure_kernel32(kernel32) -> None:
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstanceGuard:
    def __init__(self, name: str = "Local\\StreamStateRouter-v1"):
        self._handle = None
        self.already_running = False
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_kernel32(kernel32)
        self._handle = kernel32.CreateMutexW(None, False, name)
        if not self._handle:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateMutexW a échoué")
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        self._kernel32 = kernel32

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
