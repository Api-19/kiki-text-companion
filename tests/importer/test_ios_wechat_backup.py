"""Synthetic Finder/iTunes backup contracts; no device or real backup access."""

import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

import pytest

from companion.importer import ios_wechat_backup as ios
from companion.runtime.settings import Settings

ACCOUNT = "abcdef" + "1" * 26
OTHER_ACCOUNT = "abcdef" + "2" * 26
PEER = "synthetic_person"
OTHER_PEER = "synthetic_other_person"
DOMAIN = "AppDomain-com.tencent.xin"


def chat_table(user):
    return "Chat_" + hashlib.md5(user.encode()).hexdigest()


TABLE = chat_table(PEER)


def name_blob(nickname="", remark=""):
    # WCDB contact protobuf fields 1 (nickname) and 3 (remark).
    result = bytearray()
    for tag, text in ((1, nickname), (3, remark)):
        encoded = text.encode()
        result.extend((tag << 3 | 2, len(encoded)))
        result.extend(encoded)
    return bytes(result)


class SyntheticBackup:
    def __init__(self, root):
        self.path = root
        root.mkdir()
        (root / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
        with closing(sqlite3.connect(root / "Manifest.db")) as db:
            db.execute(
                "CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INT)"
            )
            db.commit()

    def register(self, relative, content=b"", domain=DOMAIN):
        file_id = hashlib.sha1((domain + "-" + relative).encode()).hexdigest()
        target = self.path / file_id[:2] / file_id
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)
        with closing(sqlite3.connect(self.path / "Manifest.db")) as db:
            db.execute("INSERT INTO Files VALUES (?, ?, ?, 1)", (file_id, domain, relative))
            db.commit()
        return target

    def contacts(self, contacts, account=ACCOUNT):
        path = self.register(f"Documents/{account}/DB/WCDB_Contact.sqlite")
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE Friend (userName TEXT, dbContactRemark BLOB)")
            db.executemany("INSERT INTO Friend VALUES (?, ?)", contacts.items())
            db.commit()
        return path

    def messages(self, shard, conversations, account=ACCOUNT):
        path = self.register(f"Documents/{account}/DB/message_{shard}.sqlite")
        with closing(sqlite3.connect(path)) as db:
            for table, rows in conversations.items():
                quoted = table.replace('"', '""')
                db.execute(
                    f'CREATE TABLE "{quoted}" '
                    "(MesLocalID INTEGER, MesSvrID, CreateTime, Des INTEGER, Type, Message)"
                )
                db.executemany(f'INSERT INTO "{quoted}" VALUES (?, ?, ?, ?, ?, ?)', rows)
            db.commit()
        return path


@pytest.fixture
def backup(tmp_path):
    result = SyntheticBackup(tmp_path / "backup")
    result.contacts(
        {
            PEER: name_blob("虚构昵称", "虚构备注"),
            OTHER_PEER: name_blob("另一联系人"),
            "synthetic@chatroom": name_blob("群聊"),
            "gh_synthetic": name_blob("公众号"),
            "filehelper": name_blob("服务会话"),
            "synthetic@openim": name_blob("非普通个人会话"),
        }
    )
    result.messages(
        1,
        {
            TABLE: [
                (4, 104, 1700000020, 1, 1, "最后一条\n🙂"),
                (2, 102, 1700000010, 0, 1, "同秒较早"),
            ],
            chat_table(OTHER_PEER): [(1, 201, 1700000040, 1, 1, "UNSELECTED_BODY")],
            chat_table("synthetic@chatroom"): [(1, 301, 1700000050, 1, 1, "群聊内容")],
            chat_table("gh_synthetic"): [(1, 401, 1700000060, 1, 1, "公众号内容")],
            chat_table("filehelper"): [(1, 501, 1700000070, 1, 1, "服务内容")],
            chat_table("synthetic@openim"): [(1, 601, 1700000080, 1, 1, "其他会话")],
            chat_table("missing_contact"): [(1, 701, 1700000090, 1, 1, "无联系人内容")],
        },
    )
    result.messages(
        5,
        {
            TABLE: [(3, 103, 1700000010, 1, 1, "同秒较晚"), (1, 101, 1700000000, 0, 1, "第一条")],
        },
    )
    result.messages(2, {}, account=OTHER_ACCOUNT)
    return result


