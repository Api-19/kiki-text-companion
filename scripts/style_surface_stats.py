# ruff: noqa: RUF001
"""Offline surface-form statistics; no semantic or psychological labels.

Positions are one-based event/JSONL-line/data-row numbers (CSV excludes its header).
CSV runs are bounded by TV_ID, Dialogue_ID and Speaker, in input order. Package
messages retain their boundaries; JSONL newlines delimit messages, including empty
ones. Characters are non-whitespace Unicode code points, not graphemes.

Lexical matching is longest-first, non-overlapping, separately within each category
and message. Tone particles count only the final matching form after trailing
Unicode punctuation/ENDING_MARKS and whitespace. Ellipsis runs count once;
other punctuation counts individual marks. Repetition counts maximal runs per
message, interrupted by whitespace; character and bigram counts may overlap.
N-grams reuse features._phrase_text and its ordering, with >=3 reply support and a
top-10 limit. In by-turn mode these remain the speaker's corpus-level n-grams;
all other metrics, including low_n, describe the individual output unit.

The fixed lexicon also versions emoji ranges and kaomoji patterns. Emoji sequences
join variation selectors, modifiers, ZWJ components, flag pairs, keycaps and tags;
unqualified emoji are included, and explicit VS15 is excluded. This is formal
matching, not interpretation of homographs, sticker content, topics or timing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from companion.style.features import (
    ENDING_MARKS,
    _compact,
    _has_repeated_punctuation,
    _percentile,
    _phrase_text,
)

LEXICON_PATH = Path(__file__).with_name("style_surface_lexicon.json")
WORD_GROUPS = ("interjections", "pronouns", "hedges", "intensifiers")


@dataclass
class Reply:
    speaker_id: str
    messages: list[str]
    position: int


def load_lexicon() -> tuple[dict, str]:
    raw = LEXICON_PATH.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _input_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def _speaker(value: object) -> str:
    value = _input_string(value)
    if not value.strip():
        raise ValueError("empty speaker")
    return value


def read_replies(path: Path, source: str) -> list[Reply]:
    replies = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if source == "package":
            package = json.load(stream)
            if not isinstance(package, dict) or not isinstance(package.get("events"), list):
                raise ValueError("expected package events")
            for position, event in enumerate(package["events"], 1):
                if not isinstance(event, dict) or not isinstance(event.get("reply"), list):
                    raise ValueError("expected event reply")
                messages = event["reply"]
                if not messages or any(not isinstance(message, dict) for message in messages):
                    raise ValueError("expected nonempty reply messages")
                speakers = [_speaker(message.get("speaker_id")) for message in messages]
                if len(set(speakers)) != 1:
                    raise ValueError("reply must have one speaker")
                replies.append(
                    Reply(speakers[0], [_input_string(m.get("text")) for m in messages], position)
                )
        elif source == "cped-csv":
            reader = csv.DictReader(stream)
            required = {"TV_ID", "Dialogue_ID", "Utterance_ID", "Speaker", "Utterance"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("missing CPED columns")
            previous = None
            for position, row in enumerate(reader, 1):
                speaker = _speaker(row["Speaker"])
                key = (_input_string(row["TV_ID"]), _input_string(row["Dialogue_ID"]), speaker)
                text = _input_string(row["Utterance"])
                if key == previous:
                    replies[-1].messages.append(text)
                else:
                    replies.append(Reply(speaker, [text], position))
                previous = key
        else:  # argparse limits this branch to JSONL.
            for position, line in enumerate(stream, 1):
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected JSONL object")
                replies.append(
                    Reply(
                        _speaker(row.get("speaker_id")),
                        _input_string(row.get("text")).split("\n"),
                        position,
                    )
                )
    return replies


def _word_pattern(words: list[str]) -> re.Pattern:
    return re.compile("|".join(re.escape(word) for word in sorted(words, key=lambda w: -len(w))))


def _terminal_particles(message: str, words: list[str]) -> Counter:
    end = len(message)
    while end and (
        message[end - 1].isspace()
        or message[end - 1] in ENDING_MARKS
        or unicodedata.category(message[end - 1]).startswith("P")
    ):
        end -= 1
    for word in sorted(words, key=lambda word: -len(word)):
        if message[:end].endswith(word):
            return Counter({word: 1})
    return Counter()


def _emoji_pattern(lexicon: dict) -> re.Pattern:
    presentation = lexicon["emoji_presentation_pattern"]
    text = lexicon["emoji_text_pattern"]
    modifier = "[\U0001f3fb-\U0001f3ff]?"
    base = f"(?:{presentation}|{text})\ufe0f?(?!\ufe0e){modifier}"
    return re.compile(
        "[0-9#*]\ufe0f?\u20e3"
        "|[\U0001f1e6-\U0001f1ff]{2}"
        f"|{base}(?:\u200d{base})*(?:[\U000e0020-\U000e007e]+\U000e007f)?"
    )


def summarize(replies: list[Reply], lexicon: dict) -> dict:
    texts = ["\n".join(reply.messages) for reply in replies]
    lengths = [len(_compact(text)) for text in texts]
    n_replies, n_chars = len(replies), sum(lengths)

    def metric(count: int) -> dict:
        return {
            "count": count,
            "denominator_replies": n_replies,
            "denominator_chars": n_chars,
            "per_100_replies": count * 100 / n_replies,
            "per_1000_chars": count * 1000 / n_chars if n_chars else 0.0,
        }

    def distribution(values: list[int]) -> dict:
        return {"median": statistics.median(values), "p90": _percentile(values, 0.9)}

    messages = [message for reply in replies for message in reply.messages]
    tone_counts = Counter()
    for message in messages:
        tone_counts.update(_terminal_particles(message, lexicon["tone_particles"]))
    result = {
        "speaker_id": replies[0].speaker_id,
        "n_replies": n_replies,
        "n_chars": n_chars,
        "low_n": n_replies < 30,
        "reply_length": distribution(lengths),
        "messages_per_reply": distribution([len(reply.messages) for reply in replies]),
        "tone_particles": {word: metric(tone_counts[word]) for word in lexicon["tone_particles"]},
    }
    for group in WORD_GROUPS:
        pattern = _word_pattern(lexicon[group])
        counts = Counter(
            match.group() for message in messages for match in pattern.finditer(message)
        )
        result[group] = {word: metric(counts[word]) for word in lexicon[group]}
    punctuation = {
        "question_mark": r"[?？]",
        "exclamation_mark": r"[!！]",
        "ellipsis": r"\.{3,}|…+|⋯+",
        "wave_mark": r"[~～〜]",
    }
    result["punctuation"] = {
        name: metric(sum(len(re.findall(pattern, message)) for message in messages))
        for name, pattern in punctuation.items()
    }
    result["punctuation"]["repeated_punctuation"] = metric(
        sum(_has_repeated_punctuation(text) for text in texts)
    )
    expressions = {
        "unicode_emoji": _emoji_pattern(lexicon),
        "kaomoji": re.compile(lexicon["kaomoji_regex"]),
        "bracket_sticker": re.compile(lexicon["bracket_sticker_regex"]),
    }
    result["expressions"] = {
        name: metric(sum(len(list(pattern.finditer(message))) for message in messages))
        for name, pattern in expressions.items()
    }
    result["repetition"] = {
        name: metric(sum(len(re.findall(pattern, message)) for message in messages))
        for name, pattern in {
            "character": r"(\S)\1{2,}",
            "bigram": r"(\S{2})\1+",
        }.items()
    }
    phrase_documents = Counter()
    for text in texts:
        compact = _phrase_text(text)
        phrase_documents.update(
            {
                compact[index : index + width]
                for width in range(2, 5)
                for index in range(max(0, len(compact) - width + 1))
            }
        )
    result["common_ngrams"] = [
        {"ngram": phrase, "n_replies": count}
        for phrase, count in sorted(
            phrase_documents.items(), key=lambda item: (-item[1], -len(item[0]), item[0])
        )
        if count >= 3
    ][:10]
    result["rhythm"] = {
        "short_reply": metric(sum(length <= 4 for length in lengths)),
        "repeated_questions": metric(
            sum(
                sum(message.rstrip().endswith(("?", "？")) for message in reply.messages) >= 2
                for reply in replies
            )
        ),
    }
    return result


def build_report(replies: list[Reply], lexicon: dict, sha256: str, *, by_turn: bool) -> dict:
    grouped = defaultdict(list)
    for reply in replies:
        grouped[reply.speaker_id].append(reply)
    speakers = {speaker: summarize(items, lexicon) for speaker, items in grouped.items()}
    report = {"lexicon_version": lexicon["version"], "lexicon_sha256": sha256}
    if by_turn:
        report["turns"] = [
            {
                **summarize([reply], lexicon),
                "position": reply.position,
                "common_ngrams": speakers[reply.speaker_id]["common_ngrams"],
            }
            for reply in replies
        ]
    else:
        report["speakers"] = list(speakers.values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    for name in ("package", "cped-csv", "jsonl"):
        inputs.add_argument(f"--{name}", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--by-turn", action="store_true")
    args = parser.parse_args()
    source, path = next(
        (source, path)
        for source, path in (
            ("package", args.package),
            ("cped-csv", args.cped_csv),
            ("jsonl", args.jsonl),
        )
        if path is not None
    )
    try:
        for read_path in (path, LEXICON_PATH):
            if args.out.resolve() == read_path.resolve() or (
                args.out.exists() and args.out.samefile(read_path)
            ):
                raise ValueError("output aliases a read-only input")
        replies = read_replies(path, source)
        lexicon, sha256 = load_lexicon()
        report = build_report(replies, lexicon, sha256, by_turn=args.by_turn)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, csv.Error):
        parser.exit(2, "invalid input or output; no source text is reported\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
