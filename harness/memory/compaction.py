import json
from dataclasses import dataclass
from typing import Any

MARKER_PREFIX = "[compacted:"

_PER_MESSAGE_OVERHEAD = 4

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = estimate_tokens(message.get("content"))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_tokens(json.dumps(tool_calls))
    return total + _PER_MESSAGE_OVERHEAD


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def is_marker(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith(MARKER_PREFIX)


@dataclass(frozen=True)
class CompactionResult:
    tokens_before: int
    tokens_after: int
    messages_before: int
    messages_after: int
    exchanges_compacted: int

    @property
    def changed(self) -> bool:
        return self.exchanges_compacted > 0


def _marker(message_count: int, tokens: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": (
            f"{MARKER_PREFIX} {message_count} earlier message"
            f"{'s' if message_count != 1 else ''} (~{tokens} tokens) omitted "
            "to stay within the context budget]"
        ),
    }


def _tail_start(messages: list[dict[str, Any]], tail_token_budget: int) -> int:
    budget = tail_token_budget
    index = len(messages)
    while index > 0:
        cost = estimate_message_tokens(messages[index - 1])
        if budget - cost < 0:
            break
        budget -= cost
        index -= 1
    return index


def _oldest_compactible_group(
    messages: list[dict[str, Any]], start: int, stop: int
) -> tuple[int, int] | None:
    index = start
    while index < stop:
        if messages[index].get("role") in {"assistant", "tool"}:
            end = index + 1
            while end < stop and messages[end].get("role") in {"assistant", "tool"}:
                end += 1
            if not (end - index == 1 and is_marker(messages[index])):
                return index, end
            index = end
        else:
            index += 1
    return None


def compact(
    messages: list[dict[str, Any]],
    *,
    budget_tokens: int,
    tail_token_budget: int = 6000,
    protect_head_messages: int = 2,
    system_prompt: str = "",
) -> CompactionResult:
    system_tokens = estimate_tokens(system_prompt)
    tokens_before = estimate_messages_tokens(messages) + system_tokens
    messages_before = len(messages)
    compacted = 0

    while estimate_messages_tokens(messages) + system_tokens > budget_tokens:
        stop = _tail_start(messages, tail_token_budget)
        group = _oldest_compactible_group(messages, protect_head_messages, stop)
        if group is None:
            break

        start, end = group
        omitted = estimate_messages_tokens(messages[start:end])
        messages[start:end] = [_marker(end - start, omitted)]
        compacted += 1

    return CompactionResult(
        tokens_before=tokens_before,
        tokens_after=estimate_messages_tokens(messages) + system_tokens,
        messages_before=messages_before,
        messages_after=len(messages),
        exchanges_compacted=compacted,
    )
