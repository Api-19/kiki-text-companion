import os
from pathlib import Path

import pytest


def test_check_private_directory_does_not_create_missing_path(tmp_path):
    from companion.runtime.private_paths import check_private_directory

    directory = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        check_private_directory(directory)
    assert not directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory ownership and permissions")
@pytest.mark.parametrize("unsafe", ["mode", "foreign_owner"])
def test_check_private_directory_rejects_unsafe_posix_directory(tmp_path, monkeypatch, unsafe):
    from companion.runtime import private_paths

    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    if unsafe == "mode":
        directory.chmod(0o755)
    before = directory.lstat()
    if unsafe == "foreign_owner":
        monkeypatch.setattr(private_paths.os, "geteuid", lambda: before.st_uid + 1)

    with pytest.raises(ValueError, match=r"^unsafe private directory$"):
        private_paths.check_private_directory(directory)

    after = directory.lstat()
    assert (after.st_uid, after.st_mode) == (before.st_uid, before.st_mode)
    assert list(directory.iterdir()) == []


def test_windows_private_directory_accepts_755(tmp_path, monkeypatch):
    from companion.runtime import private_paths

    monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
    directory = tmp_path / "private"
    directory.mkdir()
    directory.chmod(0o755)
    private_paths.ensure_private_directory(directory)


def test_windows_private_directory_creates_missing_directory(tmp_path, monkeypatch):
    from companion.runtime import private_paths

    monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
    directory = tmp_path / "private"
    private_paths.ensure_private_directory(directory)
    assert directory.is_dir()


def test_windows_private_directory_rejects_link(tmp_path, monkeypatch):
    from companion.runtime import private_paths

    monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
    directory = tmp_path / "private"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        private_paths.ensure_private_directory(directory)


def test_windows_rejects_junction_ancestor(tmp_path, monkeypatch):
    from companion.runtime import private_paths

    monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
    junction = tmp_path / "junction"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)
    with pytest.raises(ValueError, match=r"^private path contains link$"):
        private_paths.reject_link_ancestors(junction / "child" / "private")
