# Sub-agents

`harness/agent/subagents.py`, with `ToolRegistry.subset` and the
`parent_run_id` on the trace emitter. Agent-to-agent communication on this
harness: what a sub-agent is, how the parent and child talk, what the child's
context holds, and which memories it may see. The design rationale is in
ADR-019 and ADR-021 in `12_ADR.md`; this is the reference.

## In one sentence

A sub-agent is the same `AgentLoop` with its own system prompt, a fresh
context holding only the delegated task, a subset of the parent's tools, its
own iteration cap and its own permission policy, which the parent reaches as
a tool called `delegate__<role>`.

## What differs from the parent

| | parent | child |
|---|---|---|
| system prompt | the harness prompt | the role's prompt from config |
| context | the user's conversation, resumed from Redis | fresh: the task, and any background the parent chose to include |
| tools | everything registered | `parent_tools.subset(role.tools)`, never `delegate__*` |
| permissions | the top-level policy | the role's own policy |
| iteration cap | `llm.max_iterations` | the role's `max_iterations` |
| time budget | `llm.max_seconds` | the same value, and the child's time also counts against the parent's, since it runs inside the parent's tool call |
| session store | Redis | none |
| episodic archive | writes every turn | none |
| long-term facts | read and write | read only: recall runs, write tools are not in the subset |
| LLM client | shared | shared; it is stateless |
| transcript | its own run id | its own run id, every event stamped with the parent's |

## How delegation works

`SubagentProvider` is a `ToolProvider` with namespace `delegate`. `load()`
returns one tool per configured role:

```
delegate__<name>(task: str, context: str = "") -> str
```

The description is the role's description plus a fixed sentence telling the
model the child sees only the task. The handler builds a child loop, runs it
on a fresh `Context`, emits a `delegated` event, and returns the child's
final message. That string lands in the parent's conversation as an ordinary
tool result.

**Down:** the task, and optionally a `context` string the parent writes.
There is no code path from the parent's conversation to the child's, so the
parent cannot leak history by accident. What the child needs, the parent
must put in the task; the system prompt tells it so.

**Up:** the child's final message. Its tool results and dead ends stay in
its context and are discarded with it. What the parent absorbs is bounded by
how the role is told to answer, not by how much the child read.

**Failure is an observation.** A child that hits its cap returns the cap
message; one whose LLM call fails returns that; one that raises is wrapped
by the registry. All three arrive in the parent as `tool` messages. The
parent's model decides what to do next. No exception crosses the boundary.

**Depth is one.** `subset()` never includes `delegate__*`, enforced in code
and tested.

**Concurrency is opt-in.** With `delegation.max_parallel` above 1, the
`delegate__*` calls in a single model response run in a thread pool; every
other tool in the batch still runs in order. Observations are appended on
the calling thread in the declared order, so I2 holds unchanged. Children
in that mode cannot prompt: their `ask` resolves to deny. The default is 1,
which is the sequential path. `parallel_research.yaml` proves the overlap
from the transcript (`events_overlap` on `delegated`). ADR-021.

## The child's context

Two messages to start: the role's system prompt and the task. It grows by one
assistant message and one tool result per act, to at most the role's cap.
When the child returns, its `Context` is garbage. It is not saved to Redis
and not archived: the parent's own turn already records the delegation call
and its result, which is the exchange worth finding later. It is fully
traced under the parent's run id, so nothing is lost.

## Memory

| memory | child reads | child writes | how |
|---|---|---|---|
| parent's conversation | no | no | no code path exists, and there should not be: inheriting it would fill the child's context before it did any work |
| long-term facts | yes, automatically | no | the child loop gets `memory=`; its recall phase prefixes hits onto the task; `memory__remember` and `memory__forget` are not in the subset unless a role lists them, and neither shipped role does |
| episodic archive | if the role lists `session__search` | no | tool subset; the child has `episodic=None` |

The child inherits knowledge and not conversation, and it can read but not
persist. Facts reach it automatically rather than "the parent passes what is
relevant" for the same reason the unbacked-claim guard exists: relying on the
model to relay a preference is the same class of failure as relying on it to
store one.

