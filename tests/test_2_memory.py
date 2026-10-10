"""One end-to-end run through the harness with a scripted model. See docs/11_invariants.md for the I-numbers."""

import time

from harness.agent.loop import AgentLoop, MemorySetup
from harness.config import ConsolidationConfig
from harness.memory import EpisodicArchive, MemoryProvider, SQLiteMemory
from harness.memory.sqlite_fts import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_SUPERSEDED
from harness.memory.tools import SessionProvider
from harness.tools import ToolRegistry

from conftest import PYTHON_FACT, SERVERS_QUERY, FakeEmbedder, ScriptedLLM, call, ctx, say

def age(memory, memory_id, *, days):
    when = time.time() - days * 86400
    memory._db.execute("UPDATE memories SET updated_at = ?, last_accessed_at = NULL WHERE id = ?", (when, memory_id))
    memory._db.commit()


def roles(context):
    return [m["role"] for m in context.messages]


def test_memory_is_written_only_by_tools_and_maintained_without_deleting(tmp_path, transcript):
    """A claimed write becomes a real tool call (ADR-017); recall is hybrid and
    degrades to keyword-only when the embedder dies; supersede, pin and forget
    through the tools; maintenance archives what goes unused but never
    deletes and never touches a pinned entry (I5); a failing maintenance pass
    cannot fail the run; a decision from one launch is found by the next
    through the archive (ADR-015)."""
    embedder = FakeEmbedder()
    memory = SQLiteMemory(tmp_path / "m.sqlite3", embeddings=embedder)
    tools = ToolRegistry()
    tools.add_provider(MemoryProvider(memory))
    claim = "Yes — I've stored this as a durable preference: you prefer Python."
    llm = ScriptedLLM([say(claim), call("memory__remember", {"content": PYTHON_FACT, "kind": "preference"}), say("Stored.")])
    context = ctx("remember that I prefer Python for backend work")

    AgentLoop(llm=llm, tools=tools, transcript=transcript, memory=MemorySetup(facts=memory)).run(context)

    assert llm.calls[1]["tool_choice"]["function"]["name"] == "memory__remember"
    assert "tool_choice" not in llm.calls[0] and "tool_choice" not in llm.calls[2]
    assert claim not in [m.get("content") for m in context.messages] and roles(context) == ["user", "assistant", "tool", "assistant"]
    assert transcript.of("unbacked_claim")[0]["claim"] == "memory_write"
    entry, = memory.all_entries()

    assert memory._keyword_search(SERVERS_QUERY, 5) == []
    assert [e.content for e in memory.search(SERVERS_QUERY)] == [PYTHON_FACT], "found by meaning, not by words"
    embedder.fails = True
    assert memory.search("Python") and memory.search(SERVERS_QUERY) == [], "keyword-only floor, nothing breaks"
    assert memory.get(entry.id).last_accessed_at is not None, "recall counts as use"

    replaced = tools.dispatch("memory__remember", {"content": "I prefer Go for backend work", "kind": "preference",
                                                   "supersedes": entry.id})
    assert f"replaces #{entry.id}" in replaced.content and memory.get(entry.id).status == STATUS_SUPERSEDED
    assert [e.content for e in memory.search("backend")] == ["I prefer Go for backend work"]
    pinned = memory.store("always use British spelling", kind="instruction", pinned=True)
    wrong = memory.store("This turned out to be wrong.", kind="fact")
    assert f"#{wrong.id}" in tools.dispatch("memory__forget", {"memory_id": wrong.id}).content
    assert memory.get(wrong.id).status == STATUS_ARCHIVED and memory.search("turned out wrong") == []

    again = memory.store("  always use   British spelling ", kind="instruction")
    assert again.id == pinned.id and again.pinned, "same text is one row, and a pin is never lost"

    recent = memory.store("A fact still in use.", kind="fact")
    stale = memory.store("Nobody ever asks about this.", kind="fact")
    for mid, days in ((recent.id, 5), (stale.id, 90), (pinned.id, 400)):
        age(memory, mid, days=days)
    before = len(memory.all_entries())

    assert memory.archive_stale(older_than_days=45) == 1
    assert memory.get(stale.id).status == STATUS_ARCHIVED and memory.get(recent.id).status == STATUS_ACTIVE
    assert memory.get(pinned.id).status == STATUS_ACTIVE, "pinned is never archived (I5)"
    assert memory.forget(pinned.id) is None and len(memory.all_entries()) == before, "I5: nothing deleted"
    durable = SQLiteMemory(tmp_path / "m.sqlite3")
    assert durable.get(stale.id).status == STATUS_ARCHIVED, "the archive is committed, not just visible"
    durable.close()

    consolidation_archive = EpisodicArchive(tmp_path / "m.sqlite3")
    consolidation_llm = ScriptedLLM([call("save_facts", {"facts": []})])
    maintained = AgentLoop(llm=ScriptedLLM([say("ok")]), tools=ToolRegistry(), transcript=transcript,
                           memory=MemorySetup(facts=memory, episodic=consolidation_archive,
                                              consolidation_llm=consolidation_llm,
                                              consolidation=ConsolidationConfig(every_n_turns=1)))
    maintained.run(ctx("anything"))
    consolidated = transcript.of("consolidated")[-1]
    assert consolidated["turns"] == 1 and consolidation_archive.unsummarized_count() == 0
    memory.close()
    assert maintained.run(ctx("still fine")).final_message == "ok", "a broken store cannot fail the run"
    consolidation_archive.close()

    archive = EpisodicArchive(tmp_path / "m.sqlite3")
    AgentLoop(llm=ScriptedLLM([say("Acknowledged, we'll proceed with PostgreSQL")]), tools=ToolRegistry(),
              memory=MemorySetup(episodic=archive, session_id="launch-1")).run(ctx("let's go with PostgreSQL over MongoDB"))
    archive.close()
    archive = EpisodicArchive(tmp_path / "m.sqlite3")  # the next launch
    session = ToolRegistry()
    session.add_provider(SessionProvider(archive))
    hit = session.dispatch("session__search", {"query": "PostgreSQL"}).content
    assert "user: let's go with PostgreSQL" in hit and "session=launch-1" in hit
    assert "No past conversation" in session.dispatch("session__search", {"query": "sourdough"}).content
    archive.close()
