"""FTS5 recall store for mem4 (feature ①) — mem4's own SQLite FTS5 database.

Design spike §10.8 decision B: mem4 owns its own FTS5 table (``recall.db``) that
indexes BOTH conversation turns and the L2/L3 microfiles, rather than piggybacking
on the built-in ``session_search`` store. This keeps recall quality and lifecycle
under mem4's control.

Chinese search is a hard requirement (Fable 5 review §3). The default unicode61
tokenizer does not segment CJK, so a naive single-table FTS5 breaks Chinese.
This module reuses the upstream ``hermes_state.py`` **dual-table** pattern
verbatim in spirit:

  * ``docs_fts``          — unicode61 (English / BM25).
  * ``docs_fts_trigram``  — trigram tokenizer (CJK / any-script substring).
  * ``_contains_cjk()`` routes CJK queries to trigram; queries with any CJK token
    shorter than 3 chars (trigram needs ≥3) fall back to a per-token LIKE scan.
  * If the SQLite build lacks the trigram tokenizer, the trigram table is skipped
    and CJK queries degrade to LIKE — never a hard failure.

Ranking layers a time-decay weight over relevance (Fable 5 review §5): recent
material outranks equally-relevant older material.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .backend import SearchHit

logger = logging.getLogger(__name__)

# unicode61 (default) table + triggers keyed on the docs table's rowid.
_DOCS_SQL = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    ref TEXT,
    kind TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(content);
CREATE TRIGGER IF NOT EXISTS docs_fts_insert AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_fts_delete AFTER DELETE ON docs BEGIN
    DELETE FROM docs_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS docs_fts_update AFTER UPDATE ON docs BEGIN
    DELETE FROM docs_fts WHERE rowid = old.id;
    INSERT INTO docs_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# trigram table for CJK substring search (mirrors hermes_state.py FTS_TRIGRAM_SQL).
_DOCS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS docs_fts_trigram_insert AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_fts_trigram_delete AFTER DELETE ON docs BEGIN
    DELETE FROM docs_fts_trigram WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS docs_fts_trigram_update AFTER UPDATE ON docs BEGIN
    DELETE FROM docs_fts_trigram WHERE rowid = old.id;
    INSERT INTO docs_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
"""

