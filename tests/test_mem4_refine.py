"""Tests for mem4 §3 refine — persistent \\n§\\n entry-format MEMORY.md refine.

The condensed output must be the memory tool's native format (entries joined by
``\\n§\\n``): each §code entry is extracted to an L2 microfile and replaced by a
one-line routing pointer; un-attributed entries stay as core. This keeps the
built-in memory tool from clobbering the refinement — verified here by
replicating its drift check.
"""

import os

from mem4 import Mem4MemoryProvider
from mem4.audit import Auditor, AUDIT_DB_FILENAME
from mem4.refine import (
    RefinePlanner, ENTRY_DELIMITER, _POINTER_MARK, _pointer_entry, _is_pointer,
)


# A MEMORY.md in the memory tool's native format: entries joined by \n§\n.
# Two coded entries (§bali, §nas) + two un-attributed core entries.
CORE1 = "ADR-020 Pipeline v3：runner.py + YAML configs、5 pipelines、parallel collectors"
CORE2 = "寶寶出國前會問穿搭/換錢/藥物等實用問題——偏好直接給數字結論+細項拆表"
# Coded entries are long (> summary cap) so the pointer is a truncated prefix and
# the distinctive marker sits BEYOND the summary cutoff (真正被抽走、不在熱區).
BALI = ("§bali 寶寶叔鼠 2026-07-07 到 07-11 峇里島行程，前兩晚住 Solia Legian、後兩晚住 "
        "AYANA RIMBA，安排烏布 ATV 與梯田鞦韆、貝尼達島浮潛、岩石酒吧看夕陽、金巴蘭海灘"
        "龍蝦 BBQ 晚餐，機場接送與換匯細節出發前再確認一次")
NAS = ("§nas DS918+ 群暉,經 Tailscale 100.98.31.81:6322 SSH,root key 名 toothless-nas,"
       "共 18 個容器含 Plex/Grafana/NPM/StirlingPDF:8082,Docker 二進位在 ContainerManager,"
       "所有變更走審查制:先 inspect 再等叔鼠 approval 才一次批量執行,絕不自作主張")

MEMORY = ENTRY_DELIMITER.join([CORE1, BALI, CORE2, NAS])

# distinctive words that sit BEYOND the ~110-char summary cutoff → must leave the
# hot zone entirely (present only in the microfile, not in the condensed pointer).
BALI_TAIL = "金巴蘭"
NAS_TAIL = "絕不自作主張"


def _memories(tmp_path):
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_memory(tmp_path, text):
    p = _memories(tmp_path) / "MEMORY.md"
    p.write_text(text, encoding="utf-8")
    return p


# -- drift-safety: the condensed output must not trip memory_tool drift -------

def _memory_tool_drifts(raw: str, char_limit: int = 2200) -> bool:
    """Replicate tools/memory_tool.py _detect_external_drift's two signals."""
    parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    roundtrip = ENTRY_DELIMITER.join(parsed)
    max_entry = max((len(e) for e in parsed), default=0)
    return (raw.strip() != roundtrip) or (max_entry > char_limit)


def test_condensed_is_drift_clean(tmp_path):
    _write_memory(tmp_path, MEMORY)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.mode == "entry" and plan.changed
    # round-trips through \n§\n and every entry is small → no memory-tool drift
    assert _memory_tool_drifts(plan.condensed) is False
    # coded entries became pointer entries; core entries preserved verbatim
    assert _POINTER_MARK in plan.condensed
    assert "mem_route(bali)" in plan.condensed and "mem_route(nas)" in plan.condensed
    assert CORE1 in plan.condensed and CORE2 in plan.condensed
    # the tail of each coded body has left the hot zone (extracted to microfile)
    assert BALI_TAIL not in plan.condensed and NAS_TAIL not in plan.condensed
    assert plan.after_bytes < plan.before_bytes


def test_apply_extracts_to_microfiles_and_pointers(tmp_path):
    p = _write_memory(tmp_path, MEMORY)
    planner = RefinePlanner(tmp_path)
    result = planner.apply()
    assert result["applied"] is True and result["microfiles"] == 2
    # microfiles hold the full bodies
    assert BALI_TAIL in (tmp_path / "mem4" / "bali.md").read_text(encoding="utf-8")
    assert NAS_TAIL in (tmp_path / "mem4" / "nas.md").read_text(encoding="utf-8")
    # MEMORY.md is now short pointers + core, drift-clean
    new = p.read_text(encoding="utf-8")
    assert _memory_tool_drifts(new) is False
    assert "mem_route(bali)" in new and BALI_TAIL not in new
    # backup of the original
    assert planner.list_backups()[0].read_text(encoding="utf-8") == MEMORY


# -- idempotency: re-refining an already-refined file is a no-op -------------

