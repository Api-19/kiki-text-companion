# ruff: noqa: RUF001
from __future__ import annotations

import json
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.privacy.paired_snippets import PairedSnippet
from scripts import build_habit_card as habit
from scripts.style_surface_stats import load_lexicon

ROOT = Path(__file__).resolve().parents[2]
ERROR = "invalid input or output; no source text is reported\n"
EXPECTED = (
    "以下是从主人 5 条真实回复中自动统计的说话习惯，只描述形式，不代表性格。\n"
    "样本较少（不足 30 条），仅供参考。\n"
    "- 一次回复通常分 1 条消息发，多的时候 2 条。\n"
    "- 每次回复总字数中位数约 8 字，长的时候约 12 字。\n"
    "- 很短的回复（不超过 4 个字）约占 40%。\n"
    "- 句末常用语气词：吧、呀、呢。\n"
    "- 每百条回复里：问号约 40 个，感叹号约 20 个，省略号约 40 个，波浪号约 20 个。\n"
    "- 每百条回复里：emoji 约 20 个，颜文字约 20 个，[表情] 类贴纸约 20 个。\n"
)


def snippets(replies):
    return [
        PairedSnippet(
            snippet_id=f"synthetic-{index}",
            conversation_id="synthetic-conversation",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            partner_turns=["合成提问"],
            owner_reply=messages,
            source_fingerprint="synthetic-fingerprint",
        )
        for index, messages in enumerate(replies, 1)
    ]


@pytest.fixture
def five_snippets():
    return snippets([
        ["纸船吧？", "出发吧！"],
        ["云朵吗？"],
        ["星光呀~", "灯塔呢..."],
        ["测试😀^_^[表情]……"],
        ["好"],
    ])


def card(items):
    lexicon, _ = load_lexicon()
    return habit.build_habit_card(items, lexicon)


def write_library(path, items):
    path.write_text(
        "".join(item.model_dump_json() + "\n" for item in items), encoding="utf-8"
    )


