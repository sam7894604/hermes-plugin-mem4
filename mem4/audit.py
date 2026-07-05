"""② Auditor — recall/route instrumentation for mem4 (local SQLite store).

Records what actually happens on each recall/route/prefetch so the value of mem4
can be measured with data instead of estimates (design spike §7; Fable 5 review
§6 point 4).

**Storage (as of 2026-07-05): a local SQLite database** (``$HERMES_HOME/mem4/
audit.db``, table ``audit_events``) — one row per event with full per-event
detail (query, arm, route, hit/hit_estimated, tool_called, injected chars/tokens,
prefetch flag, the paired baseline/mem4 inject tokens, and a stored
``paired_diff``). This is the source of truth for the eval harness and offline
analysis, and is trivially queryable with SQL. ``summary()`` rolls the rows up
into the per-event aggregates; the three controlled-measurement layers live in
``eval/harness.py`` and read the same events via :meth:`read_events`.

If a legacy ``audit.jsonl`` (the previous sink) sits next to a freshly-created
``audit.db``, its rows are imported once so no historical measurement is lost.

**Baserow 907 export is DEPRECATED and off by default.** ``export_to_baserow``
is never called automatically — it only runs if a caller passes it a writer.
mem4 audit now lives entirely in local SQLite; table 907 (``memory_audit``) is
retired. The method is kept (deprecated) for one-off manual back-fill only; see
:data:`BASEROW_DEPRECATED`.

Honesty note (design spike §7): a tool-call miss/hit is PRECISE (the tool was
called and we know the result). But the "L0 hit rate" (memory-relevant turns
that used no tool at all) can only be ESTIMATED from outside the provider — no
tool call means no event here. So per-event ``hit`` is precise; L0-hit estimation
is an aggregate the analyst computes separately.

Caveat on ``paired_diff`` (baseline_inject_tokens − mem4_inject_tokens): it models
the *counterfactual* "mem4 REPLACES the resident MEMORY.md". In a coexist/augment
deployment where MEMORY.md is NOT slimmed, both are injected and mem4 is additive,
so a positive ``paired_diff`` is a potential saving, not a realised one. See the
2026-07-05 toothless 實效驗證 note.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: Canonical SQLite store (new sink).
AUDIT_DB_FILENAME = "audit.db"
#: Legacy JSONL sink (no longer written; imported once if present).
AUDIT_LOG_FILENAME = "audit.jsonl"

#: Baserow 907 export is retired. Kept only for manual, explicit back-fill.
BASEROW_DEPRECATED = True

#: Ordered per-event columns of the ``audit_events`` table. Also (minus ``id``)
#: the dict keys returned by :meth:`Auditor.read_events`.
_EVENT_COLUMNS = (
    "ts", "session_id", "arm", "kind", "query", "tool_called",
    "hit", "hit_estimated", "route", "injected_chars", "injected_tokens",
    "prefetch_triggered", "baseline_inject_tokens", "mem4_inject_tokens",
    "paired_diff",
)


def estimate_tokens(chars: int) -> int:
    """Rough token estimate from char count (~4 chars/token). ESTIMATED."""
    return (max(0, int(chars)) + 3) // 4


@dataclass
class AuditEvent:
    ts: float
    session_id: str
    arm: str                 # "baseline" | "experiment"
    kind: str                # "route" | "search" | "prefetch"
    query: str
    tool_called: str         # "mem_route" | "mem_search" | ""
    hit: Optional[bool]      # precise for tool calls; None for prefetch-neutral
    hit_estimated: bool      # True only where hit is an estimate (never here)
    route: str               # "fts"|"trigram"|"like"|"microfile"|""
    injected_chars: int
    prefetch_triggered: bool = False
    # Paired counterfactual (② layer 2): what pure built-in WOULD inject on this
    # turn (its whole resident memory) vs what mem4 actually injected (legend +
    # this query's recall). Paired per-query so a paired-difference statistic can
    # be computed regardless of traffic mix.
    baseline_inject_tokens: int = 0
    mem4_inject_tokens: int = 0

    @property
    def injected_tokens(self) -> int:
        return estimate_tokens(self.injected_chars)

    @property
    def paired_diff(self) -> int:
        """baseline − mem4 inject tokens (positive ⇒ mem4 injected less)."""
        return int(self.baseline_inject_tokens) - int(self.mem4_inject_tokens)

    def to_row(self) -> tuple:
        return (
            self.ts, self.session_id, self.arm, self.kind, self.query,
            self.tool_called,
            (None if self.hit is None else int(bool(self.hit))),
            int(bool(self.hit_estimated)), self.route,
            int(self.injected_chars), self.injected_tokens,
            int(bool(self.prefetch_triggered)),
            int(self.baseline_inject_tokens), int(self.mem4_inject_tokens),
            self.paired_diff,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts, "session_id": self.session_id, "arm": self.arm,
            "kind": self.kind, "query": self.query, "tool_called": self.tool_called,
            "hit": self.hit, "hit_estimated": self.hit_estimated, "route": self.route,
            "injected_chars": int(self.injected_chars),
            "injected_tokens": self.injected_tokens,
            "prefetch_triggered": bool(self.prefetch_triggered),
            "baseline_inject_tokens": int(self.baseline_inject_tokens),
            "mem4_inject_tokens": int(self.mem4_inject_tokens),
            "paired_diff": self.paired_diff,
        }


class Auditor:
    """Records recall events to a local SQLite DB; summarizes; queryable via SQL.

    ``store_path`` is the SQLite database file (``audit.db``). When audit is
    disabled nothing is created — no DB file, no rows.
    """

    def __init__(self, store_path: Path, *, enabled: bool = False,
                 arm: str = "experiment", session_id: str = ""):
        self.store_path = Path(store_path)
        self.enabled = bool(enabled)
        self.arm = arm
        self.session_id = session_id
        self._lock = threading.Lock()

    # -- schema / connection -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.store_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> bool:
        """Create the table if missing. Returns True if it was just created."""
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone() is not None
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                      REAL,
                session_id              TEXT,
                arm                     TEXT,
                kind                    TEXT,
                query                   TEXT,
                tool_called             TEXT,
                hit                     INTEGER,   -- 1/0/NULL
                hit_estimated           INTEGER,
                route                   TEXT,
                injected_chars          INTEGER,
                injected_tokens         INTEGER,
                prefetch_triggered      INTEGER,
                baseline_inject_tokens  INTEGER,
                mem4_inject_tokens      INTEGER,
                paired_diff             INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_kind ON audit_events(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_arm ON audit_events(arm)")
        return not existed

    # -- recording (called from the provider hot paths) ----------------------

    def _emit(self, event: AuditEvent) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                self.store_path.parent.mkdir(parents=True, exist_ok=True)
                conn = self._connect()
                try:
                    created = self._ensure_schema(conn)
                    if created:
                        self._import_legacy_jsonl(conn)
                    cols = ", ".join(_EVENT_COLUMNS)
                    ph = ", ".join("?" for _ in _EVENT_COLUMNS)
                    conn.execute(
                        f"INSERT INTO audit_events ({cols}) VALUES ({ph})",
                        event.to_row(),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except sqlite3.Error:
            pass  # instrumentation must never break a turn

    def _import_legacy_jsonl(self, conn: sqlite3.Connection) -> int:
        """One-time import of a sibling legacy ``audit.jsonl`` into a fresh DB."""
        legacy = self.store_path.with_name(AUDIT_LOG_FILENAME)
        if not legacy.is_file():
            return 0
        n = 0
        cols = ", ".join(_EVENT_COLUMNS)
        ph = ", ".join("?" for _ in _EVENT_COLUMNS)
        for line in legacy.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            base = int(d.get("baseline_inject_tokens", 0) or 0)
            mem4 = int(d.get("mem4_inject_tokens", 0) or 0)
            hit = d.get("hit")
            chars = int(d.get("injected_chars", 0) or 0)
            row = (
                d.get("ts"), d.get("session_id", ""), d.get("arm", ""),
                d.get("kind", ""), d.get("query", ""), d.get("tool_called", ""),
                (None if hit is None else int(bool(hit))),
                int(bool(d.get("hit_estimated", False))), d.get("route", "") or "",
                chars, int(d.get("injected_tokens", estimate_tokens(chars))),
                int(bool(d.get("prefetch_triggered", False))),
                base, mem4, int(d.get("paired_diff", base - mem4)),
            )
            conn.execute(f"INSERT INTO audit_events ({cols}) VALUES ({ph})", row)
            n += 1
        return n

    def record_search(self, query: str, *, route: str, hit: bool, injected_chars: int,
                      baseline_inject_tokens: int = 0, mem4_inject_tokens: int = 0) -> None:
        self._emit(AuditEvent(
            ts=time.time(), session_id=self.session_id, arm=self.arm, kind="search",
            query=query[:500], tool_called="mem_search", hit=hit, hit_estimated=False,
            route=route, injected_chars=injected_chars,
            baseline_inject_tokens=baseline_inject_tokens, mem4_inject_tokens=mem4_inject_tokens,
        ))

    def record_route(self, code: str, *, hit: bool, injected_chars: int) -> None:
        self._emit(AuditEvent(
            ts=time.time(), session_id=self.session_id, arm=self.arm, kind="route",
            query=code[:120], tool_called="mem_route", hit=hit, hit_estimated=False,
            route="microfile" if hit else "", injected_chars=injected_chars,
        ))

    def record_prefetch(self, query: str, *, injected_chars: int,
                        baseline_inject_tokens: int = 0, mem4_inject_tokens: int = 0) -> None:
        self._emit(AuditEvent(
            ts=time.time(), session_id=self.session_id, arm=self.arm, kind="prefetch",
            query=query[:500], tool_called="", hit=injected_chars > 0, hit_estimated=False,
            route="", injected_chars=injected_chars, prefetch_triggered=injected_chars > 0,
            baseline_inject_tokens=baseline_inject_tokens, mem4_inject_tokens=mem4_inject_tokens,
        ))

    # -- reading / querying --------------------------------------------------

    def read_events(self) -> List[dict]:
        """Return all events as dicts (same shape the harness/tests expect)."""
        if not self.store_path.is_file():
            return []
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []
        try:
            try:
                rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
            except sqlite3.Error:
                return []
            out = []
            for r in rows:
                out.append({
                    "ts": r["ts"], "session_id": r["session_id"], "arm": r["arm"],
                    "kind": r["kind"], "query": r["query"], "tool_called": r["tool_called"],
                    "hit": (None if r["hit"] is None else bool(r["hit"])),
                    "hit_estimated": bool(r["hit_estimated"]),
                    "route": r["route"] or "",
                    "injected_chars": r["injected_chars"],
                    "injected_tokens": r["injected_tokens"],
                    "prefetch_triggered": bool(r["prefetch_triggered"]),
                    "baseline_inject_tokens": r["baseline_inject_tokens"],
                    "mem4_inject_tokens": r["mem4_inject_tokens"],
                    "paired_diff": r["paired_diff"],
                })
            return out
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        """Run a read-only SQL query against the store; return rows as dicts.

        A tiny convenience so an analyst can slice the store without leaving
        Python, e.g. ``auditor.query("SELECT arm, AVG(paired_diff) AS d FROM
        audit_events GROUP BY arm")``.
        """
        if not self.store_path.is_file():
            return []
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def summarize(events: List[dict]) -> Dict[str, Any]:
        searches = [e for e in events if e.get("kind") == "search"]
        routes = [e for e in events if e.get("kind") == "route"]
        prefetches = [e for e in events if e.get("kind") == "prefetch"]

        def _rate(subset):
            hits = [e for e in subset if e.get("hit")]
            return (len(hits) / len(subset)) if subset else 0.0

        route_dist: Dict[str, int] = {}
        for e in searches:
            r = e.get("route") or "none"
            route_dist[r] = route_dist.get(r, 0) + 1

        injected = [int(e.get("injected_chars", 0)) for e in events]
        avg_injected = (sum(injected) / len(injected)) if injected else 0.0

        # Paired token diff over events that carry it (search + prefetch).
        paired = [int(e.get("paired_diff", 0)) for e in events
                  if e.get("kind") in {"search", "prefetch"}
                  and (e.get("baseline_inject_tokens") or e.get("mem4_inject_tokens"))]
        median_paired = 0.0
        if paired:
            s = sorted(paired)
            mid = len(s) // 2
            median_paired = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

        return {
            "n_events": len(events),
            "n_search": len(searches),
            "n_route": len(routes),
            "n_prefetch": len(prefetches),
            "n_tool_calls": len(searches) + len(routes),
            "search_hit_rate": round(_rate(searches), 3),
            "route_hit_rate": round(_rate(routes), 3),
            "prefetch_trigger_rate": round(
                (sum(1 for e in prefetches if e.get("prefetch_triggered")) / len(prefetches))
                if prefetches else 0.0, 3),
            "route_distribution": route_dist,
            "avg_injected_chars": round(avg_injected, 1),
            "est_avg_injected_tokens": estimate_tokens(int(avg_injected)),
            "median_paired_diff_tokens": round(median_paired, 1),
        }

    def summary(self) -> Dict[str, Any]:
        """Convenience: read the store and summarize it in one call."""
        return self.summarize(self.read_events())

    # -- Baserow 907 export — DEPRECATED (retired; manual back-fill only) -----

    def build_baserow_row(self, summary: Dict[str, Any], *, date_str: str,
                          name: str, est_tokens_saved: int = 0) -> Dict[str, Any]:
        """DEPRECATED. Map an aggregate summary onto table 907's existing columns.

        Baserow 907 (``memory_audit``) is retired now that audit lives in local
        SQLite. Kept only so an operator can do a one-off manual export.
        """
        return {
            "Name": name,
            "type": "audit",
            "date": date_str,
            "Active": True,
            "entry_count": summary.get("n_events", 0),
            "mem_chars": int(summary.get("avg_injected_chars", 0)),
            "hot_hit_rate": round(summary.get("search_hit_rate", 0.0) * 100, 1),
            "est_tokens_saved": int(est_tokens_saved),
            "notes": json.dumps({"arm": self.arm, **summary}, ensure_ascii=False),
        }

    def export_to_baserow(
        self, writer: Callable[[int, List[Dict[str, Any]]], Any], *,
        date_str: str, name: str, table_id: int = 907, est_tokens_saved: int = 0,
    ) -> Dict[str, Any]:
        """DEPRECATED. Write one aggregate row via ``writer(table_id, [row])``.

        Never called automatically — audit now lives in local SQLite (``audit.db``)
        and Baserow 907 is retired/off-by-default. Retained for a manual one-off
        back-fill only. This module never imports the Baserow MCP.
        """
        row = self.build_baserow_row(
            self.summary(), date_str=date_str, name=name, est_tokens_saved=est_tokens_saved,
        )
        writer(table_id, [row])
        return row
