# Observability

`harness/observability/`. How a run is recorded and how it is measured.

## One source, two sinks

```
                     TraceEmitter.emit(**event)          every phase calls this
                            │
              ┌─────────────┼──────────────┐
              ▼                            ▼
        Transcript                    MetricsSink
   .transcripts/<ts>.jsonl      prometheus_client counters
   one line per event              /metrics on :9100
                                           │
                                           ▼
                                  Prometheus ──► Grafana
                                  scrapes every 5s    provisioned dashboard
```

Everything downstream reads the same events. Nothing in the loop knows
metrics exist; adding the Prometheus exporter added zero lines to
the loop. This is what the spec's "reusable lifecycle hooks or an equivalent
decoupled instrumentation mechanism" means here.

## The emitter

`TraceEmitter` in `events.py`. Every event it writes carries:

| field | meaning |
|---|---|
| `schema_version` | `harness.trace.v1` |
| `ts` | wall time |
| `run_id` | this process, or this sub-agent invocation |
| `parent_run_id` | present only on a sub-agent's events |
| `turn_id` | one `run()` call |
| `iteration` | which loop iteration |
| `type` | the event kind |

**Redaction.** Before any sink sees a payload, `redact()` walks it and
replaces the values of every `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`
environment variable, and anything shaped like `sk-...` or `Bearer ...`. A
tool result that fetched something with the token in it never reaches disk
with the token intact. Invariant I8.

**Fail-open.** Each sink is called in its own `try`. A sink that raises is
skipped; the run continues; the other sinks still get the event. Telemetry
never takes a run down, and adding an exporter never adds a failure mode.
Invariant I6.

## Live CLI panels

The CLI attaches `ConsoleSink` (`console.py`) as another emitter sink. It
renders lifecycle, loop steps, tool and MCP calls, permissions, sub-agents,
memory/errors, and resource changes as boxed panels while the run is
executing. Panels are sent to stderr, so `--prompt --json` keeps stdout valid
JSON. Use `--no-observability` to disable the panels; transcript and
Prometheus sinks remain independent. With the panels on, the REPL's
`thinking...` line is not shown: the panels are the progress indicator.

## Event types

| type | emitted by | carries |
|---|---|---|
| `turn_started` | loop, once per turn | nothing beyond the envelope |
| `reason` | loop, per LLM call | content, tool_calls, prompt/completion/total tokens |
| `act` | act phase, per tool call | tool, tool_call_id, arguments, result, ok, duration_ms, recovered |
| `done` / `failed` / `aborted` | loop, once per turn | final_message, or error, or reason; duration_ms |
| `malformed_tool_call` | response check | what was wrong, what was recovered |
| `unbacked_claim` | loop | the claim family, whether it was enforceable |
| `permission_decision` | act phase | tool, decision, source, answer, effective |
| `delegated` | sub-agent provider | subagent, child_run_id, iterations, ok, duration_ms |
| `context_compacted` | fit-context phase | tokens and messages before/after |
| `memory_retrieved`, `memory_retrieval_skipped`, `memory_error` | recall phase | count, reason |
| `turn_archived`, `archive_error` | finalize | messages stored |
| `consolidated`, `consolidation_error` | finalize | turns, stored, superseded, rejected, archived |
| `session_save_error` | finalize | error |

## The transcript

`Transcript` in `transcript.py`: one JSONL file per process under
`.transcripts/`, one event per line, `record()` appends. A sub-agent shares
its parent's file, distinguished by `run_id` and `parent_run_id`, so one file
holds the whole tree of a session. This is the spec's per-run structured
trace: any single run can be reconstructed from it, including which child
did what under which parent.

## Metrics

`MetricsSink` in `metrics.py` counts events into `prometheus_client`
metrics; `serve(port)` starts the `/metrics` endpoint on a background
thread. Configured by `observability.metrics_port`.

Each metric answers one question, and the dashboard shows each question once.

| metric | labels | from | answers |
|---|---|---|---|
| `harness_runs_active` | agent | `turn_started` up, terminal event down | what is running now |
| `harness_runs_total` | outcome, agent | terminal events | how turns end |
| `harness_turn_seconds` | agent | terminal `duration_ms` | how long a turn takes |
| `harness_llm_calls_total` | agent | `reason` | model round-trips per turn |
| `harness_llm_tokens_total` | direction | `reason` token fields | cost |
| `harness_tool_calls_total` | tool, status | `act` | which tools run, which fail |
| `harness_tool_seconds` | tool | `act.duration_ms` | which tools are slow |
| `harness_permission_decisions_total` | scope, decision, effective | `permission_decision` | what the policy blocked |
| `harness_memory_recalls_total` | result: hit, miss, skipped | `memory_retrieved`, `memory_retrieval_skipped` | does recall find anything |
| `harness_memory_facts_total` | result: stored, superseded, rejected, archived | `consolidated` | how the fact store changes |
| `harness_model_issues_total` | kind: `malformed_tool_call` or a claim family | `malformed_tool_call`, `unbacked_claim` | what the model got wrong and the harness caught |
| `harness_errors_total` | kind | `memory_error`, `archive_error`, `consolidation_error`, `session_save_error` | what broke and was survived |

`agent` is `parent` or `subagent`, from whether the event carries
`parent_run_id`. A sub-agent is not counted separately: its turns are the
`agent="subagent"` series, and each delegation is a `delegate__<name>` tool
call.

Turns are timed by the loop itself, from `run()` to the terminal event, so a
turn whose first model call fails is still counted and timed. Every closed
label set is exposed at 0 from startup, and a tool's `error` series exists
from its first call: a series that first appears at 1 is invisible to
`increase()`, so a single rare error would otherwise never show.

Metrics live in the process. A `--prompt` run starts, answers and exits, and
Prometheus may never scrape it. **The demo runs the REPL** and stays up.

## Prometheus and Grafana

Two Compose services, configured under `observability/` at the repo root,
and both dependencies of the harness service, so `docker compose run
agent-harness` brings the whole stack up, including after a `down`.

Prometheus scrapes the harness **by container name**. The scrape config is
a single static target, `agent-harness:9100`, polled every five seconds; no
Docker socket, no root. Docker's embedded DNS resolves a container's name
on the Compose network, so the container must actually be called
`agent-harness`. Under `docker compose up` the Compose file's
`container_name` does that. `docker compose run` ignores `container_name`
(and network aliases, and service DNS) and names its container randomly,
so a `run` must pass the name itself:

```bash
docker compose run --rm --name agent-harness agent-harness
```

Without `--name` the harness runs fine but the target never resolves and
Grafana shows "No data". The metrics endpoint starts first thing
in `startup.build_harness`, before Redis, memory or the MCP servers, so a scraper finds
it up from the first seconds.

Grafana is provisioned with the datasource and one dashboard, anonymous
viewer enabled, on `localhost:3000`. Four headline numbers (active turns,
success rate, LLM calls per turn, tokens) and nine panels, one per metric
above: turn outcomes, turn latency, tool calls, tool latency, permission
decisions, memory recall, facts from consolidation, model issues caught,
internal errors. The last three plot only non-zero values, so an empty panel
means nothing went wrong. Each panel carries a description of what it shows.

## Where the decisions are recorded

ADR-020 in `docs/12_ADR.md`. Invariants I6 and I8. Test:
`tests/test_1_turn.py::test_a_turn_runs_tools_recalls_memory_and_is_traced` (trace and metrics).
