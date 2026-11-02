# ruff: noqa: RUF001
"""Offline context evidence and closed-template rendering for habit cards."""

from __future__ import annotations

import re
from dataclasses import dataclass

from companion.privacy.paired_snippets import PairedSnippet
from scripts.style_surface_stats import Reply, _emoji_pattern, summarize

MAX_CARD_CHARACTERS = 600
CONTEXT_HEADING = "- 不同情况下的接话习惯（倾向，不是每次都这样）：\n"
# Order also resolves equal-support ties. A snippet's partner_turns is the final
# contiguous partner block; context_turns never participates in classification.
CONTEXTS = ("提问", "连发", "告别/睡前", "问候", "情绪低落", "分享")
QUESTION_WORDS = ("吗", "呢", "怎么", "为什么", "什么", "几点", "要不要")
KEYWORDS = {
    "告别/睡前": ("晚安", "睡了", "拜拜", "先忙", "去睡"),
    "问候": ("早安", "早上好", "在吗", "在干嘛"),
    "情绪低落": ("难受", "累", "烦", "哭", "不开心", "好困"),
}


def _categories(partner_turns: list[str]) -> list[str]:
    text = "\n".join(partner_turns).rstrip()
    labels = []
    if text.endswith(("？", "?")) or any(word in text for word in QUESTION_WORDS):
        labels.append("提问")
    if len(partner_turns) >= 3:
        labels.append("连发")
    labels.extend(label for label, words in KEYWORDS.items() if any(word in text for word in words))
    if not labels and sum(not char.isspace() for char in text) >= 12:
        labels.append("分享")
    return labels


@dataclass
class _ReplyForms:
    total: int
    messages: int | float
    length: int | float
    # Counts and total retain both observed and complementary event counts.
    counts: dict[str, int]


@dataclass
class _ContextEvidence:
    category: str
    group: _ReplyForms
    baseline: _ReplyForms
    source_version: str = "habit-contexts-v1"


def _forms(
    snippets: list[PairedSnippet], lexicon: dict, expression_patterns: list[re.Pattern],
) -> _ReplyForms:
    stats = summarize([
        Reply(speaker_id="owner", messages=item.owner_reply, position=index)
        for index, item in enumerate(snippets, 1)
    ], lexicon)
    return _ReplyForms(
        total=len(snippets),
        messages=stats["messages_per_reply"]["median"],
        length=stats["reply_length"]["median"],
        counts={
            "以问句结尾": sum(
                "\n".join(item.owner_reply).rstrip().endswith(("？", "?")) for item in snippets
            ),
            "含表情": sum(
                any(pattern.search(message) for pattern in expression_patterns
                    for message in item.owner_reply)
                for item in snippets
            ),
            "不超过 4 字的短回复": stats["rhythm"]["short_reply"]["count"],
        },
    )


def _number(value: int | float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _render(evidence: _ContextEvidence) -> str:
    group, baseline = evidence.group, evidence.baseline
    phrases = []
    if (group.length != baseline.length
            and abs(group.length - baseline.length) >= baseline.length * 0.3):
        direction = "更长" if group.length > baseline.length else "更短"
        phrases.append(
            f"回复{direction}（约 {_number(group.length)} 字，"
            f"整体约 {_number(baseline.length)} 字）"
        )
    if (group.messages != baseline.messages
            and abs(group.messages - baseline.messages) >= baseline.messages * 0.3):
        direction = "更多" if group.messages > baseline.messages else "更少"
        phrases.append(
            f"分条{direction}（中位数 {_number(group.messages)} 条，"
            f"整体 {_number(baseline.messages)} 条）"
        )
    for behavior, count in group.counts.items():
        overall = baseline.counts[behavior]
        # Compare exact event counts before rounding displayed percentages.
        difference = count * baseline.total - overall * group.total
        if abs(difference) * 100 >= 15 * group.total * baseline.total:
            direction = "更多" if difference > 0 else "更少"
            phrases.append(
                f"{behavior}{direction}（{count * 100 / group.total:.0f}%，"
                f"整体 {overall * 100 / baseline.total:.0f}%，{count}/{group.total} 段）"
            )
    if not phrases:
        return ""
    return f"  - 对方{evidence.category}时：{'，'.join(phrases)}；支持 {group.total} 段。\n"


def append_context_habits(card: str, snippets: list[PairedSnippet], lexicon: dict) -> str:
    """Append supported conditions, dropping the lowest-support rules to fit."""
    events = {}
    for item in snippets:
        events.setdefault((item.conversation_id, item.started_at), item)
    unique = list(events.values())
    patterns = [
        _emoji_pattern(lexicon), re.compile(lexicon["kaomoji_regex"]),
        re.compile(lexicon["bracket_sticker_regex"]),
    ]
    baseline = _forms(unique, lexicon, patterns)
    groups = {category: [] for category in CONTEXTS}
    for item in unique:
        for category in _categories(item.partner_turns):
            groups[category].append(item)
    candidates = []
    for category, group in groups.items():
        if len(group) >= 5:
            evidence = _ContextEvidence(category, _forms(group, lexicon, patterns), baseline)
            line = _render(evidence)
            if line:
                candidates.append((len(group), line))
    candidates.sort(key=lambda candidate: -candidate[0])
    lines = [line for _, line in candidates[:6]]
    while lines:
        result = card + CONTEXT_HEADING + "".join(lines)
        if len(result) <= MAX_CARD_CHARACTERS:
            return result
        lines.pop()
    return card