@pytest.fixture(autouse=True)
def temporary_copies(tmp_path, monkeypatch):
    paths = []
    original = ios.tempfile.mkdtemp

    def tracked(*args, **kwargs):
        path = Path(original(*args, dir=tmp_path, **kwargs))
        paths.append(path)
        return str(path)

    monkeypatch.setattr(ios.tempfile, "mkdtemp", tracked)
    yield paths
    assert all(not path.exists() for path in paths)


def test_list_accounts_and_metadata_without_reading_message(backup, monkeypatch):
    original = sqlite3.connect
    connections = []

    def connect(path, *args, **kwargs):
        assert Path(path).parent != backup.path
        assert backup.path not in Path(path).parents
        db = original(path, *args, **kwargs)
        connections.append(db)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and column == "Message":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(ios.sqlite3, "connect", connect)
    assert ios.list_accounts(backup.path) == [ACCOUNT, OTHER_ACCOUNT]
    assert ios.list_conversations(backup.path, ACCOUNT) == [
        ios.Conversation(chat_table(OTHER_PEER), "另一联系人", 1, 1700000040),
        ios.Conversation(TABLE, "虚构备注", 4, 1700000020),
    ]
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


def test_export_merges_shards_and_converts(backup, tmp_path):
    output = tmp_path / "selected.jsonl"
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output) == 4
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["server_id"] for record in records] == [101, 102, 103, 104]
    assert all(
        set(record)
        == {
            "conversation_id",
            "timestamp",
            "type",
            "text",
            "server_id",
            "is_sender",
            "sender_id",
            "sender",
        }
        for record in records
    )
    assert all(record["conversation_id"] == "share-chat" for record in records)
    assert [record["sender_id"] for record in records] == [ACCOUNT, ACCOUNT, PEER, PEER]
    assert [record["sender"] for record in records] == ["", "", "虚构备注", "虚构备注"]
    assert [record["is_sender"] for record in records] == [True, True, False, False]
    assert records[-1]["text"] == "最后一条\n🙂"
    assert b"\r\n" not in output.read_bytes()

    script = Path(__file__).resolve().parents[2] / "scripts/convert_chat_jsonl.py"
    spec = importlib.util.spec_from_file_location("ios_backup_conversion", script)
    converter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = converter
    try:
        spec.loader.exec_module(converter)
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        settings = Settings(
            environment="test",
            log_level="ERROR",
            private_data_dir=private,
            runtime_data_dir=tmp_path,
            test_results_dir=tmp_path,
        )
        converted = converter.convert(
            output,
            settings,
            conversation_id="share-chat",
            through_date=date(2099, 12, 30),
            tz=ZoneInfo("Asia/Shanghai"),
        )
        assert converted.included == len(records)
        assert len(json.loads(converted.output.read_text(encoding="utf-8"))) == len(records)
    finally:
        del sys.modules[spec.name]


def test_only_selected_conversation_body_is_exported(backup, tmp_path):
    output = tmp_path / "selected.jsonl"
    ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output)
    assert "UNSELECTED_BODY" not in output.read_text(encoding="utf-8")


def test_invalid_utf8_is_exported_and_converter_skips_garbled_text(tmp_path, monkeypatch):
    backup = SyntheticBackup(tmp_path / "backup")
    backup.contacts({PEER: name_blob("虚构昵称")})
    backup.messages(
        1,
        {
            TABLE: [
                (1, 101, 1700000000, 0, 1, b"invalid \xff text"),
                (2, 102, 1700000001, 1, 1, "正常文本"),
            ]
        },
    )
    output = tmp_path / "selected.jsonl"
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output) == 2
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[0]["text"] == "invalid � text"
    assert records[1]["text"] == "正常文本"

    script = Path(__file__).resolve().parents[2] / "scripts/convert_chat_jsonl.py"
    spec = importlib.util.spec_from_file_location("ios_backup_garbled_conversion", script)
    converter = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, converter)
    spec.loader.exec_module(converter)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    settings = Settings(
        environment="test",
        log_level="ERROR",
        private_data_dir=private,
        runtime_data_dir=tmp_path,
        test_results_dir=tmp_path,
    )
    converted = converter.convert(
        output,
        settings,
        conversation_id="share-chat",
        through_date=date(2099, 12, 30),
        tz=ZoneInfo("Asia/Shanghai"),
    )
    assert converted.included == 1
    assert converted.garbled_text == 1
    normalized = json.loads(converted.output.read_text(encoding="utf-8"))
    assert [record["text"] for record in normalized] == ["正常文本"]


