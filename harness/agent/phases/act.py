"""Execute tool calls and append their observations to the conversation.

Every dispatch appends exactly one `tool` message for the id the assistant
declared, which is what keeps role alternation valid (I2). A tool that fails
returns `ToolResult(ok=False)` and that text becomes the observation (I7):
the model reads the error and routes around it, instead of the run dying on
an exception it could have corrected.

A batch is handled in three steps: decide (permissions, on the calling
thread, in order), run, observe (append + trace, on the calling thread, in
the original order). Splitting it this way is what lets `delegate__*` calls
in one response run concurrently: workers only ever touch the registry, never
the context, so I2 holds whether the batch ran in one thread or four.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from ...tools import ToolRegistry, ToolResult
from ...tools.permissions import Confirm, Policy, resolve
from ..context import Context
from .response_check import coerce_arguments


def is_delegate(name: str) -> bool:
    """The only calls that run concurrently: they are long, and they never
    touch the sandbox themselves."""
    return name.startswith("delegate__")


@dataclass(eq=False)
class _Pending:
    call_id: str
    name: str
    arguments: object
    result: ToolResult | None = None   # set early when the call will not run
    duration_ms: float = 0.0
    parallel: bool = False


def _decide(pending: _Pending, policy, confirm, emit) -> None:
    """Resolve the permission policy; a denial becomes the observation."""
    arguments = pending.arguments if isinstance(pending.arguments, dict) else {}
    if not resolve(policy, pending.name, arguments, confirm, emit):
        pending.result = ToolResult(
            ok=False, content=f"{pending.name} was not run: denied by the permission policy.")


def _run(tools: ToolRegistry, pending: _Pending) -> None:
    started = time.monotonic()
    pending.result = tools.dispatch(pending.name, pending.arguments)
    pending.duration_ms = round((time.monotonic() - started) * 1000, 1)


def _observe(context: Context, pending: _Pending, emit, **extra) -> None:
    context.add_tool_result(tool_call_id=pending.call_id, name=pending.name,
                            content=pending.result.content)
    event = {
        "type": "act", "tool": pending.name, "tool_call_id": pending.call_id,
        "arguments": pending.arguments, "result": pending.result.content,
        "ok": pending.result.ok, "duration_ms": pending.duration_ms, **extra,
    }
    if pending.parallel:
        event["parallel"] = True
    emit(**event)


def execute_tool_calls(
    tools: ToolRegistry,
    context: Context,
    calls: list[dict],
    *,
    emit: Callable[..., None],
    policy: Policy | None = None,
    confirm: Confirm | None = None,
    max_parallel: int = 1,
) -> None:
    """Run each call in `calls`, appending one observation per call.

    With `max_parallel` above one, two or more `delegate__*` calls run in a
    thread pool; everything else runs in order. Permission decisions are made
    for the whole batch first, so `ask` prompts stay sequential and precede
    any work.
    """
    batch: list[_Pending] = []
    for call in calls:
        name = call["function"]["name"]
        raw = call["function"].get("arguments")
        arguments = coerce_arguments(raw)
        pending = _Pending(call["id"], name, arguments)

        # Arguments that parsed to nothing usable, from a non-empty payload,
        # are reported to the model rather than silently run as {} — calling a
        # tool with the wrong arguments is worse than telling it what broke.
        if arguments == {} and isinstance(raw, str) and raw.strip() not in ("", "{}"):
            pending.arguments = raw
            pending.result = ToolResult(ok=False, content=f"Invalid arguments JSON: {raw!r}")
        else:
            _decide(pending, policy, confirm, emit)
        batch.append(pending)

    runnable = [p for p in batch if p.result is None]
    parallel = [p for p in runnable if is_delegate(p.name)] if max_parallel > 1 else []
    if len(parallel) < 2:
        parallel = []
    for pending in parallel:
        pending.parallel = True
    for pending in runnable:
        if not pending.parallel:
            _run(tools, pending)
    if parallel:
        with ThreadPoolExecutor(max_workers=min(max_parallel, len(parallel))) as pool:
            list(pool.map(lambda p: _run(tools, p), parallel))

    for pending in batch:
        _observe(context, pending, emit)


def execute_recovered_call(
    tools: ToolRegistry,
    context: Context,
    recovered: dict,
    *,
    content: str | None,
    iteration: int,
    emit: Callable[..., None],
    policy: Policy | None = None,
    confirm: Confirm | None = None,
) -> None:
    """Run a tool call the model wrote as text instead of calling properly.

    The intent is recovered immediately rather than spending a turn asking for
    a retry. A `tool_calls` entry is synthesized first so the conversation
    stays API-valid: a `tool` message is only legal after an assistant turn
    that declared that same `tool_call_id`.
    """
    name = recovered["name"]
    arguments = coerce_arguments(recovered.get("arguments"))
    call_id = f"recovered_{iteration}"

    context.add_assistant(content, tool_calls=[{
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }])
    emit(type="malformed_tool_call", content=content, recovered=[name])

    pending = _Pending(call_id, name, arguments)
    _decide(pending, policy, confirm, emit)
    if pending.result is None:
        _run(tools, pending)
    _observe(context, pending, emit, recovered=True)