def cli(library, out, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_habit_card.py"),
            "--library", str(library),
            "--out", str(out),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_five_snippets_exact_card(five_snippets):
    assert card(five_snippets) == EXPECTED


def test_card_writer_requests_lf(tmp_path, five_snippets, monkeypatch, capsys):
    library, out = tmp_path / "library.jsonl", tmp_path / "habit_card.txt"
    write_library(library, five_snippets)
    original = habit.os.fdopen
    options = []

    def fdopen(*args, **kwargs):
        options.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(habit.os, "fdopen", fdopen)
    assert habit.main(["--library", str(library), "--out", str(out)]) == 0
    capsys.readouterr()
    assert options[0].get("newline") == "\n"
    assert out.read_bytes() == EXPECTED.encode("utf-8")


def test_thirty_replies_omit_low_n_line():
    output = card(snippets([["合成回复"]] * 30))
    assert output.startswith("以下是从主人 30 条真实回复")
    assert "样本较少" not in output


@pytest.mark.parametrize("count", [1, 21])
def test_no_tone_particle_reaches_threshold(count):
    replies = [["合成回复"]] * count
    if count > 1:
        replies[0] = ["合成回复吧"]
    assert "- 很少用句末语气词。\n" in card(snippets(replies))


def test_tone_particle_at_five_percent_is_included():
    output = card(snippets([["合成回复吧"]] + [["合成回复"]] * 19))
    assert "- 句末常用语气词：吧。\n" in output


def test_only_owner_reply_is_counted(five_snippets):
    original = card(five_snippets)
    for item in five_snippets:
        item.partner_turns = ["?" * 1000]
        item.context_turns = ["？" * 1000]
    assert card(five_snippets) == original


def test_fractional_medians_and_integer_rate_rounding():
    output = card(snippets([["甲"], ["乙", "丙"], ["丁戊己"], ["庚辛", "壬癸"]]))
    assert "通常分 1.5 条消息发，多的时候 2 条。" in output
    assert "中位数约 2.5 字，长的时候约 4 字。" in output
    output = card(snippets([["甲？"], ["乙"], ["丙"]]))
    assert "问号约 33 个" in output


def test_written_card_contains_no_source_substrings(tmp_path):
    items = context_library(["合成昵称纸船专属提问吗"], ["霜桥😀？"], count=5)
    for item in items:
        item.context_turns = ["合成前文松塔专名"]
    library, out = tmp_path / "library.jsonl", tmp_path / "habit_card.txt"
    write_library(library, items)
    result = cli(library, out)
    assert result.returncode == 0, result.stderr
    output = out.read_text(encoding="utf-8")
    assert CONTEXT_HEADING in output
    for item in items:
        for text in item.partner_turns + item.owner_reply + item.context_turns:
            assert all(text[index:index + 4] not in output for index in range(len(text) - 3))
    assert "common_ngrams" not in output


@pytest.mark.parametrize("count", [5, 30])
def test_cli_success_permissions_and_counts(tmp_path, five_snippets, count):
    library, out = tmp_path / "library.jsonl", tmp_path / "habit_card.txt"
    write_library(library, five_snippets * (count // 5))
    result = cli(library, out)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == json.dumps({"n_replies": count, "low_n": count < 30}) + "\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    if count == 5:
        assert out.read_text(encoding="utf-8") == EXPECTED


def test_cli_existing_output_is_unchanged(tmp_path, five_snippets):
    library, out = tmp_path / "library.jsonl", tmp_path / "habit_card.txt"
    write_library(library, five_snippets)
    out.write_bytes(b"existing content\n")
    result = cli(library, out)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ERROR
    assert out.read_bytes() == b"existing content\n"


def test_cli_same_input_and_output_is_rejected(tmp_path, five_snippets):
    library = tmp_path / "library.jsonl"
    write_library(library, five_snippets)
    original = library.read_bytes()
    result = cli(library, library)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ERROR
    assert library.read_bytes() == original


@pytest.mark.parametrize("content", ["", "not-json\n", "{}\n"])
def test_cli_invalid_library_does_not_create_output(tmp_path, content):
    library, out = tmp_path / "library.jsonl", tmp_path / "habit_card.txt"
    library.write_text(content, encoding="utf-8")
    result = cli(library, out)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ERROR
    assert not out.exists()


def test_cli_invalid_arguments_have_fixed_error(tmp_path):
    result = cli(tmp_path / "library.jsonl", tmp_path / "habit_card.txt", "--unknown")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ERROR


CONTEXT_HEADING = "- 不同情况下的接话习惯（倾向，不是每次都这样）：\n"
CONTEXT_CASES = [
    (["去哪？"], "提问"),
    (["甲", "乙", "丙"], "连发"),
    (["晚安"], "告别/睡前"),
    (["早安"], "问候"),
    (["难受"], "情绪低落"),
    (["合成的长篇分享包含十二个字以上"], "分享"),
]


def event(partner, reply, index):
    item = snippets([reply])[0]
    item.snippet_id = f"context-{index}"
    item.started_at += timedelta(seconds=index)
    item.partner_turns = partner
    return item


def context_library(partner, reply, *, count=5, baseline=None):
    baseline = ["松塔石阶风铃月影"] if baseline is None else baseline
    return [event(partner, reply, index) for index in range(count)] + [
        event(["嗯"], baseline, index) for index in range(count, count + 20)
    ]


def context_items(output):
    if CONTEXT_HEADING not in output:
        return []
    return output.split(CONTEXT_HEADING)[1].splitlines()


@pytest.mark.parametrize(("partner", "category"), CONTEXT_CASES)
def test_each_context_reports_medians_and_independent_support(partner, category):
    output = card(context_library(partner, ["霜桥"]))
    assert context_items(output) == [
        f"  - 对方{category}时：回复更短（约 2 字，整体约 8 字），"
        "不超过 4 字的短回复更多（100%，整体 20%，5/5 段）；支持 5 段。"
    ]


@pytest.mark.parametrize("partner", [
    ["能来吗"], ["你呢"], ["怎么走"], ["为什么"], ["什么事"], ["几点"], ["要不要"],
    ["去哪?  "], ["睡了"], ["拜拜"], ["先忙"], ["去睡"], ["早上好"],
    ["在吗"], ["在干嘛"], ["累"], ["烦"], ["哭"], ["不开心"], ["好困"],
])
def test_keyword_contexts_are_recognized(partner):
    assert CONTEXT_HEADING in card(context_library(partner, ["霜桥"]))


@pytest.mark.parametrize("count", [1, 4])
def test_insufficient_context_support_omits_section(count):
    assert CONTEXT_HEADING not in card(context_library(["去哪？"], ["霜桥"], count=count))


def test_no_obvious_difference_omits_section():
    assert CONTEXT_HEADING not in card(context_library(["去哪？"], ["霜桥"], baseline=["松塔"]))


def test_context_uses_final_partner_block_and_allows_multiple_labels():
    items = context_library(["晚安", "早安", "去哪？"], ["霜桥"])
    for item in items:
        item.context_turns = ["难受好困"]
    output = card(items)
    assert [line.split("时：")[0] for line in context_items(output)] == [
        "  - 对方提问", "  - 对方连发", "  - 对方告别/睡前", "  - 对方问候",
    ]
    assert "对方情绪低落" not in output
    assert "对方分享" not in output


@pytest.mark.parametrize("partner", [[], ["短消息"], ["甲 " * 11]])
def test_unclassified_short_or_empty_partner_has_no_rule(partner):
    assert CONTEXT_HEADING not in card(context_library(partner, ["霜桥"]))


def test_duplicate_events_do_not_inflate_support_or_context_baseline():
    items = context_library(["去哪？"], ["霜桥"])
    duplicates = [item.model_copy(update={"snippet_id": f"copy-{i}"})
                  for i, item in enumerate(items * 3)]
    assert context_items(card(items + duplicates)) == context_items(card(items))
    assert CONTEXT_HEADING not in card([items[0]] * 8 + items[5:])


def test_same_timestamp_in_different_conversations_is_independent():
    items = context_library(["去哪？"], ["霜桥"])
    for index, item in enumerate(items[:5]):
        item.started_at = items[0].started_at
        item.conversation_id = f"conversation-{index}"
    assert "支持 5 段" in card(items)


@pytest.mark.parametrize(("reply", "baseline", "expected"), [
    (["霜桥", "松塔", "风铃"], ["石阶月影霜桥风铃松塔白云"],
     "分条更多（中位数 3 条，整体 1 条）"),
    (["霜桥？"], ["松塔。"], "以问句结尾更多（100%，整体 20%，5/5 段）"),
    (["霜桥？？？"], ["松塔石阶风"], "以问句结尾更多（100%，整体 20%，5/5 段）"),
    (["霜桥。"], ["松塔？"], "以问句结尾更少（0%，整体 80%，0/5 段）"),
    (["霜桥？", "松塔。"], ["石阶月影风铃"], None),
    (["霜桥😀"], ["松塔。"], "含表情更多（100%，整体 20%，5/5 段）"),
    (["霜桥^_^"], ["松塔白云。"], "含表情更多（100%，整体 20%，5/5 段）"),
    (["霜桥[表情]😀^_^"], ["松塔白云石阶月影风铃"],
     "含表情更多（100%，整体 20%，5/5 段）"),
    (["霜桥松塔"], ["石阶月影风铃"],
     "不超过 4 字的短回复更多（100%，整体 20%，5/5 段）"),
])
def test_reply_metrics_are_event_rates_and_use_last_message(reply, baseline, expected):
    output = card(context_library(["去哪？"], reply, baseline=baseline))
    if expected is None:
        assert "以问句结尾" not in output
    else:
        assert expected in output


@pytest.mark.parametrize(("question_count", "visible"), [(9, True), (10, False)])
def test_fifteen_percentage_point_threshold_is_inclusive(question_count, visible):
    items = [event(["去哪？"], ["霜桥？" if i < 3 else "霜桥。"], i) for i in range(5)]
    items += [event(["嗯"], ["松塔？" if i < question_count - 3 else "松塔。"], i + 5)
              for i in range(15)]
    output = card(items)
    assert (CONTEXT_HEADING in output) == visible
    if visible:
        assert "以问句结尾更多（60%，整体 45%，3/5 段）" in output


@pytest.mark.parametrize(("length", "visible"), [(7, True), (8, False), (13, True), (12, False)])
def test_thirty_percent_median_threshold_is_inclusive(length, visible):
    items = context_library(["去哪？"], ["甲" * length], baseline=["乙" * 10])
    assert (CONTEXT_HEADING in card(items)) == visible


@pytest.mark.parametrize(("count", "visible"), [(7, True), (8, False), (13, True), (12, False)])
def test_message_count_median_threshold_without_length_difference(count, visible):
    reply = ["甲"] * (count - 1) + ["甲" * (130 - count + 1)]
    output = card(context_library(["去哪？"], reply, baseline=["乙" * 13] * 10))
    assert (CONTEXT_HEADING in output) == visible
    if visible:
        direction = "更多" if count > 10 else "更少"
        assert f"分条{direction}（中位数 {count} 条，整体 10 条）" in output
        assert "回复更长" not in output
        assert "回复更短" not in output


def test_six_context_limit_without_length_pressure():
    items = []
    for partner, _ in CONTEXT_CASES:
        start = len(items)
        items.extend(event(partner, ["霜桥？"], start + i) for i in range(5))
    items.extend(event(["嗯"], ["松塔。"], 100 + i) for i in range(100))
    output = card(items)
    assert len(context_items(output)) == 6
    assert len(output) <= 600


@pytest.mark.parametrize("scenario", ["companion", "generic"])
def test_card_length_drops_lowest_support_first(scenario):
    items = []
    for count, (partner, _) in enumerate(CONTEXT_CASES, 5):
        start = len(items)
        items.extend(event(partner, ["霜桥😀？", "松塔"], start + i) for i in range(count))
    items.extend(event(["嗯"], ["石阶月影风铃白云"] * 3, 100 + i) for i in range(100))
    options = {"scenario": "generic"} if scenario == "generic" else {}
    output = habit.build_habit_card(items, load_lexicon()[0], **options)
    kept = context_items(output)
    assert len(output) <= 600
    assert 0 < len(kept) < 6
    names = [line.split("时：")[0] for line in kept]
    assert names == [f"  - 对方{category}" for _, category in reversed(CONTEXT_CASES)][: len(kept)]
    # Every excluded rule would exceed the limit when appended to the retained card.
    next_partner, _ = list(reversed(CONTEXT_CASES))[len(kept)]
    next_line = context_items(
        habit.build_habit_card(
            context_library(
                next_partner,
                ["霜桥😀？", "松塔"],
                baseline=["石阶月影风铃白云"] * 3,
            ),
            load_lexicon()[0],
            **options,
        )
    )[0]
    assert len(output) + len(next_line) + 1 > 600


def test_card_length_breaks_equal_support_ties_by_category_order():
    items = []
    for partner, _ in CONTEXT_CASES:
        start = len(items)
        items.extend(event(partner, ["霜桥😀？", "松塔"], start + i) for i in range(5))
    items.extend(event(["嗯"], ["石阶月影风铃白云"] * 3, 100 + i) for i in range(100))
    output = card(items)
    kept = context_items(output)
    assert len(output) <= 600
    assert 0 < len(kept) < 6
    assert [line.split("时：")[0] for line in kept] == [
        f"  - 对方{category}" for _, category in CONTEXT_CASES
    ][:len(kept)]


def test_card_budget_omits_heading_if_no_context_item_fits(monkeypatch):
    # A smaller budget exercises removal of the final item without huge source strings.
    from scripts import habit_card_contexts

    items = context_library(["去哪？"], ["霜桥"])
    original = card(items).split(CONTEXT_HEADING)[0]
    monkeypatch.setattr(habit_card_contexts, "MAX_CARD_CHARACTERS", len(original))
    assert card(items) == original


def test_generic_card_changes_intro_only(five_snippets):
    lexicon, _ = load_lexicon()
    generic = habit.build_habit_card(five_snippets, lexicon, scenario="generic")
    first, rest = generic.split("\n", 1)
    assert first == "以下是从训练集 5 条回复中自动统计的表达形式，只描述形式，不代表性格。"
    assert rest == EXPECTED.split("\n", 1)[1]
