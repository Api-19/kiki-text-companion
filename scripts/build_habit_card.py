# ruff: noqa: RUF001
"""Build an offline form-and-context habit card from a filtered snippet library."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Literal

# Support the documented direct invocation without requiring PYTHONPATH.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.privacy.paired_snippets import PairedSnippet, read_snippets
from scripts.habit_card_contexts import append_context_habits
from scripts.style_surface_stats import Reply, load_lexicon, summarize


def _number(value: int | float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def build_habit_card(snippets: list[PairedSnippet], lexicon: dict, *,
                     scenario: Literal["companion", "generic"] = "companion") -> str:
    """Describe owner reply forms using only aggregate statistics and fixed words."""
    if not snippets:
        raise ValueError("empty library")
    stats = summarize(
        [
            Reply(speaker_id="owner", messages=snippet.owner_reply, position=i)
            for i, snippet in enumerate(snippets, 1)
        ],
        lexicon,
    )
    intro = (
        f"以下是从训练集 {stats['n_replies']} 条回复中自动统计的表达形式，只描述形式，不代表性格。"
        if scenario == "generic" else
        f"以下是从主人 {stats['n_replies']} 条真实回复中自动统计的说话习惯，"
        "只描述形式，不代表性格。"
    )
    lines = [intro]
    if stats["low_n"]:
        lines.append("样本较少（不足 30 条），仅供参考。")
    messages = stats["messages_per_reply"]
    length = stats["reply_length"]
    lines.extend([
        f"- 一次回复通常分 {_number(messages['median'])} 条消息发，"
        f"多的时候 {_number(messages['p90'])} 条。",
        f"- 每次回复总字数中位数约 {_number(length['median'])} 字，"
        f"长的时候约 {_number(length['p90'])} 字。",
        "- 很短的回复（不超过 4 个字）约占 "
        f"{stats['rhythm']['short_reply']['per_100_replies']:.0f}%。",
    ])
    tones = sorted(
        (
            (word, metric["per_100_replies"])
            for word, metric in stats["tone_particles"].items()
            if metric["per_100_replies"] >= 5
        ),
        key=lambda item: -item[1],
    )[:3]
    lines.append(
        f"- 句末常用语气词：{'、'.join(word for word, _ in tones)}。"
        if tones else "- 很少用句末语气词。"
    )
    punctuation = stats["punctuation"]
    expressions = stats["expressions"]
    lines.extend([
        f"- 每百条回复里：问号约 {punctuation['question_mark']['per_100_replies']:.0f} 个，"
        f"感叹号约 {punctuation['exclamation_mark']['per_100_replies']:.0f} 个，"
        f"省略号约 {punctuation['ellipsis']['per_100_replies']:.0f} 个，"
        f"波浪号约 {punctuation['wave_mark']['per_100_replies']:.0f} 个。",
        f"- 每百条回复里：emoji 约 {expressions['unicode_emoji']['per_100_replies']:.0f} 个，"
        f"颜文字约 {expressions['kaomoji']['per_100_replies']:.0f} 个，"
        f"[表情] 类贴纸约 {expressions['bracket_sticker']['per_100_replies']:.0f} 个。",
    ])
    return append_context_habits("\n".join(lines) + "\n", snippets, lexicon)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--library", required=True, type=Path)
        parser.add_argument("--out", required=True, type=Path)
        args = parser.parse_args(argv)
        snippets = read_snippets(args.library)
        lexicon, _ = load_lexicon()
        card = build_habit_card(snippets, lexicon)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(card)
    except (OSError, ValueError):
        print("invalid input or output; no source text is reported", file=sys.stderr)
        return 2
    print(json.dumps({"n_replies": len(snippets), "low_n": len(snippets) < 30}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
