# ruff: noqa: RUF001
"""Cross-platform text console for snippet-style dialogue."""

from __future__ import annotations

import argparse
import json
import os
import stat
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from companion.runtime.local_identity import current_user_id
from companion.runtime.private_paths import check_private_directory, reject_link_ancestors
from companion.runtime.settings import Settings, load_settings
from companion.runtime.share_credentials import load_api_key, normalize_base_url
from companion.runtime.share_lock import ShareSessionBusy, share_session_lock

if TYPE_CHECKING:
    from companion.dialogue.config import DialogueConfig, DialogueSecrets
    from companion.runtime.speech_dialogue import SpeechDialogueBridge


def _build_bridge(
    settings: Settings, config: DialogueConfig, secrets: DialogueSecrets, user_id: str,
) -> SpeechDialogueBridge:
    from companion.dialogue.composition import build_real_orchestrator, build_snippet_style_runtime
    from companion.runtime.speech_dialogue import SpeechDialogueBridge

    adapter, habit_card = build_snippet_style_runtime(config, clock=lambda: datetime.now(UTC))
    memory = None
    if os.name == "posix":
        from companion.memory import build_private_memory_service

        memory = build_private_memory_service(settings, clock=lambda: datetime.now(UTC))
    orchestrator = build_real_orchestrator(
        settings, config, secrets, trusted_user_id=user_id, snippet_style=adapter,
        snippet_habit_card=habit_card, memory_gateway=memory,
        privacy_revision_sources=(memory, adapter) if memory is not None else (adapter,),
    )
    return SpeechDialogueBridge(
        orchestrator=orchestrator, privacy_revision=orchestrator.privacy_revision,
    )


def _manage_memory(settings: Settings, config_path: Path, user_id: str, action: str) -> int:
    from companion.contracts import ConsentRecord
    from companion.memory import MemoryField, build_private_memory_service

    with open(
        config_path, encoding="utf-8",
        opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
    ) as config_file:
        info = os.fstat(config_file.fileno())
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe sharing configuration")
        target = json.load(config_file)["snippet_style"]["partner_user_id"]
    if type(target) is not str or target != user_id:
        raise ValueError("sharing account mismatch")

    def confirm(prompt: str, word: str) -> bool:
        try:
            accepted = input(prompt).strip() == word
        except (EOFError, KeyboardInterrupt):
            accepted = False
        if not accepted:
            print("已取消。")
        return accepted

    if action == "grant" and not confirm(
        "允许跨会话在本机保存你明确要求记住的称呼、偏好和备注。"
        "匹配的记忆可能随提示发送给已配置的模型服务。输入「同意」确认：",
        "同意",
    ):
        return 0
    if action == "revoke" and not confirm(
        "撤回授权并删除当前账户的长期记忆。输入「撤回」确认：", "撤回",
    ):
        return 0

    memory = build_private_memory_service(settings, clock=lambda: datetime.now(UTC))
    if action == "grant":
        existing = memory.get_consent(user_id)
        fields = {field.value for field in MemoryField}
        if existing and existing.granted:
            fields.update(existing.allowed_fields)
        memory.grant_consent(ConsentRecord(
            user_id=user_id, granted=True, granted_at=datetime.now(UTC),
            allowed_fields=sorted(fields),
        ))
        print("长期记忆已授权。")
    elif action == "revoke":
        receipt = memory.revoke_consent(user_id)
        print(f"授权已撤回，已删除 {receipt.deleted_count} 条记忆。")
    else:
        entries = memory.list_memories(user_id)
        if not entries:
            print("暂无长期记忆。")
            return 0
        for number, entry in enumerate(entries, 1):
            value = "".join(char if char.isprintable() else " " for char in entry.value)
            print(f"{number}. {value}")
        if action == "delete":
            try:
                number = int(input("输入要删除的编号，直接回车退出：").strip())
            except (ValueError, EOFError, KeyboardInterrupt):
                print("已取消。")
                return 0
            if not 1 <= number <= len(entries):
                print("编号无效。")
                return 1
            if confirm("输入「删除」确认：", "删除"):
                receipt = memory.delete_memory(user_id, entries[number - 1].memory_id)
                print(f"已删除 {receipt.deleted_count} 条记忆。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = None
    with ExitStack() as stack:
        try:
            from companion.dialogue.config import load_dialogue_config
            from companion.dialogue.models import ProviderConfig
            from companion.runtime.speech_dialogue import SpeechDialogueBridgeError

            parser = argparse.ArgumentParser(
                add_help=False, allow_abbrev=False, exit_on_error=False,
            )
            parser.add_argument("--project-root", type=Path, default=Path.cwd())
            parser.add_argument("--memory", choices=("grant", "list", "delete", "revoke"))
            args, unknown = parser.parse_known_args(argv)
            if unknown:
                raise ValueError("unknown arguments")
            if args.memory and os.name != "posix":
                print("此系统暂不支持本地长期记忆管理。")
                return 1
            settings = load_settings(args.project_root)
            stack.enter_context(share_session_lock(settings.runtime_data_dir))
            user_id = current_user_id()
            config_path = settings.private_data_dir / "dialogue/runtime-config.json"
            reject_link_ancestors(config_path)
            check_private_directory(settings.private_data_dir)
            check_private_directory(config_path.parent)
            if args.memory:
                return _manage_memory(settings, config_path, user_id, args.memory)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            provider = ProviderConfig.model_validate(payload["provider"])
            base_url = normalize_base_url(str(provider.base_url))
            api_key_env = provider.api_key_env
            key = load_api_key(api_key_env, base_url)
            environ = {**os.environ, api_key_env: key}
            config, secrets = load_dialogue_config(settings, environ=environ)
            if (
                normalize_base_url(str(config.provider.base_url)) != base_url
                or config.provider.api_key_env != api_key_env
            ):
                raise ValueError("provider changed while loading configuration")
            if config.snippet_style is None:
                raise ValueError("snippet style configuration unavailable")
            sources = config.snippet_style
            source_directories = {
                parent
                for source in (
                    sources.library_source, sources.habit_card_source, sources.veiled_terms_source,
                )
                if source is not None
                for parent in source.parents
                if parent.is_relative_to(settings.private_data_dir)
            } - {settings.private_data_dir, config_path.parent}
            for directory in sorted(source_directories, key=lambda path: len(path.parts)):
                check_private_directory(directory)
            bridge = _build_bridge(settings, config, secrets, user_id)
        except ShareSessionBusy:
            print("已经有一个聊天窗口在运行，请先关闭它。")
            return 1
        except Exception:
            print("本地记忆管理暂不可用。" if args and args.memory else "文本聊天暂时无法启动。")
            return 1

        try:
            print("已连接。输入消息后回车发送，输入 /exit 退出。")
            while True:
                try:
                    line = input("你：").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if not line:
                    continue
                if line == "/exit":
                    return 0
                try:
                    response = bridge.reply(user_id=user_id, text=line)
                except SpeechDialogueBridgeError:
                    print("这次没能回复，换个说法再试吧。")
                    continue
                print(f"{config.character_name}：{response.text}")
        finally:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