@pytest.mark.parametrize(
    "public",
    [
        "list_accounts",
        "list_conversations",
        "export_conversation_jsonl",
    ],
)
def test_public_backup_operations_reject_encryption(backup, tmp_path, public):
    (backup.path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    arguments = {
        "list_accounts": (backup.path,),
        "list_conversations": (backup.path, ACCOUNT),
        "export_conversation_jsonl": (backup.path, ACCOUNT, TABLE, tmp_path / "out.jsonl"),
    }
    with pytest.raises(ios.IosBackupError, match=r"^encrypted backup$"):
        getattr(ios, public)(*arguments[public])


def test_is_encrypted_returns_true_for_encrypted_backup(backup):
    (backup.path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    assert ios.is_encrypted(backup.path) is True


def test_find_backups_includes_encrypted_and_unencrypted(tmp_path, monkeypatch):
    encrypted = SyntheticBackup(tmp_path / "a-encrypted")
    unencrypted = SyntheticBackup(tmp_path / "z-unencrypted")
    (encrypted.path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))

    def reject_encryption_check(backup):
        raise AssertionError("discovery must not inspect encryption")

    monkeypatch.setattr(ios, "is_encrypted", reject_encryption_check)
    assert ios.find_backups([tmp_path]) == [encrypted.path, unencrypted.path]


def test_missing_encryption_flag_or_plist_is_unencrypted(backup):
    assert ios.is_encrypted(backup.path) is False
    (backup.path / "Manifest.plist").write_bytes(plistlib.dumps({}))
    assert ios.is_encrypted(backup.path) is False
    (backup.path / "Manifest.plist").unlink()
    assert ios.is_encrypted(backup.path) is False


@pytest.mark.parametrize(
    "contents",
    [
        b"not a plist",
        b"<?xml version='1.0'?><plist><dict>",
        plistlib.dumps([]),
    ],
)
def test_invalid_manifest_has_fixed_error_category(backup, contents):
    (backup.path / "Manifest.plist").write_bytes(contents)
    with pytest.raises(ios.IosBackupError, match=r"^invalid backup manifest$"):
        ios.is_encrypted(backup.path)


def test_metadata_maximum_is_not_biased_by_empty_shards_or_zero(tmp_path):
    backup = SyntheticBackup(tmp_path / "negative-timestamps")
    backup.contacts({PEER: name_blob("虚构昵称")})
    backup.messages(1, {TABLE: [(1, 101, -20, 0, 1, "early")]})
    backup.messages(5, {TABLE: [(2, 102, -10, 1, 1, "later")]})
    backup.messages(8, {TABLE: []})
    assert ios.list_conversations(backup.path, ACCOUNT) == [
        ios.Conversation(TABLE, "虚构昵称", 2, -10),
    ]


@pytest.mark.parametrize("operation", ["list", "export"])
def test_reject_nonempty_wal(backup, tmp_path, operation):
    backup.register(f"Documents/{ACCOUNT}/DB/message_5.sqlite-wal", b"synthetic WAL")
    with pytest.raises(ios.IosBackupError, match=r"^wal not supported$"):
        if operation == "list":
            ios.list_conversations(backup.path, ACCOUNT)
        else:
            ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, tmp_path / "out.jsonl")


def test_empty_wal_is_accepted(backup, tmp_path):
    backup.register(f"Documents/{ACCOUNT}/DB/message_5.sqlite-wal")
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, tmp_path / "out.jsonl") == 4


