"""Windows ACL and handle checks for private share memory."""

from __future__ import annotations

import ctypes as c
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes as w
from typing import TYPE_CHECKING

from companion.runtime.local_identity import _read_current_windows_sid_bytes

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

_PTR = c.c_void_p
_PTR_REF = c.POINTER(_PTR)
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_CREATOR_OWNER = "S-1-3-0"
_READ_CONTROL = 0x20000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 7
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_OPEN_REPARSE_AND_DIRECTORY = 0x02200000
_FILE_ALL_ACCESS = 0x1F01FF
_DACL_PROTECTED = 0x1000
_INHERIT_ONLY = 0x08
_OBJECT_AND_CONTAINER_INHERIT = 0x03


class _FileInfo(c.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("modified", w.FILETIME),
        ("volume", w.DWORD),
        ("size_high", w.DWORD),
        ("size_low", w.DWORD),
        ("links", w.DWORD),
        ("index_high", w.DWORD),
        ("index_low", w.DWORD),
    ]


class _Acl(c.Structure):
    _fields_ = [
        ("revision", w.BYTE), ("reserved", w.BYTE), ("size", w.WORD),
        ("count", w.WORD), ("reserved2", w.WORD),
    ]


class _Ace(c.Structure):
    _fields_ = [
        ("kind", w.BYTE), ("flags", w.BYTE), ("size", w.WORD),
        ("mask", w.DWORD), ("sid_start", w.DWORD),
    ]


class _SecurityAttributes(c.Structure):
    _fields_ = [("size", w.DWORD), ("descriptor", _PTR), ("inherit_handle", w.BOOL)]


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes = arguments
    function.restype = result
    return function


