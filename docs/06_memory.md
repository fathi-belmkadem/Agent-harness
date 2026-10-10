# Memory

The agent has three kinds of memory, matching the three layers in the course
notes: short-term (the running conversation), long-term facts (what it
knows), and episodic (what happened). All three live in `harness/memory/`.
The code there carries no comments; this document is where the reasoning
lives. Every function named below exists under that name.

```
harness/memory/
├── session_store.py   short-term: the live conversation, in Redis
├── compaction.py      short-term: keeping the conversation inside the token budget
├── sqlite_fts.py      long-term facts: SQLite + FTS5 + vectors, and their maintenance
├── episodic.py        episodic: every past turn, searchable
├── summarize.py       episodic turns → long-term facts, extracted by a model
├── tools.py           the five memory__* and session__* tools the model calls
├── embeddings.py      vectors from the innkube proxy, shared by facts and episodic
└── retrieval.py       search machinery shared by facts and episodic
```

One SQLite file, `.memory/memory.sqlite3`, holds both the facts and the
episodic archive. Redis holds the live conversation.

---

## How a turn uses memory

The agent loop runs five phases per turn. Three of them touch memory.

| phase | function | what it does |
|---|---|---|
| fit context | `context_fit.fit_context` | compacts the conversation if it is over the token budget |
| recall | `recall.build_recall_block` | searches long-term facts and prefixes the hits onto the user's message |
| finalize | `finalize.save_session` | writes the conversation to Redis |
| finalize | `finalize.archive_turn` | appends the finished turn to the episodic archive |
| finalize | `finalize.maybe_consolidate` | extracts facts from unsummarized turns every N turns, then archives stale ones |

The model also has five tools it can call itself: `memory__remember`,
`memory__search`, `memory__forget`, `session__search`, `session__read_turn`.

---

## Short-term memory

### The live conversation — `session_store.py`

`RedisSessionStore(client, session_id)` holds the conversation for one
session id. `load()` returns the message list; `save(messages)` replaces it
whole. `connect(url)` opens the Redis client.

Redis was chosen over SQLite for this because the conversation is rewritten
on every turn, and a full-replace of a list is what Redis does cheaply. If
Redis is unreachable at startup the harness still runs with a fresh,
non-persistent conversation. A failed save is logged as `session_save_error`
and never fails the turn.

`--new-session` starts fresh instead of loading the stored conversation.

### Fitting the context window — `compaction.py`

`compact(messages, budget_tokens, tail_token_budget, protect_head_messages, system_prompt)`
shrinks the conversation in place when it exceeds the budget, and returns a
`CompactionResult` with token and message counts before and after.

The budget is in tokens, not messages. Five messages carrying a pasted file
blow the window while forty short ones do not. Tokens are estimated by
`estimate_tokens` as characters divided by four, plus a small per-message
overhead. Crude, but it sits behind one function so a real tokenizer can
replace it without touching callers.

Three rules decide what may be compacted:

- **The head is protected.** The first `protect_head_messages` messages stay
  verbatim. Default 2.
- **The tail is protected.** The most recent `tail_token_budget` tokens stay
  verbatim. Default 6000.
- **User messages are never compacted, anywhere.** Only the assistant's work
  and tool results collapse. The user's instructions are the intent
  everything else derives from; paraphrasing "use the existing helper, don't
  add one" is how an agent confidently does the opposite six turns later.

The oldest eligible exchange in the middle, meaning one assistant message
and the tool results that followed it, is replaced by a single marker message
until the budget is met. `is_marker(message)` recognises one by its
`[compacted:` prefix, so a resumed session does not compact the same span
twice. The marker carries the assistant role so role alternation holds.

A marker is a placeholder, not a summary. The content of a compacted
exchange is gone rather than condensed. Doing better needs a second model
call and is held until an auxiliary-model abstraction exists.

Config: `session.context_budget_tokens` (24000), `compaction.protect_head_messages`,
`compaction.tail_token_budget`. Trace event: `context_compacted`.

---

## Long-term facts — `sqlite_fts.py`

### What is stored

