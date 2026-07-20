"""Tests for C-⑤ mem4 doctor — read-only component health + idle warnings."""

import json
import time
from datetime import datetime, timezone, timedelta

from mem4.doctor import collect, format_report
from mem4.recall import RecallStore


def _home(tmp_path):
    (tmp_path / "memories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mem4").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_dream_state(tmp_path, **kw):
    (tmp_path / "mem4" / ".dream_state.json").write_text(
        json.dumps(kw), encoding="utf-8")


def test_empty_home_does_not_crash(tmp_path):
    _home(tmp_path)
    rep = collect(tmp_path)
    assert rep["components"]["recall"]["docs"] == 0
    assert rep["components"]["dream"]["consolidation_count"] == 0
    # signals_since_last defaults 0 → no dream warning on a truly empty home
    assert not any("Dream" in w for w in rep["warnings"])
    assert "mem4 doctor" in format_report(rep)


def test_dream_never_ran_with_pending_signals_warns(tmp_path):
    _home(tmp_path)
    _write_dream_state(tmp_path, consolidation_count=0, signals_since_last=5)
    rep = collect(tmp_path)
    assert any("Dream 從未觸發" in w for w in rep["warnings"])


def test_dream_stale_warns(tmp_path):
    _home(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    _write_dream_state(tmp_path, consolidation_count=3,
                       last_consolidation_at=old, signals_since_last=4)
    rep = collect(tmp_path)
    assert rep["components"]["dream"]["last_consolidation_age_days"] > 14
    assert any("停滯" in w for w in rep["warnings"])


def test_refine_aggressive_zone_without_apply_warns(tmp_path):
    _home(tmp_path)
    (tmp_path / "memories" / "MEMORY.md").write_text("x" * 200, encoding="utf-8")
    rep = collect(tmp_path, char_limit=200)          # fill = 100% ≥ 0.80
    assert rep["components"]["refine"]["aggressive_zone"] is True
    assert any("refine 從未套用" in w for w in rep["warnings"])


def test_refine_aggressive_but_applied_no_warn(tmp_path):
    _home(tmp_path)
    (tmp_path / "memories" / "MEMORY.md").write_text("x" * 200, encoding="utf-8")
    (tmp_path / "mem4" / "_refine_state.json").write_text(
        json.dumps({"last_applied_hash": "abc"}), encoding="utf-8")
    rep = collect(tmp_path, char_limit=200)
    assert rep["components"]["refine"]["ever_applied"] is True
    assert not any("refine 從未套用" in w for w in rep["warnings"])


def test_recall_counts_and_healthy_home_no_warnings(tmp_path):
    _home(tmp_path)
    # a populated recall store
    rs = RecallStore(tmp_path / "mem4" / "recall.db")
    rs.index("turn:1", "峇里島 記帳 匯率", "turn", time.time())
    rs.index("microfile:sys", "host toothless", "microfile", time.time())
    rs.close()
    # small hot zone (low fill) + refine applied + dream recently ran, no pending
    (tmp_path / "memories" / "MEMORY.md").write_text("short", encoding="utf-8")
    _write_dream_state(tmp_path, consolidation_count=2,
                       last_consolidation_at=datetime.now(timezone.utc).isoformat(),
                       signals_since_last=0)
    rep = collect(tmp_path, char_limit=2200)
    assert rep["components"]["recall"]["docs"] == 2
    assert rep["components"]["recall"]["turns"] == 1
    assert rep["components"]["recall"]["microfiles"] == 1
    assert rep["warnings"] == []
    assert "✓ 無退化告警" in format_report(rep)
