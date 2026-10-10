from __future__ import annotations

import re
import time
from ..tools import Tool, tool
from .episodic import EpisodicArchive
from .sqlite_fts import MEMORY_KINDS, SQLiteMemory

_WS_RE = re.compile(r"\s+")


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _preview(text: str, width: int = 240) -> str:
    flat = _WS_RE.sub(" ", text).strip()
    return flat if len(flat) <= width else flat[: width - 1] + "…"


class MemoryProvider:
    def __init__(self, memory: SQLiteMemory) -> None:
        self._memory = memory

    @property
    def namespace(self) -> str:
        return "memory"

    def load(self) -> list[Tool]:
        return [
            tool(
                "memory__remember",
                "Store a stable user preference, project fact, or confirmed decision for "
                "future sessions. When this replaces something already remembered — the "
                "same fact with a changed value — pass the old entry's id as `supersedes` "
                "so the outdated one stops being recalled. Set `pinned` when the user "
                "says something must always be remembered: pinned entries are never "
                "archived by maintenance, however long they go unused.",
                self._remember,
                {
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                    "supersedes": {"type": "integer",
                                   "description": "Id of the memory this replaces, if any."},
                    "pinned": {"type": "boolean",
                               "description": "Exempt this entry from automatic archiving."},
                },
                ["content"],
            ),
            tool(
                "memory__search",
                "Search relevant long-term memories from earlier sessions.",
                self._search,
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                ["query"],
            ),
            tool(
                "memory__forget",
                "Stop recalling a memory that is wrong or no longer true, by id. It is "
                "archived rather than deleted, so the decision stays reviewable.",
                self._forget,
                {"memory_id": {"type": "integer"}},
                ["memory_id"],
            ),
        ]

    def _remember(self, content: str, kind: str = "fact",
                  supersedes: int | None = None, pinned: bool = False) -> str:
        entry = self._memory.store(content, kind=kind, supersedes=supersedes, pinned=pinned)
        replaced = f" (replaces #{supersedes})" if supersedes is not None else ""
        marker = ", pinned" if entry.pinned else ""
        return f"Stored memory #{entry.id}{replaced}{marker}: {entry.content}"

    def _search(self, query: str, limit: int = 5) -> str:
        entries = self._memory.search(query, limit=min(limit, 10))
        if not entries:
            return "No relevant long-term memories found."
        return "\n".join(f"#{entry.id} [{entry.kind}] {entry.content}" for entry in entries)

    def _forget(self, memory_id: int) -> str:
        entry = self._memory.forget(memory_id)
        if entry is None:
            return f"No memory #{memory_id} to forget (unknown id, or it is pinned)."
        return f"Archived memory #{memory_id}; it will no longer be recalled."


class SessionProvider:
    def __init__(self, archive: EpisodicArchive) -> None:
        self._archive = archive

    @property
    def namespace(self) -> str:
        return "session"

    def load(self) -> list[Tool]:
        return [
            tool(
                "session__search",
                "Search past conversations for what was said or decided. Returns dated "
                "excerpts with a session and turn id. Use this when the user refers to an "
                "earlier conversation ('what did we decide about X', 'last time we "
                "discussed Y') rather than to a stored fact.",
                self._search,
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                ["query"],
            ),
            tool(
                "session__read_turn",
                "Read one full exchange from a past conversation, given the session and "
                "turn ids returned by session__search.",
                self._read_turn,
                {"session_id": {"type": "string"}, "turn_id": {"type": "string"}},
                ["session_id", "turn_id"],
            ),
        ]

    def _search(self, query: str, limit: int = 5) -> str:
        hits = self._archive.search(query, limit=min(limit, 10))
        if not hits:
            return "No past conversation matches that."
        return "\n\n".join(
            f"[{_when(hit.created_at)}] session={hit.session_id} turn={hit.turn_id}\n"
            f"  user: {_preview(hit.user)}\n"
            f"  assistant: {_preview(hit.assistant) or '(no text answer)'}"
            for hit in hits
        )

    def _read_turn(self, session_id: str, turn_id: str) -> str:
        messages = self._archive.turn(session_id, turn_id)
        if not messages:
            return f"No archived turn {turn_id} in session {session_id}."
        lines = [f"[{_when(messages[0]['created_at'])}] session={session_id} turn={turn_id}"]
        for message in messages:
            label = f"tool:{message['tool_name']}" if message["tool_name"] else message["role"]
            lines.append(f"{label}: {message['content']}")
        return "\n".join(lines)
