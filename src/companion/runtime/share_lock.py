"""Cross-process session lock shared by share setup and text chat."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from companion.runtime.private_paths import (
    IS_WINDOWS,
    ensure_private_directory,
    reject_link_ancestors,
)


class ShareSessionBusy(RuntimeError):
    """The runtime directory is already in use by a share session."""


@contextmanager
def share_session_lock(runtime_data_dir: Path) -> Iterator[None]:
    dialogue_dir = runtime_data_dir / "dialogue"
    lock_path = dialogue_dir / "text-chat.lock"
    reject_link_ancestors(lock_path)
    missing = []
    directory = runtime_data_dir.parent
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        ensure_private_directory(directory)
    for directory in (runtime_data_dir, dialogue_dir):
        if not IS_WINDOWS and directory.exists():
            info = directory.lstat()
            if (
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) != 0o700
            ):
                directory.chmod(0o700)
        ensure_private_directory(directory)
    with lock_path.open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ShareSessionBusy from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