@pytest.mark.parametrize("server_id", [0, -1, "105", None, 101])
def test_invalid_or_cross_shard_duplicate_text_ids(backup, tmp_path, server_id):
    backup.messages(8, {TABLE: [(8, server_id, 1700000100, 1, 1, "无效 ID")]})
    with pytest.raises(ios.IosBackupError, match=r"^invalid message id$"):
        ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, tmp_path / "out.jsonl")


def test_nontext_ids_are_repaired_and_text_is_null(backup, tmp_path):
    backup.messages(
        12, {TABLE: [(8, 0, 1700000100, 1, 3, b"image"), (9, 101, 1700000101, 0, 34, "audio")]}
    )
    output = tmp_path / "out.jsonl"
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output) == 6
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["text"] for record in records[-2:]] == [None, None]
    assert [record["server_id"] for record in records[-2:]] == [105, 106]


@pytest.mark.parametrize(
    ("media_ids", "expected_ids"),
    [
        ((0, 0), (104, 105)),
        ((500, 500), (500, 501)),
        ((102, 102), (104, 105)),
        ((-1, None), (104, 105)),
        (("102", 1.5), (104, 105)),
        ((b"invalid-id", 700), (701, 700)),
    ],
)
def test_repaired_media_ids_preserve_converted_boundaries(
    tmp_path,
    monkeypatch,
    media_ids,
    expected_ids,
):
    backup = SyntheticBackup(tmp_path / "backup")
    backup.contacts({PEER: name_blob("虚构昵称")})
    backup.messages(
        1,
        {
            TABLE: [
                (5, 103, 1700000003, 0, 1, "末条文本"),
                (4, media_ids[1], 1700000002, 1, 3, b"image"),
                (1, 101, 1700000000, 1, 1, "首条文本"),
            ]
        },
    )
    backup.messages(
        5,
        {
            TABLE: [
                (3, 102, 1700000001, 0, 1, "中间文本"),
                (2, media_ids[0], 1700000001, 1, 3, b"image"),
            ]
        },
    )
    output = tmp_path / "selected.jsonl"
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output) == 5
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["server_id"] for record in records] == [
        101,
        expected_ids[0],
        102,
        expected_ids[1],
        103,
    ]
    assert len({record["server_id"] for record in records}) == 5
    assert all(type(record["server_id"]) is int and record["server_id"] > 0 for record in records)
    repeated = tmp_path / "repeated.jsonl"
    assert ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, repeated) == 5
    assert repeated.read_bytes() == output.read_bytes()

    script = Path(__file__).resolve().parents[2] / "scripts/convert_chat_jsonl.py"
    spec = importlib.util.spec_from_file_location("ios_backup_media_conversion", script)
    converter = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, converter)
    spec.loader.exec_module(converter)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    settings = Settings(
        environment="test",
        log_level="ERROR",
        private_data_dir=private,
        runtime_data_dir=tmp_path,
        test_results_dir=tmp_path,
    )
    converted = converter.convert(
        output,
        settings,
        conversation_id="share-chat",
        keep_nontext_markers=True,
        through_date=date(2099, 12, 30),
        tz=ZoneInfo("Asia/Shanghai"),
    )
    assert converted.included == 5
    assert converted.nontext_kept == 2
    assert converted.nontext_skipped == 0
    normalized = json.loads(converted.output.read_text(encoding="utf-8"))
    assert [record["content_type"] for record in normalized] == [
        "text",
        "image",
        "text",
        "image",
        "text",
    ]
    assert [record["text"] for record in normalized] == [
        "首条文本",
        None,
        "中间文本",
        None,
        "末条文本",
    ]


@pytest.mark.parametrize(
    ("column", "value"), [("CreateTime", "bad"), ("CreateTime", 1.5), ("Type", "1"), ("Type", None)]
)
def test_timestamp_and_type_require_integers(backup, tmp_path, column, value):
    row = [8, 108, 1700000100, 1, 1, "synthetic"]
    row[{"CreateTime": 2, "Type": 4}[column]] = value
    backup.messages(8, {TABLE: [row]})
    with pytest.raises(ios.IosBackupError, match=r"^invalid message data$"):
        ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, tmp_path / "out.jsonl")


