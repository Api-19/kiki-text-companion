# ruff: noqa: RUF001
"""Blind pair presentation and one aggregate export per completed batch."""

import json
import os
import secrets
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from companion.contracts import DialogueResponse
from companion.runtime.private_paths import ensure_private_directory, reject_link_ancestors

BLIND_NOTICE = (
    "盲评每题最多 3 次模型请求，沿用当前服务和月度费用上限。"
    "选择 A 或 B 后，只沿用你选的回复。差不多、都不好或跳过时沿用 A。"
    "退出时保存匿名汇总。输入「同意」开始："
)

_VOTE_CODES = {
    "A": "a", "a": "a", "B": "b", "b": "b",
    "差不多": "tie", "tie": "tie", "都不好": "both_bad", "both_bad": "both_bad",
    "两条都不好": "both_bad", "跳过": "skip", "skip": "skip",
    "退出": "exit", "exit": "exit", "/exit": "exit",
}


def normalize_vote(value: object) -> str | None:
    """Map one displayed vote label or canonical code to a stable code."""
    if type(value) is not str:
        return None
    return _VOTE_CODES.get(value if len(value) != 1 else value.upper())


@dataclass(frozen=True)
class BlindRound:
    """One presented pair; order[0] is reply A's index, order[1] is reply B's."""

    round_id: str
    order: tuple[int, int]


class BlindBatch:
    def __init__(self, output_root: Path) -> None:
        self.batch_id = secrets.token_hex(12)
        self.path = output_root / "blind-feedback" / f"feedback-{self.batch_id}.json"
        self._counts: Counter[str] = Counter()
        self._saved = False
        self._pending: BlindRound | None = None

    @property
    def pending_round(self) -> BlindRound | None:
        return self._pending

    def present(self, replies: tuple[DialogueResponse, DialogueResponse]) -> BlindRound:
        """Randomize A/B order and remember the pair until it is voted on."""
        if self._pending is not None:
            raise ValueError("a blind round is already pending")
        if type(replies) is not tuple or len(replies) != 2:
            raise ValueError("invalid blind pair")
        layered_at = secrets.randbelow(2)
        order = (1, 0) if layered_at == 0 else (0, 1)
        round_ = BlindRound(secrets.token_hex(8), order)
        self._pending = round_
        return round_

    def vote(self, round_id: str, code: str) -> int | None:
        """Record one vote for the pending round and return the reply to keep."""
        pending = self._pending
        if pending is None or pending.round_id != round_id:
            raise ValueError("no matching blind round")
        normalized = normalize_vote(code)
        if normalized is None:
            raise ValueError("invalid blind vote")
        self._pending = None
        self._counts["comparable"] += 1
        self._counts["skip" if normalized == "exit" else normalized] += 1
        if normalized in {"a", "b"}:
            route = "layered" if pending.order[int(normalized == "b")] == 1 else "original"
            self._counts[route] += 1
        if normalized == "exit":
            return None
        return pending.order[1 if normalized == "b" else 0]

    def choose(self, replies: tuple[DialogueResponse, DialogueResponse]) -> int | None:
        round_ = self.present(replies)
        for label, index in zip(("A", "B"), round_.order, strict=True):
            print(f"回复 {label}")
            for line in replies[index].text.splitlines():
                print("  " + "".join(c for c in line if c.isprintable()))
        while True:
            try:
                vote = input("选择 A / B / 差不多 / 都不好 / 跳过 / 退出：").strip()
            except (EOFError, KeyboardInterrupt):
                vote = "退出"
            code = normalize_vote(vote)
            if code is not None:
                return self.vote(round_.round_id, code)
            print("请选择列出的选项。")

    def record_failure(self) -> None:
        self._counts["failed"] += 1

    def payload(self) -> dict:
        counts = self._counts
        decisive = counts["a"] + counts["b"]
        return {
            "schema_version": 1, "experiment_version": "share-fsw-v1",
            "batch_id": self.batch_id,
            "total_rounds": counts["comparable"] + counts["failed"],
            "comparable_rounds": counts["comparable"],
            "decisive_votes": decisive,
            "layered_wins": counts["layered"],
            "original_wins": decisive - counts["layered"],
            "tie": counts["tie"], "both_bad": counts["both_bad"],
            "skipped": counts["skip"], "failed": counts["failed"],
            "presentation": {"a_votes": counts["a"], "b_votes": counts["b"]},
        }

    def _serialized(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, indent=2) + "\n"

    def save(self) -> Path:
        reject_link_ancestors(self.path)
        if self._saved:
            try:
                saved = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._saved = False
            else:
                if saved != self._serialized():
                    raise ValueError("saved feedback changed")
                return self.path
        missing = []
        parent = self.path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            ensure_private_directory(directory)
        ensure_private_directory(self.path.parent)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent, delete=False, mode="w", encoding="utf-8",
            ) as output:
                temporary = Path(output.name)
                output.write(self._serialized())
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._saved = True
        return self.path
