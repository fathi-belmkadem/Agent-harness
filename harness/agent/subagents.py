"""Sub-agents: a second loop the parent reaches as a tool.

A sub-agent is the same `AgentLoop` with its own system prompt, a fresh
context holding only the delegated task, a subset of the parent's tools, its
own iteration cap and its own permission policy. The parent's model discovers
it as `delegate__<name>` and calls it like any other tool; the child's final
message comes back as the tool result.

What crosses the boundary is deliberately narrow. Down: the task string, and
optionally a context string the parent chooses to write. Up: the child's
final message. The parent's conversation never enters the child, and the
child's intermediate work -- every page it read, every dead end -- is
discarded with its context. It is fully traced under the parent's run id,
so nothing is lost; it just never sits in anyone's context window.

Memory: the child reads long-term facts the same way the parent does, through
the automatic recall phase, because a writer that does not know the user's
preferences produces output they have to correct. It never writes them: the
write tools are not in its subset, and it has no session store and no
episodic archive. The parent is the only agent that speaks to the user, so
it is the only one that decides what is durable.

Depth is one. `ToolRegistry.subset` never includes `delegate__*`.
"""
from __future__ import annotations

import time
from typing import Callable

from ..config import SubagentConfig
from ..llm_client import LLMClient
from ..memory import SQLiteMemory
from ..tools import Tool, ToolRegistry, tool
from ..tools.permissions import Confirm
from .context import Context
from .loop import AgentLoop, Limits, MemorySetup


class SubagentProvider:
    def __init__(
        self,
        configs: tuple[SubagentConfig, ...],
        *,
        llm: LLMClient,
        parent_tools: ToolRegistry,
        parent_run_id: str,
        emit: Callable[..., None],
        transcript=None,
        extra_sinks: tuple = (),
        memory: SQLiteMemory | None = None,
        memory_retrieval_limit: int = 5,
        confirm: Confirm | None = None,
        max_seconds: float | None = None,
        parallel: bool = False,
    ) -> None:
        self._configs = configs
        self._llm = llm
        self._parent_tools = parent_tools
        self._parent_run_id = parent_run_id
        self._emit = emit
        self._transcript = transcript
        self._extra_sinks = extra_sinks
        self._memory = memory
        self._memory_retrieval_limit = memory_retrieval_limit
        self._confirm = confirm
        self._max_seconds = max_seconds
        # When the parent runs delegates concurrently, children must not
        # prompt: two threads sharing one stdin is not a question anyone can
        # answer. `ask` then resolves to deny, the rule for any run that
        # cannot ask.
        self._parallel = parallel
        if parallel:
            self._confirm = None

    @property
    def namespace(self) -> str:
        return "delegate"

    def load(self) -> list[Tool]:
        return [
            tool(
                f"delegate__{config.name}",
                f"{config.description} The sub-agent sees only the task you give it, "
                "not this conversation: include everything it needs. It returns its "
                "final answer as this tool's result."
                + (" Independent tasks can be delegated in the same response; "
                   "they run concurrently." if self._parallel else ""),
                self._handler(config),
                {"task": {"type": "string", "description": "The complete task, self-contained."},
                 "context": {"type": "string",
                             "description": "Optional background the sub-agent needs, copied from this conversation."}},
                ["task"],
            )
            for config in self._configs
        ]

    def _handler(self, config: SubagentConfig) -> Callable[..., str]:
        def run(task: str, context: str = "") -> str:
            return self._run(config, task, context)
        run.__name__ = f"delegate_{config.name}"
        return run

    def _run(self, config: SubagentConfig, task: str, background: str) -> str:
        started = time.monotonic()
        tools = self._parent_tools.subset(list(config.tools))
        loop = AgentLoop(
            llm=self._llm,
            tools=tools,
            limits=Limits(max_iterations=config.max_iterations, max_seconds=self._max_seconds),
            memory=MemorySetup(facts=self._memory, retrieval_limit=self._memory_retrieval_limit),
            permissions=config.permissions,
            confirm=self._confirm,
            transcript=self._transcript,
            extra_sinks=self._extra_sinks,
            parent_run_id=self._parent_run_id,
        )
        child = Context(system_prompt=config.system_prompt)
        prompt = f"{task}\n\nBackground:\n{background}" if background.strip() else task
        child.add_user(prompt)

        result = loop.run(child)
        ok = not (result.failed or result.hit_iteration_cap or result.budget_exceeded)
        self._emit(
            type="delegated", subagent=config.name, child_run_id=loop.trace.run_id,
            iterations=result.iterations_used, ok=ok,
            tools_available=tools.names(),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return result.final_message
