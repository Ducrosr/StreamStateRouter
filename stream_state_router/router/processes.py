from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Callable


TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class WindowsRunningProcessProvider:
    """Read running executable names through Toolhelp32 with a short cache."""

    def __init__(
        self,
        *,
        cache_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError(
                "WindowsRunningProcessProvider is only available on Windows"
            )
        self.cache_seconds = max(0.05, float(cache_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._cached_at = 0.0
        self._cached_names: frozenset[str] = frozenset()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def names(self) -> frozenset[str]:
        now = self._clock()
        with self._lock:
            if self._cached_at and now - self._cached_at < self.cache_seconds:
                return self._cached_names

        names = self._snapshot_names()
        with self._lock:
            self._cached_at = now
            self._cached_names = names
        return names

    def _snapshot_names(self) -> frozenset[str]:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(
            TH32CS_SNAPPROCESS,
            0,
        )
        if snapshot in (None, 0) or int(snapshot) == int(INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            raise OSError(error, "CreateToolhelp32Snapshot failed")

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        names: set[str] = set()
        try:
            ok = bool(self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while ok:
                name = str(entry.szExeFile or "").strip().casefold()
                if name:
                    names.add(name)
                ok = bool(
                    self._kernel32.Process32NextW(snapshot, ctypes.byref(entry))
                )
        finally:
            self._kernel32.CloseHandle(snapshot)
        return frozenset(names)