@pytest.mark.parametrize("bad_id", ["../" + "a" * 37, "a" * 39, "g" * 40, "a" * 41])
def test_manifest_file_id_is_validated(backup, bad_id):
    with closing(sqlite3.connect(backup.path / "Manifest.db")) as db:
        db.execute(
            "UPDATE Files SET fileID = ? WHERE relativePath = ?",
            (bad_id, f"Documents/{ACCOUNT}/DB/message_5.sqlite"),
        )
        db.commit()
    with pytest.raises(ios.IosBackupError, match=r"^invalid file id$"):
        ios.list_accounts(backup.path)


@pytest.mark.parametrize(
    "table", ["Chat_bad", "Chat_" + "A" * 32, TABLE + '"; DROP TABLE Friend; --', "../" + TABLE]
)
def test_export_rejects_invalid_table_names(backup, tmp_path, table):
    with pytest.raises(ios.IosBackupError, match=r"^invalid table name$"):
        ios.export_conversation_jsonl(backup.path, ACCOUNT, table, tmp_path / "out.jsonl")


def test_listing_rejects_invalid_chat_table_name(backup):
    backup.messages(8, {"Chat_bad": []})
    with pytest.raises(ios.IosBackupError, match=r"^invalid table name$"):
        ios.list_conversations(backup.path, ACCOUNT)


@pytest.mark.parametrize(
    "user", ["synthetic@chatroom", "gh_synthetic", "filehelper", "missing_contact", "absent"]
)
def test_unresolved_or_nonpersonal_conversations_cannot_be_exported(backup, tmp_path, user):
    with pytest.raises(ios.IosBackupError, match=r"^conversation not found$"):
        ios.export_conversation_jsonl(backup.path, ACCOUNT, chat_table(user), tmp_path / "out")


def test_existing_output_is_unchanged(backup, tmp_path):
    output = tmp_path / "existing.jsonl"
    output.write_bytes(b"existing contents\n")
    with pytest.raises(ios.IosBackupError, match=r"^output already exists$"):
        ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output)
    assert output.read_bytes() == b"existing contents\n"


@pytest.mark.parametrize("fail_before_export", [False, True])
def test_cleanup_failure_is_reported(
    backup, tmp_path, monkeypatch, temporary_copies, fail_before_export
):
    original = shutil.rmtree

    def fail_cleanup(path):
        raise PermissionError("synthetic cleanup failure")

    monkeypatch.setattr(ios.shutil, "rmtree", fail_cleanup)
    try:
        with pytest.raises(ios.IosBackupError, match=r"^temporary cleanup failed$"):
            ios.export_conversation_jsonl(
                backup.path,
                ACCOUNT,
                "Chat_bad" if fail_before_export else TABLE,
                tmp_path / "out.jsonl",
            )
        assert temporary_copies
    finally:
        for path in temporary_copies:
            original(path)


def test_database_failure_closes_connection_and_cleans_temp(backup, monkeypatch):
    original = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        connections.append(db)
        db.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
        return db

    monkeypatch.setattr(ios.sqlite3, "connect", connect)
    with pytest.raises(ios.IosBackupError, match=r"^backup operation failed$"):
        ios.list_accounts(backup.path)
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


