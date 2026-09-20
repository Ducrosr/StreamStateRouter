from __future__ import annotations

import ctypes
import unittest
from ctypes import wintypes

from stream_state_router.router.foreground import (
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
        )

        _configure_user32(dll)

        self.assertEqual(dll.GetForegroundWindow.argtypes, [])
        self.assertIs(dll.GetForegroundWindow.restype, wintypes.HWND)
        self.assertEqual(dll.GetWindowThreadProcessId.argtypes[0], wintypes.HWND)
        self.assertEqual(dll.GetWindowTextW.argtypes[-1], ctypes.c_int)

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
