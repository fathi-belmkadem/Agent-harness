import time
from dataclasses import dataclass, field

from ..config import ConsolidationConfig
from ..llm_client import LLMCallFailed, LLMClient
from ..memory import EpisodicArchive, RedisSessionStore, SQLiteMemory
from ..observability.events import TraceEmitter
from ..observability.transcript import Transcript
from ..tools import ToolRegistry
from ..tools.clarify import clarification
from ..tools.permissions import Confirm, Policy
from . import phases
from .context import Context, build_request_messages


@dataclass
class RunResult:
    final_message: str
    iterations_used: int
    hit_iteration_cap: bool = False
    budget_exceeded: bool = False
    failed: bool = False
    clarified: bool = False  # the turn ended on a question for the user


@dataclass(frozen=True)
class Limits:
    """How much one turn may spend, and how big the context may grow."""
    max_iterations: int = 15
    max_tokens: int | None = None
    max_seconds: float | None = None
    context_budget_tokens: int | None = None  # None: never compact
    tail_token_budget: int = 6000
    protect_head_messages: int = 2


@dataclass
class MemorySetup:
    """Every memory store the loop touches. Each part is optional."""
    facts: SQLiteMemory | None = None
    retrieval_limit: int = 5
    episodic: EpisodicArchive | None = None
    session_store: RedisSessionStore | None = None
    session_id: str = ""  # one per launch; the archive files this process's turns under it
    consolidation: ConsolidationConfig | None = None
    consolidation_llm: LLMClient | None = None


