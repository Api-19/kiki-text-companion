"""Explicit production assembly shared by Dialogue entry points."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from companion.dialogue.config import DialogueConfig, DialogueSecrets
from companion.dialogue.memory import MemoryGateway
from companion.dialogue.observer import TurnObserver
from companion.dialogue.privacy_revision import ContentRevisionSource
from companion.dialogue.profile import PartnerProfileProvider
from companion.dialogue.provider import OpenAICompatibleProvider
from companion.dialogue.shared_terms import SharedTerm, load_shared_terms
from companion.dialogue.usage import MonthlyUsageLedger
from companion.runtime.settings import Settings

if TYPE_CHECKING:
    from companion.dialogue.orchestrator import DialogueOrchestrator, SnippetStyleSource
    from companion.style.snippet_source import FileSnippetStyleSource


def build_snippet_style_runtime(
    config: DialogueConfig, *, clock: Callable[[], datetime]
) -> tuple[FileSnippetStyleSource, str | None]:
    """Build a live partner snippet source and read the habit card once."""
    from companion.style.snippet_source import FileSnippetStyleSource

    sources = config.snippet_style
    if sources is None:
        raise ValueError("snippet style configuration unavailable")
    habit_card = None
    if sources.habit_card_source is not None:
        habit_card = sources.habit_card_source.read_text(encoding="utf-8")
        if not habit_card.strip():
            raise ValueError("habit card is empty")
    source = FileSnippetStyleSource(
        library_source=sources.library_source,
        partner_user_id=sources.partner_user_id,
        style_version=sources.style_version,
        clock=clock,
    )
    return source, habit_card


def load_snippet_shared_terms(config: DialogueConfig) -> tuple[SharedTerm, ...] | None:
    """Load the configured shared-terms dictionary; None when the segment has none."""
    sources = config.snippet_style
    if sources is None:
        raise ValueError("snippet style configuration unavailable")
    if sources.shared_terms_source is None:
        return None
    return load_shared_terms(sources.shared_terms_source)


def build_real_orchestrator(
    settings: Settings,
    config: DialogueConfig,
    secrets_: DialogueSecrets,
    *,
    trusted_user_id: str,
    snippet_style: SnippetStyleSource,
    memory_gateway: MemoryGateway | None = None,
    profile_source: PartnerProfileProvider | None = None,
    snippet_habit_card: str | None = None,
    snippet_shared_terms: Sequence[SharedTerm] | None = None,
    snippet_session_check: bool = False,
    turn_observer: TurnObserver | None = None,
    privacy_revision_sources: Sequence[ContentRevisionSource] = (),
) -> DialogueOrchestrator:
    from companion.dialogue.orchestrator import DialogueOrchestrator

    provider = OpenAICompatibleProvider(
        config=config.provider,
        api_key=secrets_.api_key,
        max_output_tokens=config.budget.max_output_tokens,
    )
    return DialogueOrchestrator(
        config=config,
        snippet_style=snippet_style,
        snippet_habit_card=snippet_habit_card,
        snippet_shared_terms=snippet_shared_terms,
        snippet_session_check=snippet_session_check,
        veiled_terms_path=Path(__file__).resolve().parents[3] / "config/privacy/veiled_terms.json",
        provider=provider,
        ledger=MonthlyUsageLedger(settings.runtime_data_dir / "dialogue", config.budget),
        clock=lambda: datetime.now(UTC),
        test_results_dir=settings.test_results_dir,
        memory_gateway=memory_gateway,
        profile_source=profile_source,
        turn_observer=turn_observer,
        privacy_revision_sources=privacy_revision_sources,
    )
