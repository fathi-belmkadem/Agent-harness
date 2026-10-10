"""Decide whether a model response can be acted on as-is.

Four things a response can get wrong, all observed live against Qwen3:
a tool call emitted as text instead of through the API; a structured call
missing a field; an empty reply offered as the answer; a reply claiming a
tool ran when none did. Each has a recovery here rather than a crash in the
loop (ADR-004, ADR-017, ADR-023).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# A reply asserting that a tool-backed action *happened this turn*, when no
# such tool ran. Observed live: asked five times to remember a preference, the
# model said "I've stored this" every time and never called the tool. Patterns
# need a first-person, this-turn lead-in ("I've", "now", "already", ...) so
# narrating history ("last week you exported a PDF") does not match.
#
# `enforce`: may the loop re-request with the tool forced? True where a wrong
# guess is harmless (a read, a search) or validated on real transcripts; False
# where a false positive would *do* something. Unenforced claims are traced.
_LEAD = r"\b(?:I(?:'ve| have|’ve)?|now|is|been|already|successfully)\s+"
_SPAN = r"[^.\n]{0,80}?"
_MEMORY = r"memory|memories|preference|preferences|long-term|long term"
# Markdown emphasis sits between verb and subject ("now **pinned** in memory").
_MARKUP_RE = re.compile(r"[*_`]+")


def _claim(verbs: str, nouns: str) -> "re.Pattern[str]":
    return re.compile(rf"{_LEAD}(?:{verbs})\b{_SPAN}\b(?:{nouns})\b", re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    family: str
    pattern: "re.Pattern[str]"
    tools: tuple[str, ...]
    enforce: bool
    # A true "I remember X" is usually backed by the automatic recall block,
    # not a tool call, so it only counts as unbacked when recall was empty.
    needs_empty_recall: bool = False


_CLAIMS = (
    Claim("memory_write", _claim("stored|saved|recorded|pinned|remembered|updated|added|committed", _MEMORY),
          ("memory__remember",), True),
    Claim("memory_recall_claim", re.compile(r"\bi\s+(?:remember|recall)\b", re.IGNORECASE),
          ("memory__search", "session__search"), True, needs_empty_recall=True),
    Claim("memory_forget", _claim("forgotten|forgot|removed|deleted|archived|erased|cleared", _MEMORY),
          ("memory__forget",), False),
    Claim("memory_search", _claim(r"searched|checked|looked(?: up| through| in)?|scanned|reviewed",
                                  "my memory|long-term memory|memories|past conversations|"
                                  "previous conversations|earlier conversations|conversation history"),
          ("memory__search", "session__search"), True),
    Claim("web_search", _claim("searched|looked up|checked|queried|found",
                               "the web|online|the internet|brave|search results|web search"),
          ("brave-search__brave_web_search",), True),
    Claim("file_read", _claim("read|opened|inspected|reviewed|examined",
                              "the file|file contents|contents of|your file"),
          ("read_file",), True),
    Claim("file_write", _claim("written|wrote|created|saved|generated",
                               r"the file|a file|the folder|a folder|the directory|to disk|"
                               r"[\w/.-]+\.(?:md|txt|py|json|yaml|csv)"),
          ("write_file", "create_folder", "append_file", "replace_in_file", "copy_file"), False),
    Claim("pdf_export", _claim("exported|rendered|generated|created|saved",
                               "as a pdf|to pdf|the pdf|a pdf|pdf file"),
          ("export_pdf",), False),
)
CLAIM_FAMILIES = tuple(claim.family for claim in _CLAIMS)


def unbacked_claims(content: str | None, called: set[str], *, has_recall: bool = False) -> list[Claim]:
    """Every action `content` claims that no tool in `called` accounts for.
    `has_recall`: this turn's automatic recall block was non-empty."""
    if not content:
        return []
    text = _MARKUP_RE.sub("", content)
    return [c for c in _CLAIMS
            if not (c.needs_empty_recall and has_recall)
            and not called.intersection(c.tools) and c.pattern.search(text)]


