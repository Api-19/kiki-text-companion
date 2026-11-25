# ruff: noqa: RUF001
"""Local confirmation and process-scoped facts for share text chat."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from companion.contracts import ConsentRecord, DialogueResponse, MemoryEntry
from companion.dialogue.memory import parse_memory_command
from companion.memory.errors import MemoryAuthorizationError, MemoryServiceError
from companion.memory.models import MemoryField
from companion.memory.retrieval import select_matching_memories, validate_memory_query

if TYPE_CHECKING:
    from companion.dialogue.memory import ParsedMemoryCommand
    from companion.memory import CreateMemoryRequest, MemoryService

_PREFIXES = ("帮我记住", "请记住", "记住")
_NOTE_PREFIX = "记住这件事："
_SESSION_LIMIT = 20
NOTE_CONSENT_TEXT = (
    "允许本机按当前账户长期保存「明确备注」字段。校准产生的事实只用于当前模拟目标。"
    "匹配的备注可能随正常聊天提示发送给已配置的模型。可查看、删除和撤回授权。"
)


class ShareFacts:
    def __init__(self, user_id: str, *, target_scope: str = "share-v1") -> None:
        self._user_id = user_id
        self.target_scope = target_scope
        self._memory: MemoryService | None = None
        self._entries: list[MemoryEntry] = []

    def bind_memory(self, memory: MemoryService) -> None:
        self._memory = memory

    def grant_notes(self) -> None:
        if self._memory is None:
            raise MemoryServiceError("memory service unavailable")
        previous = self._memory.get_consent(self._user_id)
        allowed_fields = set(previous.allowed_fields) if previous and previous.granted else set()
        allowed_fields.add(MemoryField.EXPLICIT_NOTE.value)
        self._memory.grant_consent(ConsentRecord(
            user_id=self._user_id, granted=True, granted_at=datetime.now(UTC),
            allowed_fields=sorted(allowed_fields),
        ))

    def save_durable(self, command: str) -> None:
        if self._memory is None:
            raise MemoryServiceError("memory service unavailable")
        parsed = parse_memory_command(command, user_id=self._user_id,
                                      source_timestamp=datetime.now(UTC))
        if parsed is None:
            raise ValueError("invalid fact confirmation")
        self._memory.create_scoped_note(parsed.request, self.target_scope)

    def confirm_durable(self, command: str) -> None:
        try:
            self.save_durable(command)
        except MemoryAuthorizationError:
            print(NOTE_CONSENT_TEXT)
            choice = input("输入「同意」授权并保存原事实，输入 2 仅本次，其他输入跳过：").strip()
            if choice == "同意":
                self.grant_notes()
                self.save_durable(command)
            elif choice == "2":
                parsed = parse_memory_command(command, user_id=self._user_id,
                                              source_timestamp=datetime.now(UTC))
                self.save(parsed.request)
                print("已记在本次聊天，退出即清除。")
                return
            else:
                print("没有保存长期记忆。")
                return
        print("已保存长期记忆。")

    def save(self, request: CreateMemoryRequest) -> None:
        if request.user_id != self._user_id:
            raise ValueError("sharing account mismatch")
        self._entries.insert(0, MemoryEntry(
            memory_id=uuid4().hex, user_id=self._user_id, field=request.field.value,
            value=request.value, source_timestamp=request.source_timestamp, deleted_at=None,
        ))
        del self._entries[_SESSION_LIMIT:]

    def matches(self, user_id: str, query: str, limit: int) -> tuple[MemoryEntry, ...]:
        if user_id != self._user_id:
            return ()
        return select_matching_memories(
            tuple(self._entries), validate_memory_query(query, limit), limit,
        )

    def clear(self) -> None:
        self._entries.clear()


class ShareMemory:
    def __init__(self, persistent: MemoryService, temporary: ShareFacts) -> None:
        self._persistent = persistent
        self._temporary = temporary

    def create_memory(self, request: CreateMemoryRequest) -> MemoryEntry:
        return self._persistent.create_memory(request)

    def retrieve_memories(
        self, user_id: str, query: str, limit: int = 5,
    ) -> tuple[MemoryEntry, ...]:
        saved = self._persistent.retrieve_memories(
            user_id, query, limit=limit, scope=self._temporary.target_scope,
        )
        current = self._temporary.matches(user_id, query, limit)
        # Keep exact matches above approximate ones even across storage scopes.
        candidates = {
            (entry.field, entry.value): entry for entry in (*current, *saved)
        }
        return select_matching_memories(
            tuple(candidates.values()), validate_memory_query(query, limit), limit,
        )


def prepare_note(value: str, user_id: str) -> tuple[str, ParsedMemoryCommand]:
    text = _NOTE_PREFIX + value
    command = parse_memory_command(text, user_id=user_id, source_timestamp=datetime.now(UTC))
    if command is None or not command.request.value.isprintable():
        raise ValueError("invalid fact confirmation")
    return text, command


def apply_fact_choice(
    text: str,
    command: ParsedMemoryCommand,
    temporary: ShareFacts,
    scope: Literal["durable", "temporary"],
) -> str | None:
    if scope == "durable":
        return text
    temporary.save(command.request)
    return None


def is_fact_command(text: str) -> bool:
    return text.lstrip().startswith(_PREFIXES)


def parse_fact_candidate(text: str, user_id: str) -> tuple[str, ParsedMemoryCommand]:
    text = text.strip()
    prefix = next((item for item in _PREFIXES if text.startswith(item)), None)
    if prefix is None:
        raise ValueError("invalid fact confirmation")
    text = "记住" + text[len(prefix):]
    if text.startswith("记住这件事:"):
        text = _NOTE_PREFIX + text[len("记住这件事:"):]
    command = parse_memory_command(text, user_id=user_id, source_timestamp=datetime.now(UTC))
    if command is None and not text.startswith((
        "记住以后叫我", "记住我喜欢", "记住我不喜欢", _NOTE_PREFIX,
    )):
        return prepare_note(text[len("记住"):].lstrip("：: "), user_id)
    if command is None or not command.request.value.isprintable():
        raise ValueError("invalid fact confirmation")
    return text, command


class FactChoice(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(min_length=1, max_length=32)
    choice: Literal["durable", "temporary", "skip", "edit"]
    value: str | None = Field(default=None, max_length=500)


class WebFactConfirmation:
    """One local candidate, bound to the current account and scoped memory."""

    def __init__(
        self, facts: ShareFacts, user_id: str, reply: Callable[[str], DialogueResponse],
    ) -> None:
        self._facts = facts
        self._user_id = user_id
        self._reply = reply
        self._pending: tuple[str, str, ParsedMemoryCommand] | None = None

    def state(self) -> dict[str, str] | None:
        if self._pending is None:
            return None
        identifier, _, command = self._pending
        return {"id": identifier, "value": command.request.value}

    def clear(self) -> None:
        self._pending = None

    def start(self, text: str) -> tuple[int, str]:
        if self._pending is not None:
            return 409, "请先确认或跳过当前事实。"
        try:
            canonical, command = parse_fact_candidate(text, self._user_id)
        except ValueError:
            return 400, "请在「记住」后输入具体事实，最多 500 字。"
        self._pending = uuid4().hex, canonical, command
        return 200, "请确认事实。命中的事实可能随后续请求发送给已配置的模型服务。"

    def choose(self, data: FactChoice) -> tuple[int, str]:
        if self._pending is None or data.id != self._pending[0]:
            return 409, "事实确认已失效。"
        _, canonical, command = self._pending
        if data.choice == "edit":
            try:
                canonical, command = prepare_note(data.value or "", self._user_id)
            except ValueError:
                return 400, "事实格式无效。请修改或跳过。"
            self._pending = uuid4().hex, canonical, command
            return 200, "已更新候选，请再选择保存范围。"
        if data.choice == "skip":
            self.clear()
            return 200, "已跳过。"
        confirmed = apply_fact_choice(canonical, command, self._facts, data.choice)
        if confirmed is None:
            self.clear()
            return 200, "已记在本次聊天，退出即清除。"
        # The canonical explicit memory command takes the bridge's existing local-only path.
        response = self._reply(confirmed)
        if response.blocked_reason is not None or response.billable:
            return 409, "没有保存长期记忆。请检查记忆授权，或选仅本次、跳过。"
        self.clear()
        return 200, "已保存长期记忆。"


def confirm_fact(text: str, user_id: str, temporary: ShareFacts) -> tuple[bool, str | None]:
    """Return whether the command was handled, and a confirmed durable command if any."""
    if not is_fact_command(text):
        return False, None
    try:
        text, command = parse_fact_candidate(text, user_id)
    except ValueError:
        print("请在「记住」后输入具体事实，最多 500 字；没有保存。")
        return True, None
    try:
        print(f"请确认事实：{command.request.value}")
        print("记下的事实若命中后续问题，可能随提示发送给已配置的模型服务。")
        print("1 长期记（需要授权）  2 仅本次（退出即清除）  3 跳过  4 自填")
        choice = input("请选择：").strip()
        if choice == "4":
            value = input("输入你要记住的事实（作为备注），留空取消：").strip()
            if not value:
                print("已跳过。")
                return True, None
            try:
                text, command = prepare_note(value, user_id)
            except ValueError:
                print("事实格式无效；没有保存。")
                return True, None
            print(f"请确认事实：{command.request.value}")
            choice = input("1 长期记  2 仅本次  3 跳过：").strip()
        if choice in ("1", "2"):
            confirmed = apply_fact_choice(
                text, command, temporary, "durable" if choice == "1" else "temporary",
            )
            if confirmed is None:
                print("已记在本次聊天，退出即清除。")
            return True, confirmed
    except (EOFError, KeyboardInterrupt):
        pass
    print("已跳过。")
    return True, None
