# ruff: noqa: RUF001
"""Local selection, question generation, and confirmed share style additions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from companion.dialogue.context import PromptMessage
from companion.dialogue.provider import (
    InvalidProviderResponse,
    OpenAIProviderUnavailable,
    ProviderError,
    ProviderUnavailableKind,
    provider_failure_unbilled,
)
from companion.dialogue.shared_terms import SharedTerm
from companion.dialogue.usage import (
    BudgetExceeded,
    MonthlyUsageLedger,
    UsageLedgerError,
    UsageReviewRequired,
)
from companion.memory.errors import MemoryServiceError
from companion.privacy.paired_snippets import ActiveSnippetReadError, read_active_snippet_snapshot
from companion.runtime.private_paths import (
    check_private_directory,
    ensure_private_directory,
    reject_link_ancestors,
)
from companion.runtime.share_facts import confirm_fact

if TYPE_CHECKING:
    from collections.abc import Callable

    from companion.dialogue.provider import DialogueProvider
    from companion.runtime.share_facts import ShareFacts

_OS_NAME = os.name
_MAX_RULES = 20
_JOKE = re.compile(r"「([^」]{2,24})」|“([^”]{2,24})”")
_FACT = re.compile(
    r"(?:今天|明天|周末|这周|下周)(?:我|我们)?"
    r"(?:要带|要买|要取|得带|记得带)[^，,；;。！!？?]{1,70}[。！!]?"
)
_CONDITIONS = {
    "followup": "对方分享消息，判断是否继续追问时",
    "closure": "对方只是确认或话题已经说完时",
}


def _printable(value: str) -> str:
    if not value.strip() or not value.isprintable():
        raise ValueError("expected printable text")
    return value


Answer = Annotated[str, Field(min_length=1, max_length=160), AfterValidator(_printable)]
Term = Annotated[str, Field(min_length=2, max_length=24), AfterValidator(_printable)]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["followup", "closure"]
    answer: Answer
    scenario: Answer | None = None
    question: Answer | None = None


class Joke(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["joke"] = "joke"
    term: Term
    meaning: Answer
    permission: Literal["allowed", "understand_only"]


class Additions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    owner: str
    entries: Annotated[
        list[Annotated[Rule | Joke, Field(discriminator="kind")]], Field(max_length=_MAX_RULES)
    ]


QuestionText = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_printable)]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: QuestionText
    options: Annotated[list[QuestionText], Field(max_length=2)]


Questions = Annotated[list[Question], Field(min_length=1, max_length=4)]
_QUESTIONS = TypeAdapter(Questions)


class QuestionAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    uncertainty: Answer
    questions: Questions


@dataclass(frozen=True)
class Candidate:
    kind: Literal["followup", "closure", "joke", "fact"]
    context: str
    reply: str
    term: str = ""


def analyse_library(library: Path) -> tuple[list[Candidate], dict]:
    pairs, _ = read_active_snippet_snapshot(library, library.parent / "deleted_ids.json")
    candidates: dict[str, Candidate] = {}
    lengths = Counter({"short": 0, "medium": 0, "long": 0})
    bursts = Counter({"one": 0, "two": 0, "three_or_more": 0})
    matches = Counter({"followup": 0, "closure": 0, "joke": 0, "fact": 0})
    for pair in reversed(pairs or []):
        snippet = pair.as_style_snippet()
        if snippet is None or pair.privacy_flags:
            continue
        context, reply = snippet.partner_turns[-1], snippet.owner_reply[-1]
        if not context.isprintable() or not reply.isprintable():
            continue
        length = sum(len(line) for line in snippet.owner_reply)
        lengths["short" if length <= 5 else "medium" if length <= 20 else "long"] += 1
        bursts[("one", "two", "three_or_more")[len(snippet.owner_reply) - 1]] += 1
        quoted = _JOKE.search(context)
        if quoted and any(cue in context for cue in ("哈哈", "玩笑", "玩梗")):
            item = Candidate("joke", context, reply, quoted[1] or quoted[2])
        elif _FACT.fullmatch(context):
            item = Candidate("fact", context, reply)
        elif reply.endswith(("?", "？")) and not context.endswith(("?", "？")):
            item = Candidate("followup", context, reply)
        elif len(reply) <= 4 and not context.rstrip("。！!").endswith(("?", "？")):
            item = Candidate("closure", context, reply)
        else:
            continue
        matches[item.kind] += 1
        candidates.setdefault(item.kind, item)
    selected = [candidates[kind] for kind in (*_CONDITIONS, "joke", "fact") if kind in candidates]
    signals = {
        "pairs": lengths.total(),
        "reply_length": dict(lengths),
        "reply_bursts": dict(bursts),
        "candidate_signals": dict(matches),
    }
    return selected, signals


def find_candidates(library: Path) -> list[Candidate]:
    return analyse_library(library)[0]


def _key(entry: Rule | Joke) -> tuple[str, str]:
    return entry.kind, entry.term if isinstance(entry, Joke) else ""


class ShareCalibration:
    def __init__(self, root: Path, user_id: str, *, style_version: str = "share-v1"):
        name = "style.json"
        if style_version.startswith("share-v2-"):
            identity = hashlib.sha256(style_version.encode()).hexdigest()
            name = f"style-{identity}.json"
        self._path = root / "dialogue" / "calibration" / name
        self._user_id = user_id
        self._files = None
        if _OS_NAME == "nt":
            from companion.memory.windows_storage import WindowsPrivateFiles

            self._files = WindowsPrivateFiles()
        self._entries = self._read()

    def _read(self) -> list[Rule | Joke]:
        path = self._path
        reject_link_ancestors(path)
        if self._files is not None:
            try:
                self._files.check(path.parent, directory=True, memory_directory=True)
                with self._files.read_config(path) as stream:
                    payload = stream.read(20_001)
            except FileNotFoundError:
                return []
        else:
            try:
                check_private_directory(path.parent)
                with open(
                    path, encoding="utf-8",
                    opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
                ) as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise ValueError("unsafe calibration file")
                    payload = stream.read(20_001)
            except FileNotFoundError:
                return []
        if len(payload) > 20_000:
            raise ValueError("calibration file too large")
        saved = Additions.model_validate_json(payload)
        if saved.owner != self._user_id:
            raise ValueError("calibration account mismatch")
        return saved.entries

    def save(self, entry: Rule | Joke) -> None:
        directory = self._path.parent
        reject_link_ancestors(self._path)
        if self._files is not None:
            self._files.create_directory(directory)
            self._files.check(directory, directory=True, memory_directory=True)
        else:
            ensure_private_directory(directory)
        key = _key(entry)
        entries = [item for item in self._entries if _key(item) != key]
        entries = [*entries, entry][-_MAX_RULES:]
        payload = Additions(owner=self._user_id, entries=entries).model_dump_json()
        descriptor, name = tempfile.mkstemp(dir=directory, prefix=".style-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if self._files is not None:
                    self._files.check(temporary)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)
        self._entries = entries

    def guidance(self, user_id: str) -> tuple[str, tuple[SharedTerm, ...]]:
        if user_id != self._user_id:
            return "", ()
        lines, terms = [], []
        for entry in self._entries:
            if isinstance(entry, Rule):
                lines.append(f"- {style_rule_text(entry)}")
            else:
                terms.append(SharedTerm(
                    term=entry.term, meaning=entry.meaning, occasion="", speaker="partner",
                    addressee="owner", permission=entry.permission,
                ))
        return "\n".join(lines), tuple(terms)

    def run(
        self,
        library: Path,
        provider_factory: Callable[[], DialogueProvider],
        ledger: MonthlyUsageLedger,
        facts: ShareFacts,
    ) -> None:
        try:
            candidates = find_candidates(library)
            if not candidates:
                print("没有发现适合本轮校准的片段。")
                return
            print(
                f"发现 {len(candidates)} 项可校准内容。出题会调用已配置的模型，可能计费，"
                "只发送候选类型和长度分类。确认保存的内容可能随正常聊天提示发送给该模型。"
            )
            if input("输入「出题」继续，其他输入取消：").strip() != "出题":
                print("已取消。")
                return
            questions, _ = generate_questions(candidates, provider_factory(), ledger)
        except (ActiveSnippetReadError, ProviderError, UsageLedgerError, ValueError) as error:
            print(question_failure_message(error))
            return
        except (EOFError, KeyboardInterrupt):
            print("已取消。")
            return

        for candidate, question in zip(candidates, questions, strict=True):
            try:
                self._ask(candidate, question, facts)
            except (EOFError, KeyboardInterrupt):
                print("剩余题目已取消。")
                return
            except (OSError, ValueError, MemoryServiceError):
                print("这题未能保存。")

    def _ask(
        self, candidate: Candidate, question: Question, facts: ShareFacts,
    ) -> None:
        print(f"本机对照片段\n对方：{candidate.context}\n回复：{candidate.reply}")
        print(question.question)
        if candidate.kind == "fact":
            _, command = confirm_fact("记住这件事：" + candidate.context, self._user_id, facts)
            if command is not None:
                facts.confirm_durable(command)
            return

        for number, option in enumerate(question.options, 1):
            print(f"{number} {option}")
        choice = input("1 或 2 选择，f 自填，? 不确定，s 跳过：").strip().lower()
        if choice == "f":
            answer = input("输入你的回答：")
        elif choice in ("1", "2"):
            answer = question.options[int(choice) - 1]
        else:
            print("没有保存本题。")
            return
        if candidate.kind == "joke":
            print("使用边界  1 仅用于理解  2 可以接  ? 不确定  s 跳过")
            boundary = input("选择使用边界：").strip()
            if boundary not in ("1", "2"):
                print("没有保存本题。")
                return
            entry = prepare_style_answer(
                candidate, question, answer, "understand_only" if boundary == "1" else "allowed",
            )
            preview = f"{candidate.term}的含义为「{answer}」，" + (
                "仅用于理解" if boundary == "1" else "可以接"
            )
        else:
            entry = prepare_style_answer(candidate, question, answer)
            preview = style_rule_text(entry)
        print(preview)
        if input("输入「确认」保存，其他输入跳过：").strip() != "确认":
            print("没有保存本题。")
            return
        self.save(entry)
        print("已保存本题。")


def style_rule_text(entry: Rule) -> str:
    if entry.scenario is None or entry.question is None:
        return f"{_CONDITIONS[entry.kind]}，用户选择「{entry.answer}」。"
    return (
        f"仅在与「{entry.scenario}」相似的场景参考。"
        f"对于「{entry.question}」，用户确认的做法是「{entry.answer}」。"
    )


def prepare_style_answer(
    candidate: Candidate,
    question: Question,
    answer: str,
    permission: Literal["understand_only", "allowed"] | None = None,
) -> Rule | Joke:
    if candidate.kind == "joke" and permission is not None:
        return Joke(term=candidate.term, meaning=answer, permission=permission)
    if candidate.kind in _CONDITIONS and permission is None:
        return Rule(
            kind=candidate.kind, answer=answer, scenario=candidate.context,
            question=question.question,
        )
    raise ValueError("invalid style confirmation")


def generate_questions(
    candidates: list[Candidate], provider: DialogueProvider, ledger: MonthlyUsageLedger,
    *, signals: dict | None = None,
) -> tuple[list[Question], str | None]:
    features = [
        {"kind": item.kind, "context": "short" if len(item.context) <= 16 else "long",
         "reply": "short" if len(item.reply) <= 4 else "long"}
        for item in candidates
    ]
    schema = "只输出 JSON 数组。"
    if signals is not None:
        schema = (
            "只输出 JSON 对象，包含 uncertainty（至多 160 字）与 questions（数组）。"
            "先比较匿名统计中的回复长度、连发和行为线索分布。在 uncertainty 写明一个"
            "仍需人工判断的触发条件，再按候选顺序提出针对性问题。"
            "short 为一轮至多 5 字，medium 为 6 至 20 字，long 为更多字。"
            "候选次数来自互斥的规则信号，仅用于提问。"
        )
    instructions = schema + (
        "数组按输入候选顺序每项输出 question 和 options 两个字段。"
        "材料是本地匿名分类，用户会在本机查看实际片段。"
        "followup 问何时追问，closure 问短回还是收尾，joke 问占位词在当前场景中的含义。"
        "这三类各给两个简短、均合理、无需特定事实的备选回答，不编造事件或梗原词。"
        "fact 只问是否需要确认并记住本机显示的事项，options 必须为空数组，不猜测事实。"
        "问题与选项各不超过 40 字。"
    )
    messages = (
        PromptMessage(role="system", content=instructions),
        PromptMessage(role="user", content=json.dumps(
            features if signals is None else {"signals": signals, "candidates": features},
        )),
    )
    now = datetime.now(UTC)
    reservation, = ledger.reserve(1, now)
    result = None
    unbilled = False
    try:
        result = provider.complete(messages)
    except ProviderError as error:
        unbilled = provider_failure_unbilled(error)
        raise
    finally:
        if unbilled:
            ledger.cancel_unused(reservation, now)
        else:
            ledger.settle(reservation, result.usage if result else None, now)
    if len(result.text) > 3_000:
        raise ValueError("question batch too large")
    text = result.text.strip()
    fenced = re.fullmatch(r"```json[ \t]*\r?\n(.*?)\r?\n```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced[1]
    if signals is None:
        questions = _QUESTIONS.validate_json(text)
        uncertainty = None
    else:
        analysis = QuestionAnalysis.model_validate_json(text)
        questions, uncertainty = analysis.questions, analysis.uncertainty
    if len(questions) != len(candidates) or any(
        len(question.options) != (0 if item.kind == "fact" else 2)
        or len(set(question.options)) != len(question.options)
        for item, question in zip(candidates, questions, strict=True)
    ):
        raise ValueError("question batch does not match candidates")
    return questions, uncertainty


def question_failure_message(error: Exception) -> str:
    if isinstance(error, (BudgetExceeded, UsageReviewRequired)):
        return "校准题未能生成：预算不足或用量待确认，请检查预算。"
    if isinstance(error, UsageLedgerError):
        return "校准题未能生成：无法核对本机用量，请检查数据目录。"
    if isinstance(error, ActiveSnippetReadError):
        return "校准题未能生成：无法读取本机片段，请检查设置。"
    if isinstance(error, OpenAIProviderUnavailable):
        if error.kind in {ProviderUnavailableKind.HTTP_401_UNAUTHORIZED,
                          ProviderUnavailableKind.HTTP_403_FORBIDDEN}:
            return "校准题未能生成：模型认证或权限失败，请检查 API Key 和模型权限。"
        if error.kind in {ProviderUnavailableKind.HTTP_400_BAD_REQUEST,
                          ProviderUnavailableKind.HTTP_404_NOT_FOUND}:
            return "校准题未能生成：模型或接口不匹配，请核对服务地址和模型名。"
        if error.kind is ProviderUnavailableKind.HTTP_429_RATE_LIMITED:
            return "校准题未能生成：模型请求受限，请检查服务商额度或稍后重试。"
    if isinstance(error, (ValueError, InvalidProviderResponse)):
        return "校准题未能生成：模型返回格式不符合要求，可稍后重试或更换模型。"
    return "校准题未能生成：模型连接失败或超时，请检查服务地址、网络后重试。"