A fact is one sentence the agent decided was worth keeping: a preference, a
project decision, a correction. Ordinary conversation is not stored as
facts; that is what the episodic archive is for.

`MemoryEntry` fields:

| field | meaning |
|---|---|
| `id` | row id; returned to the model so it can act on a specific entry |
| `content` | the fact, whitespace-normalised |
| `kind` | one of `preference`, `fact`, `decision`, `project_context`, `instruction` |
| `status` | `active`, `superseded`, or `archived`; only `active` is ever recalled |
| `last_accessed_at` | when it was last recalled; drives archiving |
| `pinned` | exempt from every automatic transition |
| `supersedes` | id of the entry this one replaced |
| `created_at`, `updated_at` | timestamps |

There is no importance score. Whether a fact is still useful is read from
one signal only, usage: a fact that keeps getting recalled stays, one
nobody recalls ages out. An importance field the model sets, and that
maintenance then nudged up and down, was a second answer to the same
question and was removed.

### `SQLiteMemory`

`SQLiteMemory(path, max_entry_length, embeddings, min_similarity)` opens or
creates the store with `CREATE TABLE IF NOT EXISTS`. Entries are built from
named columns, so a file carrying extra columns from an older build still
opens and works.

- `store(content, kind, supersedes, pinned)` inserts or updates. The same
  content under the same kind is one row: re-storing bumps `updated_at`,
  keeps `pinned` once set, and reactivates an archived entry. If `supersedes`
  names an older entry, that entry becomes `superseded` unless it is pinned.
  The fact is committed first and embedded second, so an embedding failure
  never costs the fact; without a vector it is keyword-only.
- `search(query, limit)` is hybrid. Keyword hits come first, then semantic
  hits fill the remaining slots, deduplicated. Only `active` entries. Every
  hit's `last_accessed_at` is set.
- `archive_stale(older_than_days)` is all of maintenance, described below.
- `get(id)`, `forget(id)`, `all_entries(status)`.

Nothing is ever deleted. `forget` archives. Maintenance archives. A wrong
automated decision costs recall, not data.

### Why search is hybrid

Keyword search uses FTS5 with the `porter` stemming tokenizer, so "prefer"
matches "prefers". Stemming is not meaning: "which language suits servers"
shares no stem with "prefers Python for backend work" and keyword search
returns nothing. Measured against `octen-embedding-8b`, that pair scores
0.527 cosine while unrelated facts score around 0.19. So both searches run.

Keyword hits lead because an exact match on a filename or version string is
a stronger signal than proximity in embedding space, and vectors blur exact
identifiers. Semantic hits fill the rest.

The similarity floor is `min_similarity`, default 0.30. Measured on this
project's own facts: irrelevant queries topped out at 0.21, correct matches
ran 0.32 to 0.53. It sits near the low end on purpose. An extra marginal
memory is one line of context the model can ignore; a missed one is
invisible.

If the embedding endpoint is down, search falls back to keyword-only and
nothing breaks.

### The tools — `MemoryProvider` in `tools.py`

| tool | arguments | behaviour |
|---|---|---|
| `memory__remember` | `content`, `kind`, `supersedes`, `pinned` | store a fact; returns the id |
| `memory__search` | `query`, `limit` | hybrid search; returns `#id [kind] content` lines |
| `memory__forget` | `memory_id` | archive an entry; refuses if pinned |

The id is in the search output so the model can supersede or forget a
specific entry afterwards.

**A claim to remember is enforced.** If the model replies "I've stored this"
without having called `memory__remember` in the turn, the loop discards the
reply and re-asks once with the tool call forced through the API. Found live:
five turns of that claim with an empty store. The same check covers searches,
reads, writes and exports; see ADR-017 for which are enforced and which are
traced only. Event: `unbacked_claim`.

