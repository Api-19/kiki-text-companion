# ruff: noqa: RUF001
"""Cross-platform text console for snippet-style dialogue."""

from __future__ import annotations

import argparse
import json
import os
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
    orchestrator = build_real_orchestrator(
        settings, config, secrets, trusted_user_id=user_id, snippet_style=adapter,
        snippet_habit_card=habit_card, privacy_revision_sources=(adapter,),
    )
    return SpeechDialogueBridge(
        orchestrator=orchestrator, privacy_revision=orchestrator.privacy_revision,
    )


def main(argv: list[str] | None = None) -> int:
    with ExitStack() as stack:
        try:
            from companion.dialogue.config import load_dialogue_config
            from companion.dialogue.models import ProviderConfig
            from companion.runtime.speech_dialogue import SpeechDialogueBridgeError

            parser = argparse.ArgumentParser(
                add_help=False, allow_abbrev=False, exit_on_error=False,
            )
            parser.add_argument("--project-root", type=Path, default=Path.cwd())
            args, unknown = parser.parse_known_args(argv)
            if unknown:
                raise ValueError("unknown arguments")
            settings = load_settings(args.project_root)
            stack.enter_context(share_session_lock(settings.runtime_data_dir))
            user_id = current_user_id()
            config_path = settings.private_data_dir / "dialogue/runtime-config.json"
            reject_link_ancestors(config_path)
            check_private_directory(settings.private_data_dir)
            check_private_directory(config_path.parent)
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
            print("文本聊天暂时无法启动。")
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
