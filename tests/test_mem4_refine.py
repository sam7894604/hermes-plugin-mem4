"""Tests for mem4 §3 refine — MEMORY.md 精煉 propose/apply/restore.

Covers the safety guarantees: dry-run never touches built-in files; --apply
backs up first + is atomic + never silently overwrites microfiles; --restore
recovers the original; and the auto paths (bootstrap / Dream④) only refresh a
proposal, never rewrite MEMORY.md.
"""

import os

import pytest

from mem4 import Mem4MemoryProvider
from mem4.audit import Auditor, AUDIT_DB_FILENAME
from mem4.refine import RefinePlanner


SECTION_MEMORY = """# MEMORY.md

前言：這幾行沒有 § 歸屬，是必要核心，精煉後應留在常駐區。

§sys 系統與環境
部署目標主機 toothless 在 lightnode 上，Tokyo VPS，透過 tailscale 連線。
gateway 由 systemd 管理，重啟會有數秒中斷；設定檔在 /etc/hermes/config.yaml。
磁碟配額有限，log 需定期輪替；備份腳本每日凌晨три點跑一次到 rsync 目標。
Python 版本鎖在 3.13，虛擬環境在 ~/.hermes/venv，套件用 uv 管理不用 pip。

§fam 人物
Sam 是主要使用者，偏好繁體中文，時區 Asia/Taipei，工作時段多在深夜。
決策風格：先給建議再問確認；不喜歡被反覆追問澄清；重視可還原與安全保證。
常用工具鏈：PowerShell 為主、Bash 為輔；編輯器 VS Code；筆記在 Obsidian vault。

§proto 協定
建置指令失敗要拋出，不准用管線截斷收尾（tail 會吞退出碼造成假成功）。
驗證不自驗：宣稱完成前用 fresh-context subagent 或實跑取得證據。
指揮官不下場：讀檔超過一百行、搜尋超過三個檔案就派 subagent，主對話只收結論。
"""


def _memory_path(tmp_path):
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d / "MEMORY.md"


def _write_memory(tmp_path, text):
    p = _memory_path(tmp_path)
    p.write_text(text, encoding="utf-8")
    return p


# -- parsing / plan ----------------------------------------------------------

def test_plan_section_mode(tmp_path):
    _write_memory(tmp_path, SECTION_MEMORY)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.source_exists and plan.mode == "section"
    codes = [s.code for s in plan.sections]
    assert codes == ["sys", "fam", "proto"]
    # preamble (無 § 歸屬核心) preserved
    assert "必要核心" in plan.preamble
    # condensed carries a routing index and is smaller than the source
    assert "路由索引" in plan.condensed
    assert plan.after_bytes < plan.before_bytes
    assert plan.before_tokens > plan.after_tokens


def test_duplicate_code_sections_merge(tmp_path):
    # 手寫 MEMORY.md 常見：同一 §code 出現多次，且用裸 § 當分隔行。
    text = (
        "§sys 系統一\n甲設定值\n"
        "§\n"
        "§fam 人物\n乙\n"
        "§\n"
        "§sys 系統二\n丙設定值\n"
    )
    _write_memory(tmp_path, text)
    plan = RefinePlanner(tmp_path).plan()
    # 同 code 合併成單檔（route code → 單檔）；不再出現 sys-2
    codes = [s.code for s in plan.sections]
    assert codes == ["sys", "fam"]
    sys_body = next(s.body for s in plan.sections if s.code == "sys")
    assert "甲設定值" in sys_body and "丙設定值" in sys_body
    # 裸 § 分隔行不留在 body
    assert "§" not in sys_body


def test_inline_content_not_lost(tmp_path):
    # 手寫 MEMORY.md 常把整條記憶寫在 §code 那一行（inline），內容不可只進摘要。
    text = (
        "§fam 玄0965·毅0910·地址某處·NICU→N4\n"
        "§\n"
        "§sys claude-proxy·cron07:00·pip=cd venv\n"
        "orphan 這行沒有 § 前綴但也不可遺失\n"
        "§adr ADR018四層路由·subagent>timeout\n"
    )
    _write_memory(tmp_path, text)
    plan = RefinePlanner(tmp_path).plan()
    allbody = plan.preamble + "\n" + "\n".join(s.body for s in plan.sections)
    # 每個 § 標記行的 inline 內容都必須落在某個 body 裡（不是被截斷進摘要）
    for token in ["玄0965", "NICU→N4", "claude-proxy", "pip=cd venv",
                  "orphan 這行沒有", "ADR018四層路由", "subagent>timeout"]:
        assert token in allbody, f"content lost: {token}"


def test_dry_run_touches_nothing(tmp_path):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    planner.plan()  # dry-run: compute only
    assert p.read_text(encoding="utf-8") == original
    assert not (tmp_path / "mem4" / "sys.md").exists()
    assert not (tmp_path / "mem4" / "_refine_backups").exists()


# -- apply -------------------------------------------------------------------

