# ruff: noqa: RUF001
"""Text console behavior and assembly with synthetic dependencies."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import companion.dialogue.composition as composition
import companion.dialogue.config as dialogue_config
import companion.runtime.speech_dialogue as speech_dialogue
from companion.contracts import DialogueResponse
from companion.runtime import private_paths, share_lock, text_chat
from companion.runtime.share_credentials import CredentialError

CONNECTED = "已连接。输入消息后回车发送，输入 /exit 退出。\n"
STARTUP_FAILED = "文本聊天暂时无法启动。\n"


@pytest.fixture
def host(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        private_data_dir=tmp_path / "synthetic-private", runtime_data_dir=tmp_path / "runtime",
    )
    dialogue = settings.private_data_dir / "dialogue"
    settings.private_data_dir.mkdir(mode=0o700)
    dialogue.mkdir(mode=0o700)
    library_branch = settings.private_data_dir / "synthetic-evidence-branch"
    library_branch.mkdir(mode=0o700)
    library = library_branch / "library"
    card = settings.private_data_dir / "cards"
    terms = settings.private_data_dir / "terms"
    for directory in (library, card, terms):
        directory.mkdir(mode=0o700)
    (dialogue / "runtime-config.json").write_text(json.dumps({
        "provider": {
            "base_url": "https://synthetic.example/v1", "api_key_env": "DEEPSEEK_API_KEY",
            "model": "synthetic-model", "timeout_seconds": 10,
        },
    }), encoding="utf-8")
    config = SimpleNamespace(
        snippet_style=SimpleNamespace(
            library_source=library / "library.jsonl",
            habit_card_source=card / "habit_card.txt",
            veiled_terms_source=terms / "veiled_terms.json",
        ), character_name="合成角色",
        provider=SimpleNamespace(
            base_url="https://synthetic.example/v1", api_key_env="DEEPSEEK_API_KEY",
        ),
    )
    bridge = Mock()
    bridge.reply.return_value = DialogueResponse(
        text="合成回答", disclosed_ai_identity=False, billable=True, blocked_reason=None,
    )
    dependencies = {
        "load_settings": Mock(return_value=settings),
        "current_user_id": Mock(return_value="synthetic-user"),
        "load_api_key": Mock(return_value="synthetic-saved-key"),
        "load_dialogue_config": Mock(return_value=(config, object())),
        "_build_bridge": Mock(return_value=bridge),
    }
    for name, dependency in dependencies.items():
        target = dialogue_config if name == "load_dialogue_config" else text_chat
        monkeypatch.setattr(target, name, dependency)
    monkeypatch.setattr(text_chat.os, "environ", {})
    read = Mock(side_effect=["/exit"])
    monkeypatch.setattr("builtins.input", read)
    return SimpleNamespace(config=config, bridge=bridge, read=read, **dependencies)


def test_blank_lines_trimmed_messages_and_exit(host, capsys):
    host.read.side_effect = ["", "  \t ", "  合成消息  ", " /exit "]

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == CONNECTED + "合成角色：合成回答\n"
    host.bridge.reply.assert_called_once_with(user_id="synthetic-user", text="合成消息")
    assert host.read.call_args_list == [(('你：',),)] * 4
    host.bridge.close.assert_called_once_with()


def test_exit_without_request_uses_current_directory(host, capsys):
    assert text_chat.main([]) == 0

    host.load_settings.assert_called_once_with(Path.cwd())
    host.bridge.reply.assert_not_called()
    host.bridge.close.assert_called_once_with()
    assert capsys.readouterr().out == CONNECTED


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_input_interruption_exits_cleanly(host, capsys, interruption):
    host.read.side_effect = interruption

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == CONNECTED + "\n"
    host.bridge.reply.assert_not_called()
    host.bridge.close.assert_called_once_with()


def test_bridge_failure_continues_to_next_turn(host, capsys):
    host.read.side_effect = ["合成第一轮", "合成第二轮", "/exit"]
    host.bridge.reply.side_effect = [
        speech_dialogue.SpeechDialogueBridgeError("synthetic-hidden-detail"),
        host.bridge.reply.return_value,
    ]

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == (
        CONNECTED + "这次没能回复，换个说法再试吧。\n合成角色：合成回答\n"
    )
    assert host.bridge.reply.call_count == 2
    host.bridge.close.assert_called_once_with()


@pytest.mark.parametrize("stage", [
    "load_settings", "current_user_id", "load_api_key", "load_dialogue_config", "_build_bridge",
])
def test_startup_failure_is_fixed_and_content_free(host, capsys, stage):
    getattr(host, stage).side_effect = RuntimeError("/synthetic/path SYNTHETIC_SECRET")

    assert text_chat.main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == STARTUP_FAILED
    assert captured.err == ""
    host.read.assert_not_called()


@pytest.mark.parametrize(("link_kind", "linked_component"), [
    ("symlink", "config"),
    ("symlink", "dialogue"),
    ("junction", "dialogue"),
])
def test_config_link_rejected_before_snapshot_read(
    host, monkeypatch, capsys, link_kind, linked_component,
):
    settings = host.load_settings.return_value
    config_path = settings.private_data_dir / "dialogue/runtime-config.json"
    linked_path = config_path if linked_component == "config" else config_path.parent
    if link_kind == "symlink":
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink",
            lambda path: True if path == linked_path else original_is_symlink(path),
        )
    else:
        original_is_junction = Path.is_junction
        monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
        monkeypatch.setattr(
            Path, "is_junction",
            lambda path: True if path == linked_path else original_is_junction(path),
        )

    original_read_text = Path.read_text
    target_read_count = 0

    def observe_read_text(path, *args, **kwargs):
        nonlocal target_read_count
        if path == config_path:
            target_read_count += 1
            raise AssertionError("configuration must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_read_text)

    result = text_chat.main([])

    assert target_read_count == 0
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == STARTUP_FAILED
    assert captured.err == ""
    host.load_api_key.assert_not_called()
    host.load_dialogue_config.assert_not_called()
    host._build_bridge.assert_not_called()


def test_missing_snippet_style_is_startup_failure(host, capsys):
    host.config.snippet_style = None

    assert text_chat.main([]) == 1

    assert capsys.readouterr().out == STARTUP_FAILED
    host._build_bridge.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.parametrize("target", ["root", "dialogue"])
def test_unsafe_private_config_directory_rejected_before_read(host, monkeypatch, capsys, target):
    private = host.load_settings.return_value.private_data_dir
    directory = private if target == "root" else private / "dialogue"
    directory.chmod(0o755)
    before = directory.lstat()
    before_entries = set(directory.iterdir())
    config_path = private / "dialogue/runtime-config.json"
    original_read_text = Path.read_text
    reads = Mock()

    def observe_read(path, *args, **kwargs):
        if path == config_path:
            reads()
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_read)

    assert text_chat.main([]) == 1

    reads.assert_not_called()
    host.load_api_key.assert_not_called()
    host.load_dialogue_config.assert_not_called()
    host._build_bridge.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == STARTUP_FAILED
    assert captured.err == ""
    after = directory.lstat()
    assert (after.st_uid, after.st_mode) == (before.st_uid, before.st_mode)
    assert set(directory.iterdir()) == before_entries


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.parametrize("target", [
    "library_source", "habit_card_source", "veiled_terms_source", "intermediate",
])
def test_unsafe_snippet_directory_rejected_before_assembly(host, monkeypatch, capsys, target):
    sources = host.config.snippet_style
    source = getattr(sources, "library_source" if target == "intermediate" else target)
    directory = source.parent.parent if target == "intermediate" else source.parent
    directory.chmod(0o755)
    before = directory.lstat()
    before_entries = set(directory.iterdir())
    original_open = Path.open
    reads = Mock()

    def observe_open(path, *args, **kwargs):
        if path == source:
            reads()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe_open)

    assert text_chat.main([]) == 1

    reads.assert_not_called()
    host._build_bridge.assert_not_called()
    host.load_dialogue_config.assert_called_once()
    captured = capsys.readouterr()
    assert captured.out == STARTUP_FAILED
    assert captured.err == ""
    after = directory.lstat()
    assert (after.st_uid, after.st_mode) == (before.st_uid, before.st_mode)
    assert set(directory.iterdir()) == before_entries


def test_optional_snippet_sources_can_be_absent(host, capsys):
    host.config.snippet_style.habit_card_source = None
    host.config.snippet_style.veiled_terms_source = None

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == CONNECTED
    host._build_bridge.assert_called_once()


@pytest.mark.parametrize("argv", [
    ["--unknown", "/synthetic/path"], ["--help"], ["--project-root"],
    ["--project", "/synthetic/path"], ["unexpected-positional"],
])
def test_invalid_arguments_are_startup_failure(host, capsys, argv):
    assert text_chat.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == STARTUP_FAILED
    assert captured.err == ""
    host.load_settings.assert_not_called()


def test_explicit_project_root(host, tmp_path):
    assert text_chat.main(["--project-root", str(tmp_path)]) == 0

    host.load_settings.assert_called_once_with(tmp_path)


def test_only_configured_credentials_are_loaded_without_changing_os_environ(host, monkeypatch):
    environment = {"OPENAI_API_KEY": "synthetic-unrelated", "DEEPSEEK_API_KEY": "synthetic-wrong"}
    monkeypatch.setattr(text_chat.os, "environ", environment.copy())

    assert text_chat.main([]) == 0

    host.load_api_key.assert_called_once_with("DEEPSEEK_API_KEY", "https://synthetic.example/v1")
    assert host.load_dialogue_config.call_args.kwargs["environ"] == {
        **environment, "DEEPSEEK_API_KEY": "synthetic-saved-key",
    }
    assert text_chat.os.environ == environment


def test_windows_lock_failure_rejects_before_credentials_or_config(host, monkeypatch, capsys):
    locking = Mock(side_effect=OSError("synthetic lock contention"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        locking=locking, LK_NBLCK=2, LK_UNLCK=0,
    ))
    monkeypatch.setattr(share_lock.sys, "platform", "win32")

    assert text_chat.main([]) == 1

    assert capsys.readouterr().out == "已经有一个聊天窗口在运行，请先关闭它。\n"
    locking.assert_called_once()
    assert locking.call_args.args[1:] == (2, 1)
    host.load_api_key.assert_not_called()
    host.load_dialogue_config.assert_not_called()
    host._build_bridge.assert_not_called()


def test_startup_failure_releases_lock(host, capsys):
    host.load_api_key.side_effect = CredentialError("synthetic missing")
    assert text_chat.main([]) == 1
    host.load_api_key.side_effect = None
    assert text_chat.main([]) == 0
    assert capsys.readouterr().out == STARTUP_FAILED + CONNECTED


def test_build_bridge_wires_snippets_and_privacy_revision(monkeypatch):
    settings, config, secrets = object(), object(), object()
    adapter, card = object(), "合成习惯卡"
    orchestrator = SimpleNamespace(privacy_revision=object())
    snippets = Mock(return_value=(adapter, card))
    build = Mock(return_value=orchestrator)
    bridge = Mock(return_value=object())
    monkeypatch.setattr(composition, "build_snippet_style_runtime", snippets)
    monkeypatch.setattr(composition, "build_real_orchestrator", build)
    monkeypatch.setattr(speech_dialogue, "SpeechDialogueBridge", bridge)

    result = text_chat._build_bridge(settings, config, secrets, "synthetic-user")
    assert result is bridge.return_value

    assert snippets.call_args.args == (config,)
    before = datetime.now(UTC)
    assert before <= snippets.call_args.kwargs["clock"]() <= datetime.now(UTC)
    build.assert_called_once_with(
        settings, config, secrets, trusted_user_id="synthetic-user", snippet_style=adapter,
        snippet_habit_card=card, privacy_revision_sources=(adapter,),
    )
    bridge.assert_called_once_with(
        orchestrator=orchestrator, privacy_revision=orchestrator.privacy_revision,
    )


def test_clean_import_excludes_speech_and_memory():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import companion.runtime.text_chat; "
            "assert not any(name == root or name.startswith(root + '.') "
            "for name in sys.modules for root in ('companion.speech', 'companion.memory'))"
        )],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("field", "value"), [
    ("base_url", "https://different.example/v1"),
    ("base_url", "https://synthetic.example/V1"),
    ("api_key_env", "OPENAI_API_KEY"),
])
def test_replaced_provider_config_fails_before_building_bridge(host, capsys, field, value):
    setattr(host.config.provider, field, value)

    assert text_chat.main([]) == 1

    assert capsys.readouterr().out == STARTUP_FAILED
    host.load_api_key.assert_called_once_with("DEEPSEEK_API_KEY", "https://synthetic.example/v1")
    host._build_bridge.assert_not_called()
    host.bridge.reply.assert_not_called()
    host.read.assert_not_called()


def test_provider_snapshot_comparison_uses_normalized_address(host, capsys):
    host.config.provider.base_url = "HTTPS://SYNTHETIC.EXAMPLE/v1/"

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == CONNECTED
    host._build_bridge.assert_called_once()


@pytest.mark.parametrize(("raw_url", "normalized"), [
    ("https://api.example.com:443/v1", "https://api.example.com/v1"),
    ("https://api.example.com/合成/v1/", "https://api.example.com/%E5%90%88%E6%88%90/v1"),
])
def test_first_provider_snapshot_validated_before_loading_key(host, capsys, raw_url, normalized):
    settings = host.load_settings.return_value
    path = settings.private_data_dir / "dialogue/runtime-config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["provider"]["base_url"] = raw_url
    path.write_text(json.dumps(payload), encoding="utf-8")
    host.config.provider.base_url = normalized

    assert text_chat.main([]) == 0

    assert capsys.readouterr().out == CONNECTED
    host.load_api_key.assert_called_once_with("DEEPSEEK_API_KEY", normalized)
    host._build_bridge.assert_called_once()
