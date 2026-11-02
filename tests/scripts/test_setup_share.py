# ruff: noqa: RUF001
"""Share setup uses synthetic chats, a temporary home and fake credentials only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import socket
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from companion.dialogue.config import load_dialogue_config
from companion.dialogue.models import MAX_PROMPT_UTF8_BYTES, BudgetConfig
from companion.importer.standard_json import StandardJsonAdapter
from companion.privacy.paired_snippets import read_snippets
from companion.privacy.veiled import load_veiled_terms
from companion.runtime import share_credentials, text_chat
from companion.runtime.settings import load_settings
from companion.runtime.share_credentials import CredentialError, normalize_base_url
from scripts import setup_share as setup

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_KEY = "synthetic-setup-key"


@pytest.fixture
def session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.chdir(ROOT)
    for key in os.environ:
        if key.startswith("COMPANION_"):
            monkeypatch.delenv(key)
    # Register restoration even for environment variables assigned by main().
    for key in ("PRIVATE_DATA", "RUNTIME_DATA", "TEST_RESULTS"):
        monkeypatch.setenv(f"COMPANION_{key}_DIR", str(tmp_path / "unused"))
    keys = {}

    def save(env_name, base_url, key):
        if not key.strip():
            raise ValueError("empty api key")
        keys[env_name, normalize_base_url(base_url)] = key.strip()

    def read(env_name, base_url):
        try:
            return keys[env_name, normalize_base_url(base_url)]
        except KeyError:
            raise CredentialError("api key not found") from None

    store = Mock(side_effect=save)
    load = Mock(side_effect=read)
    monkeypatch.setattr(setup, "store_api_key", store)
    monkeypatch.setattr(setup, "load_api_key", load)
    monkeypatch.setattr(setup, "current_user_id", lambda: "synthetic-local-user")
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: SYNTHETIC_KEY)
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network")))
    return SimpleNamespace(home=home, paths=setup.share_paths(), store=store, load=load, save=save)


def answer(monkeypatch, *values):
    pending = iter(values)
    prompts = []

    def input_value(prompt):
        prompts.append(prompt)
        return next(pending)

    monkeypatch.setattr("builtins.input", input_value)
    return prompts


def standard_rows(count=40):
    rows = []
    for index in range(count):
        for owner in (False, True):
            rows.append({
                "schema_version": 1,
                "conversation_id": "share-chat",
                "message_id": str(index * 2 + int(owner) + 1),
                "timestamp": (datetime(2026, 1, 1, tzinfo=UTC)
                              + timedelta(hours=index, minutes=int(owner))).isoformat(),
                "source_speaker_id": "synthetic-owner" if owner else "synthetic-partner",
                "source_speaker_name": None,
                "direction": "outbound" if owner else "inbound",
                "content_type": "text",
                "text": (f"合成回复{chr(0x4E00 + index * 29)}呀" if owner
                         else f"合成话题{chr(0x4E00 + index * 29)}继续聊"),
            })
    return rows


def chat_file(tmp_path, count=40):
    path = tmp_path / "synthetic-standard.json"
    path.write_text(json.dumps(standard_rows(count), ensure_ascii=False), encoding="utf-8")
    return path


def jsonl_file(tmp_path, rows):
    path = tmp_path / "synthetic.jsonl"
    records = [dict(
        timestamp=int(datetime.fromisoformat(item["timestamp"]).timestamp()),
        type=1 if item["content_type"] == "text" else 3,
        server_id=int(item["message_id"]),
        is_sender=item["direction"] == "outbound",
        sender_id=item["source_speaker_id"], sender="虚构",
        conversation_id="share-chat", text=item["text"], raw="UNDECODED_SYNTHETIC_RAW",
    ) for item in rows]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    return path


def standard_args(path, *extra):
    return ["--format", "standard", "--chat", str(path), *extra]


def config_path(session):
    return session.paths.private / "dialogue/runtime-config.json"


def assert_ready(session, api_key_env="DEEPSEEK_API_KEY"):
    private = session.paths.private
    consent = json.loads((private / "share/consent.json").read_text(encoding="utf-8"))
    assert consent == {
        "version": 1,
        "agreed_at": consent["agreed_at"],
        "consent_sha256": hashlib.sha256(setup.CONSENT_TEXT.encode("utf-8")).hexdigest(),
    }
    assert datetime.fromisoformat(consent["agreed_at"]).tzinfo is not None
    library = read_snippets(private / "style/snippets/library.jsonl")
    holdout = read_snippets(private / "style/snippets/holdout.jsonl")
    assert library
    assert {item.snippet_id for item in library}.isdisjoint(item.snippet_id for item in holdout)
    assert (private / "style/snippets/habit_card.txt").is_file()
    assert json.loads((private / "share/terms.json").read_text()) == {}
    assert load_veiled_terms(private / "privacy/veiled_terms.json").scan("合成").high == ()
    payload = json.loads(config_path(session).read_text(encoding="utf-8"))
    assert set(payload) == {"provider", "budget", "snippet_style"}
    assert "shared_terms_source" not in payload["snippet_style"]
    assert payload["snippet_style"]["style_version"] == "share-v1"
    assert payload["snippet_style"]["partner_user_id"] == "synthetic-local-user"
    config, _ = load_dialogue_config(load_settings(ROOT), environ={api_key_env: "offline-check"})
    assert config.snippet_style.library_source == private / "style/snippets/library.jsonl"
    assert config.style is None and config.style_framework is None
    assert config.provider.timeout_seconds == 30
    budget = BudgetConfig.model_validate(payload["budget"])
    assert budget.monthly_cap_cny == Decimal("100.00")
    assert budget.worst_case_request_cny == Decimal("0.21")
    assert budget.max_output_tokens == 512
    assert budget.worst_case_request_cny >= (
        Decimal(MAX_PROMPT_UTF8_BYTES) * budget.input_cny_per_million
        + Decimal(budget.max_output_tokens) * budget.output_cny_per_million
    ) / Decimal(1_000_000)
    assert os.environ["COMPANION_PRIVATE_DATA_DIR"] == str(private)
    assert os.environ["COMPANION_RUNTIME_DATA_DIR"] == str(session.paths.runtime)
    assert os.environ["COMPANION_TEST_RESULTS_DIR"] == str(session.paths.test_results)
    return config, library, holdout


def test_standard_full_setup(session, tmp_path, monkeypatch, capsys):
    prompts = answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(standard_args(chat_file(tmp_path), "--owner-pronoun", "她",
                                    "--partner-pronoun", "他")) == 0
    config, library, holdout = assert_ready(session)
    assert config.snippet_style.pronouns.model_dump() == {"owner": "她", "partner": "他"}
    assert str(config.provider.base_url) == "https://api.deepseek.com/v1"
    assert config.provider.model == "deepseek-chat"
    assert len(library) >= 30 and holdout
    session.store.assert_called_once_with("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL, SYNTHETIC_KEY)
    assert prompts == [
        "被模仿的人（主人）如同意，请输入“同意”两个字后回车：",
        "陪聊的人（对方）如同意，请输入“同意”两个字后回车：",
        "模型服务地址（直接回车用 https://api.deepseek.com/v1）：",
        "模型名（直接回车用 deepseek-chat）：",
    ]
    output = capsys.readouterr().out
    assert f"片段数：{len(library)}" in output
    assert f"留出数：{len(holdout)}" in output
    assert "样本结论：通过" in output
    assert "自动检查通过；是否像本人未经人工验证。" in output
    assert '以后双击"开始聊天"即可。' in output


def test_jsonl_full_setup(session, tmp_path, monkeypatch):
    # Same exporter field shape as tests/importer/test_jsonl_conversion_script.py.
    source = jsonl_file(tmp_path, standard_rows())
    answer(monkeypatch, "同意", "同意", "", "", "https://example.invalid/v1", "synthetic-model")
    assert setup.main(["--format", "jsonl", "--chat", str(source), "--through-date",
                       "2026-01-02", "--timezone", "America/New_York"]) == 0
    config, _, _ = assert_ready(session, "OPENAI_API_KEY")
    assert config.provider.model == "synthetic-model"
    session.store.assert_called_once_with(
        "OPENAI_API_KEY", "https://example.invalid/v1", SYNTHETIC_KEY,
    )


@pytest.mark.parametrize(("raw_url", "normalized"), [
    ("https://api.example.com:443/v1", "https://api.example.com/v1"),
    ("https://api.example.com/合成/v1/", "https://api.example.com/%E5%90%88%E6%88%90/v1"),
])
def test_setup_credentials_match_validated_chat_address(
    session, tmp_path, monkeypatch, raw_url, normalized,
):
    answer(monkeypatch, "同意", "同意", "", "", raw_url, "")
    assert setup.main(standard_args(chat_file(tmp_path))) == 0
    session.store.assert_called_once_with("OPENAI_API_KEY", normalized, SYNTHETIC_KEY)
    assert not (session.paths.private / "share/prepared.jsonl").exists()
    monkeypatch.setattr(text_chat, "load_api_key", session.load)
    monkeypatch.setattr(text_chat, "current_user_id", lambda: "synthetic-local-user")
    bridge = Mock()
    build = Mock(return_value=bridge)
    monkeypatch.setattr(text_chat, "_build_bridge", build)
    answer(monkeypatch, "/exit")

    assert text_chat.main([]) == 0

    build.assert_called_once()
    bridge.close.assert_called_once()
    assert session.load.call_args.args == ("OPENAI_API_KEY", normalized)


@pytest.fixture
def backup_helpers():
    # Reuse the original constructor without changing importer assertions or pytest imports.
    spec = importlib.util.spec_from_file_location(
        "setup_synthetic_backup", ROOT / "tests/importer/test_ios_wechat_backup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_backup(helpers, path, rows=None):
    backup = helpers.SyntheticBackup(path)
    backup.contacts({helpers.PEER: helpers.name_blob("合成联系人")})
    backup.messages(1, {helpers.TABLE: [
        (index + 1, int(item["message_id"]),
         int(datetime.fromisoformat(item["timestamp"]).timestamp()),
         0 if item["direction"] == "outbound" else 1,
         1 if item["content_type"] == "text" else 3, item["text"])
        for index, item in enumerate(standard_rows() if rows is None else rows)
    ]})
    backup.messages(2, {}, account=helpers.OTHER_ACCOUNT)
    return backup


def test_ios_backup_full_setup(session, tmp_path, monkeypatch, capsys, backup_helpers):
    root = tmp_path / "backups"
    root.mkdir()
    backup_helpers.SyntheticBackup(root / "a-backup")
    selected = make_backup(backup_helpers, root / "b-backup")
    monkeypatch.setattr(setup.ios, "default_backup_roots", lambda: [root])
    prompts = answer(monkeypatch, "同意", "同意", "", "", "2", "1", "1", "", "")
    assert setup.main([]) == 0
    assert_ready(session)
    assert (session.paths.private / "share/export.jsonl").is_file()
    assert len(prompts) == 9
    assert prompts[2:5] == [
        "被模仿的人是男生还是女生？输入 他 或 她（直接回车为 他）：",
        "陪聊的人是男生还是女生？输入 他 或 她（直接回车为 她）：",
        "请选择备份编号：",
    ]
    output = capsys.readouterr().out
    assert "1. a-backup" in output and "2. b-backup" in output
    assert "1. 合成联系人（80，2026-01-02）" in output
    # Rebuild must remove the exclusive export output before exporting again.
    answer(monkeypatch, "同意", "同意", "1", "1", "", "")
    assert setup.main(["--backup", str(selected.path), "--rebuild"]) == 0


def test_encrypted_backup_fails(session, tmp_path, monkeypatch, capsys, backup_helpers):
    backup = backup_helpers.SyntheticBackup(tmp_path / "encrypted")
    (backup.path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(["--backup", str(backup.path)]) == 1
    assert not config_path(session).exists()
    assert "请在 Finder/iTunes 里关闭'加密本地备份'后重新备份" in capsys.readouterr().out


def test_no_backup_fails(session, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.ios, "default_backup_roots", lambda: [tmp_path / "absent"])
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([]) == 1
    assert "设置没有完成：找不到 iPhone 备份" in capsys.readouterr().out
    assert not config_path(session).exists()


def test_consent_discloses_unreplaced_names():
    assert ("人名、地名、学校、单位等信息不会被替换，会随聊天片段原样发送给你选择的云端模型服务。"
            in setup.CONSENT_TEXT)
    for text in ("只学习主人发出的消息", "对方消息只作上下文", "数据只存在本机",
                 "第三方导出工具可能违反微信用户协议", "由使用者自行判断"):
        assert text in setup.CONSENT_TEXT


@pytest.mark.parametrize("answers", [("不同意",), ("同意", "不同意")])
def test_consent_refusal_creates_nothing(session, monkeypatch, answers):
    answer(monkeypatch, *answers)
    assert setup.main([]) == 2
    assert list(session.home.iterdir()) == []
    session.store.assert_not_called()


def test_rebuild_preserves_deletions_ledger_and_key(session, tmp_path, monkeypatch, capsys):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(args) == 0
    _, original_library, _ = assert_ready(session)
    card = session.paths.private / "style/snippets/habit_card.txt"
    card.write_text("stale synthetic card", encoding="utf-8")
    deleted = card.with_name("deleted_ids.json")
    saved = json.dumps({
        "snippet_ids": [],
        "ranges": [[original_library[0].started_at.isoformat()] * 2],
    })
    deleted.write_text(saved, encoding="utf-8")
    ledger = session.paths.runtime / "synthetic-ledger.json"
    ledger.write_text("synthetic saved ledger", encoding="utf-8")
    session.store.reset_mock()
    answer(monkeypatch, "同意", "同意")
    assert setup.main(args) == 1
    assert "已经设置过；如需重新生成请加 --rebuild" in capsys.readouterr().out
    assert card.read_text(encoding="utf-8") == "stale synthetic card"
    prompts = []
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: prompts.append(prompt) or "")
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--rebuild"]) == 0
    assert "回车" in prompts[0] and "保留" in prompts[0]
    assert "真实回复中自动统计" in card.read_text(encoding="utf-8")
    assert deleted.read_text(encoding="utf-8") == saved
    assert ledger.read_text(encoding="utf-8") == "synthetic saved ledger"
    _, library, _ = assert_ready(session)
    assert original_library[0].snippet_id not in {item.snippet_id for item in library}
    session.store.assert_not_called()


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl"])
@pytest.mark.parametrize("deletion", ["id", "range", "absent"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_setup_supports_only_time_range_deletions(
    session, tmp_path, monkeypatch, capsys, backup_helpers,
    format_name, deletion, rebuild,
):
    rows = standard_rows()
    for row in rows[:6]:
        row["timestamp"] = rows[2]["timestamp"]
    target_replies = [rows[3]["text"], rows[5]["text"]]
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args, selection = ["--backup", str(backup.path)], ("1", "1")
    else:
        source = jsonl_file(tmp_path, rows)
        args, selection = ["--format", "jsonl", "--chat", str(source)], ()
    args += ["--owner-pronoun", "他", "--partner-pronoun", "她"]
    if rebuild:
        answer(monkeypatch, "同意", "同意", *selection, "", "")
        assert setup.main(args) == 0
        # Removing a complete preceding pair can make an old ID refer to
        # another reply. ID deletions must be rejected even if IDs still match.
        original = read_snippets(session.paths.private / "style/snippets/library.jsonl")
        target = next(item for item in original if item.owner_reply == [target_replies[0]])
        snippet_ids = [target.snippet_id]
        rows = rows[2:]
        if format_name == "ios-backup":
            backup = make_backup(backup_helpers, tmp_path / "changed-backup", rows)
            args[1] = str(backup.path)
        else:
            jsonl_file(tmp_path, rows)
        args.append("--rebuild")
    else:
        snippet_ids = ["synthetic-deleted-1", "synthetic-deleted-2"]
    deleted = session.paths.private / "style/snippets/deleted_ids.json"
    saved = json.dumps({
        "snippet_ids": snippet_ids if deletion == "id" else [],
        "ranges": [[rows[2]["timestamp"]] * 2] if deletion == "range" else [],
    }).encode("utf-8")
    if deletion != "absent":
        setup._prepare_directories(session.paths)
        deleted.write_bytes(saved)
    capsys.readouterr()
    session.store.reset_mock()
    build = Mock(wraps=setup.build_paired_snippets.main)
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    answer(monkeypatch, "同意", "同意", *selection, "", "")

    result = setup.main(args)

    captured = capsys.readouterr()
    if deletion == "id":
        assert result == 1
        assert (f"分享模式只支持按时间段删除；删除记录里有 {len(snippet_ids)} 条按片段记录的条目，"
                "请改为按时间段记录后再重新设置。" in captured.out)
        assert "设置没有完成：片段库" in captured.out
        assert not config_path(session).exists()
        assert not deleted.with_name("habit_card.txt").exists()
        build.assert_not_called()
        session.store.assert_not_called()
    else:
        assert result == 0
        _, library, holdout = assert_ready(session)
        replies = [item.owner_reply for item in (*library, *holdout)]
        for reply in target_replies:
            if deletion == "range":
                assert [reply] not in replies
            else:
                assert [reply] in replies
        build.assert_called_once()
    if deletion != "absent":
        assert deleted.read_bytes() == saved
    else:
        assert not deleted.exists()
    assert captured.err == ""


def test_snippet_direction_conflict_fails(session, tmp_path, monkeypatch, capsys):
    source = chat_file(tmp_path)
    rows = standard_rows()
    rows[3]["source_speaker_id"] = "second-outbound-speaker"
    source.write_text(json.dumps(rows), encoding="utf-8")
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(standard_args(source)) == 1
    # Full standard validation now runs before filtering and snippet construction.
    assert "设置没有完成：聊天转换" in capsys.readouterr().out
    assert not config_path(session).exists()


def test_small_library_is_insufficient_but_successful(session, tmp_path, monkeypatch, capsys):
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(standard_args(chat_file(tmp_path, 10))) == 0
    assert "样本结论：证据不足" in capsys.readouterr().out
    assert_ready(session)


def test_insecure_provider_fails_before_api_key(session, tmp_path, monkeypatch, capsys):
    get_key = Mock(return_value=SYNTHETIC_KEY)
    monkeypatch.setattr(setup.getpass, "getpass", get_key)
    answer(monkeypatch, "同意", "同意", "", "", "http://example.com/v1", "")
    assert setup.main(standard_args(chat_file(tmp_path))) == 1
    assert "设置没有完成：模型服务" in capsys.readouterr().out
    get_key.assert_not_called()
    session.store.assert_not_called()
    assert not config_path(session).exists()


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl", "standard"])
@pytest.mark.parametrize("failure", ["provider", "credentials"])
def test_failed_setup_can_retry_same_command(
    session, tmp_path, monkeypatch, capsys, backup_helpers, format_name, failure,
):
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup")
        args = ["--backup", str(backup.path)]
        selection = ("1", "1")
    elif format_name == "jsonl":
        source = jsonl_file(tmp_path, standard_rows())
        args = ["--format", "jsonl", "--chat", str(source)]
        selection = ()
    else:
        args = standard_args(chat_file(tmp_path))
        selection = ()
    args += ["--owner-pronoun", "他", "--partner-pronoun", "她"]
    if failure == "credentials":
        session.store.side_effect = RuntimeError("synthetic credential failure")
    answer(monkeypatch, "同意", "同意", *selection,
           "http://example.com/v1" if failure == "provider" else "", "")
    assert setup.main(args) == 1
    expected_step = "模型服务" if failure == "provider" else "保存 API key"
    assert f"设置没有完成：{expected_step}" in capsys.readouterr().out
    assert not config_path(session).exists()
    snippets = session.paths.private / "style/snippets"
    card = snippets / "habit_card.txt"
    assert card.exists()
    card.write_text("stale synthetic card", encoding="utf-8")
    prepared = session.paths.private / "share/prepared.jsonl"
    if format_name != "standard":
        assert prepared.is_file()
        prepared.write_bytes(b"stale synthetic prepared output\n")
    else:
        filtered = session.paths.private / "share/filtered-standard.json"
        assert filtered.is_file()
        filtered.write_bytes(b"stale synthetic filtered output\n")
    original = read_snippets(snippets / "library.jsonl")[0]
    deleted = snippets / "deleted_ids.json"
    deleted.write_text(json.dumps({
        "snippet_ids": [], "ranges": [[original.started_at.isoformat()] * 2],
    }))
    ledger = session.paths.runtime / "synthetic-ledger.json"
    ledger.write_bytes(b"synthetic ledger")
    veiled = session.paths.private / "privacy/veiled_terms.json"
    preserved = {path: path.read_bytes() for path in (deleted, ledger, veiled)}

    session.store.side_effect = session.save
    answer(monkeypatch, "同意", "同意", *selection, "", "")
    assert setup.main(args) == 0
    _, library, _ = assert_ready(session)
    assert original.snippet_id not in {item.snippet_id for item in library}
    assert "真实回复中自动统计" in card.read_text(encoding="utf-8")
    if format_name != "standard":
        assert b"stale synthetic" not in prepared.read_bytes()
    else:
        source = Path(args[args.index("--chat") + 1])
        assert StandardJsonAdapter().parse(filtered) == StandardJsonAdapter().parse(source)
    for path, content in preserved.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize("damaged", [False, True])
def test_existing_veiled_terms_are_preserved_and_validated(
    session, tmp_path, monkeypatch, capsys, rebuild, damaged,
):
    args = standard_args(chat_file(tmp_path), "--owner-pronoun", "他", "--partner-pronoun", "她")
    if rebuild:
        answer(monkeypatch, "同意", "同意", "", "")
        assert setup.main(args) == 0
        args.append("--rebuild")
    path = session.paths.private / "privacy/veiled_terms.json"
    setup._prepare_directories(session.paths)
    content = (b"{broken synthetic lexicon" if damaged else json.dumps({
        "version": 1, "note": "合成词表，保留格式与末尾换行",
        "high": {"name": ["虚构姓名"]}, "suspect": {"place": ["虚构单位"]},
        "variants": {"allow_separators": True, "pinyin_initials": {"虚构姓名": ["xgxm"]}},
    }, ensure_ascii=False, indent=4).encode("utf-8") + b"\n\n")
    path.write_bytes(content)
    session.store.reset_mock()
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(args) == (1 if damaged else 0)
    assert path.read_bytes() == content
    if damaged:
        assert "设置没有完成：用户词表" in capsys.readouterr().out
        assert not config_path(session).exists()
        session.store.assert_not_called()
    else:
        lexicon = load_veiled_terms(path)
        assert lexicon.scan("x-g-x-m").high == ("name:虚构姓名",)
        assert lexicon.scan("虚构单位").suspect == ("place:虚构单位",)


@pytest.mark.parametrize("format_name", ["jsonl", "ios-backup", "standard"])
def test_setup_excludes_media_pairs_and_keeps_adjacent_text(
    session, tmp_path, monkeypatch, backup_helpers, format_name,
):
    rows = standard_rows()
    rows[0]["text"], rows[1]["text"] = "看看这个", "颜色很亮"
    # A neutral prompt also needs the actual marker to exclude its image-dependent reply.
    rows[4]["text"], rows[5]["text"] = "今天挑了新颜色", "暖色更合适"
    for index in (0, 4):
        rows.append({
            **rows[index], "message_id": str(1000 + index),
            "timestamp": (datetime.fromisoformat(rows[index]["timestamp"])
                          + timedelta(seconds=30)).isoformat(),
            "content_type": "image", "text": None,
        })
    rows.sort(key=lambda item: item["timestamp"])
    convert = Mock(wraps=setup.convert)
    monkeypatch.setattr(setup, "convert", convert)
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args = ["--backup", str(backup.path)]
        selection = ("1", "1")
    else:
        source = jsonl_file(tmp_path, rows) if format_name == "jsonl" else chat_file(tmp_path)
        if format_name == "standard":
            source.write_text(json.dumps(rows), encoding="utf-8")
        args = ["--format", format_name, "--chat", str(source)]
        selection = ()
    args += ["--owner-pronoun", "他", "--partner-pronoun", "她"]
    answer(monkeypatch, "同意", "同意", *selection, "", "")
    assert setup.main(args) == 0
    _, library, holdout = assert_ready(session)
    for snippet in (*library, *holdout):
        assert "颜色很亮" not in snippet.owner_reply
        assert "暖色更合适" not in snippet.owner_reply
    clean = standard_rows()
    assert any(item.partner_turns == [clean[2]["text"]]
               and item.owner_reply == [clean[3]["text"]] for item in library)
    if format_name != "standard":
        assert convert.call_args.kwargs["keep_nontext_markers"] is True
    else:
        convert.assert_not_called()


def test_ios_backup_zero_id_image_excludes_media_pair(
    session, tmp_path, monkeypatch, backup_helpers,
):
    rows = standard_rows()
    rows[4]["text"], rows[5]["text"] = "今天挑了新颜色", "暖色更合适"
    rows.append({
        **rows[4], "message_id": "0",
        "timestamp": (datetime.fromisoformat(rows[4]["timestamp"])
                      + timedelta(seconds=30)).isoformat(),
        "content_type": "image", "text": None,
    })
    rows.sort(key=lambda item: item["timestamp"])
    backup = make_backup(backup_helpers, tmp_path / "backup", rows)
    answer(monkeypatch, "同意", "同意", "1", "1", "", "")
    assert setup.main([
        "--backup", str(backup.path), "--owner-pronoun", "他", "--partner-pronoun", "她",
    ]) == 0
    _, library, holdout = assert_ready(session)
    for snippet in (*library, *holdout):
        assert "暖色更合适" not in snippet.owner_reply
        assert "今天挑了新颜色" not in snippet.partner_turns
    clean = standard_rows()
    assert any(item.partner_turns == [clean[2]["text"]]
               and item.owner_reply == [clean[3]["text"]] for item in library)


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl"])
@pytest.mark.parametrize("image_id", ["0", "5"])
def test_same_second_media_reply_excluded_after_preparation(
    session, tmp_path, monkeypatch, capsys, backup_helpers, format_name, image_id,
):
    rows = standard_rows()
    rows[4]["text"], rows[5]["text"] = "今天挑了新颜色", "暖色更合适"
    rows.insert(5, {
        **rows[4], "message_id": image_id, "timestamp": rows[5]["timestamp"],
        "content_type": "image", "text": None,
    })
    convert = Mock(wraps=setup.convert)
    monkeypatch.setattr(setup, "convert", convert)
    build = Mock(wraps=setup.build_paired_snippets.main)
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args, selection = ["--backup", str(backup.path)], ("1", "1")
    else:
        source = jsonl_file(tmp_path, rows)
        original = source.read_bytes()
        args, selection = ["--format", "jsonl", "--chat", str(source)], ()
    answer(monkeypatch, "同意", "同意", *selection, "", "")

    assert setup.main([*args, "--owner-pronoun", "他", "--partner-pronoun", "她"]) == 0

    _, library, holdout = assert_ready(session)
    assert all("暖色更合适" not in item.owner_reply
               and "今天挑了新颜色" not in item.partner_turns for item in (*library, *holdout))
    clean = standard_rows()
    assert any(item.partner_turns == [clean[2]["text"]]
               and item.owner_reply == [clean[3]["text"]] for item in library)
    prepared = session.paths.private / "share/prepared.jsonl"
    assert convert.call_args.args[0] == prepared
    prepared_rows = [json.loads(line) for line in prepared.read_bytes().splitlines()]
    assert len(prepared_rows) == len(rows)
    assert len({row["server_id"] for row in prepared_rows}) == len(prepared_rows)
    assert [row["type"] for row in prepared_rows[5:7]] == [3, 1]
    assert prepared_rows[6]["text"] == "暖色更合适"
    standard = json.loads(Path(build.call_args.args[0][1]).read_text(encoding="utf-8"))
    assert len(standard) == len(rows)
    assert [row["message_id"] for row in standard] == [
        str(row["server_id"]) for row in prepared_rows
    ]
    assert [row["content_type"] for row in standard[5:7]] == ["image", "text"]
    assert standard[6]["text"] == "暖色更合适"
    assert "同秒含图片等消息的时刻：1 处（已按原顺序保留）" in capsys.readouterr().out
    if format_name == "jsonl":
        assert source.read_bytes() == original


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl"])
@pytest.mark.parametrize("order", ["image-first", "text-first", "text-only"])
def test_same_second_standard_order_follows_input_lines(
    session, tmp_path, monkeypatch, capsys, backup_helpers, format_name, order,
):
    rows = standard_rows()
    timestamp = rows[5]["timestamp"]
    if order == "text-only":
        rows[4]["timestamp"] = timestamp
        # IDs conflict with line order, so downstream sorting must use the new IDs.
        rows[4]["message_id"], rows[5]["message_id"] = "6", "5"
        expected_types = ["text", "text"]
        expected_texts = [rows[4]["text"], rows[5]["text"]]
    else:
        rows.insert(5 if order == "image-first" else 6, {
            **rows[4], "message_id": "0", "timestamp": timestamp,
            "content_type": "image", "text": None,
        })
        expected_types = ["image", "text"] if order == "image-first" else ["text", "image"]
        reply = standard_rows()[5]["text"]
        expected_texts = [None, reply] if order == "image-first" else [reply, None]
    if format_name == "ios-backup":
        # make_backup sets MesLocalID=index+1; the insertion above sets its relative order.
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args, selection = ["--backup", str(backup.path)], ("1", "1")
    else:
        source = jsonl_file(tmp_path, rows)
        args, selection = ["--format", "jsonl", "--chat", str(source)], ()
    build = Mock(wraps=setup.build_paired_snippets.main)
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    answer(monkeypatch, "同意", "同意", *selection, "", "")

    assert setup.main([*args, "--owner-pronoun", "他", "--partner-pronoun", "她"]) == 0

    standard = json.loads(Path(build.call_args.args[0][1]).read_text(encoding="utf-8"))
    same_second = [row for row in standard
                   if datetime.fromisoformat(row["timestamp"]) == datetime.fromisoformat(timestamp)]
    assert [row["content_type"] for row in same_second] == expected_types
    assert [row["text"] for row in same_second] == expected_texts
    base_id = int(datetime.fromisoformat(timestamp).timestamp()) * 1000
    assert [row["message_id"] for row in same_second] == [str(base_id), str(base_id + 1)]
    assert len(standard) == len(rows)
    _, library, holdout = assert_ready(session)
    if order == "text-only":
        assert any(item.partner_turns == [rows[4]["text"]]
                   and item.owner_reply == [rows[5]["text"]] for item in (*library, *holdout))
    groups = 0 if order == "text-only" else 1
    assert f"同秒含图片等消息的时刻：{groups} 处（已按原顺序保留）" in capsys.readouterr().out


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl"])
def test_too_many_same_second_rows_fails_without_content(
    session, tmp_path, monkeypatch, capsys, backup_helpers, format_name,
):
    rows = standard_rows(501)[:1001]
    for row in rows:
        row["timestamp"] = rows[0]["timestamp"]
        row["text"] = "SYNTHETIC_HIDDEN_CONTENT"
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args, selection = ["--backup", str(backup.path)], ("1", "1")
    else:
        source = jsonl_file(tmp_path, rows)
        args, selection = ["--format", "jsonl", "--chat", str(source)], ()
    convert = Mock(wraps=setup.convert)
    monkeypatch.setattr(setup, "convert", convert)
    answer(monkeypatch, "同意", "同意", *selection)

    assert setup.main([*args, "--owner-pronoun", "他", "--partner-pronoun", "她"]) == 1

    captured = capsys.readouterr()
    assert "设置没有完成：导出聊天" in captured.out
    assert "SYNTHETIC_HIDDEN_CONTENT" not in captured.out
    assert captured.err == ""
    convert.assert_not_called()
    assert not config_path(session).exists()


@pytest.mark.parametrize(("choices", "owner", "partner"), [
    (("她", "他"), "她", "他"),
    (("他", "他"), "他", "他"),
    (("", ""), "他", "她"),
    (("女", "她", "男", "他"), "她", "他"),
])
def test_first_setup_asks_pronouns(session, tmp_path, monkeypatch, choices, owner, partner):
    prompts = answer(monkeypatch, "同意", "同意", *choices, "", "")
    assert setup.main(standard_args(chat_file(tmp_path))) == 0
    payload = json.loads(config_path(session).read_text(encoding="utf-8"))
    assert payload["snippet_style"]["pronouns"] == {"owner": owner, "partner": partner}
    owner_prompt = "被模仿的人是男生还是女生？输入 他 或 她（直接回车为 他）："
    partner_prompt = "陪聊的人是男生还是女生？输入 他 或 她（直接回车为 她）："
    repeats = 2 if len(choices) == 4 else 1
    assert prompts == [setup.OWNER_CONSENT_PROMPT, setup.PARTNER_CONSENT_PROMPT,
                       *([owner_prompt] * repeats), *([partner_prompt] * repeats),
                       setup.PROVIDER_URL_PROMPT, setup.PROVIDER_MODEL_PROMPT]


@pytest.mark.parametrize(("owner", "partner"), [("她", "他"), ("他", "他")])
@pytest.mark.parametrize("override", [
    (), ("--owner-pronoun", "他"), ("--partner-pronoun", "她"),
    ("--owner-pronoun", "他", "--partner-pronoun", "她"),
])
def test_rebuild_preserves_pronouns_unless_explicit(
    session, tmp_path, monkeypatch, owner, partner, override,
):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--owner-pronoun", owner, "--partner-pronoun", partner]) == 0
    prompts = answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--rebuild", *override]) == 0
    payload = json.loads(config_path(session).read_text(encoding="utf-8"))
    assert payload["snippet_style"]["pronouns"] == {
        "owner": "他" if "--owner-pronoun" in override else owner,
        "partner": "她" if "--partner-pronoun" in override else partner,
    }
    assert prompts == [setup.OWNER_CONSENT_PROMPT, setup.PARTNER_CONSENT_PROMPT,
                       setup.PROVIDER_URL_PROMPT, setup.PROVIDER_MODEL_PROMPT]


@pytest.mark.parametrize("previous_config", ["{invalid JSON", "{}"])
def test_rebuild_explicit_pronouns_ignores_broken_previous_config(
    session, tmp_path, monkeypatch, previous_config,
):
    setup._prepare_directories(session.paths)
    path = config_path(session)
    path.write_text(previous_config, encoding="utf-8")
    answer(monkeypatch, "同意", "同意", "", "")

    assert setup.main(standard_args(
        chat_file(tmp_path), "--rebuild", "--owner-pronoun", "她", "--partner-pronoun", "他",
    )) == 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["snippet_style"]["pronouns"] == {"owner": "她", "partner": "他"}
    config, _ = load_dialogue_config(
        load_settings(ROOT), environ={"DEEPSEEK_API_KEY": "offline-check"},
    )
    assert config.snippet_style.pronouns.model_dump() == {"owner": "她", "partner": "他"}


def test_api_key_is_not_written_to_share_files(session, tmp_path, monkeypatch):
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(standard_args(chat_file(tmp_path))) == 0
    session.store.assert_called_once_with("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL, SYNTHETIC_KEY)
    files = [path for path in session.paths.root.rglob("*") if path.is_file()]
    assert files
    for path in files:
        assert SYNTHETIC_KEY.encode("utf-8") not in path.read_bytes()


def test_getpass_fallback_aborts_before_reading_or_storing_key(
    session, tmp_path, monkeypatch, capsys,
):
    raw_input = Mock(side_effect=AssertionError("ordinary input was reached"))
    monkeypatch.setattr(setup.getpass, "getpass", setup.getpass.fallback_getpass)
    monkeypatch.setattr(setup.getpass, "_raw_input", raw_input)
    answer(monkeypatch, "同意", "同意", "", "", "", "")

    assert setup.main(standard_args(chat_file(tmp_path))) == 1
    raw_input.assert_not_called()
    session.store.assert_not_called()
    session.load.assert_not_called()
    assert not config_path(session).exists()
    assert "设置没有完成：保存 API key" in capsys.readouterr().out


def test_getpass_key_is_stored_without_output(session, tmp_path, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(setup.getpass, "getpass",
                        lambda prompt: prompts.append(prompt) or SYNTHETIC_KEY)
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(standard_args(chat_file(tmp_path))) == 0
    assert prompts == ["请粘贴 API key（输入时不显示）："]
    session.store.assert_called_once_with("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL, SYNTHETIC_KEY)
    output = capsys.readouterr()
    assert SYNTHETIC_KEY not in output.out + output.err


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_platform_paths(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(setup.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Synthetic\AppData\Local")
    paths = setup.share_paths()
    if platform == "win32":
        root = PureWindowsPath(r"C:\Users\Synthetic\AppData\Local\kiki-share")
        assert PureWindowsPath(str(paths.root)) == root
        for field, name in (("private", "private"), ("runtime", "runtime"),
                            ("test_results", "test-results")):
            assert PureWindowsPath(str(getattr(paths, field))) == root / name
    else:
        parent = "Library/Application Support" if platform == "darwin" else ".local/share"
        assert paths.root == tmp_path / parent / "kiki-share"


@pytest.mark.parametrize("failure", ["habit", "credentials", "offline", "overlap"])
def test_late_failure_removes_rebuilt_config(
    session, tmp_path, monkeypatch, capsys, failure,
):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(args) == 0
    if failure == "habit":
        monkeypatch.setattr(setup.build_habit_card, "main", lambda argv: 2)
    elif failure == "credentials":
        session.store.side_effect = RuntimeError("synthetic failure")
    elif failure == "offline":
        monkeypatch.setattr(setup, "load_dialogue_config", Mock(side_effect=ValueError))
    else:
        original = setup.build_paired_snippets.main

        def overlap(argv):
            result = original(argv)
            directory = session.paths.private / "style/snippets"
            (directory / "holdout.jsonl").write_bytes((directory / "library.jsonl").read_bytes())
            return result

        monkeypatch.setattr(setup.build_paired_snippets, "main", overlap)
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--rebuild"]) == 1
    assert not config_path(session).exists()
    assert "设置没有完成：" in capsys.readouterr().out


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl", "standard"])
@pytest.mark.parametrize(("zone_name", "through_date"), [
    ("Asia/Shanghai", date(2026, 7, 17)),
    ("America/New_York", date(2026, 3, 8)),
    ("America/New_York", date(2026, 11, 1)),
])
def test_setup_through_date_filters_before_build(
    session, tmp_path, monkeypatch, backup_helpers, format_name, zone_name, through_date,
):
    zone = ZoneInfo(zone_name)
    day = datetime.combine(through_date, datetime.min.time(), zone)
    midnight = datetime.combine(through_date + timedelta(days=1), datetime.min.time(), zone)
    rows = standard_rows()
    boundary = [
        (day, False, "text", "本地零点的合成话题"),
        (day + timedelta(minutes=1), True, "text", "本地零点的合成回复呀"),
        (day + timedelta(hours=12), False, "image", None),
        (midnight - timedelta(seconds=2), False, "text", "最后一组的合成话题"),
        (midnight - timedelta(seconds=1), True, "text", "最后一秒的合成回复呀"),
        (midnight - timedelta(seconds=1), False, "text", "等待跨日回复的合成话题"),
        (midnight, True, "text", "排除的午夜合成回复"),
        (midnight + timedelta(minutes=1), True, "text", "排除的次日合成回复"),
    ]
    for index, (timestamp, owner, kind, text) in enumerate(boundary, 1001):
        rows.append({
            **standard_rows(1)[int(owner)], "message_id": str(index),
            "timestamp": timestamp.astimezone(UTC).isoformat(),
            "content_type": kind, "text": text,
        })
    # The retained input includes a moment whose UTC and local calendar dates differ.
    assert any(datetime.fromisoformat(row["timestamp"]).date()
               != datetime.fromisoformat(row["timestamp"]).astimezone(zone).date()
               for row in rows[-8:-2])
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup", rows)
        args, selection, source = ["--backup", str(backup.path)], ("1", "1"), None
    else:
        source = jsonl_file(tmp_path, rows) if format_name == "jsonl" else chat_file(tmp_path)
        if format_name == "standard":
            source.write_text(json.dumps(rows), encoding="utf-8")
        args, selection = ["--format", format_name, "--chat", str(source)], ()
    original_bytes = source.read_bytes() if source is not None else None
    expected = rows[:-2]
    if format_name == "standard":
        expected_payload = [
            {"schema_version": 1, **record.model_dump(mode="json")}
            for record in StandardJsonAdapter().parse(source).records[:-2]
        ]

    def signature(row):
        return (datetime.fromisoformat(row["timestamp"]), row["direction"],
                row["content_type"], row["text"])

    real_build = setup.build_paired_snippets.main

    def inspect_and_build(argv):
        chat = Path(argv[argv.index("--chat") + 1])
        payload = json.loads(chat.read_text(encoding="utf-8"))
        assert [signature(row) for row in payload] == [signature(row) for row in expected]
        assert any(row["content_type"] == "image" for row in payload)
        if format_name == "standard":
            assert chat == session.paths.private / "share/filtered-standard.json"
            assert payload == expected_payload
            assert source.read_bytes() == original_bytes
            if os.name == "posix":
                assert chat.stat().st_mode & 0o777 == 0o600
        return real_build(argv)

    build = Mock(side_effect=inspect_and_build)
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    answer(monkeypatch, "同意", "同意", *selection, "", "")
    assert setup.main([*args, "--owner-pronoun", "他", "--partner-pronoun", "她",
                       "--through-date", through_date.isoformat(), "--timezone", zone_name]) == 0
    build.assert_called_once()
    _, library, holdout = assert_ready(session)
    excluded = {row["text"] for row in rows[-2:]}
    assert all(excluded.isdisjoint(item.owner_reply) for item in (*library, *holdout))


def test_standard_through_date_includes_fractional_last_second(session, tmp_path, monkeypatch):
    rows = standard_rows()
    timestamps = [
        "2026-07-17T23:59:59+08:00",
        "2026-07-17T23:59:59.999999+08:00",
        "2026-07-17T15:59:59.999999Z",
        "2026-07-18T01:59:59.999999+10:00",
        "2026-07-18T00:00:00+08:00",
        "2026-07-17T16:00:00Z",
    ]
    for index, timestamp in enumerate(timestamps, 1001):
        rows.append({**standard_rows(1)[1], "message_id": str(index), "timestamp": timestamp})
    source = chat_file(tmp_path)
    source.write_text(json.dumps(rows), encoding="utf-8")
    real_build = setup.build_paired_snippets.main

    def inspect_and_build(argv):
        payload = json.loads(Path(argv[argv.index("--chat") + 1]).read_text(encoding="utf-8"))
        assert [row["message_id"] for row in payload] == [row["message_id"] for row in rows[:-2]]
        assert [datetime.fromisoformat(row["timestamp"]) for row in payload] == [
            datetime.fromisoformat(row["timestamp"]) for row in rows[:-2]
        ]
        return real_build(argv)

    build = Mock(side_effect=inspect_and_build)
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(standard_args(source, "--owner-pronoun", "他", "--partner-pronoun", "她",
                                    "--through-date", "2026-07-17")) == 0
    build.assert_called_once()


@pytest.mark.parametrize("bad_time", [
    "missing", None, 1784246400, "2026-07-18T00:00:00", "not-a-time",
    "2026-02-30T00:00:00+08:00",
])
def test_standard_invalid_time_stops_before_build(
    session, tmp_path, monkeypatch, capsys, bad_time,
):
    rows = standard_rows()
    # This last message would be excluded, but still requires full adapter validation.
    rows.append({**rows[-1], "message_id": "1001", "timestamp": "2026-07-18T00:00:00+08:00"})
    if bad_time == "missing":
        del rows[-1]["timestamp"]
    else:
        rows[-1]["timestamp"] = bad_time
    source = chat_file(tmp_path)
    source.write_text(json.dumps(rows), encoding="utf-8")
    original = source.read_bytes()
    build = Mock()
    key_prompt = Mock(side_effect=AssertionError("must not ask for key"))
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    monkeypatch.setattr(setup, "_read_api_key", key_prompt)
    answer(monkeypatch, "同意", "同意")
    assert setup.main(standard_args(source, "--owner-pronoun", "他", "--partner-pronoun", "她",
                                    "--through-date", "2026-07-17")) == 1
    assert "设置没有完成：聊天转换" in capsys.readouterr().out
    build.assert_not_called()
    key_prompt.assert_not_called()
    session.store.assert_not_called()
    assert source.read_bytes() == original
    assert not config_path(session).exists()
    assert not (session.paths.private / "share/filtered-standard.json").exists()


def test_standard_through_date_empty_selection_stops_before_build(
    session, tmp_path, monkeypatch, capsys,
):
    source = chat_file(tmp_path)
    build = Mock()
    key_prompt = Mock(side_effect=AssertionError("must not ask for key"))
    monkeypatch.setattr(setup.build_paired_snippets, "main", build)
    monkeypatch.setattr(setup, "_read_api_key", key_prompt)
    answer(monkeypatch, "同意", "同意")
    assert setup.main(standard_args(source, "--owner-pronoun", "他", "--partner-pronoun", "她",
                                    "--through-date", "2025-12-31")) == 1
    assert "设置没有完成：聊天转换" in capsys.readouterr().out
    build.assert_not_called()
    key_prompt.assert_not_called()
    session.store.assert_not_called()
    assert not config_path(session).exists()
    assert not (session.paths.private / "share/filtered-standard.json").exists()


@pytest.mark.parametrize("alias", ["same-path", "hardlink"])
def test_standard_filter_preserves_input_when_output_aliases_source(tmp_path, alias):
    source = chat_file(tmp_path, count=40)
    original = source.read_bytes()
    out = source if alias == "same-path" else tmp_path / "filtered-standard.json"
    kwargs = {"through_date": date(2026, 1, 1), "tz": ZoneInfo("Asia/Shanghai")}
    if alias == "same-path":
        with pytest.raises(ValueError):
            setup._filter_standard_chat(source, out, **kwargs)
    else:
        out.hardlink_to(source)
        assert setup._filter_standard_chat(source, out, **kwargs) == out
        assert StandardJsonAdapter().parse(out).records == (
            StandardJsonAdapter().parse(source).records[:32]
        )
        if os.name == "posix":
            assert out.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == original


@pytest.mark.parametrize("format_name", ["ios-backup", "jsonl", "standard"])
def test_timezone_default_date_is_forwarded(
    session, tmp_path, monkeypatch, backup_helpers, format_name,
):
    clock_calls = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz):
            clock_calls.append(tz)
            return cls(2026, 3, 9, 2, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(setup, "datetime", FrozenDatetime)
    standard = chat_file(tmp_path)
    convert = Mock(return_value=SimpleNamespace(output=standard))
    monkeypatch.setattr(setup, "convert", convert)
    filtered = Mock(wraps=setup._filter_standard_chat)
    monkeypatch.setattr(setup, "_filter_standard_chat", filtered)
    if format_name == "ios-backup":
        backup = make_backup(backup_helpers, tmp_path / "backup")
        args, selection = ["--backup", str(backup.path)], ("1", "1")
    else:
        source = jsonl_file(tmp_path, standard_rows()) if format_name == "jsonl" else standard
        args, selection = ["--format", format_name, "--chat", str(source)], ()
    answer(monkeypatch, "同意", "同意", *selection, "", "")
    zone = ZoneInfo("America/New_York")
    assert setup.main([*args, "--timezone", str(zone), "--owner-pronoun", "他",
                       "--partner-pronoun", "她"]) == 0
    call = filtered.call_args if format_name == "standard" else convert.call_args
    assert call.kwargs["through_date"] == date(2026, 3, 8)
    assert call.kwargs["tz"] == zone
    assert clock_calls.count(zone) == 1
    if format_name == "standard":
        convert.assert_not_called()
        filtered.assert_called_once()
    else:
        assert call.kwargs["conversation_id"] == "share-chat"
        assert call.kwargs["keep_nontext_markers"] is True
        convert.assert_called_once()
        filtered.assert_not_called()


@pytest.mark.parametrize("format_name", ["jsonl", "standard"])
def test_chat_argument_is_required(session, monkeypatch, capsys, format_name):
    answer(monkeypatch, "同意", "同意")
    assert setup.main(["--format", format_name]) == 1
    assert "设置没有完成：参数" in capsys.readouterr().out
    assert not config_path(session).exists()


def test_rebuild_new_address_requires_key_after_empty_input(session, tmp_path, monkeypatch, capsys):
    args = standard_args(chat_file(tmp_path), "--owner-pronoun", "他", "--partner-pronoun", "她")
    address_a, address_b = "https://a.example/v1", "https://b.example/v1"
    answer(monkeypatch, "同意", "同意", address_a, "")
    assert setup.main(args) == 0
    session.store.reset_mock()
    prompts = []
    keys = iter(["", "  ", "synthetic-b-key"])
    monkeypatch.setattr(
        setup.getpass, "getpass", lambda prompt: prompts.append(prompt) or next(keys),
    )
    answer(monkeypatch, "同意", "同意", address_b, "")

    assert setup.main([*args, "--rebuild"]) == 0

    assert len(prompts) == 3
    assert "这个服务地址还没有保存过 key，请粘贴" in capsys.readouterr().out
    session.store.assert_called_once_with("OPENAI_API_KEY", address_b, "synthetic-b-key")
    assert session.load("OPENAI_API_KEY", address_a) == SYNTHETIC_KEY
    assert session.load("OPENAI_API_KEY", address_b) == "synthetic-b-key"
    assert json.loads(config_path(session).read_text())["provider"]["base_url"] == address_b


def test_rebuild_native_read_error_uses_existing_credential_prompt(
    session, tmp_path, monkeypatch, capsys,
):
    args = standard_args(chat_file(tmp_path), "--owner-pronoun", "他", "--partner-pronoun", "她")
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main(args) == 0
    session.store.reset_mock()
    new_key = "synthetic-rebuild-key"
    backend = type("WinVaultKeyring", (), {
        "__module__": "keyring.backends.Windows",
        "get_password": Mock(return_value=new_key),
    })()
    get_keyring = Mock(side_effect=[OSError("synthetic-native-read-error"), backend])
    monkeypatch.setattr(share_credentials.keyring, "get_keyring", get_keyring)
    monkeypatch.setattr(setup, "load_api_key", share_credentials.load_api_key)
    prompts = []
    keys = iter(["", new_key])
    monkeypatch.setattr(
        setup.getpass, "getpass", lambda prompt: prompts.append(prompt) or next(keys),
    )
    answer(monkeypatch, "同意", "同意", "", "")

    assert setup.main([*args, "--rebuild"]) == 0

    assert prompts == [setup.REBUILD_API_KEY_PROMPT, setup.API_KEY_PROMPT]
    assert "这个服务地址还没有保存过 key，请粘贴" in capsys.readouterr().out
    session.store.assert_called_once_with("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL, new_key)
    assert session.load("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL) == new_key
    assert get_keyring.call_count == 2
    backend.get_password.assert_called_once_with(
        share_credentials.SERVICE, f"DEEPSEEK_API_KEY|{setup.DEFAULT_BASE_URL}",
    )


def test_rebuild_can_keep_key_previously_saved_for_target_address(session, tmp_path, monkeypatch):
    args = standard_args(chat_file(tmp_path), "--owner-pronoun", "他", "--partner-pronoun", "她")
    answer(monkeypatch, "同意", "同意", "https://a.example/v1", "")
    assert setup.main(args) == 0
    session.save("OPENAI_API_KEY", "https://b.example/v1", "synthetic-b-key")
    session.store.reset_mock()
    session.load.reset_mock()
    monkeypatch.setattr(setup.getpass, "getpass", Mock(return_value=""))
    address_b = "https://B.EXAMPLE/v1/"
    answer(monkeypatch, "同意", "同意", address_b, "")

    assert setup.main([*args, "--rebuild"]) == 0

    session.store.assert_not_called()
    assert session.load.call_count >= 1
    assert all(call.args == ("OPENAI_API_KEY", normalize_base_url(address_b))
               for call in session.load.call_args_list)
    assert session.load("OPENAI_API_KEY", address_b) == "synthetic-b-key"
    assert_ready(session, "OPENAI_API_KEY")


@pytest.mark.parametrize("rebuild", [False, True])
def test_key_readback_failure_never_writes_runtime_config(
    session, tmp_path, monkeypatch, capsys, rebuild,
):
    args = standard_args(chat_file(tmp_path), "--owner-pronoun", "他", "--partner-pronoun", "她")
    if rebuild:
        answer(monkeypatch, "同意", "同意", "", "")
        assert setup.main(args) == 0
        args.append("--rebuild")
    session.load.side_effect = CredentialError("credential store unavailable")
    write = Mock(wraps=setup._write_json)
    monkeypatch.setattr(setup, "_write_json", write)
    answer(monkeypatch, "同意", "同意", "", "")

    assert setup.main(args) == 1

    assert "设置没有完成：保存 API key" in capsys.readouterr().out
    session.load.assert_called_with("DEEPSEEK_API_KEY", setup.DEFAULT_BASE_URL)
    assert not config_path(session).exists()
    assert all(call.args[0] != config_path(session) for call in write.call_args_list)


@pytest.mark.parametrize("rebuild", [False, True])
def test_setup_is_rejected_without_file_changes_while_chat_holds_lock(
    session, tmp_path, monkeypatch, capsys, rebuild,
):
    from companion.runtime.share_lock import share_session_lock

    args = standard_args(chat_file(tmp_path))
    setup._prepare_directories(session.paths)
    if rebuild:
        config_path(session).write_bytes(b"existing synthetic config must not be read")
        args.append("--rebuild")
    card = session.paths.private / "style/snippets/habit_card.txt"
    card.write_text("synthetic card must survive", encoding="utf-8")
    prepare = Mock(side_effect=AssertionError("must lock before preparing directories"))
    load_settings = Mock(side_effect=AssertionError("must lock before loading settings"))
    get_key = Mock(side_effect=AssertionError("must lock before asking for key"))
    monkeypatch.setattr(setup, "_prepare_directories", prepare)
    monkeypatch.setattr(setup, "load_settings", load_settings)
    monkeypatch.setattr(setup.getpass, "getpass", get_key)
    prompts = answer(monkeypatch, "同意", "同意")

    with share_session_lock(session.paths.runtime):
        before = {path.relative_to(session.paths.root): path.read_bytes()
                  for path in session.paths.root.rglob("*") if path.is_file()}
        assert setup.main(args) == 1
        after = {path.relative_to(session.paths.root): path.read_bytes()
                 for path in session.paths.root.rglob("*") if path.is_file()}

    assert before == after
    output = capsys.readouterr().out
    assert output.endswith("聊天窗口还开着，请先关闭再设置。\n")
    assert "设置没有完成：" not in output
    assert prompts == [setup.OWNER_CONSENT_PROMPT, setup.PARTNER_CONSENT_PROMPT]
    prepare.assert_not_called()
    load_settings.assert_not_called()
    get_key.assert_not_called()
    session.store.assert_not_called()
    session.load.assert_not_called()


def test_setup_holds_chat_lock_from_directory_preparation_through_config_publication(
    session, tmp_path, monkeypatch, capsys,
):
    from companion.runtime import text_chat
    from companion.runtime.share_lock import share_session_lock

    settings = SimpleNamespace(
        private_data_dir=session.paths.private, runtime_data_dir=session.paths.runtime,
    )
    monkeypatch.setattr(text_chat, "load_settings", Mock(return_value=settings))
    monkeypatch.setattr(text_chat, "current_user_id", lambda: "synthetic-local-user")
    load_key = Mock(return_value=SYNTHETIC_KEY)
    build_bridge = Mock()
    monkeypatch.setattr(text_chat, "load_api_key", load_key)
    monkeypatch.setattr(text_chat, "_build_bridge", build_bridge)
    original_prepare = setup._prepare_directories
    original_write = setup._write_json
    checked_stages = []

    def reject_chat(stage):
        assert text_chat.main([]) == 1
        assert capsys.readouterr().out.endswith("已经有一个聊天窗口在运行，请先关闭它。\n")
        load_key.assert_not_called()
        build_bridge.assert_not_called()
        checked_stages.append(stage)

    def prepare(paths):
        reject_chat("before directories")
        original_prepare(paths)

    def write_json(path, payload):
        original_write(path, payload)
        if path == config_path(session):
            reject_chat("after config publication")

    monkeypatch.setattr(setup, "_prepare_directories", prepare)
    monkeypatch.setattr(setup, "_write_json", write_json)
    answer(monkeypatch, "同意", "同意", "", "", "", "")

    assert setup.main(standard_args(chat_file(tmp_path))) == 0

    assert checked_stages == ["before directories", "after config publication"]
    assert_ready(session)
    with share_session_lock(session.paths.runtime):
        assert config_path(session).is_file()


def count_habit_card_builds(monkeypatch):
    calls = []
    original = setup.build_habit_card.main

    def counted(argv):
        calls.append(argv)
        return original(argv)

    monkeypatch.setattr(setup.build_habit_card, "main", counted)
    return calls


def test_rebuild_keep_habit_card_preserves_existing_card(session, tmp_path, monkeypatch, capsys):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(args) == 0
    card = session.paths.private / "style/snippets/habit_card.txt"
    card.write_bytes("原版卡\n".encode())
    calls = count_habit_card_builds(monkeypatch)
    capsys.readouterr()
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--rebuild", "--keep-habit-card"]) == 0
    assert card.read_bytes() == "原版卡\n".encode()
    assert calls == []
    assert config_path(session).is_file()
    assert "习惯卡：保留现有（未重新生成）" in capsys.readouterr().out
    assert_ready(session)


def test_keep_habit_card_without_card_stops_and_keeps_config(
    session, tmp_path, monkeypatch, capsys,
):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(args) == 0
    (session.paths.private / "style/snippets/habit_card.txt").unlink()
    saved = config_path(session).read_bytes()
    session.store.reset_mock()
    capsys.readouterr()
    answer(monkeypatch, "同意", "同意")
    assert setup.main([*args, "--rebuild", "--keep-habit-card"]) == 1
    out = capsys.readouterr().out
    assert "没有找到现有习惯卡，不能保留；去掉 --keep-habit-card 重新设置。" in out
    assert config_path(session).read_bytes() == saved
    session.store.assert_not_called()


def test_rebuild_without_keep_habit_card_regenerates_card(session, tmp_path, monkeypatch):
    args = standard_args(chat_file(tmp_path))
    answer(monkeypatch, "同意", "同意", "", "", "", "")
    assert setup.main(args) == 0
    card = session.paths.private / "style/snippets/habit_card.txt"
    card.write_bytes("原版卡\n".encode())
    answer(monkeypatch, "同意", "同意", "", "")
    assert setup.main([*args, "--rebuild"]) == 0
    assert card.read_bytes() != "原版卡\n".encode()
