from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from companion.runtime import local_identity
from companion.runtime.local_identity import LocalIdentityError, current_user_id


@pytest.mark.skipif(os.name == "nt", reason="POSIX uid only")
def test_current_posix_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_identity, "_is_windows", lambda: False)

    assert current_user_id() == f"posix-uid-{os.getuid()}"


class FakeTokenApi:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.token = object()
        self.events: list[str] = []

    def open_current_token(self) -> object:
        self.events.append("open")
        if self.failure == "open":
            raise OSError
        return self.token

    def read_token_sid(self, token: object) -> bytes:
        assert token is self.token
        self.events.append("read")
        if self.failure == "read":
            raise OSError
        return b"synthetic-windows-sid"

    def close_token(self, token: object) -> bool:
        assert token is self.token
        self.events.append("close")
        if self.failure == "close":
            raise OSError
        return True


def test_current_windows_user_id_uses_token_api(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeTokenApi()
    monkeypatch.setattr(local_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(local_identity, "_CtypesWindowsTokenApi", lambda: api)

    result = current_user_id()

    assert result == f"windows-sid-sha256:{hashlib.sha256(b'synthetic-windows-sid').hexdigest()}"
    assert api.events == ["open", "read", "close"]


@pytest.mark.parametrize("failure", ["open", "read", "close"])
def test_windows_token_failure_raises_local_error_and_closes_token(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    api = FakeTokenApi(failure)
    monkeypatch.setattr(local_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(local_identity, "_CtypesWindowsTokenApi", lambda: api)

    with pytest.raises(LocalIdentityError, match=r"^local identity unavailable$"):
        current_user_id()

    assert api.events == (["open"] if failure == "open" else ["open", "read", "close"])


class SyntheticTokenUser(ctypes.Structure):
    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    _fields_ = (("user", SidAndAttributes),)


def install_synthetic_ctypes(
    monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None, length: int = 21,
) -> tuple[list[str], Mock]:
    events: list[str] = []
    sid_pointer = 0x1234

    def open_token(process: object, access: int, token: object) -> int:
        events.append("open")
        ctypes.cast(token, ctypes.POINTER(wintypes.HANDLE))[0] = wintypes.HANDLE(42)
        return 1

    def token_information(
        token: object, info_class: int, buffer: object, size: int, required: object,
    ) -> int:
        assert token.value == 42
        assert info_class == 1
        if buffer is None:
            events.append("size")
            assert size == 0
            ctypes.cast(required, ctypes.POINTER(wintypes.DWORD))[0] = (
                0 if failure == "size_zero" else ctypes.sizeof(SyntheticTokenUser)
            )
            return 0
        events.append("read")
        assert size == ctypes.sizeof(SyntheticTokenUser)
        if failure == "read":
            return 0
        user = ctypes.cast(buffer, ctypes.POINTER(SyntheticTokenUser))
        user.contents.user.sid = None if failure == "null_sid" else sid_pointer
        return 1

    def get_length(pointer: ctypes.c_void_p) -> int:
        events.append("length")
        assert pointer.value == sid_pointer
        return length

    def close_handle(token: object) -> int:
        assert token.value == 42
        events.append("close")
        return 1

    kernel32 = SimpleNamespace(
        GetCurrentProcess=Mock(return_value=7), CloseHandle=Mock(side_effect=close_handle),
    )
    advapi32 = SimpleNamespace(
        OpenProcessToken=Mock(side_effect=open_token),
        GetTokenInformation=Mock(side_effect=token_information),
        GetLengthSid=Mock(side_effect=get_length),
    )
    libraries = {"kernel32": kernel32, "advapi32": advapi32}
    monkeypatch.setattr(local_identity.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: libraries[name], raising=False)
    string_at = Mock(return_value=b"synthetic-windows-sid")
    monkeypatch.setattr(ctypes, "string_at", string_at)
    return events, string_at


def test_ctypes_sid_reads_two_pass_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    events, string_at = install_synthetic_ctypes(monkeypatch)

    assert local_identity._read_current_windows_sid_bytes() == b"synthetic-windows-sid"
    assert events == ["open", "size", "read", "length", "close"]
    string_at.assert_called_once_with(0x1234, 21)


@pytest.mark.parametrize(
    ("failure", "length", "expected"),
    [
        ("size_zero", 21, ["open", "size", "close"]),
        ("read", 21, ["open", "size", "read", "close"]),
        ("null_sid", 21, ["open", "size", "read", "close"]),
        (None, 0, ["open", "size", "read", "length", "close"]),
        (None, 1025, ["open", "size", "read", "length", "close"]),
    ],
)
def test_ctypes_sid_read_failures_close_token(
    monkeypatch: pytest.MonkeyPatch, failure: str | None, length: int, expected: list[str],
) -> None:
    events, string_at = install_synthetic_ctypes(monkeypatch, failure=failure, length=length)

    with pytest.raises(LocalIdentityError, match=r"^local identity unavailable$"):
        local_identity._read_current_windows_sid_bytes()

    assert events == expected
    string_at.assert_not_called()


def test_ctypes_sid_accepts_maximum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    events, string_at = install_synthetic_ctypes(monkeypatch, length=1024)

    assert local_identity._read_current_windows_sid_bytes() == b"synthetic-windows-sid"
    assert events == ["open", "size", "read", "length", "close"]
    string_at.assert_called_once_with(0x1234, 1024)


def test_ctypes_library_load_failure_uses_local_error(monkeypatch: pytest.MonkeyPatch) -> None:
    events, string_at = install_synthetic_ctypes(monkeypatch)
    loader = Mock(side_effect=OSError)
    monkeypatch.setattr(ctypes, "WinDLL", loader)

    with pytest.raises(LocalIdentityError, match=r"^local identity unavailable$"):
        local_identity._read_current_windows_sid_bytes()

    loader.assert_called_once_with("kernel32", use_last_error=True)
    assert events == []
    string_at.assert_not_called()
