"""Private directory checks with simplified Windows link protection."""

import os
import stat
from pathlib import Path

IS_WINDOWS: bool = os.name == "nt"


def is_link(path: Path) -> bool:
    return path.is_symlink() or (IS_WINDOWS and path.is_junction())


def reject_link_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if is_link(current):
            raise ValueError("private path contains link")
        if current == current.parent:
            return
        current = current.parent


def check_private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or is_link(path):
        raise ValueError("unsafe private directory")
    if not IS_WINDOWS and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe private directory")


def ensure_private_directory(path: Path) -> None:
    try:
        check_private_directory(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        check_private_directory(path)