Three reasons a child does not write facts. **Authority:** facts are things
the user said or confirmed; the parent talks to the user, the child talks to
a web page, and a researcher that stored "the team uses PostgreSQL" after
reading it on a forum has stored a rumour as a preference. **Visibility:**
the parent's store calls are traced, guarded and surfaced in its reply; a
child's would be silent. **One writer:** supersession, pinning and the
unbacked-claim guard all reason about a single agent's stores in a single
conversation. The one write that does happen is correct: `search()` sets
`last_accessed_at`, so a fact a child used counts as used and is not archived.

## Permissions at two levels

The child carries its own `Policy`, so the writer can be told to `ask`
before `export_pdf` while the researcher never sees that tool at all. The
subset decides what a role *can* do; the policy decides what it *may* do
without asking.

The delegation call itself is a tool in the parent, so the parent's policy
governs it too: `delegate__*: ask` puts a human gate in front of every
delegation. A child's `ask` with no terminal, or in parallel mode, resolves to
deny, the same rule as the parent; `subagent_report.yaml` asserts exactly
that path.

## Observability

Every event the child emits carries `parent_run_id`. The parent emits one
`delegated` event per call:

```json
{"type": "delegated", "subagent": "researcher", "child_run_id": "23acde4566f4",
 "iterations": 4, "ok": true, "tools_available": ["brave-search__brave_web_search", "..."],
 "duration_ms": 8412.3}
```

On the dashboard a child's turns are the `agent="subagent"` series of the
turn panels, and each delegation is a `delegate__<name>` row under tool calls.

## The two roles that ship

```yaml
subagents:
  - name: researcher
    description: Researches one question using web search and long-term memory and returns a sourced bullet list. Cannot touch the filesystem.
    system_prompt: |
      ...answer the one question you are given by searching, then reply with a
      concise bullet list of findings, each with its source URL. Do not write files.
    tools: ["brave-search__*", "everything__*", "memory__search", "session__search"]
    max_iterations: 8
  - name: writer
    description: Writes given content to a workspace file, optionally exporting it as PDF. Cannot search the web.
    system_prompt: |
      ...write exactly the content you are given to the path you are given...
    tools: ["write_file", "create_folder", "read_file", "append_file", "export_pdf"]
    max_iterations: 6
    permissions:
      rules:
        - {match: "export_pdf", decision: ask}
```

## The task that benefits

"Research how MCP servers handle authentication and write a summary to
`reports/mcp-auth.md`." Measured on one run: the children made four tool
calls and read 18,493 bytes, the largest a single 18 KB search result; the
parent received 2,144 bytes across its two delegate results. 88% of what was
read never entered the parent's context, and stays out for every later turn
of the session. The researcher's context is thrown away.

One measured thing worth knowing: given the same filesystem tools as the
writer, the parent writes the file itself rather than delegate, because
delegating is more work from its point of view. Giving the parent a
read-only policy, the lecture's tiered model, makes delegation the only path.

## Configuration

`name` must be an identifier, since it becomes a tool name. `tools` must be
non-empty. `system_prompt` is required. `max_iterations` defaults to 8.
`permissions` defaults to allow-all. The provider is registered after the
MCP servers connect, so a role's globs can match their tools.

## Try it

```
docker compose run --rm --name agent-harness agent-harness
you> Research how MCP servers handle authentication and have the writer put a summary in reports/mcp-auth.md.
```

The startup banner lists `· sub-agents: researcher, writer`. After the turn,
`workspace/reports/mcp-auth.md` exists and the newest file in `.transcripts/`
holds two `delegated` events.

## Not built

Child-to-child messages, depth two, a shared scratchpad, streamed progress,
children that persist anything, parallelism across responses (a scheduler).

## Where the decisions are recorded

ADR-019 and ADR-021 in `docs/12_ADR.md`. Tests in
`tests/test_4_subagents.py::test_subagents_are_isolated_tools_governed_by_their_own_policy`.
