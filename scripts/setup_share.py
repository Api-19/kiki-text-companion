# ruff: noqa: RUF001
"""Set up local share-mode chat with consent and free, offline checks only."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.dialogue.config import load_dialogue_config
from companion.dialogue.models import BudgetConfig, ProviderConfig
from companion.importer import ios_wechat_backup as ios
from companion.importer.standard_json import StandardJsonAdapter
from companion.privacy.paired_snippets import read_snippets
from companion.privacy.veiled import load_veiled_terms
from companion.runtime.local_identity import current_user_id
from companion.runtime.private_paths import ensure_private_directory, reject_link_ancestors
from companion.runtime.settings import load_settings
from companion.runtime.share_credentials import (
    CredentialError,
    load_api_key,
    normalize_base_url,
    store_api_key,
)
from companion.runtime.share_lock import ShareSessionBusy, share_session_lock
from scripts import build_habit_card, build_paired_snippets
from scripts.convert_chat_jsonl import convert
from scripts.share_prepare_jsonl import prepare_share_jsonl

CONSENT_TEXT = """请两位分别确认同意使用这段聊天设置分享模式。
只学习主人发出的消息；对方消息只作上下文，不作为被模仿的说话样本。
导出的聊天原始数据只存在本机；聊天时会把所需片段发送给你选择的云端模型服务。
只自动脱敏固定格式信息，不自动识别人名、地名、学校或单位。
人名、地名、学校、单位等信息不会被替换，会随聊天片段原样发送给你选择的云端模型服务。
第三方导出工具可能违反微信用户协议，由使用者自行判断。
设置只做免费离线检查，不请求云端模型；是否像本人未经人工验证。"""
OWNER_CONSENT_PROMPT = "被模仿的人（主人）如同意，请输入“同意”两个字后回车："
PARTNER_CONSENT_PROMPT = "陪聊的人（对方）如同意，请输入“同意”两个字后回车："
OWNER_PRONOUN_PROMPT = "被模仿的人是男生还是女生？输入 他 或 她（直接回车为 他）："
PARTNER_PRONOUN_PROMPT = "陪聊的人是男生还是女生？输入 他 或 她（直接回车为 她）："
PROVIDER_URL_PROMPT = "模型服务地址（直接回车用 https://api.deepseek.com/v1）："
PROVIDER_MODEL_PROMPT = "模型名（直接回车用 deepseek-chat）："
API_KEY_PROMPT = "请粘贴 API key（输入时不显示）："
REBUILD_API_KEY_PROMPT = "请粘贴 API key（输入时不显示；直接回车保留已保存的 key）："
MISSING_HABIT_CARD_TEXT = "没有找到现有习惯卡，不能保留；去掉 --keep-habit-card 重新设置。"
KEPT_HABIT_CARD_TEXT = "习惯卡：保留现有（未重新生成）"
ALREADY_CONFIGURED_TEXT = "已经设置过；如需重新生成请加 --rebuild"
ENCRYPTED_BACKUP_TEXT = "请在 Finder/iTunes 里关闭'加密本地备份'后重新备份"
CHECK_SUMMARY_TEXT = "自动检查通过；是否像本人未经人工验证。"
START_CHAT_TEXT = '以后双击"开始聊天"即可。'
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


@dataclass(frozen=True)
class SharePaths:
    root: Path
    private: Path
    runtime: Path
    test_results: Path


def share_paths() -> SharePaths:
    """Return platform paths without creating directories or reading user data."""
    if sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"]) / "kiki-share"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/kiki-share"
    else:
        root = Path.home() / ".local/share/kiki-share"
    return SharePaths(root, root / "private", root / "runtime", root / "test-results")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description=__doc__)
    parser.add_argument("--format", choices=("ios-backup", "jsonl", "standard"),
                        default="ios-backup")
    parser.add_argument("--chat", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--through-date", type=date.fromisoformat)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--owner-pronoun", choices=("他", "她"))
    parser.add_argument("--partner-pronoun", choices=("他", "她"))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--keep-habit-card", action="store_true")
    args = parser.parse_args(argv)
    if args.format in ("jsonl", "standard") and args.chat is None:
        parser.error("--chat required")
    args.timezone = ZoneInfo(args.timezone)
    if args.through_date is None:
        args.through_date = datetime.now(args.timezone).date()
    return args


def _prepare_directories(paths: SharePaths) -> None:
    reject_link_ancestors(paths.root)
    missing = []
    parent = paths.root.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        ensure_private_directory(directory)
    for directory in (
        paths.root, paths.private, paths.runtime, paths.test_results,
        paths.private / "share", paths.private / "style", paths.private / "style/snippets",
        paths.private / "privacy", paths.private / "dialogue",
    ):
        ensure_private_directory(directory)


def _write_json(path: Path, payload: dict | list[dict]) -> None:
    reject_link_ancestors(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")


def _filter_standard_chat(
    source: Path, out: Path, *, through_date: date, tz: ZoneInfo,
) -> Path:
    if source.resolve() == out.resolve():
        raise ValueError("standard input and filtered output must differ")
    reject_link_ancestors(out)
    out.unlink(missing_ok=True)
    records = StandardJsonAdapter().parse(source).records
    cutoff_utc = datetime.combine(
        through_date + timedelta(days=1), time.min, tzinfo=tz,
    ).astimezone(UTC)
    payload = [
        {"schema_version": 1, **record.model_dump(mode="json")}
        for record in records if record.timestamp.astimezone(UTC) < cutoff_utc
    ]
    if not payload:
        raise ValueError("no messages through cutoff date")
    _write_json(out, payload)
    return out


def _choose(labels: list[str], prompt: str) -> int:
    for index, label in enumerate(labels, 1):
        print(f"{index}. {label}")
    selected = int(input(prompt))
    if not 1 <= selected <= len(labels):
        raise ValueError("invalid selection")
    return selected - 1


def _ask_pronoun(prompt: str, default: str) -> str:
    while True:
        pronoun = input(prompt).strip() or default
        if pronoun in ("他", "她"):
            return pronoun


def _read_api_key(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main(argv: list[str] | None = None) -> int:
    step = "参数"
    pending_config: Path | None = None
    with ExitStack() as stack:
        try:
            args = _arguments(argv)
            paths = share_paths()
            os.environ.update({
                "COMPANION_PRIVATE_DATA_DIR": str(paths.private),
                "COMPANION_RUNTIME_DATA_DIR": str(paths.runtime),
                "COMPANION_TEST_RESULTS_DIR": str(paths.test_results),
            })

            step = "同意确认"
            print(CONSENT_TEXT)
            if input(OWNER_CONSENT_PROMPT) != "同意":
                return 2
            if input(PARTNER_CONSENT_PROMPT) != "同意":
                return 2

            step = "本机目录"
            stack.enter_context(share_session_lock(paths.runtime))
            config_path = paths.private / "dialogue/runtime-config.json"
            reject_link_ancestors(config_path)
            if config_path.exists() and not args.rebuild:
                print(ALREADY_CONFIGURED_TEXT)
                raise ValueError("already configured")
            _prepare_directories(paths)

            step = "人称"
            previous_pronouns = (
                json.loads(config_path.read_text(encoding="utf-8"))["snippet_style"]["pronouns"]
                if args.rebuild and config_path.exists()
                and (args.owner_pronoun is None or args.partner_pronoun is None) else None
            )
            if args.owner_pronoun is None:
                args.owner_pronoun = (previous_pronouns["owner"] if previous_pronouns is not None
                                      else _ask_pronoun(OWNER_PRONOUN_PROMPT, "他"))
            if args.partner_pronoun is None:
                args.partner_pronoun = (
                    previous_pronouns["partner"] if previous_pronouns is not None
                    else _ask_pronoun(PARTNER_PRONOUN_PROMPT, "她")
                )

            step = "本机目录"
            snippets = paths.private / "style/snippets"
            habit_card = snippets / "habit_card.txt"
            if args.keep_habit_card:
                reject_link_ancestors(habit_card)
                if not habit_card.is_file():
                    print(MISSING_HABIT_CARD_TEXT)
                    raise ValueError("no habit card to keep")
            if args.rebuild:
                config_path.unlink(missing_ok=True)
            export = paths.private / "share/export.jsonl"
            prepared = paths.private / "share/prepared.jsonl"
            # Without a published config, retry can regenerate these exclusive outputs.
            for intermediate in (export, prepared, habit_card):
                reject_link_ancestors(intermediate)
                if not (args.keep_habit_card and intermediate == habit_card):
                    intermediate.unlink(missing_ok=True)
            settings = load_settings(Path.cwd())

            step = "同意记录"
            _write_json(paths.private / "share/consent.json", {
                "version": 1,
                "agreed_at": datetime.now(UTC).isoformat(),
                "consent_sha256": hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest(),
            })

            source = args.chat
            if args.format == "ios-backup":
                step = "找不到 iPhone 备份"
                backups = ([args.backup] if args.backup is not None
                           else ios.find_backups(ios.default_backup_roots()))
                if not backups:
                    raise ios.IosBackupError("no backups")
                step = "选择 iPhone 备份"
                backup = backups[0] if len(backups) == 1 else backups[
                    _choose([item.name for item in backups], "请选择备份编号：")
                ]
                step = "检查 iPhone 备份"
                if ios.is_encrypted(backup):
                    print(ENCRYPTED_BACKUP_TEXT)
                    raise ios.IosBackupError("encrypted backup")
                step = "选择微信账号"
                accounts = ios.list_accounts(backup)
                account = accounts[0] if len(accounts) == 1 else accounts[
                    _choose(accounts, "请选择账号编号：")
                ]
                step = "选择聊天会话"
                conversations = ios.list_conversations(backup, account)
                labels = [
                    f"{item.display_name}（{item.message_count}，"
                    f"{datetime.fromtimestamp(item.last_timestamp, args.timezone).date()}）"
                    for item in conversations
                ]
                conversation = conversations[_choose(labels, "请选择会话编号：")]
                step = "导出聊天"
                source = export
                ios.export_conversation_jsonl(backup, account, conversation.table, source)

            if args.format != "standard":
                step = "导出聊天"
                stats = prepare_share_jsonl(source, prepared)
                source = prepared
                print(f"同秒含图片等消息的时刻：{stats.same_second_groups} 处（已按原顺序保留）")

            step = "聊天转换"
            if args.format == "standard":
                standard = _filter_standard_chat(
                    source, paths.private / "share/filtered-standard.json",
                    through_date=args.through_date, tz=args.timezone,
                )
            else:
                standard = convert(
                    source, settings, conversation_id="share-chat",
                    through_date=args.through_date, tz=args.timezone, keep_nontext_markers=True,
                ).output

            step = "片段库"
            terms_path = paths.private / "share/terms.json"
            _write_json(terms_path, {})
            deleted_path = snippets / "deleted_ids.json"
            if deleted_path.exists():
                snippet_ids = json.loads(deleted_path.read_text(encoding="utf-8"))["snippet_ids"]
                if snippet_ids:
                    print(
                        "分享模式只支持按时间段删除；"
                        f"删除记录里有 {len(snippet_ids)} 条按片段记录的条目，"
                        "请改为按时间段记录后再重新设置。"
                    )
                    raise ValueError("unsupported deleted snippet IDs")
            if build_paired_snippets.main([
                "--chat", str(standard), "--terms", str(terms_path),
                "--out-dir", str(snippets), "--direction-mapping", "--exclude-near-nontext",
            ]) != 0:
                raise ValueError("snippet build failed")

            step = "习惯卡"
            if args.keep_habit_card:
                print(KEPT_HABIT_CARD_TEXT)
            elif build_habit_card.main([
                "--library", str(snippets / "library.jsonl"), "--out", str(habit_card),
            ]) != 0:
                raise ValueError("habit card build failed")

            step = "用户词表"
            veiled_terms = paths.private / "privacy/veiled_terms.json"
            reject_link_ancestors(veiled_terms)
            if veiled_terms.exists():
                load_veiled_terms(veiled_terms)
            else:
                _write_json(veiled_terms, {
                    "version": 1, "note": "", "high": {}, "suspect": {},
                    "variants": {"allow_separators": False, "pinyin_initials": {}},
                })

            step = "模型服务"
            base_url = input(PROVIDER_URL_PROMPT).strip() or DEFAULT_BASE_URL
            model = input(PROVIDER_MODEL_PROMPT).strip() or "deepseek-chat"
            api_key_env = ("DEEPSEEK_API_KEY" if normalize_base_url(base_url) == DEFAULT_BASE_URL
                           else "OPENAI_API_KEY")
            payload = {
                "provider": {
                    "base_url": base_url, "model": model, "timeout_seconds": 30,
                    "api_key_env": api_key_env,
                },
                "budget": {
                    "monthly_cap_cny": "100.00", "input_cny_per_million": "4",
                    "output_cny_per_million": "16", "max_output_tokens": 512,
                    "worst_case_request_cny": "0.21",
                },
                "snippet_style": {
                    "library_source": "style/snippets/library.jsonl",
                    "habit_card_source": "style/snippets/habit_card.txt",
                    "veiled_terms_source": "privacy/veiled_terms.json",
                    "pronouns": {"owner": args.owner_pronoun, "partner": args.partner_pronoun},
                    "partner_user_id": current_user_id(), "style_version": "share-v1",
                },
            }
            provider = ProviderConfig.model_validate(payload["provider"])
            base_url = normalize_base_url(str(provider.base_url))
            BudgetConfig.model_validate(payload["budget"])

            step = "保存 API key"
            prompt = REBUILD_API_KEY_PROMPT if args.rebuild else API_KEY_PROMPT
            while True:
                key = _read_api_key(prompt)
                if key.strip() or not args.rebuild:
                    store_api_key(api_key_env, base_url, key)
                    break
                try:
                    load_api_key(api_key_env, base_url)
                    break
                except CredentialError:
                    print("这个服务地址还没有保存过 key，请粘贴")
                    prompt = API_KEY_PROMPT
            load_api_key(api_key_env, base_url)

            step = "免费离线检查"
            library = read_snippets(snippets / "library.jsonl")
            holdout = read_snippets(snippets / "holdout.jsonl")
            if {item.snippet_id for item in library}.intersection(
                item.snippet_id for item in holdout
            ):
                raise ValueError("library and holdout overlap")
            # Publish last; a failed read-back must leave no launchable configuration.
            pending_config = config_path
            _write_json(config_path, payload)
            load_dialogue_config(load_settings(Path.cwd()), environ={api_key_env: "offline-check"})
            print(f"片段数：{len(library)}")
            print(f"留出数：{len(holdout)}")
            print(f"样本结论：{'通过' if len(library) >= 30 else '证据不足'}")
            print(CHECK_SUMMARY_TEXT)
            print(START_CHAT_TEXT)
        except ShareSessionBusy:
            print("聊天窗口还开着，请先关闭再设置。")
            return 1
        except Exception:
            if pending_config is not None:
                pending_config.unlink(missing_ok=True)
            print(f"设置没有完成：{step}")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
