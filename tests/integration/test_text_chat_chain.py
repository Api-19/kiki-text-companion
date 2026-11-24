# ruff: noqa: RUF001
"""Text dialogue and local memory refusal through real assembly with synthetic data."""

import io
import json
import os
import select
import signal
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

import companion.dialogue.composition as composition
import companion.dialogue.config as dialogue_config
from companion.contracts import ConsentRecord
from companion.dialogue.context import CONTEXT_END, CONTEXT_START
from companion.dialogue.identity import AI_DISCLOSURE
from companion.dialogue.models import ProviderResult, ProviderUsage
from companion.dialogue.orchestrator import MEMORY_DISABLED_FALLBACK, MEMORY_WRITE_FAILED_FALLBACK
from companion.memory import CreateMemoryRequest, build_private_memory_service
from companion.privacy.paired_snippets import PairedSnippet, write_snippets
from companion.runtime import private_paths, text_chat
from companion.runtime.settings import load_settings
from companion.runtime.share_credentials import CredentialError
from companion.runtime.share_lock import share_session_lock


@pytest.fixture
def text_chat_project(tmp_path, monkeypatch):
    tmp_path = tmp_path / "project"
    tmp_path.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.toml").write_text(
        '[companion]\nenvironment = "test"\nlog_level = "INFO"\n'
        'private_data_dir = "synthetic-private"\nruntime_data_dir = "runtime_data"\n'
        'test_results_dir = "synthetic-results"\n',
        encoding="utf-8",
    )
    private = tmp_path / "synthetic-private"
    snippets = private / "style" / "snippets"
    private.mkdir(mode=0o700)
    (private / "style").mkdir(mode=0o700)
    snippets.mkdir(mode=0o700)
    write_snippets(snippets / "library.jsonl", [PairedSnippet(
        snippet_id="synthetic-text-chat-1", conversation_id="synthetic-conversation",
        started_at=datetime(2026, 1, 1, tzinfo=UTC), source_fingerprint="f" * 64,
        partner_turns=["合成拼图放在哪里"], owner_reply=["合成片段：先把拼图放好，稍后慢慢看"],
    )])
    (snippets / "habit_card.txt").write_text("合成习惯卡标题\n喜欢短句", encoding="utf-8")
    dialogue = private / "dialogue"
    dialogue.mkdir(mode=0o700)
    (dialogue / "runtime-config.json").write_text(json.dumps({
        "provider": {
            "base_url": "https://synthetic.example/v1", "model": "synthetic-model",
            "timeout_seconds": 10,
        },
        "budget": {
            "monthly_cap_cny": "100.00", "worst_case_request_cny": "1.00",
            "input_cny_per_million": "1.00", "output_cny_per_million": "2.00",
            "max_output_tokens": 256,
        },
        "snippet_style": {
            "library_source": "style/snippets/library.jsonl",
            "habit_card_source": "style/snippets/habit_card.txt",
            "partner_user_id": "synthetic-user",
        },
    }), encoding="utf-8")
    (dialogue / "runtime-config.json").chmod(0o600)
    monkeypatch.setattr(text_chat.os, "environ", {
        "OPENAI_API_KEY": "synthetic-unused-key", "DEEPSEEK_API_KEY": "synthetic-unused-key",
    })
    monkeypatch.setattr(text_chat, "current_user_id", lambda: "synthetic-user")
    monkeypatch.setattr(text_chat, "load_api_key", Mock(return_value="synthetic-saved-key"))
    monkeypatch.setattr("keyring.get_keyring", Mock(side_effect=AssertionError("real keyring")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network")))
    provider = Mock()
    provider.complete.side_effect = [ProviderResult(
        text=text, usage=ProviderUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
    ) for text in ("合成第一轮回答", "合成第二轮回答")]
    monkeypatch.setattr(composition, "OpenAICompatibleProvider", Mock(return_value=provider))
    return tmp_path, provider


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_real_config_rejects_unsafe_private_source_directory(
    text_chat_project, monkeypatch, capsys,
):
    project_root, provider = text_chat_project
    private = project_root / "synthetic-private"
    target = private / "synthetic-library"
    target.mkdir(mode=0o700)
    (target / "library.jsonl").write_bytes((private / "style/snippets/library.jsonl").read_bytes())
    config_path = private / "dialogue/runtime-config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["snippet_style"]["library_source"] = "synthetic-library/library.jsonl"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    target.chmod(0o755)
    before = target.lstat()
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))
    original_load = dialogue_config.load_dialogue_config
    loaded = []

    def load_config(*args, **kwargs):
        result = original_load(*args, **kwargs)
        loaded.append(result)
        return result

    load = Mock(side_effect=load_config)
    monkeypatch.setattr(dialogue_config, "load_dialogue_config", load)

    assert text_chat.main(["--project-root", str(project_root)]) == 1

    load.assert_called_once()
    assert len(loaded) == 1
    assert loaded[0][0].snippet_style.library_source == target / "library.jsonl"
    composition.OpenAICompatibleProvider.assert_not_called()
    provider.complete.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == "文本聊天暂时无法启动。\n"
    assert captured.err == ""
    after = target.lstat()
    assert (after.st_uid, after.st_mode) == (before.st_uid, before.st_mode)


