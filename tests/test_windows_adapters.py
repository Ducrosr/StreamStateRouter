from __future__ import annotations

import ctypes
import os
import unittest
from ctypes import wintypes
from unittest.mock import patch

from stream_state_router.router.foreground import (
    WindowsForegroundProvider,
    _configure_kernel32 as configure_foreground_kernel32,
    _configure_user32,
)
from stream_state_router.services.single_instance import (
    _configure_kernel32 as configure_mutex_kernel32,
)


class FakeFunction:
    def __init__(self):
        self.argtypes = None
        self.restype = None


class FakeDLL:
    def __init__(self, *names: str):
        for name in names:
            setattr(self, name, FakeFunction())


class WindowsAdapterSignatureTests(unittest.TestCase):
    def test_foreground_user32_signatures_are_explicit(self):
        dll = FakeDLL(
            "GetForegroundWindow",
            "GetWindowThreadProcessId",
            "GetWindowTextLengthW",
            "GetWindowTextW",
            "GetClassNameW",
        )

        _configure_user32(dll)

        self.assertEqual(dll.GetForegroundWindow.argtypes, [])
        self.assertIs(dll.GetForegroundWindow.restype, wintypes.HWND)
        self.assertEqual(dll.GetWindowThreadProcessId.argtypes[0], wintypes.HWND)
        self.assertEqual(dll.GetWindowTextW.argtypes[-1], ctypes.c_int)
        self.assertEqual(dll.GetClassNameW.argtypes[0], wintypes.HWND)
        self.assertEqual(dll.GetClassNameW.argtypes[-1], ctypes.c_int)

    def test_foreground_provider_ignores_its_own_process_before_reading_title(self):
        class FakeUser32:
            def GetForegroundWindow(self):
                return 123

            def GetWindowThreadProcessId(self, _hwnd, pid_ptr):
                pid_ptr._obj.value = os.getpid()
                return 1

        provider = WindowsForegroundProvider.__new__(WindowsForegroundProvider)
        provider._user32 = FakeUser32()
        provider._kernel32 = object()
        provider._window_title = lambda _hwnd: self.fail("self title must not be read")
        provider._process_path = lambda _pid: self.fail("self path must not be read")

        with patch("stream_state_router.router.foreground.os.getpid", return_value=os.getpid()):
            self.assertIsNone(provider.get())

    def test_foreground_kernel32_signatures_are_explicit(self):
        dll = FakeDLL("OpenProcess", "QueryFullProcessImageNameW", "CloseHandle")

        configure_foreground_kernel32(dll)

        self.assertIs(dll.OpenProcess.restype, wintypes.HANDLE)
        self.assertIs(dll.QueryFullProcessImageNameW.restype, wintypes.BOOL)
        self.assertEqual(dll.CloseHandle.argtypes, [wintypes.HANDLE])

    def test_single_instance_mutex_signatures_are_explicit(self):
        dll = FakeDLL("CreateMutexW", "CloseHandle")

        configure_mutex_kernel32(dll)

        self.assertEqual(
            dll.CreateMutexW.argtypes,
            [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR],
        )
        self.assertIs(dll.CreateMutexW.restype, wintypes.HANDLE)


if __name__ == "__main__":
    unittest.main()
