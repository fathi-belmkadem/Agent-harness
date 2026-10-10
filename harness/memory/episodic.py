from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..observability.events import redact
from .embeddings import EmbeddingClient
from .retrieval import embed_blob, fts_sync_ddl, match_query, merge_hybrid, open_db, semantic_search

_SEARCHABLE_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class EpisodicHit:
    session_id: str
    turn_id: str
    created_at: float
    user: str
    assistant: str


class EpisodicArchive:
    def __init__(
        self,
        path: Path,
        *,
        max_message_length: int = 4000,
        embeddings: EmbeddingClient | None = None,
        min_similarity: float = 0.30,
    ) -> None:
        self.max_message_length = max_message_length
        self.embeddings = embeddings
        self.min_similarity = min_similarity
        self._db = open_db(path, schema="""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_name TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_turn ON messages(session_id, turn_id);
            CREATE TABLE IF NOT EXISTS turn_vectors (
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                embedding BLOB NOT NULL,
                PRIMARY KEY (session_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """ + fts_sync_ddl("messages", "messages_fts", ("content",)))

    def record_turn(self, session_id: str, turn_id: str, messages: list[dict]) -> int:
        if not messages:
            return 0
        already = self._db.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND turn_id = ? LIMIT 1",
            (session_id, turn_id),
        ).fetchone()
        if already is not None:
            return 0

        now = time.time()
        rows = []
        for message in messages:
            role = message.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            rows.append((
                session_id, turn_id, role, self._clean(message.get("content")),
                message.get("name") if role == "tool" else None,
                now,
            ))
        if not rows:
            return 0

        self._db.executemany(
            "INSERT INTO messages (session_id, turn_id, role, content, tool_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._db.commit()
        self._embed_turn(session_id, turn_id, now)
        return len(rows)

    def _embed_turn(self, session_id: str, turn_id: str, created_at: float) -> None:
        if self.embeddings is None:
            return
        hit = self._hit(session_id, turn_id, created_at)
        blob = embed_blob(self.embeddings, " ".join(part for part in (hit.user, hit.assistant) if part).strip())
        if blob is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO turn_vectors (session_id, turn_id, created_at, embedding) "
                "VALUES (?, ?, ?, ?)",
                (session_id, turn_id, created_at, blob),
            )
            self._db.commit()

    def _vectors(self) -> list[tuple[tuple[str, str, float], bytes]]:
        rows = self._db.execute("SELECT session_id, turn_id, created_at, embedding FROM turn_vectors").fetchall()
        return [((r["session_id"], r["turn_id"], r["created_at"]), r["embedding"]) for r in rows]

    def _clean(self, content: object) -> str:
        text = redact(content if isinstance(content, str) else "") or ""
        if len(text) > self.max_message_length:
            text = text[: self.max_message_length] + " …[truncated]"
        return text

    def search(self, query: str, limit: int = 5) -> list[EpisodicHit]:
        if limit <= 0:
            return []
        keys = merge_hybrid(
            self._keyword_turns(query, limit),
            lambda: semantic_search(self.embeddings, query, self._vectors,
                                    threshold=self.min_similarity, limit=limit),
            limit,
        )
        return [self._hit(*key) for key in keys]

    def _keyword_turns(self, query: str, limit: int) -> list[tuple[str, str, float]]:
        expression = match_query(query)
        if expression is None:
            return []
        # A hit is a whole turn: rank each turn by its best-matching message.
        placeholders = ",".join("?" for _ in _SEARCHABLE_ROLES)
        rows = self._db.execute(
            f"""
            SELECT session_id, turn_id, MAX(created_at) AS created_at
            FROM (
                SELECT messages.session_id, messages.turn_id, messages.created_at,
                       messages_fts.rank AS score
                FROM messages_fts
                JOIN messages ON messages.id = messages_fts.rowid
                WHERE messages_fts MATCH ? AND messages.role IN ({placeholders})
            )
            GROUP BY session_id, turn_id
            ORDER BY MIN(score), MAX(created_at) DESC
            LIMIT ?
            """,
            (expression, *_SEARCHABLE_ROLES, limit),
        ).fetchall()
        return [(row["session_id"], row["turn_id"], row["created_at"]) for row in rows]

    def _hit(self, session_id: str, turn_id: str, created_at: float) -> EpisodicHit:
        rows = self._db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? AND turn_id = ? "
            "ORDER BY id",
            (session_id, turn_id),
        ).fetchall()
        user = next((r["content"] for r in rows if r["role"] == "user"), "")
        assistant = next(
            (r["content"] for r in reversed(rows) if r["role"] == "assistant" and r["content"]),
            "",
        )
        return EpisodicHit(session_id=session_id, turn_id=turn_id,
                           created_at=created_at, user=user, assistant=assistant)

    def turn(self, session_id: str, turn_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT role, content, tool_name, created_at FROM messages "
            "WHERE session_id = ? AND turn_id = ? ORDER BY id",
            (session_id, turn_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _summarized_through(self) -> int:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'summarized_through'").fetchone()
        return row["value"] if row else 0

    def unsummarized_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE id > ? GROUP BY session_id, turn_id)",
            (self._summarized_through(),),
        ).fetchone()[0]

    def unsummarized_turns(self, limit: int) -> list[tuple[int, EpisodicHit]]:
        """Oldest first, each with the id of its last message, which is what
        `mark_summarized` takes once the turn has been processed."""
        rows = self._db.execute(
            "SELECT session_id, turn_id, MAX(id) AS last_id, MIN(created_at) AS created_at "
            "FROM messages WHERE id > ? GROUP BY session_id, turn_id ORDER BY last_id LIMIT ?",
            (self._summarized_through(), limit),
        ).fetchall()
        return [(r["last_id"], self._hit(r["session_id"], r["turn_id"], r["created_at"])) for r in rows]

    def mark_summarized(self, message_id: int) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES ('summarized_through', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)",
            (message_id,),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
