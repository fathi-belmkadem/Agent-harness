# Documentation

Numbered in reading order. Start at 01 for the map, read 02 to 10 for one
component each in the order a turn uses them, 11 and 12 for the rules and
the decisions behind everything. Each component doc names the files and
functions it describes, says what is deliberately not built, and points at
the ADR that records why.

## The map

| | doc | what it holds |
|---|---|---|
| 01 | [architecture](01_architecture.md) | the components and their seams, one turn as it executes, the deployment; Mermaid, renders on GitHub |

## The components

| | doc | code |
|---|---|---|
| 02 | [the ReAct loop](02_react-loop.md) | `harness/agent/loop.py`, `harness/agent/phases/` |
| 03 | [context](03_context.md) | `harness/agent/context.py`, `harness/memory/compaction.py` |
| 04 | [tools](04_tools.md) | `harness/tools/` |
| 05 | [MCP](05_mcp.md) | `harness/mcp/` |
| 06 | [memory](06_memory.md) | `harness/memory/` |
| 07 | [permissions](07_permissions.md) | `harness/tools/permissions.py` |
| 08 | [sub-agents](08_subagents.md) | `harness/agent/subagents.py` |
| 09 | [interactive terminal input](09_interactive.md) | `harness/interactive.py` |
| 10 | [observability](10_observability.md) | `harness/observability/` |

Skills, hooks and sandboxing from the lecture's list map as follows: skills
are `harness/skills.py` and the files under `skills/` (ADR-022); hooks are the
trace emitter and its sinks, under 10; sandboxing is the path resolver under
04 plus the container under 01.

## The rules and the decisions

| | doc | what it holds |
|---|---|---|
| 11 | [invariants](11_invariants.md) | eight properties the harness must not break, each with the test that enforces it, and three things deliberately not invariants |
| 12 | [ADRs](12_ADR.md) | the nineteen decisions in force, newest first, each with the measurement or the failure behind it |

## How to read the code alongside

```
harness/
├── cli.py              arguments, the REPL and one-shot mode
├── startup.py          config → a ready loop: metrics, Redis, memory, tools, MCP, sub-agents
├── console.py          status lines and warnings
├── interactive.py      editable TTY input and permission prompts
├── config.py           every config block, validated at load
├── llm_client.py       the chat-completions client
├── skills.py           SKILL.md loader and the skill__read tool
├── agent/              the loop, its phases, the context, the system prompt, sub-agents
├── memory/             facts, episodic archive, compaction, consolidation, embeddings
├── tools/              registry, protocol, built-ins, sandbox, permissions, clarify
├── mcp/                transport, client, provider
└── observability/      emitter, transcript, metrics
skills/                 procedures the model reads on demand (see skills/README.md)
```

Tests: six end-to-end runs, one per file in `tests/test_[1-6]_*.py`, each one
realistic task through `AgentLoop` with a scripted model that pins the
invariants it passes on the way; the fakes (LLM, Redis, MCP server, embedder)
are in `tests/conftest.py`.