# Recall/snippet tuning.
_HALF_LIFE_DAYS = 30.0      # time-decay half-life for ranking
_SNIPPET_CHARS = 240
_FTS_OPERATORS = {"AND", "OR", "NOT"}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _contains_cjk(text: str) -> bool:
    """True if text contains CJK characters (copied from hermes_state.py)."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
            return True
    return False


def _count_cjk(text: str) -> int:
    n = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or 0x3000 <= cp <= 0x303F or
                0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
                0xAC00 <= cp <= 0xD7AF):
            n += 1
    return n


def _fts_match(query: str) -> str:
    """Build an OR-of-quoted-tokens FTS5 MATCH string.

    mem4 recall queries are formed by the model / harness in natural language, so
    ANY matching token should surface the doc (BM25 then ranks by how well it
    matches). Implicit AND (every token must appear) would miss almost every
    natural-language query. Each token is quoted so FTS5 special chars can't
    break MATCH; boolean operators are not exposed to mem4 recall.
    """
    parts = []
    for tok in query.split():
        t = tok.strip()
        if t:
            parts.append('"' + t.replace('"', '""') + '"')
    return " OR ".join(parts)


@dataclass
class _Row:
    id: int
    ref: str
    kind: str
    content: str
    ts: float


class RecallStore:
    """mem4's own dual-table FTS5 recall database."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.trigram_available = True
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_DOCS_SQL)
            try:
                self._conn.executescript(_DOCS_TRIGRAM_SQL)
                self.trigram_available = True
            except sqlite3.OperationalError as exc:
                if "no such tokenizer: trigram" in str(exc).lower():
                    self.trigram_available = False
                    logger.warning(
                        "mem4 recall: SQLite trigram tokenizer unavailable — "
                        "CJK search will fall back to LIKE."
                    )
                else:
                    raise
            self._conn.commit()

    # -- indexing ------------------------------------------------------------

    def index(self, ref: str, content: str, kind: str, ts: float) -> bool:
        """Insert a document; dedup by content hash. Returns True if inserted."""
        if not content or not content.strip():
            return False
        h = content_hash(content)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO docs(ref, kind, content, content_hash, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (ref, kind, content, h, float(ts)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs")
            self._conn.commit()

    # -- search --------------------------------------------------------------

    def search(self, query: str, *, limit: int = 5, now: float) -> List[SearchHit]:
        if not query or not query.strip():
            return []
        q = query.strip()
        # Pull a candidate pool (limit * 4) so time-decay re-ranking has room.
        pool = max(limit * 4, limit)
        rows, route = self._route_and_fetch(q, pool)
        hits = self._rerank_with_decay(rows, route, now, limit)
        return hits

    def _route_and_fetch(self, q: str, pool: int) -> Tuple[List[_Row], str]:
        if not _contains_cjk(q):
            return self._fts_fetch("docs_fts", _fts_match(q), pool), "fts"

        raw = q.strip('"').strip()
        cjk_count = _count_cjk(raw)
        tokens_for_check = [
            t for t in raw.split()
            if t.upper() not in _FTS_OPERATORS and _contains_cjk(t)
        ]
        any_short_cjk = any(_count_cjk(t) < 3 for t in tokens_for_check)

        if cjk_count >= 3 and not any_short_cjk and self.trigram_available:
            rows = self._fts_fetch("docs_fts_trigram", _fts_match(raw), pool)
            if rows:
                return rows, "trigram"
            # trigram matched nothing → try LIKE as a safety net
        return self._like_fetch(raw, pool), "like"

    def _fts_fetch(self, table: str, match_query: str, pool: int) -> List[_Row]:
        if not match_query.strip():
            return []
        sql = f"""
            SELECT d.id, d.ref, d.kind, d.content, d.ts
            FROM {table}
            JOIN docs d ON d.id = {table}.rowid
            WHERE {table} MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        with self._lock:
            try:
                cur = self._conn.execute(sql, (match_query, pool))
            except sqlite3.OperationalError:
                return []
            return [_Row(r["id"], r["ref"], r["kind"], r["content"], r["ts"])
                    for r in cur.fetchall()]

    def _like_fetch(self, raw: str, pool: int) -> List[_Row]:
        tokens = [t for t in raw.split() if t.upper() not in _FTS_OPERATORS] or [raw]
        clauses, params = [], []
        for tok in tokens:
            esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")
        sql = f"""
            SELECT id, ref, kind, content, ts
            FROM docs
            WHERE {' OR '.join(clauses)}
            ORDER BY ts DESC
            LIMIT ?
        """
        params.append(pool)
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [_Row(r["id"], r["ref"], r["kind"], r["content"], r["ts"])
                    for r in cur.fetchall()]

    def _rerank_with_decay(self, rows: List[_Row], route: str, now: float,
                           limit: int) -> List[SearchHit]:
        scored = []
        for pos, row in enumerate(rows):
            base = 1.0 / (pos + 1)  # relevance proxy from the source ordering
            age_days = max(0.0, (now - row.ts) / 86400.0)
            weight = 0.5 ** (age_days / _HALF_LIFE_DAYS)
            score = base * weight
            snippet = row.content.strip().replace("\n", " ")[:_SNIPPET_CHARS]
            scored.append(SearchHit(
                ref=row.ref, snippet=snippet, score=score,
                kind=row.kind, ts=row.ts, route=route,
            ))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]

    # -- backfill (resumable) ------------------------------------------------

    def backfill_batch(
        self,
        fetch: Callable[[int, int], Iterable[Tuple[int, str, str, float]]],
        *,
        since_rowid: int = 0,
        batch_size: int = 200,
    ) -> Tuple[int, int, bool]:
        """Index one batch from a history source. Resumable via ``since_rowid``.

        ``fetch(since_rowid, batch_size)`` yields ``(rowid, ref, content, ts)``
        rows with rowid > since_rowid in ascending order. Returns
        ``(indexed_count, new_cursor, has_more)``. Dedup is by content hash, so
        re-processing a row is a no-op; the cursor advances by source rowid so
        rows are never re-fetched.
        """
        batch = list(fetch(since_rowid, batch_size))
        if not batch:
            return (0, since_rowid, False)
        indexed = 0
        cursor = since_rowid
        for rowid, ref, content, ts in batch:
            if self.index(ref, content, "turn", ts):
                indexed += 1
            cursor = max(cursor, int(rowid))
        has_more = len(batch) >= batch_size
        return (indexed, cursor, has_more)
