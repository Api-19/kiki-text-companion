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
from companion.runtime.share_credentials import credential_slot, load_api_key, normalize_base_url
from companion.runtime.share_lock import ShareSessionBusy, share_session_lock

if TYPE_CHECKING:
    from companion.dialogue.config import DialogueConfig, DialogueSecrets
    from companion.runtime.share_calibration import ShareCalibration
    from companion.runtime.share_facts import ShareFacts
    from companion.runtime.speech_dialogue import SpeechDialogueBridge


def _build_bridge(
    settings: Settings, config: DialogueConfig, secrets: DialogueSecrets, user_id: str,
    *, session_facts: ShareFacts | None = None, calibration: ShareCalibration | None = None,
) -> SpeechDialogueBridge:
    from companion.dialogue.composition import build_real_orchestrator, build_snippet_style_runtime
    from companion.runtime.speech_dialogue import SpeechDialogueBridge

    adapter, habit_card = build_snippet_style_runtime(config, clock=lambda: datetime.now(UTC))
    memory = None
    if os.name in {"posix", "nt"}:
        from companion.memory import build_private_memory_service

        memory = build_private_memory_service(settings, clock=lambda: datetime.now(UTC))
    gateway = memory
    if memory is not None and session_facts is not None:
        from companion.runtime.share_facts import ShareMemory

        session_facts.bind_memory(memory)
        gateway = ShareMemory(memory, session_facts)
    orchestrator = build_real_orchestrator(
        settings, config, secrets, trusted_user_id=user_id, snippet_style=adapter,
        snippet_habit_card=habit_card, memory_gateway=gateway,
        snippet_calibration=calibration.guidance if calibration is not None else None,
        privacy_revision_sources=(memory, adapter) if memory is not None else (adapter,),
    )
    return SpeechDialogueBridge(
        orchestrator=orchestrator, privacy_revision=orchestrator.privacy_revision,
    )


def _read_account_config(settings: Settings, config_path: Path, user_id: str) -> dict:
    if os.name == "nt":
        from companion.memory.windows_storage import WindowsPrivateFiles

        files = WindowsPrivateFiles()
        files.check(settings.private_data_dir, directory=True)
        files.check(config_path.parent, directory=True)
        with files.read_config(config_path) as config_file:
            payload = json.load(config_file)
    else:
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
            payload = json.load(config_file)
    target = payload["snippet_style"]["partner_user_id"]
    if type(target) is not str or target != user_id:
        raise ValueError("sharing account mismatch")
    return payload


def _manage_memory(settings: Settings, user_id: str, action: str) -> int:
    from companion.contracts import ConsentRecord
    from companion.memory import MemoryField, build_private_memory_service

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


