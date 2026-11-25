"""Real Windows ACL checks in disposable directories."""

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion.memory import MemoryStoreError, build_private_memory_service
from companion.memory.windows_storage import WindowsPrivateFiles
from companion.runtime.settings import Settings

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows native ACLs")


@pytest.fixture
def protected_root(tmp_path):
    files = WindowsPrivateFiles()
    root = tmp_path / "private"
    files.create_directory(root)
    settings = Settings(
        environment="test", log_level="INFO", private_data_dir=root,
        runtime_data_dir=tmp_path / "runtime", test_results_dir=tmp_path / "results",
    )
    return files, settings


def service(settings):
    return build_private_memory_service(settings, clock=lambda: datetime.now(UTC))


def grant_everyone_read(path: Path):
    subprocess.run(
        ["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"],
        check=True, capture_output=True, timeout=10,
    )


def test_sqlite_sidecars_inherit_private_acl(protected_root):
    files, settings = protected_root
    assert service(settings).get_consent("synthetic") is None
    directory = settings.private_data_dir / "memory"
    db = directory / "memory.sqlite3"
    files.check(directory, directory=True, memory_directory=True)
    files.check(db)
    # Use a real open WAL transaction to inspect actual SQLite .wal/.shm creates.
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        conn.execute("CREATE TABLE synthetic_probe (value TEXT)")
        conn.commit()
        files.check(db.with_name(db.name + "-wal"))
        files.check(db.with_name(db.name + "-shm"))
    finally:
        conn.close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        conn.execute("INSERT INTO synthetic_probe VALUES ('synthetic')")
        files.check(db.with_name(db.name + "-journal"))
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("target", ["directory", "database", "journal", "wal", "shm"])
def test_existing_broad_acl_is_rejected(protected_root, target):
    _, settings = protected_root
    memory = service(settings)
    directory = settings.private_data_dir / "memory"
    db = directory / "memory.sqlite3"
    path = {"directory": directory, "database": db}.get(target)
    if path is None:
        path = db.with_name(db.name + "-" + target)
        path.write_bytes(b"synthetic")
    grant_everyone_read(path)
    with pytest.raises(MemoryStoreError):
        memory.get_consent("synthetic")
    with pytest.raises(MemoryStoreError):
        service(settings)


def test_hardlink_and_reparse_are_rejected(protected_root):
    files, settings = protected_root
    assert service(settings).get_consent("synthetic") is None
    db = settings.private_data_dir / "memory" / "memory.sqlite3"
    alias = db.with_name("linked.sqlite3")
    os.link(db, alias)
    with pytest.raises(MemoryStoreError):
        service(settings)
    alias.unlink()
    link = db.with_name(db.name + "-journal")
    try:
        link.symlink_to(db)
    except OSError as error:
        if error.winerror != 1314:
            raise
        pytest.skip("Windows symlink creation requires Developer Mode or privilege")
    with pytest.raises(ValueError):
        files.check(link)
    with pytest.raises(MemoryStoreError):
        service(settings)