@pytest.mark.parametrize("link_kind", ("symlink", "junction"))
def test_runtime_link_is_rejected_before_lock_or_credentials(
    text_chat_project, monkeypatch, capsys, link_kind,
):
    project_root, provider = text_chat_project
    target = project_root / "runtime-target"
    target.mkdir(mode=0o700)
    alias = project_root / "runtime-alias"
    if link_kind == "symlink":
        alias.symlink_to(target, target_is_directory=True)
    else:
        alias.mkdir()
        monkeypatch.setattr(private_paths, "IS_WINDOWS", True)
        monkeypatch.setattr(Path, "is_junction", lambda path: path == alias)
        original_resolve = Path.resolve

        def resolve(path, strict=False):
            return original_resolve(target if path == alias else path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
    assert alias.resolve() == target
    assert not private_paths.is_link(target)
    config_path = project_root / "config/default.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'runtime_data_dir = "runtime_data"',
            f'runtime_data_dir = "{alias.name}"',
        ),
        encoding="utf-8",
    )
    lock = Mock(wraps=text_chat.share_session_lock)
    monkeypatch.setattr(text_chat, "share_session_lock", lock)
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 1

    output = capsys.readouterr()
    assert output.out == "文本聊天暂时无法启动。\n"
    assert output.err == ""
    lock.assert_not_called()
    text_chat.load_api_key.assert_not_called()
    composition.OpenAICompatibleProvider.assert_not_called()
    provider.complete.assert_not_called()


