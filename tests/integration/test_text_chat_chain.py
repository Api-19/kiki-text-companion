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
from companion.dialogue.identity import AI_DISCLOSURE
from companion.dialogue.models import ProviderResult, ProviderUsage
from companion.dialogue.orchestrator import MEMORY_DISABLED_FALLBACK
from companion.privacy.paired_snippets import PairedSnippet, write_snippets
from companion.runtime import private_paths, text_chat
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
    for root in (private_root, runtime_root):
        for pattern in ("*.sqlite*", "*.db"):
            assert not list(root.rglob(pattern)), (root, pattern)
    ledgers = list((runtime_root / "dialogue").glob("*.json"))
    assert ledgers
    assert all(json.loads(path.read_text(encoding="utf-8")) for path in ledgers)


def test_memory_command_is_refused_locally_without_billing(text_chat_project, monkeypatch, capsys):
    project_root, provider = text_chat_project
    monkeypatch.setattr("sys.stdin", io.StringIO("记住我明天要早起\n/exit\n"))
    ledger_root = project_root / "runtime_data" / "dialogue"
    before = {path.name: path.read_bytes() for path in ledger_root.glob("*.json")}
    assert before == {}

    assert text_chat.main(["--project-root", str(project_root)]) == 0

    provider.complete.assert_not_called()
    assert f"kiki：{AI_DISCLOSURE}\n{MEMORY_DISABLED_FALLBACK}\n" in capsys.readouterr().out
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
