"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

import time

from harness.agent.loop import AgentLoop, MemorySetup
from harness.agent.subagents import SubagentProvider
from harness.config import SubagentConfig
from harness.llm_client import LLMResponse
from harness.memory import MemoryProvider, SQLiteMemory
from harness.tools import ToolRegistry
from harness.tools.permissions import Policy

from conftest import RoutedLLM, call, ctx, say, slow_tool, tool_call


RESEARCHER = SubagentConfig(name="researcher", description="Researches.", system_prompt="ROLE: research",
                            tools=("search", "memory__search"), max_iterations=2)
WRITER = SubagentConfig(name="writer", description="Writes.", system_prompt="ROLE: write", tools=("write_file",),
                        max_iterations=3, permissions=Policy(rules=(("write_file", "ask"),)))


def test_subagents_are_isolated_tools_governed_by_their_own_policy(tmp_path, transcript):
    """Two research delegations in one response run concurrently, then a
    writer. The child sees only the task, reads facts but has no write tool,
    cannot delegate; a child at its cap is an observation, not a failure; its
    events nest under the parent; its policy is its own, and `ask` with nobody
    to ask is deny (ADR-018), which is what a parallel child always is. The
    parent's own denial is an observation too (I7)."""
    memory = SQLiteMemory(tmp_path / "m.sqlite3")
    memory.store("the user prefers British spelling", kind="preference")
    tools = ToolRegistry()
    tools.register(slow_tool("search", delay=0.25))
    tools.register(slow_tool("write_file"))
    tools.add_provider(MemoryProvider(memory))
    fan_out = LLMResponse(content=None, total_tokens=1, tool_calls=[
        tool_call("delegate__researcher", {"task": "task alpha: which spelling does the user prefer"}, id_="c1"),
        tool_call("delegate__researcher", {"task": "task beta"}, id_="c2")])
    llm = RoutedLLM({
        "what is X": [fan_out, call("delegate__writer", {"task": "write it"}), call("write_file"), say("final")],
        "task alpha": [call("search"), say("alpha found")],
        "task beta": [call("search"), call("search")],           # never answers: cap is 2
        "write it": [call("write_file"), say("could not write")],
    })
    asked = []
    loop = AgentLoop(llm=llm, tools=tools, transcript=transcript, memory=MemorySetup(facts=memory),
                     max_parallel_tools=2,
                     permissions=Policy(rules=(("write_file", "deny"),)),
                     confirm=lambda name, args: asked.append(name) or True)
    tools.add_provider(SubagentProvider((RESEARCHER, WRITER), llm=llm, parent_tools=tools,
                                        parent_run_id=loop.trace.run_id, emit=loop._log, transcript=transcript,
                                        memory=memory, confirm=loop.confirm, parallel=True))
    context = ctx("what is X")

    started = time.monotonic()
    result = loop.run(context)
    elapsed = time.monotonic() - started

    assert result.final_message == "final" and not result.failed
    observations = [m["content"] for m in context.messages if m["role"] == "tool"]
    assert observations[0] == "alpha found" and "maximum of 2 iterations" in observations[1]
    assert observations[2] == "could not write" and "denied by the permission policy" in observations[3]
    assert elapsed < 0.7, "alpha (0.25 s) and beta (0.5 s) overlapped"

    child = llm.calls("task alpha")[0]
    assert child["messages"][0] == {"role": "system", "content": "ROLE: research"}
    assert len(child["messages"]) == 2 and "British spelling" in child["messages"][1]["content"]
    assert sorted(child["tools"]) == ["memory__search", "search"], "the subset, no write tool, no delegate"
    assert "delegate__researcher" not in tools.subset(["*"]).names()

    delegated = transcript.of("delegated")
    assert [(e["subagent"], e["ok"]) for e in delegated] == [("researcher", True), ("researcher", False), ("writer", True)]
    parent_id = loop.trace.run_id
    child_ids = {e["run_id"] for e in transcript.events() if e.get("parent_run_id") == parent_id}
    assert len(child_ids) == 3 and parent_id not in child_ids
    assert all(e["child_run_id"] in child_ids for e in delegated)
    starts = [e["ts"] - e["duration_ms"] / 1000 for e in delegated[:2]]
    assert max(starts) < min(e["ts"] for e in delegated[:2])
    assert [a.get("parallel") for a in transcript.of("act") if a["tool"].startswith("delegate__")] == [True, True, None]

    decisions = [d for d in transcript.of("permission_decision") if d["tool"] == "write_file"]
    child_ask = next(d for d in decisions if d.get("parent_run_id"))
    assert (child_ask["decision"], child_ask["answer"], child_ask["effective"]) == ("ask", None, "deny")
    parent_deny = next(d for d in decisions if not d.get("parent_run_id"))
    assert (parent_deny["source"], parent_deny["effective"]) == ("rule:write_file", "deny")
    assert asked == [], "nobody was prompted: the parent denies outright and children cannot ask"
    memory.close()