**A claim to recall, e.g. "I remember ...", is checked too**, but it can't be
gated on `called` the way the others are: a true recall is usually backed by
the *automatic* recall block, not a tool call, since `build_recall_block`
already injects hits before the model sees the prompt. So this family is
skipped whenever this turn's recall block was non-empty, and otherwise
behaves like the rest — unbacked, it forces a real `memory__search` instead
of letting the model narrate facts nobody retrieved. Found live: asked "who
am I" against an empty `memories` table (`memory_retrieved count: 0`, no
tool call at all), the model answered with a fabricated personal profile.

### Automatic recall — `phases/recall.py`

Every turn, `build_recall_block` searches facts with the latest user message
as the query and formats the hits into a block that is prefixed onto a
*copy* of that user message at request time. Three properties follow:

- The system prompt is byte-identical every turn, so the provider's prompt
  cache stays warm.
- The stored conversation keeps the user's raw text; recalled facts never
  accumulate into history.
- The block states that it is background context, not instructions.

`is_trivial_prompt` skips the search for "ok", "thanks", "hi" and the like.
Those carry no query signal, and searching on them drags in stale context.
Trace events: `memory_retrieved`, `memory_retrieval_skipped`, `memory_error`.

### Maintenance — `summarize.py` and `SQLiteMemory.archive_stale`

After a turn, once `every_n_turns` archived turns are unsummarized,
`finalize.maybe_consolidate` runs two steps. First, `summarize_turns` sends
up to `max_turns_per_pass` of the oldest unsummarized turns to a model,
forcing one `save_facts` call; a fact is only stored if its quoted evidence
is found verbatim in the user's own words from those turns, which is what
keeps an invented fact out — not the prompt. Then `archive_stale(archive_after_days)`
runs as before: one rule, one SQL statement, one commit — every unpinned
active fact nobody has stored or recalled in `archive_after_days` (default
45) is archived. Pinned entries are exempt. Nothing is deleted.

Duplicates are handled at write time, not by maintenance, in two ways that
do not overlap:

- **Identical** facts (same kind, same text after whitespace normalising)
  are one row, by content hash. Re-storing refreshes the row.
- **Changed** facts ("now Python 3.8, not 3.7") are the model's call,
  through `memory__remember(supersedes=id)`. Only the model knows which one
  is true now: "prefers Python 3.7" and "prefers Python 3.8" score 0.853
  cosine, so similarity alone would say they are the same fact.

An earlier third mechanism, a lexical near-duplicate merge in maintenance,
only caught rewordings that differ by a word or punctuation. It was
removed; such rewordings simply age out through archiving like anything
else nobody recalls.

Trace events: `consolidated` (with `turns`, `stored`, `superseded`,
`rejected`, `archived`), `consolidation_error`.

---

## Episodic memory — `episodic.py`

### What is stored

Every completed turn, including failed ones. What went wrong is exactly what
the next session may need to find. A turn is the user's message and
everything the assistant did in response: its messages, its tool calls by
name, and tool results.

Each launch of the harness gets one session id. Turns are filed under it.

| table | columns |
|---|---|
| `messages` | `session_id`, `turn_id`, `role`, `content`, `tool_name`, `created_at` |
| `messages_fts` | full-text index over `content` |
| `turn_vectors` | one embedding per turn, over the question plus the answer |

Content is redacted on the way in with the same rules as the transcript, and
truncated at `max_message_length` (4000). The archive outlives the
transcript, and it is for finding a conversation, not replaying one.

### `EpisodicArchive`

- `record_turn(session_id, turn_id, messages)` appends one turn. Idempotent:
  the same turn id twice is a no-op, so a retry cannot double the archive.
  Embeds the exchange if a client is configured.
- `search(query, limit)` returns `EpisodicHit` objects, **one per turn**,
  each carrying the user's question and the last assistant message with
  text. Hybrid, same policy as facts. The keyword side groups matching
  messages by turn in SQL and ranks each turn by its best message
  (FTS5 `rank`), so exactly `limit` distinct turns come back.
- `turn(session_id, turn_id)` returns every archived message of one turn,
  including tool rows.

### Why the hit is a whole exchange

The first version indexed messages and returned the best-matching one. For
"deadline" against "deadline?" / "we agreed on Friday", the top hit was the
question, because it was the one message with the word in it, and the
answer was not returned at all. A question and its answer are only useful
together.