def test_rerefine_is_noop(tmp_path):
    _write_memory(tmp_path, MEMORY)
    planner = RefinePlanner(tmp_path)
    planner.apply()
    refined = (tmp_path / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    # plan on the already-refined file: unchanged
    plan2 = planner.plan()
    assert plan2.changed is False
    assert planner.apply()["applied"] is False        # no-op
    # apply_if_changed short-circuits via hash state
    assert planner.apply_if_changed()["reason"] == "unchanged since last refine"
    # file untouched
    assert (tmp_path / "memories" / "MEMORY.md").read_text(encoding="utf-8") == refined


def test_dream_reextracts_new_inline_entry(tmp_path):
    # start refined, then the memory tool appends a NEW coded entry
    p = _write_memory(tmp_path, MEMORY)
    planner = RefinePlanner(tmp_path)
    planner.apply()
    refined = p.read_text(encoding="utf-8")
    cron = ("§cron runner.py 所有 cron job 都要用顯式 polling loop、絕不能只靠 "
            "notify_on_complete，因為 agent 會輸出 waiting 就結束，2026-07-06 已修正美股"
            "盤後、美股盤前、台股盤前、台股盤後、午間動態五個排程的收尾邏輯")
    p.write_text(refined + ENTRY_DELIMITER + cron, encoding="utf-8")
    # apply_if_changed picks up the change and extracts the new entry
    result = planner.apply_if_changed()
    assert result["applied"] is True
    after = p.read_text(encoding="utf-8")
    assert "mem_route(cron)" in after and "午間動態五個排程" not in after
    assert "午間動態五個排程" in (tmp_path / "mem4" / "cron.md").read_text(encoding="utf-8")
    # existing bali/nas pointers preserved (not re-extracted / duplicated)
    assert after.count("mem_route(bali)") == 1


def test_pointer_entries_are_not_reextracted(tmp_path):
    # a file that is ALL pointers + core → nothing to extract
    ptrs = ENTRY_DELIMITER.join([
        CORE1, _pointer_entry("bali", "峇里島"), _pointer_entry("nas", "NAS 設定")])
    _write_memory(tmp_path, ptrs)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.sections == [] and plan.changed is False


# -- safety: backup / restore / atomic-failure -------------------------------

def test_restore_recovers_original(tmp_path):
    p = _write_memory(tmp_path, MEMORY)
    planner = RefinePlanner(tmp_path)
    planner.apply()
    assert p.read_text(encoding="utf-8") != MEMORY
    assert planner.restore()["restored"] is True
    assert p.read_text(encoding="utf-8") == MEMORY


def test_atomic_failure_leaves_original_intact(tmp_path, monkeypatch):
    p = _write_memory(tmp_path, MEMORY)
    planner = RefinePlanner(tmp_path)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    result = planner.apply()
    assert result["applied"] is False
    assert p.read_text(encoding="utf-8") == MEMORY      # untouched
    assert planner.list_backups()                        # backup still made


def test_never_silently_overwrites_microfile(tmp_path):
    _write_memory(tmp_path, MEMORY)
    root = tmp_path / "mem4"
    root.mkdir(parents=True, exist_ok=True)
    (root / "bali.md").write_text("既有峇里島微檔 do-not-lose", encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    result = planner.apply()
    # merge keeps old content AND adds new; the pre-merge original is backed up
    merged = (root / "bali.md").read_text(encoding="utf-8")
    assert "do-not-lose" in merged and BALI_TAIL in merged
    assert result["overwritten_microfiles"] == 1
    bak = list((root / "_refine_backups").glob("microfiles-*/bali.md"))
    assert bak and "do-not-lose" in bak[0].read_text(encoding="utf-8")


def test_no_memory_file_graceful(tmp_path):
    planner = RefinePlanner(tmp_path)
    assert planner.plan().source_exists is False
    assert planner.apply()["applied"] is False
    assert planner.apply_if_changed()["applied"] is False
    assert planner.restore()["restored"] is False


# -- the refined file remains a valid memory-tool store ----------------------

def test_refined_file_accepts_memory_tool_add_remove(tmp_path):
    # After refine, simulate the memory tool: load (split), add an entry, save
    # (join), and confirm it still parses cleanly and refine handles it next.
    p = _write_memory(tmp_path, MEMORY)
    RefinePlanner(tmp_path).apply()
    refined = p.read_text(encoding="utf-8")
    entries = [e.strip() for e in refined.split(ENTRY_DELIMITER) if e.strip()]
    # add + remove like the memory tool does
    entries.append("新記憶：測試 memory 工具相容")
    entries = [e for e in entries if "ADR-020" not in e]  # remove one
    rewritten = ENTRY_DELIMITER.join(entries)
    p.write_text(rewritten, encoding="utf-8")
    assert _memory_tool_drifts(rewritten) is False
    # refine still works on the memory-tool-rewritten file
    plan = RefinePlanner(tmp_path).plan()
    assert plan.mode == "entry"


# -- audit -------------------------------------------------------------------

def test_apply_records_audit_event(tmp_path):
    _write_memory(tmp_path, MEMORY)
    auditor = Auditor(tmp_path / "mem4" / AUDIT_DB_FILENAME,
                      enabled=True, arm="experiment", session_id="t")
    RefinePlanner(tmp_path, auditor=auditor).apply()
    events = [e for e in auditor.read_events() if e["kind"] == "refine"]
    assert len(events) == 1 and events[0]["route"] == "refine"
    assert events[0]["paired_diff"] > 0                  # hot-zone tokens removed


# -- provider Dream④ toggle --------------------------------------------------

def test_provider_persist_on_dream_toggle(tmp_path):
    p = _write_memory(tmp_path, MEMORY)
    # toggle OFF (default): Dream refreshes proposal only, MEMORY.md untouched
    prov = Mem4MemoryProvider({"backend": "local-file", "dream": {"enabled": True},
                               "audit": {"enabled": False}})
    prov.initialize("s1", hermes_home=str(tmp_path))
    prov.shutdown()
    assert p.read_text(encoding="utf-8") == MEMORY       # not applied
    assert (tmp_path / "mem4" / "_refine_proposal.md").is_file()

    # toggle ON: Dream persists (re-refine applied)
    prov2 = Mem4MemoryProvider({"backend": "local-file", "dream": {"enabled": True},
                                "audit": {"enabled": False},
                                "refine": {"persist_on_dream": True}})
    prov2.initialize("s2", hermes_home=str(tmp_path))
    prov2.shutdown()
    after = p.read_text(encoding="utf-8")
    assert "mem_route(bali)" in after and BALI_TAIL not in after