def main(
    argv: list[str] | None = None, *, _lock_held: bool = False, _first_run: bool = False,
) -> int:
    args = None
    with ExitStack() as stack:
        try:
            from companion.dialogue import composition
            from companion.dialogue.config import load_dialogue_config
            from companion.dialogue.models import ProviderConfig
            from companion.dialogue.usage import MonthlyUsageLedger
            from companion.runtime.share_blind import BLIND_NOTICE, BlindBatch
            from companion.runtime.share_calibration import ShareCalibration
            from companion.runtime.share_facts import ShareFacts, confirm_fact
            from companion.runtime.speech_dialogue import SpeechDialogueBridgeError

            parser = argparse.ArgumentParser(
                add_help=False, allow_abbrev=False, exit_on_error=False,
            )
            parser.add_argument("--project-root", type=Path, default=Path.cwd())
            parser.add_argument("--memory", choices=("grant", "list", "delete", "revoke"))
            parser.add_argument("--blind", action="store_true")
            args, unknown = parser.parse_known_args(argv)
            if unknown:
                raise ValueError("unknown arguments")
            if args.memory and os.name not in {"posix", "nt"}:
                print("此系统暂不支持本地长期记忆管理。")
                return 1
            settings = load_settings(args.project_root)
            if not _lock_held:
                stack.enter_context(share_session_lock(settings.runtime_data_dir))
            user_id = current_user_id()
            config_path = settings.private_data_dir / "dialogue/runtime-config.json"
            reject_link_ancestors(config_path)
            check_private_directory(settings.private_data_dir)
            check_private_directory(config_path.parent)
            if args.memory or os.name == "nt":
                payload = _read_account_config(settings, config_path, user_id)
            else:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
            if args.memory:
                return _manage_memory(settings, user_id, args.memory)
            provider = ProviderConfig.model_validate(payload["provider"])
            base_url = normalize_base_url(str(provider.base_url))
            api_key_env = provider.api_key_env
            source_path = payload["snippet_style"].get("library_source", "")
            slot = credential_slot(api_key_env, source_path)
            key = load_api_key(slot, base_url)
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
            if credential_slot(api_key_env, sources.library_source.as_posix()) != slot:
                raise ValueError("credential slot changed while loading configuration")
            if os.name == "nt" and sources.partner_user_id != user_id:
                raise ValueError("sharing account mismatch")
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
            session_facts = (
                ShareFacts(user_id, target_scope=sources.style_version)
                if os.name in {"posix", "nt"} else None
            )
            calibration = None
            if session_facts is not None and sources.partner_user_id == user_id:
                try:
                    calibration = ShareCalibration(
                        settings.private_data_dir, user_id, style_version=sources.style_version,
                    )
                except (OSError, ValueError):
                    print("本地风格校准资料不可用。")
            bridge = _build_bridge(
                settings, config, secrets, user_id, session_facts=session_facts,
                calibration=calibration,
            )
        except ShareSessionBusy:
            print("已经有一个聊天窗口在运行，请先关闭它。")
            return 1
        except Exception:
            print("本地记忆管理暂不可用。" if args and args.memory else "文本聊天暂时无法启动。")
            return 1

        def build_calibration_provider():
            return composition.OpenAICompatibleProvider(
                config=config.provider, api_key=secrets.api_key,
                max_output_tokens=min(config.budget.max_output_tokens, 640),
            )

        def calibrate(*, web: bool, auto_generate: bool = False) -> None:
            ledger = MonthlyUsageLedger(settings.runtime_data_dir / "dialogue", config.budget)
            if web:
                from companion.runtime.share_calibration_web import run_web_calibration

                run_web_calibration(
                    library=sources.library_source, calibration=calibration,
                    provider_factory=build_calibration_provider, ledger=ledger,
                    facts=session_facts, user_id=user_id,
                    auto_generate=auto_generate,
                )
            else:
                calibration.run(
                    sources.library_source, build_calibration_provider, ledger,
                    session_facts,
                )

        batch = None
        blind_active = False

        def start_blind() -> None:
            nonlocal batch, blind_active
            if batch is not None:
                print("本窗口已有一个盲评批次。")
                return
            try:
                if input(BLIND_NOTICE).strip() == "同意":
                    batch = BlindBatch(settings.test_results_dir)
                    blind_active = True
            except (EOFError, KeyboardInterrupt):
                pass

        def save_feedback() -> None:
            while batch is not None:
                try:
                    path = batch.save()
                except (OSError, ValueError):
                    print("汇总未保存。请检查结果目录权限和可用空间。如已有同名损坏文件，请先移开。")
                    try:
                        if input("回车重试，输入「退出」结束：").strip():
                            return
                    except (EOFError, KeyboardInterrupt):
                        return
                else:
                    print(f"匿名反馈已保存：{path}")
                    return

        try:
            if _first_run:
                if calibration is not None and session_facts is not None:
                    calibrate(web=True, auto_generate=True)
                else:
                    print("首次校准暂不可用，可以继续聊天。")
            print(
                "聊天窗口已就绪。输入消息后回车发送，输入 /calibrate-web 打开本机校准，"
                "输入 /blind 开始双回复盲评，输入 /exit 退出。"
            )
            if args.blind:
                start_blind()
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
                if line == "/blind":
                    start_blind()
                    continue
                if line == "/feedback":
                    if batch is None:
                        print("本窗口还没有盲评反馈。")
                    else:
                        blind_active = False
                        save_feedback()
                        print("盲评已结束，可以继续普通聊天。")
                    continue
                if line in {"/calibrate", "/calibrate-web"}:
                    if calibration is None or session_facts is None:
                        print("当前账户的本地风格校准不可用。")
                    else:
                        calibrate(web=line == "/calibrate-web")
                    continue
                handled = False
                if session_facts is not None:
                    handled, confirmed = confirm_fact(line, user_id, session_facts)
                    if handled:
                        if confirmed is None:
                            continue
                        line = confirmed
                if blind_active and line.startswith("/"):
                    print("未识别的本地命令。")
                    continue
                local_memory = handled or line.startswith("记住")
                if blind_active and batch is not None and not local_memory:
                    try:
                        if not bridge.share_blind_turn(
                            user_id=user_id, text=line, choose=batch.choose,
                        ):
                            return 0
                    except SpeechDialogueBridgeError:
                        batch.record_failure()
                        print("这轮未能生成可比较的两条回复，未计票。可以继续下一题。")
                    continue
                try:
                    response = bridge.reply(user_id=user_id, text=line)
                except SpeechDialogueBridgeError:
                    print("这次没能回复，换个说法再试吧。")
                    continue
                print(f"{config.character_name}：{response.text}")
        finally:
            bridge.close()
            if session_facts is not None:
                session_facts.clear()
            if batch is not None:
                save_feedback()


if __name__ == "__main__":
    raise SystemExit(main())
