# ruff: noqa: RUF001
"""Blind pair presentation and one aggregate export per completed batch."""

import json
import os
import secrets
import tempfile
from collections import Counter
from pathlib import Path

from companion.contracts import DialogueResponse
from companion.runtime.private_paths import ensure_private_directory, reject_link_ancestors

BLIND_NOTICE = (
    "盲评每题最多 3 次模型请求，沿用当前服务和月度费用上限。"
    "选择 A 或 B 后，只沿用你选的回复。差不多、都不好或跳过时沿用 A。"
    "退出时保存匿名汇总。输入「同意」开始："
)


class BlindBatch:
    def __init__(self, output_root: Path) -> None:
        self.batch_id = secrets.token_hex(12)
        self.path = output_root / "blind-feedback" / f"feedback-{self.batch_id}.json"
        self._counts: Counter[str] = Counter()
        self._saved = False

    def choose(self, replies: tuple[DialogueResponse, DialogueResponse]) -> int | None:
        layered_at = secrets.randbelow(2)
        order = (1, 0) if layered_at == 0 else (0, 1)
        for label, index in zip(("A", "B"), order, strict=True):
            print(f"回复 {label}")
            for line in replies[index].text.splitlines():
                print("  " + "".join(c for c in line if c.isprintable()))
        while True:
            try:
                vote = input("选择 A / B / 差不多 / 都不好 / 跳过 / 退出：").strip()
            except (EOFError, KeyboardInterrupt):
                vote = "退出"
            code = {
                "A": "a", "B": "b", "差不多": "tie", "都不好": "both_bad",
                "两条都不好": "both_bad", "跳过": "skip", "退出": "exit", "/exit": "exit",
            }.get(vote if len(vote) != 1 else vote.upper())
            if code is not None:
                break
            print("请选择列出的选项。")
        self._counts["comparable"] += 1
        self._counts[code if code != "exit" else "skip"] += 1
        if code in {"a", "b"}:
            route = "layered" if order[int(code == "b")] == 1 else "original"
            self._counts[route] += 1
        if code == "exit":
            return None
        return order[1 if code == "b" else 0]

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
