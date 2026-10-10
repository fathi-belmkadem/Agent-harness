import json
import re
from dataclasses import dataclass

from .episodic import EpisodicArchive
from .sqlite_fts import MEMORY_KINDS, SQLiteMemory

MAX_FACTS_PER_PASS = 10
KNOWN_FACTS_SHOWN = 10

_SYSTEM = (
    "You maintain an assistant's long-term memory about one user. Read the "
    "conversation excerpts and extract only durable facts the USER stated or "
    "explicitly confirmed: who they are, their background and goals, "
    "preferences, decisions, corrections, standing instructions, facts about "
    "their project. Write each fact in the third person about the user "
    "('The user lives in Passau'), never as 'I'. Never extract what the "
    "assistant claimed on its own, tool output, one-off tasks, questions or "
    "small talk. Skip anything already in the known facts. If a new fact "
    "changes the value of a known fact, set `supersedes` to that fact's id. "
    "Each fact must quote, word for word, the user's words it comes from, in "
    "`evidence`. Call save_facts exactly once; an empty list is a fine answer."
)

SAVE_FACTS = {
    "type": "function",
    "function": {
        "name": "save_facts",
        "description": "Save the durable facts found in the conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "One self-contained sentence in the third person, starting 'The user'."},
                            "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                            "evidence": {"type": "string", "description": "The user's exact words."},
                            "supersedes": {"type": ["integer", "null"]},
                        },
                        "required": ["content", "kind", "evidence"],
                    },
                },
            },
            "required": ["facts"],
        },
    },
}


@dataclass(frozen=True)
class SummaryResult:
    turns: int = 0
    stored: int = 0
    superseded: int = 0
    rejected: int = 0


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def summarize_turns(
    archive: EpisodicArchive, memory: SQLiteMemory, llm, *, max_turns: int = 20,
) -> SummaryResult:
    """Raises if the model call fails, so the turns stay unsummarized and the
    next pass retries them."""
    turns = archive.unsummarized_turns(max_turns)
    if not turns:
        return SummaryResult()
    user_text = "\n".join(hit.user for _, hit in turns if hit.user)
    known = memory.search(user_text, limit=KNOWN_FACTS_SHOWN) if user_text else []
    excerpts = "\n\n".join(f"user: {hit.user}\nassistant: {hit.assistant}" for _, hit in turns)
    known_block = "\n".join(f"#{e.id} [{e.kind}] {e.content}" for e in known) or "(none)"

    response = llm.chat(
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": f"Known facts:\n{known_block}\n\nConversation:\n{excerpts}"}],
        tools=[SAVE_FACTS],
        tool_choice={"type": "function", "function": {"name": "save_facts"}},
    )
    try:
        facts = json.loads(response.tool_calls[0]["function"]["arguments"])["facts"]
        if not isinstance(facts, list):
            raise ValueError("facts is not a list")
    except (TypeError, IndexError, KeyError, ValueError):
        # A malformed answer is not retried: the same turns would likely get
        # the same answer, and would block every later pass.
        archive.mark_summarized(turns[-1][0])
        return SummaryResult(turns=len(turns), rejected=1)

    said = _normalise(user_text)
    shown = {e.id for e in known}
    stored = superseded = rejected = 0
    for fact in facts[:MAX_FACTS_PER_PASS]:
        if not isinstance(fact, dict):
            rejected += 1
            continue
        evidence = _normalise(str(fact.get("evidence") or ""))
        supersedes = fact.get("supersedes")
        if not evidence or evidence not in said or (supersedes is not None and supersedes not in shown):
            rejected += 1
            continue
        try:
            memory.store(str(fact.get("content", "")), kind=fact.get("kind", "fact"), supersedes=supersedes)
        except ValueError:
            rejected += 1
            continue
        stored += 1
        superseded += supersedes is not None
    rejected += max(0, len(facts) - MAX_FACTS_PER_PASS)

    archive.mark_summarized(turns[-1][0])
    return SummaryResult(turns=len(turns), stored=stored, superseded=superseded, rejected=rejected)
