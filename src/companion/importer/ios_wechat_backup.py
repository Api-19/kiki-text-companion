"""Export one personal conversation from an unencrypted iOS WeChat backup.

Database schemas are read from temporary copies. Contact remark protobuf fields
1 and 3 contain the nickname and remark respectively. No backup is auto-selected.
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

_DOMAIN = "AppDomain-com.tencent.xin"
_FILE_ID = re.compile(r"[0-9a-fA-F]{40}")
_TABLE = re.compile(r"Chat_[0-9a-f]{32}")
_DATABASE_PATH = re.compile(
    r"Documents/([^/]+)/DB/(WCDB_Contact\.sqlite|message_[^/]+\.sqlite(?:-wal)?)"
)
_SERVICE_USERS = {
    "weixin",
    "filehelper",
    "fmessage",
    "medianote",
    "floatbottle",
    "newsapp",
    "qqmail",
    "qmessage",
    "tmessage",
    "qqsync",
    "feedsapp",
    "blogapp",
    "facebookapp",
    "masssendapp",
    "voiceinputapp",
    "officialaccounts",
    "notification_messages",
    "brandsessionholder",
    "appbrandcustomerservicemsg",
    "appbrand_notify_message",
    "service_officialaccounts",
    "weixinreminder",
}


class IosBackupError(RuntimeError):
    """A fixed failure category without source data or paths."""


@dataclass(frozen=True)
class Conversation:
    table: str
    display_name: str
    message_count: int
    last_timestamp: int


def default_backup_roots(platform: str | None = None) -> list[Path]:
    """Return existing platform roots in discovery order (``nt`` or ``posix``)."""
    platform = os.name if platform is None else platform
    if platform == "nt":
        roots = [
            Path(os.environ[key]).joinpath(*suffix)
            for key, suffix in (
                ("USERPROFILE", ("Apple", "MobileSync", "Backup")),
                ("APPDATA", ("Apple Computer", "MobileSync", "Backup")),
            )
            if os.environ.get(key)
        ]
    else:
        roots = [Path.home() / "Library/Application Support/MobileSync/Backup"]
    try:
        return [root for root in roots if root.is_dir()]
    except OSError:
        raise IosBackupError("backup discovery failed") from None


def find_backups(roots: list[Path]) -> list[Path]:
    """Find immediate backup directories by name, regardless of encryption."""
    try:
        return sorted(
            (
                child
                for root in roots
                if root.is_dir()
                for child in root.iterdir()
                if child.is_dir() and (child / "Manifest.db").is_file()
            ),
            key=lambda child: child.name,
        )
    except OSError:
        raise IosBackupError("backup discovery failed") from None


def is_encrypted(backup: Path) -> bool:
    """Return the backup encryption flag; a missing manifest or flag means False."""
    try:
        manifest = backup / "Manifest.plist"
        if not manifest.exists():
            return False
        with manifest.open("rb") as source:
            metadata = plistlib.load(source)
        if not isinstance(metadata, dict):
            raise IosBackupError("invalid backup manifest")
        encrypted = metadata.get("IsEncrypted", False)
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
        raise IosBackupError("invalid backup manifest") from None
    return bool(encrypted)


@contextmanager
def _copied_database(source: Path, directory: Path):
    destination = directory / source.name
    shutil.copyfile(source, destination)
    connection = sqlite3.connect(destination)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _backup(backup: Path):
    if is_encrypted(backup):
        raise IosBackupError("encrypted backup")
    try:
        directory = Path(tempfile.mkdtemp(prefix="companion-ios-"))
    except OSError:
        raise IosBackupError("backup operation failed") from None
    try:
        try:
            with _copied_database(backup / "Manifest.db", directory) as manifest:
                files = {}
                for relative, file_id in manifest.execute(
                    "SELECT relativePath, fileID FROM Files WHERE domain = ? AND flags = 1",
                    (_DOMAIN,),
                ):
                    if _DATABASE_PATH.fullmatch(relative):
                        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
                            raise IosBackupError("invalid file id")
                        files[relative] = backup / file_id[:2] / file_id
                yield files, directory
        except (OSError, sqlite3.Error, UnicodeError):
            raise IosBackupError("backup operation failed") from None
    finally:
        try:
            shutil.rmtree(directory)
        except OSError:
            raise IosBackupError("temporary cleanup failed") from None


def list_accounts(backup: Path) -> list[str]:
    """Return full account directory identifiers having message shards."""
    with _backup(backup) as (files, _):
        return sorted(
            {
                relative.split("/")[1]
                for relative in files
                if relative.rsplit("/", 1)[1].startswith("message_")
                and relative.endswith(".sqlite")
            }
        )


def _varint(blob: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(blob):
        byte = blob[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7
    raise IosBackupError("invalid contact data")


def _contact_name(blob: bytes) -> str:
    names = {}
    offset = 0
    while offset < len(blob):
        key, offset = _varint(blob, offset)
        field, wire = key >> 3, key & 7
        if wire == 0:
            _, offset = _varint(blob, offset)
        elif wire == 2:
            length, offset = _varint(blob, offset)
            end = offset + length
            if end > len(blob):
                raise IosBackupError("invalid contact data")
            if field in (1, 3):
                names[field] = blob[offset:end].decode("utf-8")
            offset = end
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        else:
            raise IosBackupError("invalid contact data")
        if offset > len(blob):
            raise IosBackupError("invalid contact data")
    return names.get(3) or names.get(1) or ""


def _contacts(files: dict, directory: Path, account: str) -> dict[str, tuple[str, str]]:
    source = files.get(f"Documents/{account}/DB/WCDB_Contact.sqlite")
    if source is None:
        return {}
    contacts = {}
    with _copied_database(source, directory) as db:
        for user, blob in db.execute("SELECT userName, dbContactRemark FROM Friend"):
            if not user or "@" in user or user.startswith("gh_") or user in _SERVICE_USERS:
                continue
            table = "Chat_" + hashlib.md5(user.encode("utf-8")).hexdigest()
            contacts[table] = (user, _contact_name(blob or b""))
    return contacts


def _shards(files: dict, account: str) -> list[Path]:
    prefix = f"Documents/{account}/DB/message_"
    shards = []
    for relative in sorted(files):
        if relative.startswith(prefix) and relative.endswith(".sqlite"):
            wal = files.get(relative + "-wal")
            if wal is not None and wal.stat().st_size:
                raise IosBackupError("wal not supported")
            shards.append(files[relative])
    return shards


def _validate_table(table: str) -> None:
    if not _TABLE.fullmatch(table):
        raise IosBackupError("invalid table name")


def list_conversations(backup: Path, account: str) -> list[Conversation]:
    """List resolved personal conversations using only row counts and timestamps."""
    with _backup(backup) as (files, directory):
        contacts = _contacts(files, directory, account)
        totals = {}
        for source in _shards(files, account):
            with _copied_database(source, directory) as db:
                for (table,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name GLOB 'Chat_*'"
                ):
                    _validate_table(table)
                    if table in contacts:
                        count, latest = db.execute(
                            f'SELECT COUNT(*), MAX(CreateTime) FROM "{table}"'
                        ).fetchone()
                        previous_count, previous_latest = totals.get(table, (0, None))
                        if latest is None:
                            latest = previous_latest
                        elif previous_latest is not None:
                            latest = max(previous_latest, latest)
                        totals[table] = previous_count + count, latest
        return sorted(
            (
                Conversation(table, contacts[table][1], count, latest if latest is not None else 0)
                for table, (count, latest) in totals.items()
            ),
            key=lambda conversation: (-conversation.last_timestamp, conversation.table),
        )


def export_conversation_jsonl(backup: Path, account: str, table: str, out_path: Path) -> int:
    """Merge all shards of a selected personal conversation into one new JSONL file."""
    with _backup(backup) as (files, directory):
        _validate_table(table)
        contacts = _contacts(files, directory, account)
        if table not in contacts:
            raise IosBackupError("conversation not found")
        rows = []
        found = False
        for source in _shards(files, account):
            with _copied_database(source, directory) as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone():
                    found = True
                    rows.extend(
                        db.execute(
                            "SELECT MesLocalID, MesSvrID, CreateTime, Des, Type, Message "
                            f'FROM "{table}"'
                        )
                    )
        if not found:
            raise IosBackupError("conversation not found")
        ids = set()
        records = []
        user, name = contacts[table]
        for local_id, server_id, timestamp, direction, kind, body in rows:
            if type(timestamp) is not int or type(kind) is not int:
                raise IosBackupError("invalid message data")
            text = None
            if kind == 1:
                if type(server_id) is not int or server_id <= 0 or server_id in ids:
                    raise IosBackupError("invalid message id")
                ids.add(server_id)
                text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
                if not isinstance(text, str):
                    raise IosBackupError("invalid message data")
            record = dict(
                conversation_id="share-chat",
                timestamp=timestamp,
                type=kind,
                text=text,
                server_id=server_id,
                is_sender=direction == 0,
                sender_id=account if direction == 0 else user,
                sender="" if direction == 0 else name,
            )
            records.append((timestamp, local_id, record))
        records.sort(key=lambda item: (item[0], item[1]))
        next_id = (
            max(
                (
                    record["server_id"]
                    for _, _, record in records
                    if type(record["server_id"]) is int and record["server_id"] > 0
                ),
                default=0,
            )
            + 1
        )
        # All text IDs are already reserved, including those later in the conversation.
        for _, _, record in records:
            if record["type"] != 1:
                server_id = record["server_id"]
                if type(server_id) is not int or server_id <= 0 or server_id in ids:
                    server_id = next_id
                    record["server_id"] = server_id
                    next_id += 1
                ids.add(server_id)
        try:
            descriptor = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise IosBackupError("output already exists") from None
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            for _, _, record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(records)
