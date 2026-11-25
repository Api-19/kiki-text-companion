# ruff: noqa: RUF001
"""Local confirmation and process-scoped facts for share text chat."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from companion.contracts import ConsentRecord, MemoryEntry
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


def confirm_fact(text: str, user_id: str, temporary: ShareFacts) -> tuple[bool, str | None]:
    """Return whether the command was handled, and a confirmed durable command if any."""
    prefix = next((prefix for prefix in _PREFIXES if text.startswith(prefix)), None)
    if prefix is None:
        return False, None
    text = "记住" + text[len(prefix):]
    if text.startswith("记住这件事:"):
        text = _NOTE_PREFIX + text[len("记住这件事:"):]
    now = datetime.now(UTC)
    command = parse_memory_command(text, user_id=user_id, source_timestamp=now)
    try:
        if command is None and not text.startswith(("记住以后叫我", "记住我喜欢", "记住我不喜欢",
                                                     _NOTE_PREFIX)):
            text = _NOTE_PREFIX + text[len("记住"):].lstrip("：: ")
            command = parse_memory_command(text, user_id=user_id, source_timestamp=now)
        if command is None or not command.request.value.isprintable():
            print("请在「记住」后输入具体事实，最多 500 字；没有保存。")
            return True, None
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
