"""Production Memory factory with platform-specific private file checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from companion.memory.errors import MemoryStoreError
from companion.memory.service import (
    MemoryService,
    _sanitize_public_errors,
    _service_with_store,
)
from companion.memory.store import _MemoryStore
from companion.runtime.settings import Settings

if TYPE_CHECKING:
    from companion.memory.windows_storage import WindowsPrivateFiles

_OS_NAME = os.name
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_AUXILIARY_SUFFIXES = ("-journal", "-wal", "-shm")
_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS |= getattr(os, "O_CLOEXEC", 0)


def _current_uid() -> int:
    return os.geteuid()


class _PrivateBoundary:
    def __init__(self, private_root: Path) -> None:
        self._root = private_root
        self._directory = private_root / "memory"
        self.database_path = self._directory / "memory.sqlite3"
        self._identity: tuple[int, int] | None = None
        self._directory_identity: tuple[int, int] | None = None

    def _create_directory(self) -> None:
        with suppress(FileExistsError):
            os.mkdir(self._directory, _DIRECTORY_MODE)

    def _check_memory_directory(self) -> tuple[int, int]:
        info = _check_directory(self._directory)
        return info.st_dev, info.st_ino

    def _file_identity(self, path: Path) -> tuple[int, int]:
        info = _check_file(path)
        return info.st_dev, info.st_ino

    def _create_database(self) -> None:
        descriptor = os.open(self.database_path, _CREATE_FLAGS, _FILE_MODE)
        os.close(descriptor)

    def prepare(self) -> None:
        self._check_root()
        self._create_directory()
        self._directory_identity = self._check_memory_directory()
        try:
            os.lstat(self.database_path)
        except FileNotFoundError:
            if any(os.path.lexists(path) for path in self._auxiliary_paths()):
                raise ValueError("orphan auxiliary file") from None
            self._create_database()
        self._identity = self._file_identity(self.database_path)
        self.verify()

    def verify(self) -> None:
        try:
            self._check_root()
            if self._directory_identity != self._check_memory_directory():
                raise ValueError("memory directory identity changed")
            if self._identity != self._file_identity(self.database_path):
                raise ValueError("database identity changed")
            self._check_auxiliary_files()
        except Exception:
            raise MemoryStoreError("memory store unavailable") from None

    def _check_root(self) -> None:
        if not self._root.is_absolute() or os.pardir in self._root.parts:
            raise ValueError("private root must be absolute")
        for ancestor in reversed(self._root.parents):
            if not stat.S_ISDIR(os.lstat(ancestor).st_mode):
                raise ValueError("unsafe private root ancestor")
        _check_directory(self._root)

    def _auxiliary_paths(self) -> tuple[Path, ...]:
        name = self.database_path.name
        return tuple(self._directory / f"{name}{suffix}" for suffix in _AUXILIARY_SUFFIXES)

    def _check_auxiliary_files(self) -> None:
        for path in self._auxiliary_paths():
            try:
                self._file_identity(path)
            except FileNotFoundError:
                continue


class _WindowsBoundary(_PrivateBoundary):
    def __init__(self, private_root: Path, files: WindowsPrivateFiles) -> None:
        super().__init__(private_root)
        self._files = files

    def _check_root(self) -> None:
        self._files.check(self._root, directory=True)

    def _create_directory(self) -> None:
        self._files.create_directory(self._directory)

    def _check_memory_directory(self) -> tuple[int, int]:
        return self._files.check(self._directory, directory=True, memory_directory=True)

    def _file_identity(self, path: Path) -> tuple[int, int]:
        return self._files.check(path)

    def _create_database(self) -> None:
        self._files.create_file(self.database_path)


def _check_directory(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _current_uid()
        or stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE
    ):
        raise ValueError("unsafe private directory")
    return info


def _check_file(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _current_uid()
        or stat.S_IMODE(info.st_mode) != _FILE_MODE
        or info.st_nlink != 1
    ):
        raise ValueError("unsafe private file")
    return info


@_sanitize_public_errors
def build_private_memory_service(
    settings: Settings, *, clock: Callable[[], datetime]
) -> MemoryService:
    try:
        if _OS_NAME not in {"posix", "nt"} or not isinstance(settings, Settings):
            raise ValueError("private memory storage unavailable")
        root = Path(settings.private_data_dir)
        if _OS_NAME == "nt":
            from companion.memory.windows_storage import WindowsPrivateFiles

            boundary = _WindowsBoundary(root, WindowsPrivateFiles())
        else:
            boundary = _PrivateBoundary(root)
        boundary.prepare()
    except Exception:
        raise MemoryStoreError("memory store unavailable") from None
    store = _MemoryStore(boundary.database_path, boundary_check=boundary.verify)
    return _service_with_store(store, clock)
