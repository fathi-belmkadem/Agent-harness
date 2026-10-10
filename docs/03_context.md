# Context

`harness/agent/context.py` and `harness/memory/compaction.py`. The
short-term memory: everything the model sees on one call, and how it is kept
inside the window.

## What the model sees

One request is assembled by `build_request_messages(context, recall_block)`
from three parts:

1. **The system prompt.** Built once in `startup.py`, byte-identical on every
   turn of a session. Never rewritten, never appended to. This is invariant
   I1: the provider's prompt cache is keyed on this prefix, and anything
   that changes per turn invalidates it.
2. **The conversation.** `Context.messages`, a list of `user`, `assistant`
   and `tool` messages in the OpenAI shape, resumed from Redis at startup and
   grown by the loop.
3. **Recalled facts**, if any. Prefixed onto a *copy* of the latest user
   message at request time, under a header saying they are background, not
   instructions. The stored conversation keeps the user's raw text; recall
   never accumulates into history.

Tool schemas travel separately as the `tools` parameter of the API call, so
they are part of what the model sees but not part of the conversation.

## `Context`

| method | does |
|---|---|
| `add_user(text)` | append a user message |
| `add_assistant(text, tool_calls=None)` | append the model's reply, with its declared calls if any |
| `add_tool_result(tool_call_id, name, content)` | append one observation for one declared call |
| `as_chat_messages()` | the system prompt plus the messages, as the API wants them |

`format_recall_block(entries)` renders retrieved facts with the
not-instructions header; empty input renders nothing.

## Why the conversation must be bounded

Every turn appends at least two messages, and a tool result can be a whole
file. Left alone, the conversation reaches the model's window and then
performance degrades well before the hard limit: the effect the lecture calls
context rot. So the loop's first phase, `fit_context`, keeps it under a
budget.

## Compaction

`compact(messages, budget_tokens, tail_token_budget, protect_head_messages, system_prompt)`
in `memory/compaction.py`. Runs at the start of every turn; does nothing when
the estimate is under budget.

The budget is in **tokens, not messages.** Five messages carrying a pasted
file blow the window while forty short ones do not. `estimate_tokens` is
characters divided by four plus a small per-message overhead, deliberately
crude, behind one function so a real tokenizer can replace it later.

Three rules decide what may be compacted:

| rule | default | why |
|---|---|---|
| the head is protected | first 2 messages | the opening frames the task |
| the tail is protected | last 6000 tokens | what the model is working on now |
| user messages are never compacted | always | the user's words are the intent everything else derives from; paraphrasing "use the existing helper, don't add one" is how an agent confidently does the opposite six turns later |

Only the middle is eligible, and only the assistant's side of it: an
assistant message and the tool results that followed it collapse into one
marker message with the assistant role, so role alternation holds. The
oldest eligible exchange goes first, until the budget is met. `is_marker`
recognises a marker by its `[compacted:` prefix, so a resumed session never
compacts the same span twice.

A marker is a placeholder, not a summary. The content is gone rather than
condensed. Doing better needs a second model call per compaction and an
auxiliary-model abstraction the harness does not have; it is on the roadmap
and deliberately not built.

`CompactionResult` reports tokens and messages before and after, which the
loop traces as `context_compacted`.

## Configuration

```yaml
session:
  context_budget_tokens: 24000     # the budget; message count is ignored when set
compaction:
  protect_head_messages: 2
  tail_token_budget: 6000
```

## Where the decisions are recorded

ADR-006 (recall rides the user message), ADR-008 (compaction rules).
Invariants I1, I3, I4.
