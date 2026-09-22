from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


ERROR_SUCCESS = 0
QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10
MONITOR_DEFAULTTOPRIMARY = 1


class LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [
        ("Numerator", wintypes.UINT),
        ("Denominator", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", ctypes.c_int),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.UINT),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # DISPLAYCONFIG_MODE_INFO is 16-byte header + a 48-byte union.
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", LUID),
        ("modeInfo", ctypes.c_byte * 48),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", LUID),
        ("id", wintypes.UINT),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", ctypes.c_uint16 * 32),
    ]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]

    @property
    def supported(self) -> bool:
        return bool(self.value & 0x1)

    @property
    def enabled(self) -> bool:
        return bool(self.value & 0x2)


class DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _utf16_array(value: ctypes.Array) -> str:
    raw = bytes(value)
    return raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


class _DisplayConfigAPI:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Le contrôle HDR n'est disponible que sous Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        self.user32.GetDisplayConfigBufferSizes.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        self.user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG

        self.user32.QueryDisplayConfig.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(DISPLAYCONFIG_MODE_INFO),
            ctypes.c_void_p,
        ]
        self.user32.QueryDisplayConfig.restype = wintypes.LONG

        self.user32.DisplayConfigGetDeviceInfo.argtypes = [
            ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
        ]
        self.user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
        self.user32.DisplayConfigSetDeviceInfo.argtypes = [
            ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)
        ]
        self.user32.DisplayConfigSetDeviceInfo.restype = wintypes.LONG

        self.user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
        self.user32.MonitorFromPoint.restype = wintypes.HMONITOR
        self.user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR,
            ctypes.POINTER(MONITORINFOEXW),
        ]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL

    @staticmethod
    def _check(code: int, operation: str) -> None:
        if int(code) != ERROR_SUCCESS:
            raise OSError(int(code), f"{operation} a échoué")

    def active_paths(self) -> tuple[DISPLAYCONFIG_PATH_INFO, ...]:
        path_count = wintypes.UINT()
        mode_count = wintypes.UINT()
        self._check(
            self.user32.GetDisplayConfigBufferSizes(
                QDC_ONLY_ACTIVE_PATHS,
                ctypes.byref(path_count),
                ctypes.byref(mode_count),
            ),
            "GetDisplayConfigBufferSizes",
        )
        if path_count.value <= 0:
            return ()

        paths = (DISPLAYCONFIG_PATH_INFO * path_count.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, mode_count.value))()
        actual_paths = wintypes.UINT(path_count.value)
        actual_modes = wintypes.UINT(mode_count.value)
        self._check(
            self.user32.QueryDisplayConfig(
                QDC_ONLY_ACTIVE_PATHS,
                ctypes.byref(actual_paths),
                paths,
                ctypes.byref(actual_modes),
                modes,
                None,
            ),
            "QueryDisplayConfig",
        )
        return tuple(paths[: actual_paths.value])

    def source_name(self, path: DISPLAYCONFIG_PATH_INFO) -> str:
        info = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        info.header.size = ctypes.sizeof(info)
        info.header.adapterId = path.sourceInfo.adapterId
        info.header.id = path.sourceInfo.id
        self._check(
            self.user32.DisplayConfigGetDeviceInfo(
                ctypes.byref(info.header)
            ),
            "DisplayConfigGetDeviceInfo(GET_SOURCE_NAME)",
        )
        return _utf16_array(info.viewGdiDeviceName)

    def primary_device_name(self) -> str:
        monitor = self.user32.MonitorFromPoint(
            POINT(0, 0),
            MONITOR_DEFAULTTOPRIMARY,
        )
        if not monitor:
            raise OSError("Moniteur principal introuvable")
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str(info.szDevice).split("\x00", 1)[0]

    def advanced_color_info(
        self,
        path: DISPLAYCONFIG_PATH_INFO,
    ) -> DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO:
        info = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
        info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
        info.header.size = ctypes.sizeof(info)
        info.header.adapterId = path.targetInfo.adapterId
        info.header.id = path.targetInfo.id
        self._check(
            self.user32.DisplayConfigGetDeviceInfo(
                ctypes.byref(info.header)
            ),
            "DisplayConfigGetDeviceInfo(GET_ADVANCED_COLOR_INFO)",
        )
        return info

    def set_advanced_color(
        self,
        path: DISPLAYCONFIG_PATH_INFO,
        enabled: bool,
    ) -> None:
        info = DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE()
        info.header.type = DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE
        info.header.size = ctypes.sizeof(info)
        info.header.adapterId = path.targetInfo.adapterId
        info.header.id = path.targetInfo.id
        info.value = 1 if enabled else 0
        self._check(
            self.user32.DisplayConfigSetDeviceInfo(
                ctypes.byref(info.header)
            ),
            "DisplayConfigSetDeviceInfo(SET_ADVANCED_COLOR_STATE)",
        )


class WindowsHDRController:
    """Enable/disable Windows HDR through the documented DisplayConfig API."""

    def __init__(self, api: _DisplayConfigAPI | None = None) -> None:
        self._api = api

    def _backend(self) -> _DisplayConfigAPI:
        if self._api is None:
            self._api = _DisplayConfigAPI()
        return self._api

    def set_enabled(self, enabled: bool, *, scope: str = "primary") -> int:
        wanted_scope = str(scope or "primary").strip().casefold()
        if wanted_scope not in {"primary", "all"}:
            raise ValueError("scope HDR doit être primary ou all")

        api = self._backend()
        paths = list(api.active_paths())
        if wanted_scope == "primary":
            primary = api.primary_device_name().casefold()
            paths = [
                path
                for path in paths
                if api.source_name(path).casefold() == primary
            ]
        if not paths:
            raise RuntimeError("Aucun écran actif correspondant n'a été trouvé.")

        supported: list[DISPLAYCONFIG_PATH_INFO] = []
        for path in paths:
            info = api.advanced_color_info(path)
            if info.supported:
                supported.append(path)
        if not supported:
            raise RuntimeError(
                "Aucun écran ciblé ne prend en charge HDR/Advanced Color."
            )

        changed = 0
        for path in supported:
            current = api.advanced_color_info(path)
            if current.enabled == bool(enabled):
                continue
            api.set_advanced_color(path, bool(enabled))
            verify = api.advanced_color_info(path)
            if verify.enabled != bool(enabled):
                raise RuntimeError(
                    "Windows n'a pas acquitté le changement HDR demandé."
                )
            changed += 1
        return changed
