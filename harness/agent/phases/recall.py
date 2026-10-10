"""Prefetch long-term memory for the turn.

The block this returns is prefixed onto a *copy* of the latest user message at
request-assembly time, never folded into the system prompt (I1) and never
written back into the stored conversation.

Memory is context, not correctness: a store that raises is logged and the turn
continues without recall, because answering with less background beats not
answering at all.
"""
from __future__ import annotations

from typing import Callable

from ...memory import SQLiteMemory
from ...memory.retrieval import is_trivial_prompt
from ..context import Context, format_recall_block


def latest_user_message(context: Context) -> str:
    return next(
        (message["content"] for message in reversed(context.messages)
         if message.get("role") == "user"),
        "",
    )


def build_recall_block(
    context: Context,
    memory: SQLiteMemory | None,
    *,
    limit: int,
    emit: Callable[..., None],
) -> str:
    """Retrieved memories formatted for injection, or "" if there are none."""
    if memory is None:
        return ""

    query = latest_user_message(context)
    if is_trivial_prompt(query):
        # "ok" / "thanks" carry no query signal: searching on them costs a
        # round-trip and risks dragging in stale context that derails a
        # one-word exchange.
        emit(type="memory_retrieval_skipped", reason="trivial_prompt")
        return ""

    try:
        entries = memory.search(query, limit=limit)
    except Exception as e:  # noqa: BLE001 - memory is optional context
        emit(type="memory_error", operation="search", error=str(e))
        entries = []

    emit(type="memory_retrieved", count=len(entries))
    return format_recall_block([entry.content for entry in entries])
