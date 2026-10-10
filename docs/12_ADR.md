# Architecture Decision Records

The decisions in force, newest first, each with the measurement or the
failure behind it. Numbers are stable so the code and the other docs can cite
them; five numbers are absent — 003 was superseded by 011, 009 by 024, and
013, 014 and 016 recorded code cleanups whose result is simply the current
code.

---

## ADR-024: One signal for fact usefulness, no automatic merging

**Status:** Accepted (final cleanup)

**Context:** Facts carried two answers to "is this still useful": an
`importance` score (set by the model, max-merged on upsert, nudged up and
down by maintenance, and the archive gate) and usage (`last_accessed_at`,
`access_count`). The two defaults for importance even disagreed (3 in the
store, 4 in the tool). Duplicates were caught three ways: an exact content
hash at write time, `supersedes` from the model, and a lexical merge in
maintenance that only caught rewordings a word or a comma apart.

**Decision:** Usage is the only signal. Importance, promote, decay and the
lexical merge are gone. Maintenance is one rule, one statement, one commit:
archive every unpinned active fact nobody stored or recalled in
`archive_after_days`. Duplicates are the content hash (identical) and
`supersedes` (changed value — the model's call, since "Python 3.7" and
"Python 3.8" sit at 0.853 cosine and similarity cannot say which is true
now). Rewordings that the merge used to catch age out through archiving.
Supersedes ADR-009.

---

## ADR-023: `clarify` is a tool that ends the turn

**Status:** Accepted (week 4)

**Context:** A model unsure what the user meant picks an interpretation and
acts; the wrong guess surfaces after the tools have run. Asking in prose is
invisible to the loop, and the model tends to ask and keep going.

**Decision:** `clarify(question)` is a registered tool with a no-op handler.
After dispatch the loop recognises the name: the question becomes the turn's
final message, appended as the assistant reply (I2 holds), traced as `done`
with `clarified: true`, and the next user message answers it. No new phase,
no new state. A separate approval hook was rejected: the permission policy
(ADR-018) already produces every outcome it would.

---

## ADR-022: Skills are files, indexed in the prompt, read on demand

**Status:** Accepted (week 4)

**Context:** Procedural memory was one hardcoded string. Procedures that recur
were either in the prompt every turn or nowhere.

**Decision:** `skills/<category>/<name>/SKILL.md` with a two-field frontmatter.
At startup the loader is strict (a malformed skill refuses to start, where the
author sees it) and `prompt_builder.py` builds the system prompt once: the
static core plus a name/description index, so I1 holds. The body is fetched
with one tool, `skill__read`, and costs no context until a task matches.
Rejected: a `skill__list` tool (the index is already in the prompt),
`version`/`tags` fields nothing reads, lenient loading.

---

## ADR-021: Parallel delegation is response-level, threaded, delegates-only, and the context is single-writer

**Status:** Accepted (week 4)

**Context:** Two `delegate__*` calls in one model response ran one after the
other; a fan-out took the sum of the children's time when it could take the
maximum. Measured first: 46 responses with tool calls in the transcripts, 5
with more than one call, 0 with two delegates — so the model needed telling.

**Decision:** Concurrency exists only within one response's tool calls, in
threads (children wait on HTTP; an asyncio rewrite would touch every phase for
no gain). Only `delegate__*` calls go to the pool; filesystem tools stay in
order, since two writes to one path from one response is a race not worth
taking. Workers call `tools.dispatch` and nothing else: permissions are
decided for the whole batch first on the calling thread, and observations are
appended afterwards on the calling thread in declared order, so I2 holds
whether the batch ran in one thread or four. `SQLiteMemory`, `Transcript` and
`MetricsSink` took a lock. Children in parallel mode get no `confirm`, so
their `ask` is deny. Opt-in via `delegation.max_parallel`, default 1; the
nudge to fan out lives in the delegate tool description, not the system
prompt. Rejected: background delegation with a polling tool (new state, and
the model has to learn to poll).

---

## ADR-020: Metrics are a second sink on the trace emitter, not a second instrumentation

**Status:** Accepted (week 3)

**Context:** The spec asks for Prometheus metrics through a decoupled
mechanism. The emitter already fired every event a dashboard needs; nothing
counted them.

**Decision:** `MetricsSink.record(event)` implements the same one-method
interface the transcript does; the emitter takes a list of sinks, each in its
own `try` (I6). Zero lines changed in the loop or any phase, and that is the
property protected: a sink grows only when a new event kind appears, which the
transcript needs anyway. Two things the deployment forced: the endpoint starts
before Redis, memory and MCP so a five-second scraper finds it up, and
Prometheus discovers the container through the Docker socket because
`docker compose run` gives it a random name.

