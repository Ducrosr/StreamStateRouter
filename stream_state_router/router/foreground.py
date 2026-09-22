from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .models import ForegroundApp


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _configure_user32(user32) -> None:
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int


def _configure_kernel32(kernel32) -> None:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsForegroundProvider:
    """Read the real foreground HWND/PID/executable using Win32 only."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("WindowsForegroundProvider is only available on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_user32(self._user32)
        _configure_kernel32(self._kernel32)

    def get(self) -> ForegroundApp | None:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None

        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return None

        # Never synchronously query the title of SSR's own Qt window from the
        # routing worker. GetWindowText* may send WM_GETTEXT(LENGTH) to a
        # same-process window and wait for its UI thread. During save/restart the
        # UI thread is synchronously joining this worker, which can otherwise
        # create a circular wait until the join timeout expires.
        if int(pid.value) == os.getpid():
            return None

        title = self._window_title(hwnd)
        window_class = self._window_class(hwnd)
        path = self._process_path(pid.value)
        exe = os.path.basename(path) if path else ""
        return ForegroundApp(
            hwnd=hwnd,
            pid=int(pid.value),
            exe_name=exe,
            process_path=path,
            window_title=title,
            window_class=window_class,
        )

    def _window_title(self, hwnd: int) -> str:
        length = int(self._user32.GetWindowTextLengthW(wintypes.HWND(hwnd)) or 0)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self._user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, len(buffer))
        return buffer.value

    def _window_class(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        copied = int(
            self._user32.GetClassNameW(
                wintypes.HWND(hwnd),
                buffer,
                len(buffer),
            )
            or 0
        )
        return buffer.value if copied else ""

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
