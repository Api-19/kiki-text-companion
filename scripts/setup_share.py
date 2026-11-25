# ruff: noqa: RUF001
"""Set up local share-mode chat with consent and free, offline checks only."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import warnings
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
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
    credential_slot,
    delete_api_key,
    load_api_key,
    normalize_base_url,
    store_api_key,
)
from companion.runtime.share_lock import ShareSessionBusy, share_session_lock
from scripts import build_habit_card, build_paired_snippets
from scripts.convert_chat_jsonl import convert
from scripts.share_prepare_jsonl import prepare_share_jsonl

CONSENT_TEXT = """请两位分别确认同意使用这段聊天设置分享模式。
只学习你指定一方的消息；另一方的消息只作上下文。
导出的聊天原始数据只存在本机；聊天时会把所需片段发送给你选择的云端模型服务。
只自动脱敏固定格式信息，不自动识别人名、地名、学校或单位。
人名、地名、学校、单位等信息不会被替换，会随聊天片段原样发送给你选择的云端模型服务。
第三方导出工具可能违反微信用户协议，由使用者自行判断。
设置阶段先在本机分析。只有明确选择开始分析并出题，才会向模型发送匿名统计；是否像本人未经人工验证。"""
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
                        default=None)
    parser.add_argument("--guided", action="store_true")
    parser.add_argument("--chat", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--through-date", type=date.fromisoformat)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--owner-pronoun", choices=("他", "她"))
    parser.add_argument("--partner-pronoun", choices=("他", "她"))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--keep-habit-card", action="store_true")
    args = parser.parse_args(argv)
    args.guided = args.guided or (argv is None and args.format is None)
    if args.format is None and not args.guided:
        args.format = "ios-backup"
    if not args.guided and args.format in ("jsonl", "standard") and args.chat is None:
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


def _set_pronouns(args, previous: dict | None) -> None:
    if args.owner_pronoun is None:
        args.owner_pronoun = (previous["owner"] if previous is not None
                              else _ask_pronoun(OWNER_PRONOUN_PROMPT, "他"))
    if args.partner_pronoun is None:
        args.partner_pronoun = (previous["partner"] if previous is not None
                                else _ask_pronoun(PARTNER_PRONOUN_PROMPT, "她"))


def _chat_identity(standard: Path, identity: tuple[str, ...]) -> tuple[str, str]:
    records = StandardJsonAdapter().parse(standard).records
    if not records:
        raise ValueError("empty chat")
    text_directions = {record.direction.value for record in records
                       if record.content_type == "text" and record.text and record.text.strip()}
    if text_directions != {"inbound", "outbound"}:
        raise ValueError("both sides need text")
    speakers = sorted({(record.direction.value, record.source_speaker_id) for record in records})
    content = ""
    if identity[0] != "ios-backup":
        with standard.open("rb") as stream:
            content = hashlib.file_digest(stream, "sha256").hexdigest()
    def digest(source: tuple[str, ...]) -> str:
        payload = (source, records[0].conversation_id, speakers, content)
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    return digest(identity if identity[0] == "ios-backup" else identity[:1]), digest(identity)


def _discard_key(slot: str, address: str) -> None:
    with suppress(CredentialError):
        delete_api_key(slot, address)


def _publish_guided(stage: Path, private: Path, snapshot: Path) -> None:
    files = (
        "share/export.jsonl", "share/prepared.jsonl", "share/filtered-standard.json",
        "share/consent.json", "share/terms.json", "privacy/veiled_terms.json",
        "dialogue/runtime-config.json",
    )
    snapshots = private / snapshot.parent
    reject_link_ancestors(snapshots)
    ensure_private_directory(snapshots)
    with ExitStack() as rollback:
        # The existing config keeps referencing its own files until the last replace.
        (stage / snapshot).replace(private / snapshot)
        rollback.callback(shutil.rmtree, private / snapshot)
        for index, relative in enumerate(files):
            pending, final = stage / relative, private / relative
            reject_link_ancestors(final)
            if final.exists():
                saved = stage / f"rollback-{index}"
                shutil.copy2(final, saved)
                rollback.callback(os.replace, saved, final)
            else:
                rollback.callback(final.unlink, missing_ok=True)
            if pending.exists():
                pending.replace(final)
            else:
                final.unlink(missing_ok=True)
        rollback.pop_all()


class _SetupStageError(ValueError):
    """A fixed label for a setup step."""


def _build_library(
    work: Path, private: Path, prior_snippets: Path, standard: Path,
    target: str, *, keep_habit_card: bool,
) -> None:
    staged = work != private
    snippets = work / "style/snippets"
    habit_card = snippets / "habit_card.txt"
    terms_path = work / "share/terms.json"
    _write_json(terms_path, {})
    deleted_path = prior_snippets / "deleted_ids.json"
    reject_link_ancestors(deleted_path)
    if deleted_path.exists():
        snippet_ids = json.loads(deleted_path.read_text(encoding="utf-8"))["snippet_ids"]
        if snippet_ids:
            print(
                "分享模式只支持按时间段删除；"
                f"删除记录里有 {len(snippet_ids)} 条按片段记录的条目，"
                "请改为按时间段记录后再重新设置。"
            )
            raise _SetupStageError("片段库")
        if staged:
            shutil.copy2(deleted_path, snippets / "deleted_ids.json")
    if build_paired_snippets.main([
        "--chat", str(standard), "--terms", str(terms_path),
        "--out-dir", str(snippets), "--direction-mapping", "--exclude-near-nontext",
        "--target-direction", target,
    ]) != 0:
        raise _SetupStageError("片段库")

    if keep_habit_card:
        if staged:
            shutil.copy2(prior_snippets / "habit_card.txt", habit_card)
        print(KEPT_HABIT_CARD_TEXT)
    elif build_habit_card.main([
        "--library", str(snippets / "library.jsonl"), "--out", str(habit_card),
    ]) != 0:
        raise _SetupStageError("习惯卡")

    veiled_terms = private / "privacy/veiled_terms.json"
    working_terms = work / "privacy/veiled_terms.json"
    reject_link_ancestors(veiled_terms)
    if veiled_terms.exists():
        try:
            load_veiled_terms(veiled_terms)
        except ValueError:
            raise _SetupStageError("用户词表") from None
        if staged:
            shutil.copy2(veiled_terms, working_terms)
    else:
        _write_json(working_terms, {
            "version": 1, "note": "", "high": {}, "suspect": {},
            "variants": {"allow_separators": False, "pinyin_initials": {}},
        })


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

            if args.format is None:
                step = "导入方式"
                args.format = ("ios-backup", "jsonl", "standard")[_choose(
                    ["iPhone 备份", "JSONL 文件", "Standard JSON 文件"], "请选择导入方式编号：",
                )]

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

            previous_source, previous_provider, previous_budget = {}, None, None
            if args.rebuild and config_path.exists():
                try:
                    previous = json.loads(config_path.read_text(encoding="utf-8"))
                    previous_provider = ProviderConfig.model_validate(previous["provider"])
                    previous_budget = BudgetConfig.model_validate(previous["budget"])
                    previous_source = previous["snippet_style"]
                    if not isinstance(previous_source, dict):
                        raise ValueError("invalid snippet configuration")
                except (ValueError, KeyError, TypeError):
                    previous_source, previous_provider, previous_budget = {}, None, None
                    if args.owner_pronoun is None or args.partner_pronoun is None:
                        raise
            previous_version = previous_source.get("style_version", "share-v1")
            previous_target = (
                "inbound" if previous_version.startswith("share-v2-in-") else "outbound"
            )
            previous_library = previous_source.get("library_source", "style/snippets/library.jsonl")
            staged = args.guided or args.rebuild
            work = paths.private
            if staged:
                work = Path(stack.enter_context(TemporaryDirectory(dir=paths.private)))
                for relative in ("share", "style/snippets", "privacy", "dialogue"):
                    (work / relative).mkdir(mode=0o700, parents=True)
                (work / "style").chmod(0o700)

            if not args.guided:
                step = "人称"
                _set_pronouns(args, previous_source.get("pronouns"))
            step = "本机目录"
            snippets = work / "style/snippets"
            habit_card = snippets / "habit_card.txt"
            prior_snippets = (paths.private / previous_library).parent
            reject_link_ancestors(prior_snippets)
            if not prior_snippets.resolve().is_relative_to(paths.private):
                raise ValueError("invalid previous snippet directory")
            if args.keep_habit_card:
                prior_card = prior_snippets / "habit_card.txt"
                reject_link_ancestors(prior_card)
                if not prior_card.is_file():
                    print(MISSING_HABIT_CARD_TEXT)
                    raise ValueError("no habit card to keep")
            export = work / "share/export.jsonl"
            prepared = work / "share/prepared.jsonl"
            # Without a published config, retry can regenerate these exclusive outputs.
            for intermediate in (export, prepared, habit_card):
                reject_link_ancestors(intermediate)
                if not (args.keep_habit_card and intermediate == habit_card):
                    intermediate.unlink(missing_ok=True)
            settings = load_settings(Path.cwd())
            import_settings = settings.model_copy(update={"private_data_dir": work})

            step = "同意记录"
            _write_json(work / "share/consent.json", {
                "version": 1,
                "agreed_at": datetime.now(UTC).isoformat(),
                "consent_sha256": hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest(),
            })

            source = args.chat
            identity = (args.format,)
            source_label = ""
            if args.format != "ios-backup":
                if source is None:
                    step = "聊天文件"
                    filename = input("请输入聊天文件的完整路径：").strip()
                    if not filename:
                        raise ValueError("missing file")
                    source = Path(filename).expanduser()
                identity = (args.format, str(source.resolve()))
                source_label = source.name
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
                identity = ("ios-backup", account, conversation.table)
                source_label = conversation.display_name
                ios.export_conversation_jsonl(backup, account, conversation.table, source)

            if args.format != "standard":
                step = "导出聊天"
                stats = prepare_share_jsonl(source, prepared)
                source = prepared
                print(f"同秒含图片等消息的时刻：{stats.same_second_groups} 处（已按原顺序保留）")

            step = "聊天转换"
            if args.format == "standard":
                standard = _filter_standard_chat(
                    source, work / "share/filtered-standard.json",
                    through_date=args.through_date, tz=args.timezone,
                )
            else:
                standard = convert(
                    source, import_settings, conversation_id="share-chat",
                    through_date=args.through_date, tz=args.timezone, keep_nontext_markers=True,
                ).output

            target = previous_target
            version = previous_version
            if staged or args.rebuild:
                # Validate both participants before asking which persona to use.
                step = "聊天转换"
                binding, legacy_binding = _chat_identity(standard, identity)
                if args.guided:
                    step = "模拟对象"
                    labels = ["模仿原账号／发送方", "模仿聊天对方／接收方"]
                    if args.rebuild and previous_source:
                        labels.append("沿用上次的模拟方向")
                    selected = _choose(labels, "请选择模仿哪一方的编号：")
                    if selected != 2:
                        target = ("outbound", "inbound")[selected]
                side = "in" if target == "inbound" else "out"
                version = f"share-v2-{side}-{binding}"
                if previous_version == f"share-v2-{side}-{legacy_binding}":
                    version = previous_version
                if (
                    args.keep_habit_card and version != previous_version
                    and (args.guided or previous_version != "share-v1")
                ):
                    print("模拟对象已变化，请去掉 --keep-habit-card 重新生成。")
                    raise ValueError("habit card belongs to another target")
            if args.guided:
                step = "人称"
                same_target = version == previous_version
                label = "".join(char if char.isprintable() else " " for char in source_label)
                role = "聊天对方／接收方" if target == "inbound" else "原账号／发送方"
                print(f"本次仅导入「{label}」截至 {args.through_date} 的私聊，模仿{role}。")
                if args.rebuild and previous_source:
                    print("沿用此目标的已有校准。" if same_target else
                          "无法确认是同一目标，需要重新校准，旧答案不会用于此次设置。")
                step = "范围和角色同意"
                if input("双方确认同意上述范围和用途，请输入「同意」：").strip() != "同意":
                    return 2
                step = "人称"
                print("下面的人称跟随刚选定的角色。先填被模仿者，再填陪聊者。")
                _set_pronouns(args, previous_source.get("pronouns") if same_target else None)

            if not staged:
                _build_library(
                    work, paths.private, prior_snippets, standard, target,
                    keep_habit_card=args.keep_habit_card,
                )

            step = "模型服务"
            default_url, default_model = DEFAULT_BASE_URL, "deepseek-chat"
            url_prompt, model_prompt = PROVIDER_URL_PROMPT, PROVIDER_MODEL_PROMPT
            if previous_provider is not None:
                default_url = normalize_base_url(str(previous_provider.base_url))
                default_model = previous_provider.model
                url_prompt = f"模型服务地址（直接回车沿用 {default_url}）："
                model_prompt = f"模型名（直接回车沿用 {default_model}）："
            base_url = input(url_prompt).strip() or default_url
            model = input(model_prompt).strip() or default_model
            selected_url = normalize_base_url(str(ProviderConfig(
                base_url=base_url, model=model, timeout_seconds=30,
            ).base_url))
            api_key_env = ("DEEPSEEK_API_KEY" if selected_url == DEFAULT_BASE_URL
                           else "OPENAI_API_KEY")
            if previous_provider is not None and selected_url == default_url:
                api_key_env = previous_provider.api_key_env
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
                    "partner_user_id": current_user_id(), "style_version": version,
                },
            }
            if previous_budget is not None:
                payload["budget"] = previous_budget.model_dump(mode="json")
            provider = ProviderConfig.model_validate(payload["provider"])
            base_url = normalize_base_url(str(provider.base_url))
            BudgetConfig.model_validate(payload["budget"])

            step = "保存 API key"
            previous_slot = (
                credential_slot(previous_provider.api_key_env, previous_library)
                if previous_provider is not None and default_url == base_url
                else api_key_env
            )
            prompt = REBUILD_API_KEY_PROMPT if args.rebuild else API_KEY_PROMPT
            while True:
                key = _read_api_key(prompt)
                if key.strip() or not args.rebuild:
                    if not key.strip():
                        raise ValueError("empty key")
                    if not staged:
                        store_api_key(api_key_env, base_url, key)
                    break
                try:
                    load_api_key(previous_slot, base_url)
                    break
                except CredentialError:
                    print("这个服务地址还没有保存过 key，请粘贴")
                    prompt = API_KEY_PROMPT
            if not staged and load_api_key(api_key_env, base_url) != key.strip():
                raise CredentialError("credential readback mismatch")

            if staged:
                print("正在本机配对、过滤并生成习惯卡。")
                _build_library(
                    work, paths.private, prior_snippets, standard, target,
                    keep_habit_card=args.keep_habit_card,
                )

            step = "免费离线检查"
            library = read_snippets(snippets / "library.jsonl")
            holdout = read_snippets(snippets / "holdout.jsonl")
            if {item.snippet_id for item in library}.intersection(
                item.snippet_id for item in holdout
            ):
                raise ValueError("library and holdout overlap")
            # Publish last; a failed read-back must leave no launchable configuration.
            if staged:
                snapshot_name = uuid4().hex
                if args.rebuild and key.strip():
                    snapshot_name += f"-key-{uuid4().hex}"
                elif previous_slot != api_key_env:
                    snapshot_name += f"-key-{previous_slot.rsplit('|', 1)[1]}"
                snapshot = Path("style", "share-snapshots", snapshot_name)
                (work / snapshot.parent).mkdir(mode=0o700)
                snippets.replace(work / snapshot)
                payload["snippet_style"].update(
                    library_source=(snapshot / "library.jsonl").as_posix(),
                    habit_card_source=(snapshot / "habit_card.txt").as_posix(),
                )
                _write_json(work / "dialogue/runtime-config.json", payload)
                load_dialogue_config(import_settings, environ={api_key_env: "offline-check"})
                step = "保存 API key"
                slot = credential_slot(api_key_env, payload["snippet_style"]["library_source"])
                with ExitStack() as cleanup:
                    if key.strip():
                        if slot != api_key_env:
                            cleanup.callback(_discard_key, slot, base_url)
                        store_api_key(slot, base_url, key)
                    saved_key = load_api_key(slot, base_url)
                    if key.strip() and saved_key != key.strip():
                        raise CredentialError("credential readback mismatch")
                    step = "发布本机设置"
                    _publish_guided(work, paths.private, snapshot)
                    cleanup.pop_all()
                if previous_slot != api_key_env and previous_slot != slot:
                    _discard_key(previous_slot, base_url)
                if (
                    prior_snippets.parent == paths.private / snapshot.parent
                    and re.fullmatch(r"[0-9a-f]{32}(?:-key-[0-9a-f]{32})?", prior_snippets.name)
                ):
                    with suppress(OSError):
                        shutil.rmtree(prior_snippets)
            else:
                pending_config = config_path
                _write_json(config_path, payload)
                load_dialogue_config(settings, environ={api_key_env: "offline-check"})
            pending_config = None
            print(f"片段数：{len(library)}")
            print(f"留出数：{len(holdout)}")
            print(f"样本结论：{'通过' if len(library) >= 30 else '证据不足'}")
            print(CHECK_SUMMARY_TEXT)
            if args.guided:
                print("将使用你的模型分析匿名统计并出题，可能计费。聊天片段只在本机对照。")
                try:
                    accepted = input("输入「开始」分析并出题，直接回车跳过：").strip() == "开始"
                except (EOFError, KeyboardInterrupt):
                    accepted = False
                if accepted:
                    from companion.runtime import text_chat

                    if text_chat.main([], _lock_held=True, _first_run=True) != 0:
                        print("首次分析暂不可用。可在正常聊天中重新校准。")
            print(START_CHAT_TEXT)
        except ShareSessionBusy:
            print("聊天窗口还开着，请先关闭再设置。")
            return 1
        except Exception:
            if pending_config is not None:
                pending_config.unlink(missing_ok=True)
            error = sys.exception()
            if isinstance(error, ios.IosBackupError):
                if str(error) == "nonempty backup WAL":
                    print("备份中有未合并的 SQLite 日志（WAL）。请重新生成备份，或导入文件。")
                elif str(error) != "encrypted backup":
                    print("无法读取此 iPhone 备份。请检查备份是否完整、未加密且微信格式受支持。")
                print("也可选择导入 JSONL 或 Standard JSON 文件。原备份不会修改。")
            print(f"设置没有完成：{error if isinstance(error, _SetupStageError) else step}")
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
