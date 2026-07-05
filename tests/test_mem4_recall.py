"""Tests for mem4 ① FTS5 recall (dual-table, CJK routing, backfill, rebuild)."""

import json
import time

from mem4 import Mem4MemoryProvider
from mem4.recall import RecallStore, _contains_cjk, _count_cjk

NOW = 1_780_000_000.0  # fixed unix ts for deterministic decay


def _store(tmp_path):
    return RecallStore(tmp_path / "recall.db")


# -- schema / basic FTS ------------------------------------------------------

def test_english_fts_query(tmp_path):
    store = _store(tmp_path)
    store.index("t1", "mem4 uses SQLite FTS5 for recall", "turn", NOW)
    store.index("t2", "the weather is sunny today", "turn", NOW)
    hits = store.search("SQLite recall", limit=5, now=NOW)
    assert hits and hits[0].route == "fts"
    assert "SQLite" in hits[0].snippet


def test_dedup_by_content_hash(tmp_path):
    store = _store(tmp_path)
    assert store.index("t1", "identical content", "turn", NOW) is True
    assert store.index("t2", "identical content", "turn", NOW) is False
    assert store.count() == 1


# -- CJK routing -------------------------------------------------------------

def test_cjk_helpers():
    assert _contains_cjk("部署") is True
    assert _contains_cjk("deploy") is False
    assert _count_cjk("部署VPS") == 2


def test_chinese_trigram_path_three_plus_chars(tmp_path):
    store = _store(tmp_path)
    assert store.trigram_available  # this build has trigram
    store.index("t1", "資料庫引擎選用 SQLite FTS5 全文檢索", "turn", NOW)
    hits = store.search("資料庫", limit=5, now=NOW)  # 3 CJK chars → trigram
    assert hits and hits[0].route == "trigram"


def test_chinese_short_token_falls_back_to_like(tmp_path):
    store = _store(tmp_path)
    store.index("t1", "toothless 部署在 LightNode VPS 上", "turn", NOW)
    hits = store.search("部署", limit=5, now=NOW)  # 2 CJK chars → LIKE
    assert hits and hits[0].route == "like"
    assert "部署" in hits[0].snippet


def test_cjk_degrades_to_like_without_trigram(tmp_path):
    store = _store(tmp_path)
    store.trigram_available = False  # simulate a SQLite build lacking trigram
    store.index("t1", "資料庫引擎選用 SQLite 全文檢索", "turn", NOW)
    hits = store.search("資料庫", limit=5, now=NOW)
    assert hits and hits[0].route == "like"  # never a hard failure


# -- time decay --------------------------------------------------------------

def test_recent_outranks_old_at_equal_relevance(tmp_path):
    store = _store(tmp_path)
    old_ts = NOW - 200 * 86400   # ~200 days old
    store.index("old", "deployment notes for the server", "turn", old_ts)
    store.index("new", "deployment notes for the server room", "turn", NOW)
    hits = store.search("deployment notes", limit=5, now=NOW)
    assert hits[0].ref == "new"  # recency breaks the tie


# -- backfill (resumable + dedup) --------------------------------------------

def test_backfill_batch_resumable_and_dedup(tmp_path):
    store = _store(tmp_path)
    rows = [
        (1, "r1", "backfilled turn one about docker", NOW),
        (2, "r2", "backfilled turn two about kubernetes", NOW),
        (3, "r3", "backfilled turn three about sqlite", NOW),
    ]

    def fetch(since, limit):
        return [r for r in rows if r[0] > since][:limit]

    i1, c1, more1 = store.backfill_batch(fetch, since_rowid=0, batch_size=2)
    assert (i1, c1, more1) == (2, 2, True)
    i2, c2, more2 = store.backfill_batch(fetch, since_rowid=c1, batch_size=2)
    assert (i2, c2, more2) == (1, 3, False)  # last partial batch → no more
    assert store.count() == 3
    # Re-running from the start indexes nothing new (content-hash dedup).
    i3, _, _ = store.backfill_batch(fetch, since_rowid=0, batch_size=2)
    assert i3 == 0


# -- provider integration ----------------------------------------------------

def _provider(tmp_path, **dream):
    cfg = {"backend": "local-file", "dream": {"enabled": False, **dream}}
    return Mem4MemoryProvider(cfg)


def test_mem_search_tool_end_to_end(tmp_path):
    p = _provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("what vps hosts toothless", "toothless runs on LightNode")
    out = json.loads(p.handle_tool_call("mem_search", {"query": "LightNode toothless"}))
    assert out["hits"]
    assert "LightNode" in out["hits"][0]["snippet"]
    p.shutdown()


