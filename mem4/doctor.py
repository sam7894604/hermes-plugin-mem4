"""C-⑤ mem4 doctor — read-only health check across every component.

The v2 verification (2026-07-15) found mem4's failure mode was *silent idling*:
Dream had never run, refine was 0%-idle, usermind produced garbage — all with no
error and no signal. ``doctor`` turns those into VISIBLE, warnable facts: for each
mechanism, is it firing, is it having an effect, or has it silently gone idle?

Pure local reads (recall.db / audit.db / state files + MEMORY.md size); never
writes, never a network call. Safe to run against a live deployment.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dream import (
    DREAM_STATE_FILENAME, DEFAULT_THRESHOLD, DEFAULT_STALENESS_DAYS,
)
from .refine import DEFAULT_MEMORY_CHAR_LIMIT, REFINE_AGGRESSIVE_FILL

#: A component that hasn't fired within this many days (when it has pending work)
#: is flagged as stale.
STALE_AFTER_DAYS = 14


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ro_conn(db: Path) -> Optional[sqlite3.Connection]:
    """Open a SQLite DB strictly read-only; None if it does not exist."""
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def _age_days(iso_or_ts: Any) -> Optional[float]:
    """Age in days from an ISO string or a unix ts; None if unparseable."""
    if iso_or_ts is None:
        return None
    now = datetime.now(timezone.utc)
    try:
        if isinstance(iso_or_ts, (int, float)):
            then = datetime.fromtimestamp(float(iso_or_ts), tz=timezone.utc)
        else:
            then = datetime.fromisoformat(str(iso_or_ts))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() / 86400.0
    except (ValueError, OSError):
        return None


def collect(home, *, char_limit: Optional[int] = None) -> Dict[str, Any]:
    """Gather health facts for every mem4 component. Read-only.

    Returns ``{"components": {name: {...}}, "warnings": [str, ...]}``.
    """
    home = Path(home)
    root = home / "mem4"
    limit = int(char_limit) if char_limit else DEFAULT_MEMORY_CHAR_LIMIT
    comp: Dict[str, Any] = {}
    warn: List[str] = []

    # -- ① recall store -----------------------------------------------------
    recall = {"db_exists": (root / "recall.db").is_file(), "docs": 0,
              "turns": 0, "microfiles": 0, "last_indexed": None}
    conn = _ro_conn(root / "recall.db")
    if conn is not None:
        try:
            recall["docs"] = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
            for kind, n in conn.execute("SELECT kind, count(*) FROM docs GROUP BY kind"):
                if kind == "turn":
                    recall["turns"] = n
                elif kind == "microfile":
                    recall["microfiles"] = n
            mx = conn.execute("SELECT max(ts) FROM docs").fetchone()[0]
            recall["last_indexed"] = _iso(mx)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    comp["recall"] = recall
    if recall["db_exists"] and recall["docs"] == 0:
        warn.append("recall 索引為空——冷記憶檢索無資料可召回(原則③受影響)。")

    # -- prefetch (C-①), from audit ----------------------------------------
    prefetch = {"events": 0, "trigger_rate": None, "avg_injected_chars": None,
                "last_event": None}
    route = {"tool_calls": 0, "search_hit_rate": None, "route_hit_rate": None}
    conn = _ro_conn(root / "audit.db")
    if conn is not None:
        try:
            pf = conn.execute(
                "SELECT count(*), avg(CASE WHEN prefetch_triggered=1 THEN 1.0 ELSE 0.0 END),"
                " avg(injected_chars), max(ts) FROM audit_events WHERE kind='prefetch'"
            ).fetchone()
            if pf and pf[0]:
                prefetch.update(events=pf[0], trigger_rate=pf[1],
                                avg_injected_chars=(round(pf[2]) if pf[2] is not None else None),
                                last_event=_iso(pf[3]))
            tc = conn.execute(
                "SELECT count(*) FROM audit_events WHERE tool_called IS NOT NULL "
                "AND tool_called <> ''").fetchone()
            route["tool_calls"] = tc[0] if tc else 0
            for kind, key in (("search", "search_hit_rate"), ("route", "route_hit_rate")):
                r = conn.execute(
                    "SELECT avg(CASE WHEN hit=1 THEN 1.0 ELSE 0.0 END) FROM audit_events "
                    "WHERE kind=?", (kind,)).fetchone()
                route[key] = r[0] if r else None
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    comp["prefetch"] = prefetch
    comp["route_tools"] = route

    # -- refine (C-②) -------------------------------------------------------
    mem_path = home / "memories" / "MEMORY.md"
    mem_chars = 0
    if mem_path.is_file():
        try:
            mem_chars = len(mem_path.read_text(encoding="utf-8"))
        except OSError:
            pass
    fill = (mem_chars / limit) if limit else 0.0
    microfiles_on_disk = sum(
        1 for p in root.glob("*.md")
        if not p.name.startswith("_") and p.name != "memory.md"
    ) if root.is_dir() else 0
    refine_state = _read_json(root / "_refine_state.json") or {}
    last_refine_ts = None
    conn = _ro_conn(root / "audit.db")
    if conn is not None:
        try:
            r = conn.execute(
                "SELECT max(ts) FROM audit_events WHERE kind='refine'").fetchone()
            last_refine_ts = _iso(r[0]) if r else None
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    last_refine_age = _age_days(last_refine_ts)
    refine = {
        "memory_chars": mem_chars, "char_limit": limit,
        "fill_ratio": round(fill, 3),
        "aggressive_zone": fill >= REFINE_AGGRESSIVE_FILL,
        "microfiles_on_disk": microfiles_on_disk,
        "ever_applied": bool(refine_state.get("last_applied_hash")),
        "last_refine_event": last_refine_ts,
        "last_refine_age_days": last_refine_age,
    }
    comp["refine"] = refine
    if refine["aggressive_zone"] and not refine["ever_applied"]:
        warn.append(
            f"熱區填充 {fill:.0%} 已達積極區但 refine 從未套用——冷細節有被驅逐流失風險"
            "(原則②)。可 `hermes mem4 refine --apply`。")
    elif refine["aggressive_zone"] and last_refine_age is not None and last_refine_age > STALE_AFTER_DAYS:
        warn.append(
            f"熱區填充 {fill:.0%} 在積極區,但 refine 已 {last_refine_age:.0f} 天未再執行——"
            "新累積的冷細節可能未被保存(原則②)。")

    # -- Dream (④) ----------------------------------------------------------
    ds = _read_json(root / DREAM_STATE_FILENAME) or {}
    last_consol = ds.get("last_consolidation_at")
    dream = {
        "consolidation_count": int(ds.get("consolidation_count", 0)),
        "last_consolidation_at": last_consol,
        "signals_since_last": int(ds.get("signals_since_last", 0)),
        "threshold": DEFAULT_THRESHOLD,
        "staleness_days": DEFAULT_STALENESS_DAYS,
        "last_consolidation_age_days": _age_days(last_consol),
    }
    comp["dream"] = dream
    if dream["consolidation_count"] == 0:
        if dream["signals_since_last"] > 0:
            warn.append(
                f"Dream 從未觸發(consolidation_count=0)但已累積 {dream['signals_since_last']} "
                "個訊號——定期做夢整理未運作(原則④)。")
    else:
        age = dream["last_consolidation_age_days"]
        if age is not None and age > STALE_AFTER_DAYS and dream["signals_since_last"] > 0:
            warn.append(
                f"Dream 上次整理已 {age:.0f} 天前且有 {dream['signals_since_last']} 個待整併"
                "訊號——可能停滯(原則④)。")

    # -- usermind ------------------------------------------------------------
    proposal = root / "_user_summary_proposal.md"
    usermind = {"proposal_exists": proposal.is_file(),
                "proposal_updated": _iso(proposal.stat().st_mtime) if proposal.is_file() else None}
    comp["usermind"] = usermind

    return {"components": comp, "warnings": warn,
            "generated_at": datetime.now(timezone.utc).isoformat()}


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "—"


def format_report(rep: Dict[str, Any]) -> str:
    c = rep["components"]
    L: List[str] = ["", "mem4 doctor — 元件健康檢查", "─" * 40]

    rc = c["recall"]
    L.append(f"  ① recall    : {rc['docs']} docs (turn={rc['turns']} / microfile={rc['microfiles']})"
             f"  last={rc['last_indexed'] or '—'}")

    pf = c["prefetch"]
    L.append(f"  prefetch    : {pf['events']} events  trigger={_fmt_pct(pf['trigger_rate'])}"
             f"  avg_inject={pf['avg_injected_chars'] if pf['avg_injected_chars'] is not None else '—'} chars"
             f"  last={pf['last_event'] or '—'}")

    rt = c["route_tools"]
    L.append(f"  tools(route): model-initiated={rt['tool_calls']}  "
             f"search_hit={_fmt_pct(rt['search_hit_rate'])} route_hit={_fmt_pct(rt['route_hit_rate'])}")

    rf = c["refine"]
    zone = "積極" if rf["aggressive_zone"] else "被動"
    L.append(f"  ② refine    : 熱區 {rf['memory_chars']}/{rf['char_limit']} 字 "
             f"(填充 {rf['fill_ratio']:.0%}·{zone})  microfiles={rf['microfiles_on_disk']}"
             f"  ever_applied={rf['ever_applied']}  last={rf['last_refine_event'] or '—'}")

    dr = c["dream"]
    age = dr["last_consolidation_age_days"]
    age_s = f"{age:.0f}d ago" if age is not None else "never"
    L.append(f"  ④ dream     : consolidations={dr['consolidation_count']} ({age_s})  "
             f"signals={dr['signals_since_last']}/{dr['threshold']}")

    um = c["usermind"]
    L.append(f"  usermind    : proposal={'yes' if um['proposal_exists'] else 'no'}"
             f"{(' @' + um['proposal_updated']) if um['proposal_updated'] else ''}")

    L.append("")
    if rep["warnings"]:
        L.append("  ⚠ 建議：")
        for w in rep["warnings"]:
            L.append(f"    • {w}")
    else:
        L.append("  ✓ 無退化告警。")
    L.append("")
    return "\n".join(L)
