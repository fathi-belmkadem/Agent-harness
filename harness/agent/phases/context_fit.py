"""Make the conversation fit the model's window before the turn starts.

Compaction is token-budgeted: a message count says nothing about context
pressure, since one message carrying a pasted file outweighs forty short ones.
See `memory/compaction.py` for the policy and I3 for what it may never touch.
"""
from __future__ import annotations

from typing import Callable

from ...memory.compaction import compact
from ..context import Context


def fit_context(
    context: Context,
    *,
    budget_tokens: int | None,
    tail_token_budget: int,
    protect_head_messages: int,
    emit: Callable[..., None],
) -> None:
    """Compact `context` in place so the request fits. No budget, no change."""
    if budget_tokens is None:
        return
    result = compact(
        context.messages,
        budget_tokens=budget_tokens,
        tail_token_budget=tail_token_budget,
        protect_head_messages=protect_head_messages,
        system_prompt=context.system_prompt,
    )
    if result.changed:
        emit(
            type="context_compacted",
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            messages_before=result.messages_before,
            messages_after=result.messages_after,
            exchanges_compacted=result.exchanges_compacted,
        )
