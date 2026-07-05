"""② Auditor — recall/route instrumentation for mem4.

Records what actually happens on each recall/route/prefetch so the value of mem4
can be measured with data instead of estimates (design spike §7; Fable 5 review
§6 point 4). Two sinks, decoupled:

  * **Local JSONL** (always, when enabled): one line per event with full detail
    (query, hit/miss, route, injected chars, prefetch). This is the source of
    truth for the eval harness and offline analysis.
  * **Baserow 907 (memory_audit)**: an AGGREGATE summary row mapped onto the
    table's EXISTING columns (type=audit, entry_count, mem_chars, hot_hit_rate,
    est_tokens_saved, notes=JSON detail). Per-event columns do not exist on 907
    yet — see MISSING_907_FIELDS. Writing is via an injected callable so this
    module never imports the Baserow MCP (tests pass a mock).

Honesty note (design spike §7): a tool-call miss is PRECISE (the tool was called
and returned nothing). A tool-call hit is precise too. But the "L0 hit rate"
(memory-relevant turns that used no tool at all) can only be ESTIMATED from
outside the provider — no tool call means no event here. So per-event ``hit`` is
precise; L0-hit estimation is an aggregate the analyst computes separately.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

AUDIT_LOG_FILENAME = "audit.jsonl"

# Table 907 (memory_audit) is AGGREGATE-oriented. Its existing columns can hold
# a rolled-up summary row (type=audit, entry_count, mem_chars, hot_hit_rate,
# est_tokens_saved, notes). But the controlled measurement (② three layers) wants
# to persist richer, per-event / paired / distribution data that 907 has no
# columns for. Listed so the operator can decide whether to add them — the code
# does NOT alter the 907 schema on its own (aggregate export uses only existing
# columns; everything else can be packed into `notes` JSON as a fallback).
MISSING_907_FIELDS = [
    # per-event (layer 2 paired counterfactual)
    "query (text)",
    "arm (single_select: baseline/experiment)",
    "route (single_select: fts/trigram/like/microfile)",
    "hit (boolean) / hit_estimated (boolean)",
    "tool_called (single_select: mem_route/mem_search/none)",
    "session_id (text)",
    "baseline_inject_tokens (number)  — what pure built-in would inject",
    "mem4_inject_tokens (number)      — what mem4 actually injected",
    "paired_diff_tokens (number)      — baseline − mem4 (per query)",
    # distributions (layers 1 & 3) — 907 has no distribution columns
    "inject_tokens_p25/median/p75/min/max (number ×5)",
    "resident_tokens_baseline_vs_mem4 (number ×2)",
    "gold_accuracy_precise (number)   — deterministic-replay gold hit rate",
]


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
    # this query's recall). Paired per-query so a paired-difference statistic
    # can be computed regardless of traffic mix.
    baseline_inject_tokens: int = 0
    mem4_inject_tokens: int = 0

    def to_line(self) -> str:
        d = asdict(self)
        d["injected_tokens"] = estimate_tokens(self.injected_chars)
        return json.dumps(d, ensure_ascii=False)


class Auditor:
    """Records recall events locally; summarizes; exports an aggregate to 907."""

    def __init__(self, log_path: Path, *, enabled: bool = False, arm: str = "experiment",
                 session_id: str = ""):
        self.log_path = Path(log_path)
        self.enabled = bool(enabled)
        self.arm = arm
        self.session_id = session_id
        self._lock = threading.Lock()

    # -- recording (called from the provider hot paths) ----------------------

    def _emit(self, event: AuditEvent) -> None:
        if not self.enabled:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(event.to_line() + "\n")
        except OSError:
            pass  # instrumentation must never break a turn

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

    # -- reading / summarizing -----------------------------------------------

    def read_events(self) -> List[dict]:
        if not self.log_path.is_file():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

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

        return {
            "n_events": len(events),
            "n_search": len(searches),
            "n_route": len(routes),
            "n_prefetch": len(prefetches),
            "search_hit_rate": round(_rate(searches), 3),
            "route_hit_rate": round(_rate(routes), 3),
            "prefetch_trigger_rate": round(
                (sum(1 for e in prefetches if e.get("prefetch_triggered")) / len(prefetches))
                if prefetches else 0.0, 3),
            "route_distribution": route_dist,
            "avg_injected_chars": round(avg_injected, 1),
            "est_avg_injected_tokens": estimate_tokens(int(avg_injected)),
        }

    # -- Baserow 907 aggregate export (existing columns only) ----------------

    def build_baserow_row(self, summary: Dict[str, Any], *, date_str: str,
                          name: str, est_tokens_saved: int = 0) -> Dict[str, Any]:
        """Map an aggregate summary onto table 907's EXISTING columns."""
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
        """Write one aggregate row via ``writer(table_id, [row])`` (injected).

        ``writer`` is supplied by the caller (a Baserow client, or a mock in
        tests). This module never imports the Baserow MCP. Returns the row.
        """
        row = self.build_baserow_row(
            self.summarize(self.read_events()),
            date_str=date_str, name=name, est_tokens_saved=est_tokens_saved,
        )
        writer(table_id, [row])
        return row
