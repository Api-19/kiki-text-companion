"""Share-only JSONL preparation preserves synthetic source records."""

import json
import os
import stat

import pytest

from scripts.share_prepare_jsonl import prepare_share_jsonl


def test_all_ids_renumbered_with_only_id_changed(tmp_path):
    source = tmp_path / "synthetic.jsonl"
    output = tmp_path / "prepared.jsonl"
    rows = [
        {"timestamp": 1, "type": 1, "server_id": 5, "text": "synthetic"},
        *({"timestamp": index + 2, "type": 3, "server_id": message_id,
           "extra": {"keep": [None, False, "合成"]}}
          for index, message_id in enumerate((0, 5, 7, 7, -1, True, "8", None))),
        {"timestamp": 20, "type": 1, "server_id": 100, "text": "later maximum"},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = source.read_bytes()

    stats = prepare_share_jsonl(source, output)

    prepared = [json.loads(line) for line in output.read_bytes().splitlines()]
    assert [row["server_id"] for row in prepared] == [
        row["timestamp"] * 1000 for row in rows
    ]
    assert len({row["server_id"] for row in prepared}) == len(prepared)
    for original, actual in zip(rows, prepared, strict=True):
        assert {**actual, "server_id": original["server_id"]} == original
    assert stats.renumbered == len(rows)
    assert stats.same_second_groups == 0
    assert source.read_bytes() == before


def test_same_second_groups_keep_every_row_in_input_order(tmp_path):
    source = tmp_path / "synthetic.jsonl"
    output = tmp_path / "prepared.jsonl"
    rows = [
        {"timestamp": 3, "type": 1, "server_id": 80},
        {"timestamp": 1, "type": 1, "server_id": 80},
        {"timestamp": 3, "type": 1, "server_id": 0},
        {"timestamp": 2, "type": 3, "server_id": 0},
        {"timestamp": 1, "type": 3, "server_id": 0},
        {"timestamp": 1, "type": 1, "server_id": 81},
        {"timestamp": 4, "type": 49, "server_id": 2},
        {"timestamp": 4, "type": 3, "server_id": 2},
    ]
    # local_id is deliberately reversed; only the JSONL line order determines k.
    for index, row in enumerate(rows):
        row["local_id"] = 100 - index
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = source.read_bytes()

    stats = prepare_share_jsonl(source, output)

    actual = [json.loads(line) for line in output.read_bytes().splitlines()]
    assert stats.renumbered == len(rows)
    assert stats.same_second_groups == 2
    assert len(actual) == len(rows)
    assert [row["server_id"] for row in actual] == [3000, 1000, 3001, 2000, 1001, 1002, 4000, 4001]
    for original, prepared in zip(rows, actual, strict=True):
        assert {**prepared, "server_id": original["server_id"]} == original
    assert source.read_bytes() == before


def test_preparation_is_deterministic_and_idempotent(tmp_path):
    source = tmp_path / "synthetic.jsonl"
    first, second, repeated = (tmp_path / name for name in ("first", "second", "repeated"))
    source.write_bytes(
        b'{ "timestamp": 1, "type": 3, "server_id": 0, "extra": "\\u5408" }\r\n'
        b'{ "timestamp": 1, "type": 1, "server_id": 2, "text": "synthetic" }\r\n'
    )

    stats = prepare_share_jsonl(source, first)

    assert prepare_share_jsonl(source, second) == stats
    assert prepare_share_jsonl(first, repeated) == stats
    assert first.read_bytes() == second.read_bytes() == repeated.read_bytes()
    assert stats.renumbered == 2
    assert stats.same_second_groups == 1


@pytest.mark.parametrize("count", [1000, 1001])
def test_per_second_limit(tmp_path, capsys, count):
    source = tmp_path / "synthetic.jsonl"
    output = tmp_path / "prepared.jsonl"
    rows = [{"timestamp": 1, "type": 1, "server_id": index, "text": "SYNTHETIC_HIDDEN"}
            for index in range(count)]
    rows.insert(500, {"timestamp": 2, "type": 1, "server_id": 0})
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    if count == 1001:
        with pytest.raises(ValueError, match=r"^too many messages in one second$"):
            prepare_share_jsonl(source, output)
        assert not output.exists()
    else:
        stats = prepare_share_jsonl(source, output)
        actual = [json.loads(line) for line in output.read_bytes().splitlines()]
        assert stats.renumbered == 1001
        assert stats.same_second_groups == 0
        assert len({row["server_id"] for row in actual}) == 1001
        assert [row["server_id"] for row in actual if row["timestamp"] == 1] == list(
            range(1000, 2000)
        )
        assert actual[500]["server_id"] == 2000
    assert capsys.readouterr() == ("", "")


def test_output_exclusive_private_and_lf(tmp_path):
    source = tmp_path / "synthetic.jsonl"
    output = tmp_path / "prepared.jsonl"
    line = b'{"timestamp":1,"type":1,"server_id":1}'
    expected = b'{"timestamp": 1, "type": 1, "server_id": 1000}\n'
    source.write_bytes(line + b"\r\n")

    prepare_share_jsonl(source, output)

    assert output.read_bytes() == expected
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        prepare_share_jsonl(source, output)
    assert output.read_bytes() == expected