def tool_to_force(content: str | None, called: set[str], registered: list[str],
                  emit: Callable[..., None], *, has_recall: bool = False) -> str | None:
    """Trace every unbacked claim; return the tool to force, if any. The loop
    then discards the reply and re-requests once with `tool_choice`; nothing
    synthetic enters the context, so role alternation holds (I2)."""
    forced = None
    for claim in unbacked_claims(content, called, has_recall=has_recall):
        backing = [t for t in claim.tools if t in registered]
        enforceable = claim.enforce and bool(backing)
        emit(type="unbacked_claim", claim=claim.family, enforceable=enforceable, content=content)
        if enforceable and forced is None:
            forced = backing[0]
    return forced


def is_blank(content: str | None) -> bool:
    """True for a response with no usable text. Such a reply is a failure, not
    an answer: accepting it would score a model producing nothing as `done`."""
    return not (content or "").strip()


def _decode_tool_call(candidate: str) -> dict | None:
    """Parse one candidate string as a tool-call object, or None.

    `raw_decode` rather than `json.loads` so a stray trailing character — the
    model closing the object one brace too many, observed live — doesn't
    defeat recovery: it stops at the end of the first valid JSON value
    instead of requiring the whole string to parse.
    """
    try:
        parsed, _ = json.JSONDecoder().raw_decode(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
        return parsed
    return None


def parse_textual_tool_call(content: str | None) -> dict | None:
    """Recover a tool call the model wrote as text, or None for ordinary prose.

    Three shapes, in order of confidence: `<tool_call>` markup, a fenced
    ```json block (the shape a chat-tuned model falls into most naturally),
    and bare JSON.
    """
    if not content:
        return None

    match = _TOOL_CALL_TAG_RE.search(content)
    if match:
        # The markup is unambiguous on its own, so try the whole tagged body
        # first, then a fence nested inside it.
        body = match.group(1)
        inner = _CODE_FENCE_RE.search(body)
        for candidate in [body] + ([inner.group(1)] if inner else []):
            parsed = _decode_tool_call(candidate)
            if parsed is not None:
                return parsed
        return None

    fence = _CODE_FENCE_RE.search(content)
    for candidate in ([fence.group(1)] if fence else []) + [content.strip()]:
        parsed = _decode_tool_call(candidate)
        if parsed is not None:
            return parsed
    return None


def coerce_arguments(raw: object) -> object:
    """Normalize a tool call's arguments to something dispatchable.

    The API documents a JSON string. Models send objects, omit the field for
    no-argument tools, and occasionally send neither.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def malformed_call_reason(call: object) -> str | None:
    """Why `call` can't be acted on, or None if it has the minimum shape.

    The acting step reads `call["id"]` and `call["function"]["name"]`
    directly. A provider that drops a field — a streaming glitch, a proxy
    that truncates — would otherwise raise `KeyError` inside the loop and take
    the whole run down with a stack trace, rather than being reported as
    something the model can correct.
    """
    if not isinstance(call, dict):
        return "a tool call was not an object"
    if not call.get("id"):
        return "a tool call was missing its call id"
    function = call.get("function")
    if not isinstance(function, dict):
        return f"tool call {call['id']} was missing its function object"
    if not function.get("name"):
        return f"tool call {call['id']} was missing a function name"
    return None


def usable_tool_calls(
    calls: list,
    *,
    iteration: int,
    emit: Callable[..., None],
) -> list[dict]:
    """The subset of `calls` that can actually be dispatched.

    A missing id is repaired: it only has to be unique enough to pair a result
    back, so discarding an otherwise valid instruction over it would lose real
    work. A missing function name is unrecoverable — there is nothing to
    dispatch — so that call alone is dropped and the rest of the batch runs.

    Only what this returns may be recorded on the assistant message. Recording
    a call and then declining to run it leaves a declared `tool_call_id` with
    no matching `tool` message, which breaks role alternation (I2) for the
    rest of the conversation.
    """
    usable: list[dict] = []
    for index, call in enumerate(calls):
        reason = malformed_call_reason(call)
        if reason is None:
            usable.append(call)
            continue

        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and function.get("name"):
            repaired = dict(call)
            repaired["id"] = f"synthesized_{iteration}_{index}"
            usable.append(repaired)
            emit(type="malformed_tool_call", reason=reason,
                 recovered=[function["name"]])
        else:
            emit(type="malformed_tool_call", reason=reason, recovered=[])
    return usable