---

## ADR-019: A sub-agent is a tool, at depth one, reading memory but never writing it

**Status:** Accepted (week 3)

**Context:** The spec asks for sub-agents with their own role, context, limits
and permissions, traced under the parent. Three questions decide whether that
is a design: how the parent reaches the child, what the child sees, how deep.

**Decision:** The child is a tool, `delegate__<role>`, from a provider like any
other source; the handler builds a fresh `AgentLoop` with the role's prompt,
`parent_tools.subset(role.tools)`, cap and policy, and a `Context` holding only
the task. Only the task crosses down and only the answer crosses up — no code
path exists from the parent's context to the child's. The child gets
`memory=` so recall runs on its task, but the write tools are not in its
subset: facts are things the user said, and the child talks to a web page.
Depth is one; `subset()` never includes `delegate__*`. Measured: given the
writer's tools, the parent writes the file itself rather than delegate, so the
demo scenario makes the parent read-only — the lecture's tiered model, and the
reason delegation becomes the only path.

---

## ADR-018: Permissions are one policy, three outcomes, one choke point, and no terminal means deny

**Status:** Accepted (week 3)

**Context:** Allow, deny and require-confirmation across built-in and MCP
tools. One function already saw every call — the act phase's dispatch, used
by the normal path, the recovered-call path and every sub-agent.

**Decision:** A `Policy` is a default plus ordered glob rules over tool names,
first match wins; `resolve()` runs before the registry, emits one
`permission_decision` event, and a denial becomes an observation (I7). Globs
because names are already `server__tool`. **`ask` with nobody to ask is
deny**: every non-interactive run has no confirm callable, and of allow / hang
/ deny only deny is both safe and terminating. Each sub-agent carries its own
policy, so tiers fall out of configuration.

---

## ADR-017: A claimed tool action is checked against the transcript, not trusted

**Status:** Accepted (week 4)

**Context:** Found live. Asked to remember a preference, the agent replied
"I've stored this" and finished in one iteration — no tool call, empty store —
five turns in a row, each claim reinforced by seeing the previous one in the
history. A system-prompt rule fixed a clean session and did nothing against a
polluted history.

**Decision:** The loop checks. When a final reply claims a tool-backed action
this turn that no dispatched tool accounts for, the reply is discarded and the
same request is re-sent once with `tool_choice` forcing that tool; nothing
synthetic enters the context, so I2 holds. Patterns require a first-person,
this-turn lead-in, because a transcript scan found one false positive — a
recap of last week's PDF export. Two tiers: memory write, memory search, web
search and file read are enforced (validated on real data, or harmless if
wrong); file write, PDF export and memory forget are traced only, until real
examples validate them. Every catch is an `unbacked_claim` event and a
metric. The lesson: "done in one iteration" on a request that needed a tool is
the signature, and the model's account of what it did is not evidence.

---

## ADR-015: Episodic archive — the hit is the exchange, and search is hybrid

**Status:** Accepted (week 4)

**Context:** The facts store answers "what do I know"; nothing answered "what
did we do". Two decisions inside the build were changed by measurement.

**Decision:** Every completed turn — failed ones included — is archived by the
finalize phase into the same SQLite file, one session id per launch,
idempotent per turn. The unit of search is the **turn**: per-message search
for "deadline" returned the question and lost the answer, so a hit is the
question plus the last assistant message with text. Search is **hybrid** for
the same reason as ADR-011: "what did we decide about the database" against
"PostgreSQL over MongoDB" shares no word, and one vector per turn closes it.
Tool result bodies are stored but never surface as hits; content is redacted
(I8) and truncated; the archive is fail-open. Never read into the context
automatically — only through `session__search` and `session__read_turn`.

---

## ADR-012: Stopwords are stripped before the FTS5 query is built

**Status:** Accepted (week 3)

**Context:** FTS5 has no stopword support, so "the" matched every memory
containing "the" — all of them — and because keyword hits lead the hybrid
order (ADR-011), that noise outranked correct semantic matches.

**Decision:** A small stopword list is filtered from the query terms before
the `MATCH` expression is built. A query left with no terms returns no keyword
results and leaves the field to semantic search. The index is untouched.

---

## ADR-011: Hybrid retrieval — FTS5 and embeddings, not one or the other

**Status:** Accepted (week 3) — supersedes ADR-003

