"""Preserve media boundaries before converting share-mode JSONL exports."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrepareStats:
    renumbered: int
    same_second_groups: int


def prepare_share_jsonl(source: Path, out: Path) -> PrepareStats:
    """Renumber every record by second and input line order, keeping all records."""
    counts = Counter()
    media_seconds = set()
    with source.open("rb") as input_file:
        for line in input_file:
            row = json.loads(line)
            timestamp = row["timestamp"]
            counts[timestamp] += 1
            if counts[timestamp] > 1000:
                raise ValueError("too many messages in one second")
            if row["type"] != 1:
                media_seconds.add(timestamp)

        positions = Counter()
        input_file.seek(0)
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            for line in input_file:
                row = json.loads(line)
                timestamp = row["timestamp"]
                row["server_id"] = timestamp * 1000 + positions[timestamp]
                positions[timestamp] += 1
                output.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")
    return PrepareStats(
        renumbered=sum(counts.values()),
        same_second_groups=sum(counts[timestamp] >= 2 for timestamp in media_seconds),
    )
