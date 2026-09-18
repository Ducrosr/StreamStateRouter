from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .models import ForegroundApp


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowsForegroundProvider:
    """Read the real foreground HWND/PID/executable using Win32 only."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("WindowsForegroundProvider is only available on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def get(self) -> ForegroundApp | None:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None

        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return None

        title = self._window_title(hwnd)
        path = self._process_path(pid.value)
        exe = os.path.basename(path) if path else ""
        return ForegroundApp(
            hwnd=hwnd,
            pid=int(pid.value),
            exe_name=exe,
            process_path=path,
            window_title=title,
        )

    def _window_title(self, hwnd: int) -> str:
        length = int(self._user32.GetWindowTextLengthW(wintypes.HWND(hwnd)) or 0)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self._user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, len(buffer))
        return buffer.value

    def _process_path(self, pid: int) -> str:
        process = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = self._kernel32.QueryFullProcessImageNameW(
                process,
                0,
                buffer,
                ctypes.byref(size),
            )
            return buffer.value if ok else ""
        finally:
            self._kernel32.CloseHandle(process)
