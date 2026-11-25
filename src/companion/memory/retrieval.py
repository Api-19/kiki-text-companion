import re
import unicodedata

from companion.contracts import MemoryEntry

_TIME = re.compile(r"本周末|这周末|周末|下周末|下周[一二三四五六日天]|(?:周|星期)[一二三四五六日天]"
                   r"|今天|今晚|明天|明早|明晚|后天")
_ACTION = re.compile(
    r"(?P<modal>要|得|需要|记得|应该|打算|准备)?"
    r"(?P<action>携带|带上|带着|带|购买|买|取回|拿回|取|拿)(?!来|去|给|走|动|领)"
)
_QUESTION = re.compile(r"什么东西|哪些东西|什么|哪些|啥|哪样")
_END = re.compile(r"(?:吗|呢|来着|呀|啊)+$")
_PREFIX = re.compile(r"^(?:请问|出门时|出发前|出门|出发|我们|我|你|帮我|记得|需要)+")
_NEGATIVE = re.compile(r"(?:别|不要|不用|不能|不必|无需|不需要|不带|不买|不拿|不取|没带)")
_ACTIONS = {
    "携带": "带", "带上": "带", "带着": "带", "购买": "买",
    "取回": "取", "拿回": "取", "拿": "取",
}
_GENERIC_TOPICS = {"东西", "物品", "事情", "这件事", "什么东西", "啥东西", "什么", "啥"}


def _frame(text: str, *, question: bool) -> tuple[str, str, str] | None:
    if len(text) > (96 if question else 500):
        return None
    text = unicodedata.normalize("NFKC", text.strip())
    if question and not (
        text.endswith("?") or _QUESTION.search(text) or _END.search(text)
    ):
        return None
    # Only interpret one short clause. Compound or negated statements stay exact-only.
    text = re.sub(r"\s+", "", text).rstrip("?。!")
    if re.search(r"[,;。!?、]", text) or _NEGATIVE.search(text):
        return None
    times = list(_TIME.finditer(text))
    if len(times) > 1:
        return None
    time = ""
    if times:
        match = times[0]
        time = match[0]
        if time in {"本周末", "这周末"}:
            time = "周末"
        time = time.replace("星期", "周")
        if time.endswith("周天"):
            time = time[:-1] + "日"
        text = text[:match.start()] + text[match.end():]
    text = _END.sub("", _PREFIX.sub("", text))
    verb = _ACTION.search(text)
    if verb is None:
        return None
    before, after = text[:verb.start()], text[verb.end():]
    # An unmarked verb embedded in a noun or sentence fragment is ambiguous.
    if before and not verb["modal"]:
        return None
    if before and after:
        return None
    topic = after or before
    if question and (_QUESTION.fullmatch(topic) or topic in _GENERIC_TOPICS):
        topic = ""
    elif _QUESTION.search(topic) or (topic and len(topic) < 2 and question):
        return None
    action = _ACTIONS.get(verb["action"], verb["action"])
    return time, action, topic


def select_matching_memories(
    entries: tuple[MemoryEntry, ...], normalized_query: str, limit: int,
) -> tuple[MemoryEntry, ...]:
    query_frame = _frame(normalized_query, question=True)
    exact = []
    approximate: dict[int, list[MemoryEntry]] = {1: [], 2: [], 3: []}
    for entry in entries:
        if normalized_query in entry.field.casefold() or normalized_query in entry.value.casefold():
            exact.append(entry)
            if len(exact) == limit:
                return tuple(exact)
            continue
        if query_frame is None or entry.field != "explicit_note":
            continue
        fact_frame = _frame(entry.value.casefold(), question=False)
        if fact_frame is None:
            continue
        time, action, topic = query_frame
        fact_time, fact_action, fact_topic = fact_frame
        if action != fact_action or not fact_topic or (time and time != fact_time):
            continue
        if topic:
            if topic not in fact_topic:
                continue
            score = 3 if topic == fact_topic else 2
        elif time:
            score = 1
        else:
            continue
        bucket = approximate[score]
        if len(bucket) < limit:
            bucket.append(entry)

    ranked = approximate[3] + approximate[2] + approximate[1]
    return tuple(exact + ranked[:limit - len(exact)])


def validate_memory_query(query: str, limit: int) -> str:
    if not isinstance(query, str):
        raise ValueError("invalid memory query")
    normalized = query.strip().casefold()
    valid_limit = isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 5
    if not normalized or not valid_limit:
        raise ValueError("invalid memory query")
    return normalized