### Why search is hybrid here too

Tested across two separate launches: session one said "let's go with
PostgreSQL over MongoDB"; session two asked "what did we decide about the
database". The agent searched and reported nothing found. The word
"database" never appears in the exchange. With one vector per turn the same
query returns the exchange, and the agent answers with the decision and the
date.

Tool result bodies are archived but never surface as search hits. A match
inside a file the agent once read is not a memory of what happened; it is
noise that outranks the answer.

### The tools — `SessionProvider` in `tools.py`

| tool | arguments | behaviour |
|---|---|---|
| `session__search` | `query`, `limit` | dated exchanges with session and turn ids |
| `session__read_turn` | `session_id`, `turn_id` | the full turn, tool rows included |

The archive is never read into the context automatically. Only through
these tools, on request, so it can grow without bounding anything.

Trace events: `turn_archived`, `archive_error`. Archiving is fail-open.

---

## Shared machinery

### `embeddings.py`

`EmbeddingClient(base_url, model, access_token)` calls the same innkube
proxy as the chat model, with the same credential. Model:
`octen-embedding-8b`, 4096 dimensions.

- `embed(text)` returns one vector, or `None`.
- Every failure returns `None` and sets `available = False`, so a dead
  endpoint is not retried on every store and search. The stores fall back to
  keyword-only.

`pack` and `unpack` convert between a vector and the float32 blob stored in
SQLite: about 16 KB per vector against 90 KB as text. `cosine` is the
similarity, returning 0.0 for mismatched or empty vectors rather than
raising.

### `retrieval.py`

The facts store and the archive search the same way. This module is the one
copy of each step:

- `is_trivial_prompt(text)`: the gate described under automatic recall.
- `match_query(text)`: builds the FTS5 `MATCH` expression, dropping
  stopwords. FTS5 has no stopword support, and without this "the" matched
  every memory containing "the", which is all of them; that noise then
  outranked real semantic hits. A query of only stopwords returns `None`.
- `open_db(path, schema, shared)`: creates the file and applies a store's
  schema; `shared` for the facts store, which worker threads use behind a lock.
- `fts_sync_ddl(base, fts, columns)`: the virtual table plus the three
  triggers that keep it in sync with its content table.
- `embed_blob(embeddings, text)`: a text as a stored vector, or `None`.
- `semantic_search(embeddings, query, candidates, threshold, limit)`: cosine
  over every candidate, filtered and sorted; empty without an embedder.
  Brute force is the right shape at hundreds of entries.
- `merge_hybrid(keyword, semantic, limit)`: keyword keys first, semantic keys
  to fill, deduplicated. `semantic` is a callable so the embedding round-trip
  is skipped when keyword search already filled the limit.

Constants: `FTS_TOKENIZER = "porter unicode61"`, `STOPWORDS`, `TOKEN_RE`.

---

## Configuration

```yaml
session:
  enabled: true
  url: "redis://redis:6379/0"
  session_id: "default"
  context_budget_tokens: 24000

compaction:
  protect_head_messages: 2
  tail_token_budget: 6000

memory:
  enabled: true
  path: ".memory/memory.sqlite3"
  retrieval_limit: 5
  max_entry_length: 2000
  embeddings:
    enabled: true
    model: "octen-embedding-8b"
    min_similarity: 0.30
  episodic:
    enabled: true
    max_message_length: 4000
  consolidation:
    enabled: true
    every_n_turns: 5
    max_turns_per_pass: 20
    model: "qwen3-next-80b-a3b-instruct"
    archive_after_days: 45
```

## Where the decisions are recorded

`docs/12_ADR.md`: ADR-002 (Redis and SQLite), ADR-006 (recall on the user
message), ADR-007 (never delete), ADR-008 (compaction rules), ADR-010
(porter tokenizer), ADR-011 (hybrid search), ADR-012 (stopwords), ADR-015
(episodic archive), ADR-024 (one usefulness signal, no automatic merging).
`docs/11_invariants.md`: I1, I3, I5, I8.
