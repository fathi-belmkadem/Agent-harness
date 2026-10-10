from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable, Hashable, Iterable, TypeVar

from .embeddings import EmbeddingClient, cosine, pack, unpack

K = TypeVar("K", bound=Hashable)

FTS_TOKENIZER = "porter unicode61"

TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)

STOPWORDS = frozenset("""
a about all also am an and any are as at be been but by can did do does
doing for from had has have he her hers him his how i if in into is it its
me my no nor not of on or our ours out over she should so some such than
that the their theirs them then there these they this those to too us was
we were what when where which while who whom why will with would you your
yours
""".split())


_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r"[\s!?.,;:'\"()\[\]{}~‘’“”—–…-]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return bool(_TRIVIAL_PROMPT_RE.match(stripped))


def match_query(text: str) -> str | None:
    terms = [t for t in TOKEN_RE.findall(text) if t.lower() not in STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)


def fts_sync_ddl(base: str, fts: str, columns: tuple[str, ...], *, rowid: str = "id") -> str:
    cols = ", ".join(columns)
    new_vals = ", ".join(f"new.{c}" for c in columns)
    old_vals = ", ".join(f"old.{c}" for c in columns)
    return f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5(
            {cols}, content='{base}', content_rowid='{rowid}',
            tokenize='{FTS_TOKENIZER}'
        );
        CREATE TRIGGER IF NOT EXISTS {base}_ai AFTER INSERT ON {base} BEGIN
            INSERT INTO {fts}(rowid, {cols}) VALUES (new.{rowid}, {new_vals});
        END;
        CREATE TRIGGER IF NOT EXISTS {base}_ad AFTER DELETE ON {base} BEGIN
            INSERT INTO {fts}({fts}, rowid, {cols}) VALUES ('delete', old.{rowid}, {old_vals});
        END;
        CREATE TRIGGER IF NOT EXISTS {base}_au AFTER UPDATE ON {base} BEGIN
            INSERT INTO {fts}({fts}, rowid, {cols}) VALUES ('delete', old.{rowid}, {old_vals});
            INSERT INTO {fts}(rowid, {cols}) VALUES (new.{rowid}, {new_vals});
        END;
    """


def open_db(path: Path, schema: str, *, shared: bool = False) -> sqlite3.Connection:
    """Open (creating if needed) a store's SQLite file and apply its schema.
    `shared`: the connection is used from several threads, behind a lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=not shared)
    db.row_factory = sqlite3.Row
    db.executescript(schema)
    db.commit()
    return db


def embed_blob(embeddings: EmbeddingClient | None, text: str) -> bytes | None:
    """`text` as a stored vector, or None without an embedder or on failure."""
    vector = embeddings.embed(text) if embeddings is not None and text else None
    return pack(vector) if vector is not None else None


def semantic_search(
    embeddings: EmbeddingClient | None,
    query: str,
    candidates: Callable[[], Iterable[tuple[K, bytes]]],
    *,
    threshold: float,
    limit: int,
) -> list[K]:
    """Keys of the candidates closest to `query` by cosine, best first. Empty
    without an embedder or when the query cannot be embedded."""
    query_vector = embeddings.embed(query) if embeddings is not None else None
    if query_vector is None:
        return []
    scored = [(cosine(query_vector, unpack(blob)), key) for key, blob in candidates()]
    hits = sorted((pair for pair in scored if pair[0] >= threshold), key=lambda pair: pair[0], reverse=True)
    return [key for _, key in hits[:limit]]


def merge_hybrid(keyword: list[K], semantic: Callable[[], list[K]], limit: int) -> list[K]:
    """Keyword keys first, semantic keys to fill, deduplicated. `semantic` is
    only called when keyword search did not already fill the limit."""
    ordered = list(dict.fromkeys(keyword))[:limit]
    if len(ordered) < limit:
        extra = [key for key in dict.fromkeys(semantic()) if key not in ordered]
        ordered += extra[: limit - len(ordered)]
    return ordered
