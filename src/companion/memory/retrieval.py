from companion.contracts import MemoryEntry


def select_matching_memories(
    entries: tuple[MemoryEntry, ...], normalized_query: str, limit: int
) -> tuple[MemoryEntry, ...]:
    return tuple(
        entry
        for entry in entries
        if normalized_query in entry.field.casefold()
        or normalized_query in entry.value.casefold()
    )[:limit]


def validate_memory_query(query: str, limit: int) -> str:
    if not isinstance(query, str):
        raise ValueError("invalid memory query")
    normalized = query.strip().casefold()
    valid_limit = isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 5
    if not normalized or not valid_limit:
        raise ValueError("invalid memory query")
    return normalized
