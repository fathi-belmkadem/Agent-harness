# The ReAct loop

`harness/agent/loop.py` and `harness/agent/phases/`. The control structure
that turns one user message into one answer, by alternating between asking
the model what to do next and doing it.

## The shape of a turn

`AgentLoop.run(context)` is one turn. It returns a `RunResult` with the
final message, how many iterations it took, and which guard ended it if one
did. The loop is the facade; the work is in five phase modules, each a plain
function over explicit arguments.

```
run(context)
  │
  ├─ fit_context      compact the conversation if it is over the token budget
  ├─ build_recall_block   search long-term facts, format hits for injection
  │
  └─ for i in 1..max_iterations:
       ├─ llm.chat(messages + recall, tools)          the reasoning state
       ├─ no tool calls?
       │    ├─ parse_textual_tool_call → budget check, recover and act, continue
       │    ├─ is_blank → failed
       │    ├─ unbacked_claims → discard, re-ask once with tool_choice forced
       │    └─ else: done, return the answer         the terminal state
       ├─ budget exceeded? → aborted   (checked before every further spend)
       ├─ usable_tool_calls   repair or drop malformed calls
       ├─ execute_tool_calls  dispatch each, append observations   the acting state
       └─ a `clarify` call among them? → done with the question, clarified=true
  │
  finally: save_session · archive_turn · maybe_consolidate
```

The phases:

| phase | function | file |
|---|---|---|
| fit context | `fit_context` | `phases/context_fit.py` |
| recall | `build_recall_block`, `latest_user_message` | `phases/recall.py` |
| response check | `parse_textual_tool_call`, `is_blank`, `usable_tool_calls`, `malformed_call_reason`, `coerce_arguments`, `unbacked_claims`, `tool_to_force` | `phases/response_check.py` |
| act | `execute_tool_calls`, `execute_recovered_call` | `phases/act.py` |
| finalize | `save_session`, `archive_turn`, `turn_messages`, `maybe_consolidate` | `phases/finalize.py` |

Tracing is passed into each phase as an `emit` callable. No phase reaches
for a logger, and no phase constructs anything; that is what lets each be
tested with a fake model and no registry.

## The three ways a turn ends

| end | `RunResult` | when |
|---|---|---|
| answer | `final_message` | the model replied with text and no tool calls |
| guard | `hit_iteration_cap` or `budget_exceeded` | `max_iterations`, `max_tokens`, or `max_seconds` was reached |
| failure | `failed` | the LLM call raised, or the reply was empty |

The guards are what make the loop always terminate. A model that neither
answers nor emits a valid action cannot run forever: the iteration cap bounds
it, and the token and time budgets bound it again from a different direction.
The message that ends a guarded turn is written into the conversation as the
assistant's reply, so the next turn sees what happened.

## What the model's reply can be, and what the loop does with each

The model's reply is not trusted as-is. Four things can be wrong with it,
each observed live, each handled before the reply is acted on:

**A tool call written as text.** Some models emit `<tool_call>{...}</tool_call>`
or a fenced JSON block instead of using the API. `parse_textual_tool_call`
recovers the intent from three shapes, synthesizes a proper `tool_calls`
entry so the conversation stays valid, executes it, and continues. No turn is
spent asking the model to try again.

**A structured call missing a field.** A call without an `id` gets one
synthesized. A call without a function name is dropped, and the rest of the
batch still runs. Only calls that will actually be dispatched are recorded on
the assistant message, so every declared id gets its tool result.

**An empty reply.** Returned as a failed turn, not as an answer. Accepting it
had once logged a model producing nothing as a success.

**A reply claiming an action that did not happen.** "I've stored this" with
no `memory__remember` call in the turn. The reply is discarded and the same
request is sent once more with `tool_choice` forcing the tool. Found live,
five turns in a row. See `docs/12_ADR.md`, ADR-017.

## The acting state

`execute_tool_calls` dispatches each call through one function, `_dispatch`,
which is the single point every tool call in the system passes through. It
does four things, in order: asks the permission policy, calls the registry,
appends the result to the conversation as a `tool` message, and emits an
`act` event with the outcome and duration.

A tool that fails returns `ToolResult(ok=False)` and that text is the
observation. Nothing escapes as an exception. The model reads the error on
the next iteration and routes around it.

## Role alternation

Between two user turns the only legal chain is `assistant(tool_calls) → tool
→ assistant`. The loop never appends a synthetic user message, never records
a tool call it will not run, and always writes exactly one `tool` message per
declared call id. This is invariant I2; the recovery paths and the
unbacked-claim retry are both designed around it.

## Constructor arguments

| argument | what it governs |
|---|---|
| `llm`, `tools` | the model client and the registry |
| `limits: Limits` | the guards (`max_iterations`, `max_tokens`, `max_seconds`) and compaction (`context_budget_tokens`, `tail_token_budget`, `protect_head_messages`, see `03_context.md`) |
| `memory: MemorySetup` | `facts`, `retrieval_limit`, `episodic`, `session_store`, `session_id`, `consolidation`, `consolidation_llm`; see `06_memory.md` |
| `permissions`, `confirm` | the policy and the terminal prompt, see `07_permissions.md` |
| `max_parallel_tools` | above one, `delegate__*` calls in one response run concurrently |
| `transcript`, `extra_sinks`, `parent_run_id` | tracing, see `10_observability.md` |

A sub-agent is this same class with a different prompt, a tool subset, and
its own policy. See `08_subagents.md`.

## Trace events a turn emits

`reason` per LLM call with token counts; `act` per tool call; one of `done`,
`failed`, `aborted`; `malformed_tool_call`, `unbacked_claim`,
`permission_decision` when those paths fire; `context_compacted`,
`memory_retrieved`, `turn_archived`, `consolidated` from the phases.

## Where the decisions are recorded

ADR-004 (textual tool calls are executed, not retried), ADR-017 (unbacked
claims are enforced), ADR-023 (`clarify` ends the turn). Invariants I2, I6,
I7 in `docs/11_invariants.md`.
