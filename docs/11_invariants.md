# Invariants

Properties the harness must not break. Each one is enforced by a test, and each
is here because breaking it produces a failure that is hard to see and hard to
diagnose — the transcript still looks plausible, the tests still pass, and the
agent just gets quietly worse.

Review changes against this list. If a change has to break one, that's an ADR
(`docs/12_ADR.md`), not a judgement call in a diff.

---

### I1 — The system prompt is byte-stable for the life of a conversation

Anything that must reach the model mid-conversation rides a **user message** or
a **tool result**, never the system message.

*Why:* rewriting the system prompt every turn invalidates the provider's prompt
cache, and makes the stored conversation stop matching what was actually sent —
so a transcript can no longer be trusted to explain a run. It also puts
user-influenced text (recalled memories) in the position the model treats as
policy.

*Enforced by:* `tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced`

---

### I2 — Strict role alternation; no synthetic user message mid-loop

Between two real user turns the only legal chain is
`assistant(tool_calls) → tool → assistant`. Never two messages of the same role
in a row, and never a user message the person didn't type.

*Why:* providers reject or mishandle the shapes, and a fabricated user message
is indistinguishable from a real instruction on the next turn. This is why a
textual tool call is recovered by *synthesizing the tool_call and executing it*
rather than by injecting "please try again" as the user.

A second way this can break: recording a tool call on the assistant message and then declining to run it leaves a declared `tool_call_id` with no matching `tool` message. The shape guard therefore records only the calls it will actually dispatch.

*Enforced by:* `tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced` (declared ids = answered ids), `tests/test_3_context.py::test_the_context_stays_within_budget_and_bad_responses_are_handled` (markers keep the assistant role)

---

### I3 — User messages are never compacted

The head (opening messages) and a token-budgeted tail stay verbatim; only the
middle is compactible, and never the user's own words.

*Why:* what the assistant produced is largely an account of what it did, which
survives summarising with little loss. The user's instructions are the intent
everything else derives from and cannot be reconstructed from the work that
followed. Paraphrasing "use the existing helper, don't add a new one" is how an
agent confidently does the opposite six turns later.

*Enforced by:* `tests/test_3_context.py::test_the_context_stays_within_budget_and_bad_responses_are_handled`

---

### I4 — The transcript is the source of truth for compaction state

A resumed session must not re-summarize what a marker already covers.

*Why:* compaction state held only in memory is lost on restart, and re-folding
already-absorbed history silently degrades it a second time. Markers are
detected by content, so the transcript alone is enough to resume correctly.

*Enforced by:* `tests/test_3_context.py::test_the_context_stays_within_budget_and_bad_responses_are_handled`

---

### I5 — Automated maintenance never deletes

Archive is the maximum; superseding is only ever the model's explicit call.
Pinned entries are exempt from every automatic transition.

*Why:* a wrong automated judgement about what's stale should cost recall, not
data. Everything stays readable and reversible, which is also what makes it
reviewable.

*Enforced by:* `tests/test_2_memory.py::test_memory_is_written_only_by_tools_and_maintained_without_deleting`

---

### I6 — Telemetry is fail-open

A trace or hook failure logs and continues; it never changes what the agent
does and never fails a run.

*Why:* observability that can take production down is worse than none. Adding
a new exporter must not add a new failure mode.

*Enforced by:* `tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced` (exploding sink and session store), `tests/test_2_memory.py::test_memory_is_written_only_by_tools_and_maintained_without_deleting` (closed store during maintenance)

---

### I7 — Every tool error is a result, not an exception

A handler returns a string; a failure becomes `ToolResult(ok=False)` that the
model can read and route around. Nothing escapes into the loop.

*Why:* a crashed run loses the whole conversation over one bad path argument.
An error the model can see is one it can correct on the next iteration.

*Enforced by:* `tests/test_5_tools.py::test_every_tool_source_merges_into_one_sandboxed_registry`, `tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced` (a failing read is an observation), `tests/test_4_subagents.py::test_subagents_are_isolated_tools_governed_by_their_own_policy` (a denied call is an observation)

---

### I8 — Secrets never reach a transcript

Values of `*_TOKEN` / `*_KEY` / `*_SECRET` / `*_PASSWORD` environment
variables, and credential-shaped strings, are redacted before an event is
written.

*Why:* transcripts get attached to bug reports and pasted into chats. A tool
result can easily contain the token that was used to fetch it.

*Enforced by:* `tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced`

---

## Not invariants (deliberately)

- **Recall is always semantic.** It is *when embeddings are reachable* — the
  hybrid search added in week 3 retrieves a fact worded entirely differently
  from the query (`tests/test_2_memory.py::test_memory_is_written_only_by_tools_and_maintained_without_deleting`).
  But that is a quality, not a guarantee: with the endpoint down or embeddings
  switched off, recall silently falls back to keyword search with stemming,
  and a query sharing no stems misses
  (same test, after the embedder is switched off). Nothing in the
  harness may assume a fact will be recalled; memory is context, not state.
- **Consolidation resolves conflicting facts.** It merges *restatements*. A
  fact whose value changed is superseded by the model naming the old id
  (same test: the python 3.7 → 3.8 pair stays unmerged).
- **Compaction always fits the budget.** Protected user messages and markers
  are a real floor; the result reports where it actually landed.
