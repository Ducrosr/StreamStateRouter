from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import tempfile
from typing import Final

from .paths import user_data_dir


class SecretStoreUnavailable(RuntimeError):
    pass


class SecretStoreError(RuntimeError):
    pass


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob_from_bytes(value: bytes):
    raw = bytes(value)
    if not raw:
        return _DATA_BLOB(0, None), None
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    return (
        _DATA_BLOB(
            len(raw),
            ctypes.cast(
                buffer,
                ctypes.POINTER(ctypes.c_ubyte),
            ),
        ),
        buffer,
    )


class WindowsDPAPISecretStore:
    """Small per-user DPAPI store.

    The ciphertext is persisted under the SSR user data directory. DPAPI binds
    decryption to the current Windows user; plaintext is never written to disk.
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        entropy: bytes = b"StreamStateRouter:secrets:v1",
    ):
        if os.name != "nt":
            raise SecretStoreUnavailable(
                "DPAPI est disponible uniquement sous Windows"
            )
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else (user_data_dir() / "secrets")
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._entropy = bytes(entropy)
        self._crypt32 = ctypes.WinDLL(
            "crypt32",
            use_last_error=True,
        )
        self._kernel32 = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        self._configure_api()

    def _configure_api(self) -> None:
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _validate_name(name: str) -> str:
        value = str(name or "").strip()
        if not _SAFE_NAME.fullmatch(value):
            raise ValueError("Nom de secret invalide")
        return value

    def _path(self, name: str) -> Path:
        return self.root / (self._validate_name(name) + ".dpapi")

    def set(self, name: str, value: str | bytes) -> None:
        payload = (
            value.encode("utf-8")
            if isinstance(value, str)
            else bytes(value)
        )
        encrypted = self._protect(payload)
        target = self._path(name)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(self.root),
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def get_bytes(self, name: str) -> bytes | None:
        target = self._path(name)
        try:
            encrypted = target.read_bytes()
        except FileNotFoundError:
            return None
        if not encrypted:
            raise SecretStoreError("Secret DPAPI vide")
        return self._unprotect(encrypted)

    def get(self, name: str) -> str | None:
        value = self.get_bytes(name)
        return None if value is None else value.decode("utf-8")

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def _protect(self, payload: bytes) -> bytes:
        input_blob, input_buffer = _blob_from_bytes(payload)
        entropy_blob, entropy_buffer = _blob_from_bytes(
            self._entropy
        )
        output = _DATA_BLOB()
        _keepalive = (input_buffer, entropy_buffer)
        del _keepalive
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "StreamStateRouter",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(
                f"CryptProtectData a échoué : {ctypes.get_last_error()}"
            )
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(output.pbData, wintypes.HLOCAL)
                )

    def _unprotect(self, encrypted: bytes) -> bytes:
        input_blob, input_buffer = _blob_from_bytes(encrypted)
        entropy_blob, entropy_buffer = _blob_from_bytes(
            self._entropy
        )
        output = _DATA_BLOB()
        description = wintypes.LPWSTR()
        _keepalive = (input_buffer, entropy_buffer)
        del _keepalive
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(
                f"CryptUnprotectData a échoué : {ctypes.get_last_error()}"
            )
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(output.pbData, wintypes.HLOCAL)
                )
            if description:
                self._kernel32.LocalFree(
                    ctypes.cast(description, wintypes.HLOCAL)
                )