def test_temp_directory_creation_failure_stops_before_database_copy(
    backup, monkeypatch, temporary_copies, capsys
):
    calls = []
    before = list(temporary_copies)

    def fail_mkdtemp(*args, **kwargs):
        raise OSError("SYNTHETIC_TEMP_DETAIL")

    monkeypatch.setattr(ios.tempfile, "mkdtemp", fail_mkdtemp)
    monkeypatch.setattr(ios.shutil, "copyfile", lambda *args, **kwargs: calls.append("copy"))
    monkeypatch.setattr(ios.sqlite3, "connect", lambda *args, **kwargs: calls.append("connect"))
    monkeypatch.setattr(ios.shutil, "rmtree", lambda *args, **kwargs: calls.append("rmtree"))

    with pytest.raises(ios.IosBackupError) as error:
        ios.list_accounts(backup.path)
    assert str(error.value) == "backup operation failed"
    assert calls == []
    assert temporary_copies == before
    captured = capsys.readouterr()
    assert "SYNTHETIC_TEMP_DETAIL" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("blob", "expected"),
    [(name_blob("昵称", "备注"), "备注"), (name_blob("昵称"), "昵称"), (None, "")],
)
def test_contact_display_name_and_received_sender(backup, tmp_path, blob, expected):
    file_id = hashlib.sha1(
        (DOMAIN + f"-Documents/{ACCOUNT}/DB/WCDB_Contact.sqlite").encode()
    ).hexdigest()
    with closing(sqlite3.connect(backup.path / file_id[:2] / file_id)) as db:
        db.execute("UPDATE Friend SET dbContactRemark = ? WHERE userName = ?", (blob, PEER))
        db.commit()
    selected = next(
        item for item in ios.list_conversations(backup.path, ACCOUNT) if item.table == TABLE
    )
    assert selected.display_name == expected
    output = tmp_path / "out.jsonl"
    ios.export_conversation_jsonl(backup.path, ACCOUNT, TABLE, output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["sender"] == expected


def test_find_backups_is_one_level_and_sorted(tmp_path):
    root = tmp_path / "roots"
    root.mkdir()
    z = SyntheticBackup(root / "z-backup")
    a = SyntheticBackup(root / "a-backup")
    (root / "not-a-backup").mkdir()
    nested = root / "container"
    nested.mkdir()
    SyntheticBackup(nested / "deep-backup")
    os.utime(z.path / "Manifest.db", (2, 2))
    os.utime(a.path / "Manifest.db", (1, 1))
    assert ios.find_backups([root, tmp_path / "missing"]) == [a.path, z.path]


def test_default_mac_root_only_if_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ios.default_backup_roots("posix") == []
    expected = tmp_path / "Library/Application Support/MobileSync/Backup"
    expected.mkdir(parents=True)
    assert ios.default_backup_roots("posix") == [expected]
    if os.name == "posix":
        assert ios.default_backup_roots() == [expected]


@pytest.mark.parametrize("failure", ["default_root_stat", "explicit_root_listing"])
def test_backup_discovery_oserror_has_fixed_category(tmp_path, monkeypatch, capsys, failure):
    detail = "SYNTHETIC_DISCOVERY_DETAIL"
    root = tmp_path / "root"
    root.mkdir()
    mac_root = tmp_path / "Library/Application Support/MobileSync/Backup"
    original_is_dir = Path.is_dir
    original_iterdir = Path.iterdir

    def is_dir(path):
        if failure == "default_root_stat" and path == mac_root:
            raise OSError(f"{detail} {tmp_path}")
        return original_is_dir(path)

    def iterdir(path):
        if failure == "explicit_root_listing" and path == root:
            raise OSError(f"{detail} {tmp_path}")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(ios.IosBackupError) as error:
        if failure == "default_root_stat":
            ios.default_backup_roots("posix")
        else:
            ios.find_backups([root])
    assert str(error.value) == "backup discovery failed"
    captured = capsys.readouterr()
    for text in (str(error.value), captured.out, captured.err):
        assert detail not in text
        assert str(tmp_path) not in text


def test_default_windows_roots_preserve_order_and_windows_semantics(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Synthetic User")
    monkeypatch.setenv("APPDATA", r"D:\Profile\Roaming")
    expected = [
        PureWindowsPath(r"C:\Users\Synthetic User\Apple\MobileSync\Backup"),
        PureWindowsPath(r"D:\Profile\Roaming\Apple Computer\MobileSync\Backup"),
    ]
    existing = set(expected)
    monkeypatch.setattr(ios, "Path", PureWindowsPath)
    monkeypatch.setattr(PureWindowsPath, "is_dir", lambda path: path in existing, raising=False)
    assert ios.default_backup_roots("nt") == expected
    existing.remove(expected[0])
    assert ios.default_backup_roots("nt") == expected[1:]
    monkeypatch.delenv("APPDATA")
    assert ios.default_backup_roots("nt") == []