def test_main_runs_snippets_habit_card_pronouns_and_ledger(text_chat_project, monkeypatch, capsys):
    project_root, provider = text_chat_project
    private_root = project_root / "synthetic-private"
    runtime_root = project_root / "runtime_data"
    monkeypatch.setattr("sys.stdin", io.StringIO("合成拼图放在哪里\n合成下一轮\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 0

    output = capsys.readouterr().out
    assert f"kiki：{AI_DISCLOSURE}\n合成第一轮回答" in output
    assert "kiki：合成第二轮回答" in output
    assert output.count(AI_DISCLOSURE) == 1
    assert provider.complete.call_count == 2
    first_messages = provider.complete.call_args_list[0].args[0]
    system = "\n".join(message.content for message in first_messages if message.role == "system")
    assert "他的说话习惯：" in system
    assert "合成习惯卡标题" in system
    assert "合成片段：先把拼图放好，稍后慢慢看" in system
    assert "学他怎么接话" in system
    assert "- 她：合成拼图放在哪里" in system
    second_messages = provider.complete.call_args_list[1].args[0]
    assert any("合成第一轮回答" in message.content for message in second_messages)
    # Memory's actual database is memory.sqlite3; include SQLite sidecars and .db files.
    databases = {
        path for root in (private_root, runtime_root)
        for pattern in ("*.sqlite*", "*.db") for path in root.rglob(pattern)
    }
    if os.name == "posix":
        database = private_root / "memory/memory.sqlite3"
        assert databases == {database}
        assert database.stat().st_mode & 0o777 == 0o600
        assert database.parent.stat().st_mode & 0o777 == 0o700
        memory = build_private_memory_service(
            load_settings(project_root), clock=lambda: datetime.now(UTC),
        )
        assert memory.get_consent("synthetic-user") is None
        assert memory.list_memories("synthetic-user") == ()
    else:
        assert not databases
    ledgers = list((runtime_root / "dialogue").glob("*.json"))
    assert ledgers
    assert all(json.loads(path.read_text(encoding="utf-8")) for path in ledgers)


def test_memory_command_is_refused_locally_without_billing(text_chat_project, monkeypatch, capsys):
    project_root, provider = text_chat_project
    monkeypatch.setattr("sys.stdin", io.StringIO("记住我喜欢拼图\n/exit\n"))
    ledger_root = project_root / "runtime_data" / "dialogue"
    before = {path.name: path.read_bytes() for path in ledger_root.glob("*.json")}
    assert before == {}

    assert text_chat.main(["--project-root", str(project_root)]) == 0

    provider.complete.assert_not_called()
    refusal = MEMORY_WRITE_FAILED_FALLBACK if os.name == "posix" else MEMORY_DISABLED_FALLBACK
    assert f"kiki：{AI_DISCLOSURE}\n{refusal}\n" in capsys.readouterr().out
    after = {path.name: path.read_bytes() for path in ledger_root.glob("*.json")}
    assert after == before


@pytest.mark.parametrize("environment_key", ["synthetic-wrong-key", "", None])
@pytest.mark.parametrize("api_key_env", [None, "DEEPSEEK_API_KEY"])
def test_saved_key_reaches_provider_with_real_config_loader(
    text_chat_project, monkeypatch, environment_key, api_key_env,
):
    project_root, provider = text_chat_project
    config_path = project_root / "synthetic-private/dialogue/runtime-config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if api_key_env is not None:
        payload["provider"]["api_key_env"] = api_key_env
        config_path.write_text(json.dumps(payload), encoding="utf-8")
    selected_env = api_key_env or "OPENAI_API_KEY"
    if environment_key is None:
        monkeypatch.delenv(selected_env, raising=False)
    else:
        monkeypatch.setenv(selected_env, environment_key)
    before = dict(text_chat.os.environ)
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 0

    text_chat.load_api_key.assert_called_once_with(selected_env, "https://synthetic.example/v1")
    supplied_key = composition.OpenAICompatibleProvider.call_args.kwargs["api_key"]
    assert supplied_key.get_secret_value() == "synthetic-saved-key"
    provider.complete.assert_called_once()
    assert text_chat.os.environ == before


def test_missing_saved_key_fails_even_with_environment_key(text_chat_project, monkeypatch, capsys):
    project_root, provider = text_chat_project
    text_chat.load_api_key.side_effect = CredentialError("api key not found")
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 1

    assert capsys.readouterr().out == "文本聊天暂时无法启动。\n"
    text_chat.load_api_key.assert_called_once_with("OPENAI_API_KEY", "https://synthetic.example/v1")
    composition.OpenAICompatibleProvider.assert_not_called()
    provider.complete.assert_not_called()


@pytest.mark.parametrize(("raw_url", "normalized"), [
    ("https://api.example.com:443/v1", "https://api.example.com/v1"),
    ("https://api.example.com/合成/v1/", "https://api.example.com/%E5%90%88%E6%88%90/v1"),
])
def test_normalized_snapshot_starts_with_real_config_loader(
    text_chat_project, monkeypatch, capsys, raw_url, normalized,
):
    project_root, provider = text_chat_project
    config_path = project_root / "synthetic-private/dialogue/runtime-config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["provider"]["base_url"] = raw_url
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 0

    assert "已连接。" in capsys.readouterr().out
    text_chat.load_api_key.assert_called_once_with("OPENAI_API_KEY", normalized)
    provider.complete.assert_called_once()
    config = composition.OpenAICompatibleProvider.call_args.kwargs["config"]
    assert str(config.base_url).rstrip("/") == normalized


CHAT_PROCESS = """
import sys
import socket
from pathlib import Path
from unittest.mock import Mock
import keyring
import companion.dialogue.composition as composition
from companion.dialogue.models import ProviderResult, ProviderUsage
from companion.runtime import text_chat
from companion.runtime.share_credentials import CredentialError

calls = Path(sys.argv[2])
def record(event):
    with calls.open("a", encoding="utf-8") as output:
        output.write(event + "\\n")

def load_key(env_name, base_url):
    record("credentials")
    return "synthetic-process-key"

def complete(messages):
    record("complete")
    return ProviderResult(
        text="合成进程回答",
        usage=ProviderUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
    )

def build_provider(**kwargs):
    record("constructed")
    return Mock(complete=Mock(side_effect=complete))

keyring.get_keyring = Mock(side_effect=AssertionError("real keyring"))
socket.create_connection = Mock(side_effect=AssertionError("network"))
text_chat.load_api_key = load_key
text_chat.current_user_id = lambda: "synthetic-user"
composition.OpenAICompatibleProvider = build_provider
raise SystemExit(text_chat.main(["--project-root", sys.argv[1]]))
"""


@pytest.fixture
def chat_processes(text_chat_project):
    project_root, _ = text_chat_project
    runtime_root = project_root.parent / "kiki-share/runtime"
    runtime_root.parent.mkdir(mode=0o700)
    environment = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONUNBUFFERED": "1", "COMPANION_RUNTIME_DATA_DIR": str(runtime_root),
    }
    processes = []

    def start(name):
        calls = project_root.parent / f"{name}.calls"
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", CHAT_PROCESS, str(project_root), str(calls)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment,
        )
        processes.append(process)
        return process, calls

    yield start, runtime_root
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process locking")
@pytest.mark.parametrize("exit_method", ["normal", "sigkill"])
def test_independent_process_lock_and_release(
    text_chat_project, chat_processes, exit_method,
):
    project_root, _ = text_chat_project
    start, runtime_root = chat_processes
    first, first_calls = start("first")
    assert select.select([first.stdout], [], [], 10)[0], "first chat did not start"
    assert "已连接。" in first.stdout.readline()
    assert first.poll() is None
    assert "constructed" in first_calls.read_text(encoding="utf-8")
    lock_path = runtime_root / "dialogue/text-chat.lock"
    assert lock_path.is_file()
    assert lock_path.read_bytes() == b""
    assert not (project_root / "runtime_data").exists()
    assert not (project_root / "text-chat.lock").exists()

    second, second_calls = start("second")
    output, error = second.communicate("合成消息\n/exit\n", timeout=10)
    assert second.returncode == 1, error
    assert output == "已经有一个聊天窗口在运行，请先关闭它。\n"
    assert not second_calls.exists(), "credentials and provider must not be accessed"

    if exit_method == "sigkill":
        first.send_signal(signal.SIGKILL)
        first.communicate(timeout=10)
        assert first.returncode == -signal.SIGKILL
    else:
        _, error = first.communicate("/exit\n", timeout=10)
        assert first.returncode == 0, error
    assert lock_path.is_file()

    replacement, replacement_calls = start("replacement")
    output, error = replacement.communicate("合成消息\n/exit\n", timeout=10)
    assert replacement.returncode == 0, error
    assert "已连接。" in output
    assert "合成进程回答" in output
    assert "complete" in replacement_calls.read_text(encoding="utf-8")
    assert lock_path.is_file()


