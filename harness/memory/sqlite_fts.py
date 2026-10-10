from __future__ import annotations

import dataclasses
import functools
import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .embeddings import EmbeddingClient
from .retrieval import embed_blob, fts_sync_ddl, match_query, merge_hybrid, open_db, semantic_search


def _locked(method):
    """Sub-agents running in parallel share one store and recall from worker
    threads; `search()` also writes. The connection is not safe to share, so
    every entry point takes the same reentrant lock."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


MEMORY_KINDS = {"preference", "fact", "decision", "project_context", "instruction"}

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_ARCHIVED = "archived"


@dataclass(frozen=True)
class MemoryEntry:
    id: int
    content: str
    kind: str
    created_at: float
    updated_at: float
    status: str = STATUS_ACTIVE
    last_accessed_at: float | None = None
    pinned: bool = False
    supersedes: int | None = None


_FIELDS = tuple(f.name for f in dataclasses.fields(MemoryEntry))


class SQLiteMemory:
    def __init__(
        self,
        path: Path,
        max_entry_length: int = 2000,
        embeddings: EmbeddingClient | None = None,
        min_similarity: float = 0.30,
    ) -> None:
        self.max_entry_length = max_entry_length
        self.embeddings = embeddings
        self.min_similarity = min_similarity
        self.lock = threading.RLock()
        self._db = open_db(path, shared=True, schema=f"""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT '{STATUS_ACTIVE}',
                last_accessed_at REAL,
                pinned INTEGER NOT NULL DEFAULT 0,
                supersedes INTEGER,
                embedding BLOB
            );
            """ + fts_sync_ddl("memories", "memories_fts", ("content", "kind")))

    @_locked
    def store(
        self,
        content: str,
        *,
        kind: str = "fact",
        supersedes: int | None = None,
        pinned: bool = False,
    ) -> MemoryEntry:
        content = " ".join(content.split())
        if not content:
            raise ValueError("memory content cannot be empty")
        if len(content) > self.max_entry_length:
            raise ValueError(f"memory content exceeds {self.max_entry_length} characters")
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind}")
        if supersedes is not None and self.get(supersedes) is None:
            raise ValueError(f"cannot supersede unknown memory id: {supersedes}")

        # Same kind + same text is one row: storing it again refreshes it
        # (and reactivates it if archived) instead of adding a duplicate.
        content_hash = hashlib.sha256(f"{kind}\0{content}".encode()).hexdigest()
        now = time.time()
        self._db.execute(
            f"""
            INSERT INTO memories
                (content, kind, created_at, updated_at, content_hash, status, supersedes, pinned)
            VALUES (?, ?, ?, ?, ?, '{STATUS_ACTIVE}', ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                updated_at = excluded.updated_at,
                supersedes = COALESCE(excluded.supersedes, memories.supersedes),
                pinned = MAX(memories.pinned, excluded.pinned),
                status = '{STATUS_ACTIVE}'
            """,
            (content, kind, now, now, content_hash, supersedes, 1 if pinned else 0),
        )
        if supersedes is not None:
            self._db.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ? AND pinned = 0",
                (STATUS_SUPERSEDED, now, supersedes),
            )
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM memories WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        # Committed before embedding on purpose: an embedding failure must
        # never cost the fact itself. Without a vector it is keyword-only.
        blob = embed_blob(self.embeddings, content)
        if blob is not None:
            self._db.execute("UPDATE memories SET embedding = ? WHERE id = ?", (blob, row["id"]))
            self._db.commit()
        return self._entry(row)

    @_locked
    def get(self, memory_id: int) -> MemoryEntry | None:
        row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._entry(row) if row is not None else None

    @_locked
    def forget(self, memory_id: int) -> MemoryEntry | None:
        entry = self.get(memory_id)
        if entry is None or entry.pinned:
            return None
        self._db.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_ARCHIVED, time.time(), memory_id),
        )
        self._db.commit()
        return self.get(memory_id)

    @_locked
    def archive_stale(self, older_than_days: int, now: float | None = None) -> int:
        """Archive every unpinned active fact nobody has stored or recalled in
        `older_than_days`. Returns how many. Nothing is deleted (I5)."""
        now = time.time() if now is None else now
        cursor = self._db.execute(
            f"""
            UPDATE memories SET status = '{STATUS_ARCHIVED}', updated_at = ?
            WHERE status = '{STATUS_ACTIVE}' AND pinned = 0
              AND COALESCE(last_accessed_at, updated_at) < ?
            """,
            (now, now - older_than_days * 86400),
        )
        self._db.commit()
        return cursor.rowcount

    @_locked
    def all_entries(self, *, status: str | None = None) -> list[MemoryEntry]:
        if status is None:
            rows = self._db.execute("SELECT * FROM memories ORDER BY id").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    @_locked
    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        if limit <= 0:
            return []
        ids = merge_hybrid(
            self._keyword_search(query, limit),
            lambda: semantic_search(self.embeddings, query, self._vectors,
                                    threshold=self.min_similarity, limit=limit),
            limit,
        )
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE memories SET last_accessed_at = ? WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )
            self._db.commit()
        return [entry for entry in (self.get(i) for i in ids) if entry is not None]

    def _keyword_search(self, query: str, limit: int) -> list[int]:
        expression = match_query(query)
        if expression is None:
            return []
        rows = self._db.execute(
            f"""
            SELECT memories.id
            FROM memories_fts
            JOIN memories ON memories.id = memories_fts.rowid
            WHERE memories_fts MATCH ? AND memories.status = '{STATUS_ACTIVE}'
            ORDER BY bm25(memories_fts), memories.updated_at DESC
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()
        return [row["id"] for row in rows]

    def _vectors(self) -> list[tuple[int, bytes]]:
        rows = self._db.execute(
            f"SELECT id, embedding FROM memories WHERE embedding IS NOT NULL AND status = '{STATUS_ACTIVE}'"
        ).fetchall()
        return [(row["id"], row["embedding"]) for row in rows]

    @staticmethod
    def _entry(row: sqlite3.Row) -> MemoryEntry:
        # Named fields, not **row: the table carries storage-only columns
        # (content_hash, embedding) the entry does not expose.
        values = {name: row[name] for name in _FIELDS}
        values["pinned"] = bool(values["pinned"])
        return MemoryEntry(**values)

    def close(self) -> None:
        with self.lock:
            self._db.close()
