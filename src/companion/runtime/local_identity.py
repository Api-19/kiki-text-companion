from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from ctypes import wintypes
from typing import Protocol


class LocalIdentityError(RuntimeError):
    """Fixed failure at the local operating-system identity boundary."""


def _raise_unavailable() -> None:
    raise LocalIdentityError("local identity unavailable") from None


def _trusted_user_id_from_sid_bytes(value: object) -> str:
    if type(value) is not bytes or not value:
        value = None
        _raise_unavailable()
    digest = hashlib.sha256(value).hexdigest()
    value = None
    return f"windows-sid-sha256:{digest}"


class _WindowsTokenApi(Protocol):
    def open_current_token(self) -> object: ...

    def read_token_sid(self, token: object) -> object: ...

    def close_token(self, token: object) -> object: ...


class _CtypesWindowsTokenApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            _raise_unavailable()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

        class _TokenUser(ctypes.Structure):
            _fields_ = (("user", _SidAndAttributes),)

        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
        advapi32.GetLengthSid.restype = wintypes.DWORD
        self._kernel32 = kernel32
        self._advapi32 = advapi32
        self._token_user_type = _TokenUser

    def open_current_token(self) -> object:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise OSError
        return token

    def read_token_sid(self, token: object) -> object:
        required = wintypes.DWORD(0)
        self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if required.value <= 0:
            raise OSError
        buffer = ctypes.create_string_buffer(required.value)
        if not self._advapi32.GetTokenInformation(
            token, 1, buffer, required.value, ctypes.byref(required)
        ):
            raise OSError
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(self._token_user_type)).contents.user.sid
        if sid_pointer is None:
            raise OSError
        sid_length = self._advapi32.GetLengthSid(ctypes.c_void_p(sid_pointer))
        if type(sid_length) is not int or sid_length <= 0 or sid_length > 1024:
            raise OSError
        return ctypes.string_at(sid_pointer, sid_length)

    def close_token(self, token: object) -> object:
        return self._kernel32.CloseHandle(token) != 0


def _read_current_windows_sid_bytes(api: _WindowsTokenApi | None = None) -> bytes:
    token = None
    result: object = None
    selected_api: _WindowsTokenApi | None = None
    failed = False
    try:
        selected_api = _CtypesWindowsTokenApi() if api is None else api
        token = selected_api.open_current_token()
        if token is None:
            raise TypeError
        result = selected_api.read_token_sid(token)
    except Exception:
        failed = True
    finally:
        if token is not None and selected_api is not None:
            try:
                if selected_api.close_token(token) is not True:
                    failed = True
            except Exception:
                failed = True
        token = None
        selected_api = None  # type: ignore[assignment]
    if failed or type(result) is not bytes or not result:
        result = None
        _raise_unavailable()
    return result  # type: ignore[return-value]


def _is_windows() -> bool:
    return os.name == "nt"


def current_user_id() -> str:
    if _is_windows():
        return _trusted_user_id_from_sid_bytes(_read_current_windows_sid_bytes())
    return f"posix-uid-{os.getuid()}"