@pytest.mark.parametrize(("field", "value"), [
    ("base_url", "https://replaced.example/v1"),
    ("base_url", "https://synthetic.example/V1"),
    ("base_url", "https://synthetic.example/v1?team=New"),
    ("api_key_env", "DEEPSEEK_API_KEY"),
])
def test_replaced_config_snapshot_never_reaches_provider(
    text_chat_project, monkeypatch, capsys, field, value,
):
    project_root, provider = text_chat_project
    original_load = dialogue_config.load_dialogue_config

    def replaced_config(settings, *, environ):
        config, secrets = original_load(settings, environ=environ)
        replacement = config.provider.model_validate({
            **config.provider.model_dump(), field: value,
        })
        return config.model_copy(update={"provider": replacement}), secrets

    monkeypatch.setattr(dialogue_config, "load_dialogue_config", replaced_config)
    monkeypatch.setattr("sys.stdin", io.StringIO("合成消息\n/exit\n"))

    assert text_chat.main(["--project-root", str(project_root)]) == 1

    assert capsys.readouterr().out == "文本聊天暂时无法启动。\n"
    composition.OpenAICompatibleProvider.assert_not_called()
    provider.complete.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process locking")
def test_chat_process_is_rejected_while_setup_session_is_locked(chat_processes):
    start, runtime_root = chat_processes
    with share_session_lock(runtime_root):
        chat, calls = start("during-setup")
        output, error = chat.communicate("合成消息\n/exit\n", timeout=10)
        assert chat.returncode == 1, error
        assert output == "已经有一个聊天窗口在运行，请先关闭它。\n"
        assert not calls.exists(), "credentials and provider must not be accessed"

    chat, calls = start("after-setup")
    output, error = chat.communicate("合成消息\n/exit\n", timeout=10)
    assert chat.returncode == 0, error
    assert "已连接。" in output
    assert "complete" in calls.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="Protected memory storage requires POSIX")