def check_path_chain(path: Path) -> None:
    """Reject reparse points before opening a private path."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe private path")
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & 0x400:
            raise ValueError("unsafe private path ancestor")


class WindowsPrivateFiles:
    def __init__(self) -> None:
        kernel = c.WinDLL("kernel32", use_last_error=True)
        security = c.WinDLL("advapi32", use_last_error=True)
        self._close = _bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
        self._free = _bind(kernel, "LocalFree", _PTR, _PTR)
        self._open = _bind(
            kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, _PTR,
            w.DWORD, w.DWORD, w.HANDLE,
        )
        self._info = _bind(
            kernel, "GetFileInformationByHandle", w.BOOL, w.HANDLE, c.POINTER(_FileInfo),
        )
        self._mkdir = _bind(
            kernel, "CreateDirectoryW", w.BOOL, w.LPCWSTR, c.POINTER(_SecurityAttributes),
        )
        self._sid_string = _bind(
            security, "ConvertSidToStringSidW", w.BOOL, _PTR, _PTR_REF,
        )
        self._from_sddl = _bind(
            security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            w.BOOL, w.LPCWSTR, w.DWORD, _PTR_REF, _PTR,
        )
        self._security = _bind(
            security, "GetSecurityInfo", w.DWORD, w.HANDLE, c.c_int, w.DWORD,
            _PTR_REF, _PTR_REF, _PTR_REF, _PTR_REF, _PTR_REF,
        )
        self._control = _bind(
            security, "GetSecurityDescriptorControl", w.BOOL, _PTR,
            c.POINTER(w.WORD), c.POINTER(w.DWORD),
        )
        self._get_ace = _bind(security, "GetAce", w.BOOL, _PTR, w.DWORD, _PTR_REF)
        sid = c.create_string_buffer(_read_current_windows_sid_bytes())
        self._user = self._sid(c.addressof(sid))

    def _sid(self, pointer: int) -> str:
        result = _PTR()
        if not self._sid_string(pointer, c.byref(result)):
            raise c.WinError(c.get_last_error())
        try:
            return c.wstring_at(result.value)
        finally:
            self._free(result)

    def create_directory(self, path: Path) -> None:
        check_path_chain(path)
        descriptor = _PTR()
        # Use a protected DACL at creation. SQLite's ordinary sidecar creates inherit it.
        sddl = f"D:P(A;OICI;FA;;;{self._user})(A;OICI;FA;;;SY)"
        if not self._from_sddl(sddl, 1, c.byref(descriptor), None):
            raise c.WinError(c.get_last_error())
        try:
            attrs = _SecurityAttributes(c.sizeof(_SecurityAttributes), descriptor.value, False)
            if not self._mkdir(str(path), c.byref(attrs)):
                error = c.get_last_error()
                if error != 183:  # ERROR_ALREADY_EXISTS
                    raise c.WinError(error)
        finally:
            self._free(descriptor)

    def _checked_handle(
        self, path: Path, *, directory: bool, memory_directory: bool = False,
        read_data: bool = False,
    ) -> tuple[int, tuple[int, int]]:
        check_path_chain(path)
        access = _READ_CONTROL | (_GENERIC_READ if read_data else 0)
        handle = self._open(
            str(path), access, _SHARE_ALL, None, _OPEN_EXISTING,
            _OPEN_REPARSE_AND_DIRECTORY, None,
        )
        if handle == _PTR(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            info = _FileInfo()
            if not self._info(handle, c.byref(info)):
                raise c.WinError(c.get_last_error())
            if (
                info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory
                or (not directory and info.links != 1)
            ):
                raise ValueError("unsafe private object")
            self._check_acl(handle, memory_directory=memory_directory)
            return handle, (info.volume, (info.index_high << 32) | info.index_low)
        except BaseException:
            self._close(handle)
            raise

    def create_file(self, path: Path) -> None:
        check_path_chain(path)
        handle = self._open(
            str(path), _GENERIC_WRITE, _SHARE_ALL, None, _CREATE_NEW,
            _OPEN_REPARSE_AND_DIRECTORY, None,
        )
        if handle == _PTR(-1).value:
            raise c.WinError(c.get_last_error())
        self._close(handle)

    def check(
        self, path: Path, *, directory: bool = False, memory_directory: bool = False,
    ) -> tuple[int, int]:
        handle, identity = self._checked_handle(
            path, directory=directory, memory_directory=memory_directory,
        )
        self._close(handle)
        return identity

    @contextmanager
    def read_config(self, path: Path) -> Iterator[TextIO]:
        import msvcrt

        handle, _ = self._checked_handle(path, directory=False, read_data=True)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self._close(handle)
            raise
        with os.fdopen(fd, encoding="utf-8") as stream:
            yield stream

    def _check_acl(self, handle: int, *, memory_directory: bool) -> None:
        owner, acl, descriptor = _PTR(), _PTR(), _PTR()
        status = self._security(
            handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor),
        )
        if status:
            raise c.WinError(status)
        try:
            if not owner.value or self._sid(owner.value) != self._user or not acl.value:
                raise ValueError("unsafe private object owner or ACL")
            if memory_directory:
                control, revision = w.WORD(), w.DWORD()
                if not self._control(descriptor, c.byref(control), c.byref(revision)):
                    raise c.WinError(c.get_last_error())
                if not control.value & _DACL_PROTECTED:
                    raise ValueError("memory directory ACL must be protected")
            allowed = {self._user, _SYSTEM, _ADMINISTRATORS}
            full_user_access = False
            for index in range(_Acl.from_address(acl.value).count):
                address = _PTR()
                if not self._get_ace(acl, index, c.byref(address)):
                    raise c.WinError(c.get_last_error())
                ace = _Ace.from_address(address.value)
                if ace.kind != 0 or ace.size < 16:  # ACCESS_ALLOWED_ACE with a SID
                    raise ValueError("unexpected private ACL entry")
                sid = self._sid(address.value + _Ace.sid_start.offset)
                if not memory_directory and sid == _CREATOR_OWNER and ace.flags & _INHERIT_ONLY:
                    continue
                if sid not in allowed:
                    raise ValueError("unexpected private ACL principal")
                if memory_directory:
                    if ace.flags != _OBJECT_AND_CONTAINER_INHERIT:
                        raise ValueError("memory directory ACL must protect its children")
                    full_user_access |= sid == self._user and ace.mask == _FILE_ALL_ACCESS
            if memory_directory and not full_user_access:
                raise ValueError("memory directory ACL lacks full user access")
        finally:
            self._free(descriptor)
