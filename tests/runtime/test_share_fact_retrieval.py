"""Deterministic local retrieval for confirmed facts."""

# ruff: noqa: RUF001

from datetime import UTC, datetime

import pytest

from companion.contracts import MemoryEntry
from companion.memory.retrieval import select_matching_memories


def test_exact_bounded_natural_ranking_and_no_false_match():
    entries = tuple(
        MemoryEntry(
            memory_id=str(index), user_id="synthetic", field="explicit_note",
            value=value, source_timestamp=datetime.now(UTC), deleted_at=None,
        )
        for index, value in enumerate((
            "周一要带书", "周末喜欢拼图", "周末要带拼图", "周末记得带雨伞",
            "周末出门和同学打球", "周末要带便携拼图", "周末不要带剪刀",
        ))
    )
    assert select_matching_memories(entries, "拼图", 5) == (entries[1], entries[2], entries[5])
    assert select_matching_memories(entries, "周末要带什么？", 5) == (
        entries[2], entries[3], entries[5],
    )
    assert select_matching_memories(entries, "周末出门需要带啥", 1) == (entries[2],)
    assert select_matching_memories(entries, "周末拼图要带吗？", 5) == (entries[2], entries[5])
    assert select_matching_memories(
        (entries[5], entries[2]), "周末拼图要带吗？", 1,
    ) == (entries[2],)
    exact = entries[0].model_copy(update={"value": "周末要带什么？见清单"})
    assert select_matching_memories((*entries, exact), "周末要带什么？", 1) == (exact,)
    assert select_matching_memories(entries, "要带什么？", 5) == ()
    assert select_matching_memories(entries, "周末吃什么？", 5) == ()
    assert select_matching_memories(entries, "周末要带课本吗？", 5) == ()
    assert select_matching_memories(entries, "周五拼图要带吗？", 5) == ()


def test_review_scoped_note_uses_existing_authorization_and_management(tmp_path):
    from datetime import UTC, datetime

    from companion.contracts import ConsentRecord
    from companion.memory import CreateMemoryRequest, MemoryService
    from companion.memory.errors import MemoryAuthorizationError

    service = MemoryService(tmp_path / "memory.sqlite", lambda: datetime.now(UTC))
    user = "synthetic-review-user"
    request = CreateMemoryRequest(
        user_id=user, field="explicit_note", value="周末要带合成蓝伞",
        source_timestamp=datetime.now(UTC), explicitly_requested=True,
    )
    with pytest.raises(MemoryAuthorizationError):
        service.create_scoped_note(request, "share-first")
    service.grant_consent(ConsentRecord(
        user_id=user, granted=True, granted_at=datetime.now(UTC),
        allowed_fields=["explicit_note"],
    ))
    scoped = service.create_scoped_note(request, "share-first")
    assert not service.retrieve_memories(user, "周末要带什么？", scope="share-second")
    assert not service.retrieve_memories(user, "周末要带什么？")
    assert service.retrieve_memories(user, "周末要带什么？", scope="share-first") == (scoped,)
    assert service.list_memories(user) == (scoped,)
    service.delete_memory(user, scoped.memory_id)
    assert not service.retrieve_memories(user, "周末要带什么？", scope="share-first")
    service.create_scoped_note(request, "share-first")
    service.revoke_consent(user)
    assert not service.list_memories(user)


@pytest.mark.parametrize("mode", ["fence", "bad-fence", "401", "timeout"])
def test_review_calibration_format_and_reservation(mode, tmp_path):
    import json
    from urllib.error import HTTPError

    from pydantic import SecretStr

    from companion.dialogue.models import BudgetConfig, ProviderConfig
    from companion.dialogue.provider import OpenAICompatibleProvider, ProviderError
    from companion.dialogue.usage import MonthlyUsageLedger
    from companion.runtime.share_calibration import (
        Candidate,
        generate_questions,
        question_failure_message,
    )

    def transport(request, timeout):
        if mode == "401":
            raise HTTPError(request.full_url, 401, "synthetic", {}, None)
        if mode == "timeout":
            raise TimeoutError
        text = '```json\n[{"question":"会追问吗","options":["会","不会"]}]\n```'
        if mode == "bad-fence":
            text += "\nextra text"
        return json.dumps({
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()

    provider = OpenAICompatibleProvider(
        config=ProviderConfig(base_url="https://example.invalid/v1", model="synthetic",
                              timeout_seconds=5),
        api_key=SecretStr("synthetic"), max_output_tokens=100, transport=transport,
    )
    budget = BudgetConfig(
        monthly_cap_cny="100.00", worst_case_request_cny="0.21", max_output_tokens=100,
        input_cny_per_million="4", output_cny_per_million="16",
    )
    ledger = MonthlyUsageLedger(tmp_path, budget)
    args = ([Candidate("followup", "合成分享", "怎么回事？")], provider, ledger)
    if mode == "fence":
        (question,), uncertainty = generate_questions(*args)
        assert question.options == ["会", "不会"] and uncertainty is None
    else:
        with pytest.raises((ProviderError, ValueError)) as error:
            generate_questions(*args)
        label = {"bad-fence": "模型返回格式", "401": "认证或权限", "timeout": "连接失败或超时"}
        assert label[mode] in question_failure_message(error.value)
    totals = ledger.snapshot(datetime.now(UTC))
    assert totals.reserved_cny == 0
    if mode == "401":
        assert totals.settled_cny == 0
    if mode == "timeout":
        assert totals.settled_cny == budget.worst_case_request_cny