@pytest.mark.parametrize("remove", ["delete", "revoke"])
def test_share_builder_memory_lifecycle(text_chat_project, monkeypatch, capsys, remove):
    root, provider = text_chat_project
    settings = load_settings(root)
    config, secrets = dialogue_config.load_dialogue_config(settings)
    user = text_chat.current_user_id()
    assert user == config.snippet_style.partner_user_id
    bridge = text_chat._build_bridge(settings, config, secrets, user)
    writer = build_private_memory_service(settings, clock=lambda: datetime.now(UTC))
    canary = "SYNTHETIC_MEMORY_BLUE"
    command = "记住这件事：合成拼图 " + canary
    query = "合成拼图"
    assert writer.get_consent(user) is None
    refusal = bridge.reply(user_id=user, text=command)
    assert refusal.blocked_reason == "memory_write_failed"
    assert not refusal.billable
    assert writer.list_memories(user) == ()
    provider.complete.assert_not_called()
    assert not list((settings.runtime_data_dir / "dialogue").glob("*.json"))

    def grant(subject):
        writer.grant_consent(ConsentRecord(
            user_id=subject, granted=True, granted_at=datetime.now(UTC),
            allowed_fields=["explicit_note"],
        ))

    other = "synthetic-other"
    grant(other)
    assert bridge.reply(user_id=user, text=command).blocked_reason == "memory_write_failed"
    other_canary = "SYNTHETIC_MEMORY_OTHER"
    other_entry = writer.create_memory(CreateMemoryRequest(
        user_id=other, field="explicit_note", value=query + " " + other_canary,
        source_timestamp=datetime.now(UTC), explicitly_requested=True,
    ))
    provider.complete.assert_not_called()

    def manage(action, typed=""):
        monkeypatch.setattr("sys.stdin", io.StringIO(typed))
        assert text_chat.main(["--project-root", str(root), "--memory", action]) == 0
        text_chat.load_api_key.assert_not_called()
        return capsys.readouterr().out

    assert "可能" in manage("grant", "同意\n")
    assert writer.get_consent(user).granted
    result = bridge.reply(user_id=user, text=command)
    assert result.blocked_reason is None and not result.billable
    (entry,) = writer.list_memories(user)
    assert writer.list_memories(other) == (other_entry,)
    provider.complete.assert_not_called()

    def prompt():
        return "\n".join(m.content for m in provider.complete.call_args.args[0])

    def regular_reply(answer):
        provider.complete.side_effect = None
        provider.complete.return_value = ProviderResult(
            text=answer, usage=ProviderUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
        )
        assert bridge.reply(user_id=user, text=query).blocked_reason is None

    regular_reply("合成回复提到 " + canary)
    assert canary in prompt() and other_canary not in prompt()
    regular_reply("合成安全回复")
    history = prompt().split(CONTEXT_START, 1)[1].split(CONTEXT_END, 1)[0]
    assert canary in history and other_canary not in prompt()
    assert writer.list_memories(user) == (entry,)
    shown = manage("list")
    assert canary in shown and other_canary not in shown
    manage("delete", "1\n取消\n")
    manage("revoke", "取消\n")
    assert writer.list_memories(user) == (entry,)
    if remove == "delete":
        manage("delete", "1\n删除\n")
        assert writer.get_consent(user).granted
    else:
        manage("revoke", "撤回\n")
        assert not writer.get_consent(user).granted
    regular_reply("合成安全回复")
    assert canary not in prompt() and other_canary not in prompt()
    assert writer.retrieve_memories(user, query) == ()
    assert writer.get_consent(other).granted
    bridge.close()