@dataclass(eq=False)
class AgentLoop:
    llm: LLMClient
    tools: ToolRegistry
    limits: Limits = field(default_factory=Limits)
    memory: MemorySetup = field(default_factory=MemorySetup)
    permissions: Policy | None = None
    confirm: Confirm | None = None
    max_parallel_tools: int = 1  # above one, `delegate__*` calls in one response run concurrently
    transcript: Transcript | None = None
    extra_sinks: tuple = ()
    parent_run_id: str | None = None
    trace: TraceEmitter = field(init=False)
    _turn_start: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        # Stamps run/turn/iteration ids and redacts credentials. Fail-open (I6).
        self.trace = TraceEmitter(self.transcript, parent_run_id=self.parent_run_id,
                                  extra_sinks=self.extra_sinks)

    def _log(self, **event: object) -> None:
        self.trace.emit(**event)

    def _end(self, context: Context, message: str, iterations: int, event: dict,
             **flags: bool) -> RunResult:
        context.add_assistant(message)
        self._log(**event, duration_ms=round((time.monotonic() - self._turn_start) * 1000, 1))
        return RunResult(final_message=message, iterations_used=iterations, **flags)

    def _over_budget(self, context: Context, tokens_used: int, start: float,
                     iteration: int) -> RunResult | None:
        """Checked before anything that would spend more. A final answer that
        arrives over budget is still returned; it is already paid for."""
        elapsed = time.monotonic() - start
        limits = self.limits
        if not ((limits.max_tokens is not None and tokens_used >= limits.max_tokens)
                or (limits.max_seconds is not None and elapsed >= limits.max_seconds)):
            return None
        return self._end(
            context, f"Stopped: budget exceeded ({tokens_used} tokens, {elapsed:.1f}s elapsed).",
            iteration, {"type": "aborted", "reason": "budget_exceeded",
                        "tokens_used": tokens_used, "elapsed_seconds": elapsed},
            budget_exceeded=True)

    def run(self, context: Context) -> RunResult:
        start = self._turn_start = time.monotonic()
        tokens_used = 0
        self.trace.start_turn()
        self._log(type="turn_started")
        # What actually ran this turn, so a reply claiming "I've stored this"
        # is checked against what happened, not what the model says happened.
        called_this_turn: set[str] = set()
        forced_call: str | None = None
        claim_was_forced = False

        phases.fit_context(
            context, budget_tokens=self.limits.context_budget_tokens,
            tail_token_budget=self.limits.tail_token_budget,
            protect_head_messages=self.limits.protect_head_messages, emit=self._log,
        )
        # Recall rides the request, not the stored conversation (I1).
        recall_block = phases.build_recall_block(
            context, self.memory.facts, limit=self.memory.retrieval_limit, emit=self._log,
        )

        try:
            for i in range(1, self.limits.max_iterations + 1):
                self.trace.iteration = i

                # Forced only for the one re-request after an unbacked claim.
                extra = {}
                if forced_call is not None:
                    extra["tool_choice"] = {"type": "function", "function": {"name": forced_call}}
                    forced_call = None

                try:
                    response = self.llm.chat(messages=build_request_messages(context, recall_block),
                                             tools=self.tools.schemas(), **extra)
                except LLMCallFailed as e:
                    return self._end(context, f"LLM call failed: {e}", i,
                                     {"type": "failed", "error": str(e)}, failed=True)

                tokens_used += response.total_tokens or 0
                self._log(type="reason", content=response.content,
                          tool_calls=response.tool_calls,
                          prompt_tokens=response.prompt_tokens,
                          completion_tokens=response.completion_tokens,
                          total_tokens=response.total_tokens)

                if not response.tool_calls:
                    # A tool call written as text: recover the intent, don't lose a turn.
                    recovered = phases.parse_textual_tool_call(response.content)
                    if recovered is not None:
                        if aborted := self._over_budget(context, tokens_used, start, i):
                            return aborted
                        phases.execute_recovered_call(
                            self.tools, context, recovered, content=response.content,
                            iteration=i, emit=self._log,
                            policy=self.permissions, confirm=self.confirm,
                        )
                        called_this_turn.add(recovered["name"])
                        continue

                    if phases.is_blank(response.content):
                        return self._end(context, "The model returned an empty response.", i,
                                         {"type": "failed", "error": "empty_response"}, failed=True)

                    # A reply claiming a tool ran when none did is re-requested
                    # once with the tool forced (ADR-017).
                    forced_tool = phases.tool_to_force(response.content, called_this_turn,
                                                       self.tools.names(), self._log,
                                                       has_recall=bool(recall_block))
                    if forced_tool is not None and not claim_was_forced:
                        forced_call, claim_was_forced = forced_tool, True
                        continue

                    return self._end(context, response.content, i,
                                     {"type": "done", "final_message": response.content})

                if aborted := self._over_budget(context, tokens_used, start, i):
                    return aborted

                usable = phases.usable_tool_calls(response.tool_calls, iteration=i, emit=self._log)
                if not usable:
                    # Nothing dispatchable; the iteration cap bounds the retries.
                    context.add_assistant(response.content or "")
                    continue

                # Only `usable` is recorded, so every declared id gets a result (I2).
                context.add_assistant(response.content, tool_calls=usable)
                phases.execute_tool_calls(self.tools, context, usable, emit=self._log,
                                          policy=self.permissions, confirm=self.confirm,
                                          max_parallel=self.max_parallel_tools)
                called_this_turn.update(call["function"]["name"] for call in usable)

                # A question for the user is this turn's answer.
                question = clarification(usable)
                if question is not None:
                    return self._end(context, question, i,
                                     {"type": "done", "final_message": question, "clarified": True},
                                     clarified=True)

            cap = self.limits.max_iterations
            return self._end(
                context, f"Reached the maximum of {cap} iterations without a final answer.",
                cap, {"type": "aborted", "reason": "iteration_cap"}, hit_iteration_cap=True)
        finally:
            memory = self.memory
            phases.save_session(memory.session_store, context.messages, emit=self._log)
            phases.archive_turn(memory.episodic, session_id=memory.session_id,
                                turn_id=self.trace.turn_id, messages=context.messages,
                                emit=self._log)
            phases.maybe_consolidate(memory.facts, memory.episodic, memory.consolidation_llm,
                                     memory.consolidation, emit=self._log)
