"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

from prometheus_client import CollectorRegistry

from harness.agent.loop import AgentLoop, MemorySetup
from harness.llm_client import LLMResponse
from harness.memory import EpisodicArchive, SQLiteMemory
from harness.memory.session_store import RedisSessionStore
from harness.observability.events import SCHEMA_VERSION
from harness.observability.metrics import MetricsSink
from harness.tools import ToolRegistry

from conftest import PYTHON_FACT, ExplodingSink, FakeRedis, ScriptedLLM, ctx, say, tool_call


def test_a_turn_runs_tools_recalls_memory_and_is_traced(workspace, transcript, tmp_path, monkeypatch):
    """One task, two turns: a good call, a malformed sibling, a failing read and
    a tool call written as text all in one run. Recall rides a copy of the user
    message and the system prompt never changes (I1); every declared id gets a
    result (I2); failures are observations (I7); the transcript is redacted (I8),
    carries the v1 envelope, feeds the metrics; the session and the archive
    receive the turn; a sink that raises costs nothing (I6)."""
    monkeypatch.setenv("AGENT_ACCESS_TOKEN", "sk-live-key-must-not-leak")
    memory = SQLiteMemory(tmp_path / "m.sqlite3")
    memory.store(PYTHON_FACT, kind="preference")
    archive = EpisodicArchive(tmp_path / "m.sqlite3")
    redis, registry = FakeRedis(), CollectorRegistry()
    sink = MetricsSink(registry=registry)
    tools = ToolRegistry(sandbox_root=workspace)
    llm = ScriptedLLM([
        LLMResponse(content=None, total_tokens=10, prompt_tokens=8, completion_tokens=2, tool_calls=[
            tool_call("write_file", {"path": "a.py", "content": "x = 1"}, id_="c1"),
            {"id": "c2", "function": {}},                                      # no name: dropped, traced
            tool_call("read_file", {"path": "missing.txt"}, id_="c3")]),      # fails: observation
        say('<tool_call>\n{"name": "create_folder", "arguments": {"path": "out"}}\n</tool_call>'),
        say("Done. I used sk-live-key-must-not-leak to do it."),
        say("Second answer"),
    ])
    loop = AgentLoop(llm=llm, tools=tools, transcript=transcript, extra_sinks=(sink,),
                     memory=MemorySetup(facts=memory, episodic=archive, session_id="s1",
                                        session_store=RedisSessionStore(redis, "s1")))
    context = ctx("which language should I use for backend work?", system="SYSTEM")

    result = loop.run(context)
    context.add_user("and for which kind of work?")
    loop.run(context)

    assert result.final_message.startswith("Done.") and not result.failed
    assert (workspace / "a.py").read_text() == "x = 1" and (workspace / "out").is_dir()

    first, second = llm.calls[0]["messages"], llm.calls[3]["messages"]
    assert first[0] == second[0] == {"role": "system", "content": "SYSTEM"}
    assert "prefers Python" in first[-1]["content"]
    assert first[-1]["content"].endswith("which language should I use for backend work?")
    assert [m["content"] for m in context.messages if m["role"] == "user"] == [
        "which language should I use for backend work?", "and for which kind of work?"]

    declared = {c["id"] for m in context.messages if m.get("tool_calls") for c in m["tool_calls"]}
    answered = {m["tool_call_id"] for m in context.messages if m["role"] == "tool"}
    assert declared == answered and "c2" not in declared

    acts = transcript.of("act")
    assert [(a["tool"], a["ok"]) for a in acts] == [("write_file", True), ("read_file", False), ("create_folder", True)]
    assert acts[2]["recovered"] is True and isinstance(acts[0]["duration_ms"], float)
    assert len(transcript.of("malformed_tool_call")) == 2

    assert "sk-live-key-must-not-leak" not in transcript.path.read_text(encoding="utf-8")
    events = transcript.events()
    assert all(e["schema_version"] == SCHEMA_VERSION and e["run_id"] == loop.trace.run_id for e in events)
    assert len({e["turn_id"] for e in events}) == 2 and transcript.of("memory_retrieved")[0]["count"] == 1

    assert RedisSessionStore(redis, "s1").load() == context.messages
    archived = archive._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert archived == len(context.messages) and archive.search("backend")
    def metric(name, **labels):
        return registry.get_sample_value(name, labels)

    assert metric("harness_llm_calls_total", agent="parent") == 4
    assert metric("harness_runs_total", outcome="done", agent="parent") == 2
    assert metric("harness_runs_active", agent="parent") == 0, "every started turn ended"
    assert metric("harness_turn_seconds_count", agent="parent") == 2, "timed from the start, not the first reply"
    assert metric("harness_tool_calls_total", tool="write_file", status="ok") == 1
    assert metric("harness_tool_calls_total", tool="read_file", status="error") == 1
    assert metric("harness_tool_calls_total", tool="write_file", status="error") == 0, "exists before its first error"
    assert metric("harness_llm_tokens_total", direction="prompt") == 8
    assert metric("harness_model_issues_total", kind="malformed_tool_call") == 2
    assert metric("harness_model_issues_total", kind="memory_write") == 0, "rare series exist at 0"
    hits = sum(1 for e in transcript.of("memory_retrieved") if e["count"])
    assert metric("harness_memory_recalls_total", result="hit") == hits >= 1

    broken = AgentLoop(llm=ScriptedLLM([say("fine")]), tools=ToolRegistry(), transcript=ExplodingSink(),
                       memory=MemorySetup(session_store=type("S", (), {"save": lambda self, m: 1 / 0})()))
    assert broken.run(ctx("go")).final_message == "fine"
    memory.close()
    archive.close()