@pytest.mark.skipif(os.name != "posix", reason="Protected memory storage requires POSIX")
@pytest.mark.parametrize("case", ["decline", "mismatch", "unsafe_config", "busy", "non_posix"])
def test_memory_management_requires_confirmation_and_own_account(
    text_chat_project, monkeypatch, capsys, case,
):
    root, provider = text_chat_project
    if case == "mismatch":
        monkeypatch.setattr(text_chat, "current_user_id", lambda: "synthetic-other")
    if case == "non_posix":
        from types import SimpleNamespace

        monkeypatch.setattr(text_chat, "os", SimpleNamespace(name="nt"))
    settings = load_settings(root)
    if case == "unsafe_config":
        (settings.private_data_dir / "dialogue/runtime-config.json").chmod(0o644)
    args = ["--project-root", str(root), "--memory", "grant"]
    monkeypatch.setattr("sys.stdin", io.StringIO("不同意\n" if case == "decline" else "同意\n"))
    if case == "busy":
        with share_session_lock(settings.runtime_data_dir):
            assert text_chat.main(args) == 1
    else:
        assert text_chat.main(args) == (0 if case == "decline" else 1)
    text_chat.load_api_key.assert_not_called()
    composition.OpenAICompatibleProvider.assert_not_called()
    provider.complete.assert_not_called()
    assert not (settings.private_data_dir / "memory").exists()
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(os.name != "posix", reason="Protected memory storage requires POSIX")
@pytest.mark.parametrize("action", ["list", "delete", "revoke"])
def test_other_os_account_cannot_read_or_change_existing_memory(
    text_chat_project, monkeypatch, capsys, action,
):
    root, provider = text_chat_project
    settings = load_settings(root)
    memory = build_private_memory_service(settings, clock=lambda: datetime.now(UTC))
    owner = "synthetic-user"
    memory.grant_consent(ConsentRecord(
        user_id=owner, granted=True, granted_at=datetime.now(UTC),
        allowed_fields=["explicit_note"],
    ))
    entry = memory.create_memory(CreateMemoryRequest(
        user_id=owner, field="explicit_note", value="SYNTHETIC_ACCOUNT_PRIVATE",
        source_timestamp=datetime.now(UTC), explicitly_requested=True,
    ))
    monkeypatch.setattr(text_chat, "current_user_id", lambda: "synthetic-other")
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("no prompt expected")))
    assert text_chat.main(["--project-root", str(root), "--memory", action]) == 1
    assert entry.value not in capsys.readouterr().out
    assert memory.get_consent(owner).granted
    assert memory.list_memories(owner) == (entry,)
    text_chat.load_api_key.assert_not_called()
    provider.complete.assert_not_called()
