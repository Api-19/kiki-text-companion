import os
import stat
import sys
from types import SimpleNamespace

import pytest

from companion.runtime import share_lock
from companion.runtime.private_paths import IS_WINDOWS
from companion.runtime.share_lock import share_session_lock


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX directory ownership and permissions")
@pytest.mark.parametrize("target_name", ["runtime", "dialogue"])
def test_owned_755_directory_is_tightened_then_locked(tmp_path, target_name):
    runtime_dir = tmp_path / "runtime"
    dialogue_dir = runtime_dir / "dialogue"
    runtime_dir.mkdir(mode=0o700)
    dialogue_dir.mkdir(mode=0o700)
    target = runtime_dir if target_name == "runtime" else dialogue_dir
    target.chmod(0o755)

    with share_session_lock(runtime_dir):
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert (dialogue_dir / "text-chat.lock").is_file()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX directory ownership and permissions")
def test_foreign_owned_directory_rejected_before_lock_open(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    (runtime_dir / "dialogue").mkdir(mode=0o700)
    runtime_dir.chmod(0o755)
    foreign_uid = runtime_dir.stat().st_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)

    with (
        pytest.raises(ValueError, match=r"^unsafe private directory$"),
        share_session_lock(runtime_dir),
    ):
        pytest.fail("foreign-owned directory was accepted")

    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o755
    assert not (runtime_dir / "dialogue" / "text-chat.lock").exists()


def test_existing_safe_directories_can_reacquire_lock(tmp_path):
    runtime_dir = tmp_path / "runtime"
    dialogue_dir = runtime_dir / "dialogue"
    runtime_dir.mkdir(mode=0o700)
    dialogue_dir.mkdir(mode=0o700)

    for _ in range(2):
        with share_session_lock(runtime_dir):
            assert (dialogue_dir / "text-chat.lock").is_file()


def test_windows_lock_uses_same_region_for_unlock(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    dialogue_dir = runtime_dir / "dialogue"
    runtime_dir.mkdir(mode=0o700)
    dialogue_dir.mkdir(mode=0o700)
    (dialogue_dir / "text-chat.lock").write_bytes(b"synthetic")
    calls = []
    fds = []

    def locking(fd, mode, nbytes):
        fds.append(fd)
        calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        locking=locking, LK_NBLCK=2, LK_UNLCK=0,
    ))
    monkeypatch.setattr(share_lock.sys, "platform", "win32")

    with share_session_lock(runtime_dir):
        os.lseek(fds[0], 0, os.SEEK_END)

    assert calls == [(2, 1, 0), (0, 1, 0)]