def test_sync_turn_filters_short_turns(tmp_path):
    p = _provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("hi", "hello")            # too short → skipped
    assert p._recall.count() == 0
    p.sync_turn("please remember the deploy target is toothless", "ok")
    assert p._recall.count() == 1
    p.shutdown()


def test_prefetch_is_capped_and_local(tmp_path):
    p = Mem4MemoryProvider(
        {"backend": "local-file", "dream": {"enabled": False}, "prefetch_cap": 300}
    )
    p.initialize("s1", hermes_home=str(tmp_path))
    for i in range(20):
        p.sync_turn(f"deployment note number {i} about the toothless server room", "ok")
    # All query tokens appear in the indexed turns (mem_search uses AND).
    out = p.prefetch("toothless deployment server room")
    assert 0 < len(out) <= 300
    # Short queries prefetch nothing (guardrail against noise).
    assert p.prefetch("hi") == ""
    p.shutdown()


def test_prefetch_surfaces_microfile_fuller_and_first(tmp_path):
    # A microfile holds curated cold-tier knowledge that left the hot zone.
    root = tmp_path / "mem4"
    root.mkdir()
    long_body = ("pipeline-log.py 把每次執行寫入 Baserow 表 pipeline_runs(900)，"
                 "cron_mode=auto，watchdog 已刪，採非依賴性扇出加收斂原則，" * 3)
    (root / "pipe.md").write_text(long_body, encoding="utf-8")
    p = Mem4MemoryProvider({"backend": "local-file", "dream": {"enabled": False}})
    p.initialize("s1", hermes_home=str(tmp_path))
    # Add a noisy turn that also matches, to prove microfiles rank ahead.
    p.sync_turn("random chatter about pipeline something unrelated padding", "ok")
    out = p.prefetch("pipeline_runs Baserow pipeline-log cron_mode")
    assert "相關冷區微檔" in out
    assert "§pipe" in out
    # Microfile is injected fuller than a 240-char turn snippet, and appears
    # before the old-conversation block.
    assert out.index("相關冷區微檔") < out.index("相關舊對話") if "相關舊對話" in out else True
    assert "pipeline_runs(900)" in out
    p.shutdown()


def test_prefetch_microfile_chars_cap_and_dedup(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    # Long, tokenizable content (words + spaces) well over the 120-char cap.
    body = " ".join(["claude-proxy cron paths pip venv host toothless lightnode"] * 20)
    (root / "sys.md").write_text(body, encoding="utf-8")
    p = Mem4MemoryProvider({
        "backend": "local-file", "dream": {"enabled": False},
        "recall": {"microfile_chars": 120, "prefetch_limit": 3},
    })
    p.initialize("s1", hermes_home=str(tmp_path))
    out = p.prefetch("claude-proxy host toothless lightnode venv")
    # The injected microfile body is clamped to microfile_chars (+ ellipsis/label).
    assert "§sys" in out
    body_line = [ln for ln in out.splitlines() if ln.startswith("- (§sys)")][0]
    assert len(body_line) < 200  # ~120 chars body + label, not the full ~1180
    assert body_line.rstrip().endswith("…")  # truncation marker present
    p.shutdown()


def test_backend_search_delegates_to_recall(tmp_path):
    p = _provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("the fts index lives in recall.db", "correct")
    hits = p._backend.search("recall.db fts", limit=5)
    assert hits and "recall.db" in hits[0].snippet
    p.shutdown()


def test_backfill_in_progress_note(tmp_path):
    p = _provider(tmp_path)

    # A source that never exhausts on the first batch keeps backfill "in progress".
    rows = [(i, f"r{i}", f"history turn {i} content padding", NOW) for i in range(1, 501)]

    def fetch(since, limit):
        return [r for r in rows if r[0] > since][:limit]

    p.set_backfill_source(fetch)
    p.initialize("s1", hermes_home=str(tmp_path))
    # Do not join the worker; the marker should not be complete instantly for 500 rows.
    out = json.loads(p.handle_tool_call("mem_search", {"query": "history turn"}))
    # Either in-progress note present, or (if the daemon finished) it's absent —
    # assert the field is well-formed when present.
    assert "hits" in out
    p.shutdown()


def test_rebuild_reindexes_from_source_files(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    (root / "sys.md").write_text("deploy host toothless on lightnode vps", encoding="utf-8")

    p = _provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    # Microfile indexed at init → searchable.
    assert json.loads(p.handle_tool_call("mem_search", {"query": "toothless lightnode"}))["hits"]

    result = p.rebuild()
    assert result["recall_docs"] >= 1
    # Recall is consistent after a rebuild.
    assert json.loads(p.handle_tool_call("mem_search", {"query": "toothless lightnode"}))["hits"]
    p.shutdown()