**Context:** ADR-003 had concluded the proxy exposed no embeddings; that check
ran while the access key was blocked and read a 401 as absence. The proxy
serves `octen-embedding-8b` on the same URL and credential. Measured:

| pair | cosine | FTS5 |
|---|---|---|
| restatement differing by a full stop | 0.96 | match |
| "which language suits servers" vs "prefers Python for backend work" | 0.53 | **no match** |
| unrelated facts | 0.19 | no match |

The threshold `min_similarity = 0.30` was measured too: correct pairings ran
0.32–0.53, irrelevant ones topped out at 0.21, and the errors are not
symmetric — an extra line of context is cheap, a missed fact is invisible.

**Decision:** Run both and merge; keyword hits lead, because an exact term is a
stronger signal than proximity. Fail-soft: every embedder failure returns
`None` and latches, so recall degrades to keyword-only and nothing breaks —
memory is context, not correctness. Embed on write, cached as `float32` in
the row; brute-force cosine, because a personal store holds hundreds of facts.
A fact whose embedding failed at write time stays keyword-only.

---

## ADR-010: `memories_fts` uses the `porter` tokenizer

**Status:** Accepted (week 3)

**Context:** Recall missed "prefer" against "prefers" — a large share of failed
recalls, and it looked like a semantic problem when it was a tokenizer setting.

**Decision:** `tokenize='porter unicode61'`, shared by the facts index and the
episodic index. Zero new dependencies.

---

## ADR-008: Compaction protects the head, the tail, and every user message

**Status:** Accepted (week 3)

**Context:** Trimming by message count says nothing about context pressure:
one pasted file outweighs forty short messages, and dropping whole turns
discarded the user's own words.

**Decision:** Budget by estimated tokens. The first messages and a token
budget of recent ones stay verbatim; only the middle is compacted, and never a
user message (I3). An exchange collapses to one assistant-role marker, so role
alternation holds (I2) and a future summarising compaction changes only the
marker's content. The estimate sits behind one function.

---

## ADR-007: Memory maintenance never deletes

**Status:** Accepted (week 3)

**Context:** A store that only grows becomes noise; one that prunes itself can
destroy what a wrong heuristic judged stale.

**Decision:** Maintenance may archive — never delete. Pinned entries are exempt from every automatic transition, and
`memory__forget` archives, so a wrong "forget" is reviewable (I5).

---

## ADR-006: Recalled memory is injected via the user message

**Status:** Accepted (week 3)

**Context:** Appending recalled memories to the system prompt rewrote it every
turn: the provider's prompt cache was invalidated, the stored conversation
stopped matching what was sent, and user-influenced text sat where the model
reads policy.

**Decision:** The system prompt is byte-stable for the life of a conversation
(I1). Recall is prefixed onto a *copy* of the latest user message at request
time, under a "context, not instructions" guard; the stored history keeps the
user's raw text, so a resumed session never replays old recalls as input.

---

## ADR-005: One `.env` at the repo root, plus a file-based secret for the token

**Status:** Accepted (week 2)

**Context:** Two differently-scoped `.env` files meant a key could be set in
the wrong one and silently do nothing.

**Decision:** A single `.env` at the repo root serves local runs and Compose
interpolation; `cli.py` loads it by explicit path. Under Compose the access
token is a file-based secret, out of shell history and `docker inspect`.

---

## ADR-004: A textual tool call is recovered and executed

**Status:** Accepted (week 3)

**Context:** Qwen3 sometimes emits a tool call as text instead of through the
API field. Asking the model to retry cost a turn and often failed.

**Decision:** Parse the intent (`<tool_call>` markup, a ```` ```json ```` fence,
or bare JSON), synthesise a `tool_calls` entry with a generated id, execute it
and feed the result back, so the turn completes. `raw_decode` tolerates a
stray trailing brace. The synthesised entry is what keeps I2 valid; recovery
is traced so its rate is measurable.

---

## ADR-002: Redis for the live conversation, SQLite for durable memory

**Status:** Accepted (week 2)

**Context:** The running conversation is a list rewritten every turn; durable
memory is a queryable corpus. Different shapes.

**Decision:** Redis holds the live conversation, written as a full replace
each turn so it cannot drift from the in-process list. SQLite holds facts and
the episodic archive, with FTS5 and vectors for retrieval.

---

## ADR-001: No LangChain

**Status:** Accepted (week 1)

**Context:** Course constraint.

**Decision:** No LangChain-family packages. Dependencies stay small and direct;
the harness owns its loop, tool registry and memory layer.