def test_apply_backs_up_and_extracts(tmp_path):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    result = planner.apply()
    assert result["applied"] is True
    # MEMORY.md rewritten to the condensed index
    new_text = p.read_text(encoding="utf-8")
    assert new_text != original and "路由索引" in new_text
    assert len(new_text) < len(original)
    # microfiles written with the section bodies
    sys_mf = tmp_path / "mem4" / "sys.md"
    assert sys_mf.is_file() and "toothless" in sys_mf.read_text(encoding="utf-8")
    # backup of the original exists and matches byte-for-byte
    backups = planner.list_backups()
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_apply_never_silently_overwrites_microfile(tmp_path):
    _write_memory(tmp_path, SECTION_MEMORY)
    root = tmp_path / "mem4"
    root.mkdir(parents=True, exist_ok=True)
    # a pre-existing sys.md with different content must be backed up, not lost
    (root / "sys.md").write_text("既有微檔內容 do-not-lose", encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    result = planner.apply()
    assert result["overwritten_microfiles"] == 1
    mf_backups = list((root / "_refine_backups").glob("microfiles-*/sys.md"))
    assert len(mf_backups) == 1
    assert "do-not-lose" in mf_backups[0].read_text(encoding="utf-8")


def test_restore_recovers_original(tmp_path):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    planner.apply()
    assert p.read_text(encoding="utf-8") != original
    result = planner.restore()
    assert result["restored"] is True
    assert p.read_text(encoding="utf-8") == original


def test_restore_by_timestamp_and_missing(tmp_path):
    _write_memory(tmp_path, SECTION_MEMORY)
    planner = RefinePlanner(tmp_path)
    res = planner.apply()
    stamp = res["stamp"]
    assert planner.restore(stamp)["restored"] is True
    assert planner.restore("nonexistent-ts")["restored"] is False


def test_atomic_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    planner = RefinePlanner(tmp_path)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    result = planner.apply()
    assert result["applied"] is False
    # original MEMORY.md untouched; backup still made (safety-first)
    assert p.read_text(encoding="utf-8") == original
    assert planner.list_backups()  # backup written before the rewrite attempt


# -- fallbacks ---------------------------------------------------------------

def test_heading_mode_fallback(tmp_path):
    text = "# Title\n\nintro line\n\n## Alpha\nbody a\n\n## Beta\nbody b\n"
    _write_memory(tmp_path, text)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.mode == "heading"
    assert [s.code for s in plan.sections] == ["alpha", "beta"]


def test_cjk_headings_get_valid_codes(tmp_path):
    text = "# 標題\n\n引言\n\n## 系統設定\n甲\n\n## 人物關係\n乙\n"
    _write_memory(tmp_path, text)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.mode == "heading"
    # non-ascii headings degrade to s1/s2 but stay valid route codes
    assert [s.code for s in plan.sections] == ["s1", "s2"]


def test_chunk_mode_fallback(tmp_path):
    text = "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(6))
    _write_memory(tmp_path, text)
    plan = RefinePlanner(tmp_path).plan()
    assert plan.mode == "chunk"
    assert len(plan.sections) >= 2
    assert all(s.code.startswith("part") for s in plan.sections)


def test_no_memory_file_is_graceful(tmp_path):
    planner = RefinePlanner(tmp_path)  # no memories/MEMORY.md written
    plan = planner.plan()
    assert plan.source_exists is False
    assert planner.apply()["applied"] is False
    assert planner.restore()["restored"] is False


# -- proposal refresh (auto path — never applies) ----------------------------

def test_refresh_proposal_never_touches_memory(tmp_path):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    planner = RefinePlanner(tmp_path)
    plan = planner.refresh_proposal()
    assert plan is not None and plan.sections
    # proposal file written under mem4-owned tree
    assert planner.proposal_path.is_file()
    assert "精煉提案" in planner.proposal_path.read_text(encoding="utf-8")
    # built-in MEMORY.md untouched, no microfiles extracted
    assert p.read_text(encoding="utf-8") == original
    assert not (tmp_path / "mem4" / "sys.md").exists()


# -- audit -------------------------------------------------------------------

def test_apply_records_audit_event(tmp_path):
    _write_memory(tmp_path, SECTION_MEMORY)
    auditor = Auditor(
        tmp_path / "mem4" / AUDIT_DB_FILENAME,
        enabled=True, arm="experiment", session_id="t",
    )
    RefinePlanner(tmp_path, auditor=auditor).apply()
    events = [e for e in auditor.read_events() if e["kind"] == "refine"]
    assert len(events) == 1
    ev = events[0]
    assert ev["tool_called"] == "mem4_refine" and ev["route"] == "refine"
    # paired_diff = before − after tokens ⇒ hot-zone tokens removed (> 0)
    assert ev["paired_diff"] > 0
    assert "microfiles=3" in ev["query"]


# -- provider trigger (bootstrap refresh, never auto-apply) ------------------

def test_provider_bootstrap_refreshes_proposal_only(tmp_path):
    p = _write_memory(tmp_path, SECTION_MEMORY)
    original = p.read_text(encoding="utf-8")
    provider = Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": False},
        "audit": {"enabled": False},
    })
    provider.initialize("s1", hermes_home=str(tmp_path))
    provider.shutdown()
    # first bootstrap ⇒ proposal refreshed, MEMORY.md left intact
    assert (tmp_path / "mem4" / "_refine_proposal.md").is_file()
    assert p.read_text(encoding="utf-8") == original
