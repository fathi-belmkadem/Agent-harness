"""End-of-turn work: persist the conversation, then maintain memory.

Both run in the loop's `finally`, so they happen whether the turn succeeded,
failed, or hit a guard. Both swallow their own exceptions: a session that
fails to save and a maintenance pass that errors are operational problems,
never reasons to fail a run the user already has an answer from.
"""
from __future__ import annotations

from typing import Callable

from ...config import ConsolidationConfig
from ...memory import SQLiteMemory
from ...memory.episodic import EpisodicArchive
from ...memory.session_store import RedisSessionStore
from ...memory.summarize import summarize_turns


def save_session(
    session_store: RedisSessionStore | None,
    messages: list[dict],
    *,
    emit: Callable[..., None],
) -> None:
    if session_store is None:
        return
    try:
        session_store.save(messages)
    except Exception as e:  # noqa: BLE001 - a lost session must not crash the run
        emit(type="session_save_error", error=str(e))


def maybe_consolidate(
    memory: SQLiteMemory | None,
    archive: EpisodicArchive | None,
    llm,
    config: ConsolidationConfig | None,
    *,
    emit: Callable[..., None],
) -> None:
    """Once `every_n_turns` archived turns are unsummarized, extract facts
    from them, then archive facts nobody uses. Runs after the turn is
    archived, so the turn that just ended is included."""
    if memory is None or archive is None or llm is None or config is None or not config.enabled:
        return
    try:
        if archive.unsummarized_count() < config.every_n_turns:
            return
        result = summarize_turns(archive, memory, llm, max_turns=config.max_turns_per_pass)
        archived = memory.archive_stale(config.archive_after_days)
        emit(type="consolidated", turns=result.turns, stored=result.stored,
             superseded=result.superseded, rejected=result.rejected, archived=archived)
    except Exception as e:  # noqa: BLE001 - maintenance must not fail a run
        emit(type="consolidation_error", error=str(e))


def turn_messages(messages: list[dict]) -> list[dict]:
    """The slice of the conversation that belongs to the turn just finished.

    A turn starts at the user's message and runs to the end. Anchoring on the
    last user message rather than on a length recorded at the start means
    compaction, which can shrink the earlier history mid-turn, cannot shift
    the boundary.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index:]
    return []


def archive_turn(
    archive: EpisodicArchive | None,
    *,
    session_id: str,
    turn_id: str,
    messages: list[dict],
    emit: Callable[..., None],
) -> None:
    """Append the finished turn to the episodic archive.

    Runs after the answer is already in hand, so a failure here is logged and
    costs only future recall — never the turn that just completed.
    """
    if archive is None:
        return
    try:
        stored = archive.record_turn(session_id, turn_id, turn_messages(messages))
        if stored:
            emit(type="turn_archived", messages=stored)
    except Exception as e:  # noqa: BLE001 - archiving must not fail a run
        emit(type="archive_error", error=str(e))
