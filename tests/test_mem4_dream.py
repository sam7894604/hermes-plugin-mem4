"""Tests for mem4 ④ Dream consolidation (in-provider, no external cron).

Covers: threshold trigger, idle skip, staleness-floor boundary trigger,
marker+lock mutual exclusion, "only touches mem4 L2/L3 (never the built-in hot
zone)", and feature-flag-off being a complete no-op.
"""

import json
from datetime import datetime, timedelta, timezone

from mem4 import Mem4MemoryProvider
from mem4.dream import DreamProcessor, DreamState

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _seed_mirror(root, target, bodies):
    d = root / "_mirror"
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(f"\n<!-- 2026-07-04T00:00:00 add -->\n{b}\n" for b in bodies)
    (d / f"{target}.md").write_text(text, encoding="utf-8")


# -- DreamProcessor unit tests ----------------------------------------------

def test_threshold_triggers_and_dedups(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    _seed_mirror(root, "memory", ["alpha", "alpha", "beta"])  # 3 → 2 after dedup
    dp = DreamProcessor(root, enabled=True, threshold=3, staleness_days=7)

    dp.record_signal(3)
    res = dp.maybe_consolidate("threshold", now=NOW)

    assert res.ran is True
    assert res.reason == "threshold"
    assert res.targets == {"memory": {"before": 3, "after": 2}}

    state = dp.load()
    assert state.consolidation_count == 1
    assert state.signals_since_last == 0
    assert state.last_consolidation_at == NOW.isoformat()
    # Original archived before rewrite — nothing lost.
    assert (root / "_mirror" / "_archive").is_dir()


def test_idle_skip_no_pending_signal(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    dp = DreamProcessor(root, enabled=True, threshold=3, staleness_days=7)

    res = dp.maybe_consolidate("session_end", now=NOW)

    assert res.ran is False
    assert "idle" in res.skipped
    # Nothing was written when there was nothing to do.
    assert not (root / ".dream_state.json").exists()
    assert not (root / ".dream.lock").exists()


def test_staleness_floor_triggers_below_threshold(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    _seed_mirror(root, "user", ["x", "x"])
    dp = DreamProcessor(root, enabled=True, threshold=10, staleness_days=7)
    # One pending signal (below threshold 10), last consolidation 8 days ago.
    dp.save(DreamState(
        last_consolidation_at=(NOW - timedelta(days=8)).isoformat(),
        consolidation_count=1,
        signals_since_last=1,
    ))

    res = dp.maybe_consolidate("session_end", now=NOW)

    assert res.ran is True
    assert res.reason == "staleness"


def test_staleness_within_window_skips(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    dp = DreamProcessor(root, enabled=True, threshold=10, staleness_days=7)
    dp.save(DreamState(
        last_consolidation_at=(NOW - timedelta(days=2)).isoformat(),
        consolidation_count=1,
        signals_since_last=1,
    ))

    res = dp.maybe_consolidate("session_end", now=NOW)

    assert res.ran is False
    assert dp.load().consolidation_count == 1  # unchanged


def test_lock_makes_paths_mutually_exclusive(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    _seed_mirror(root, "memory", ["a", "a"])
    dp = DreamProcessor(root, enabled=True, threshold=1, staleness_days=7)
    dp.record_signal(2)

    # A fresh lock is held (simulating a concurrent consolidation).
    (root / ".dream.lock").write_text("held", encoding="utf-8")

    res = dp.maybe_consolidate("threshold", now=NOW)

    assert res.ran is False
    assert res.skipped == "locked"
    assert dp.load().consolidation_count == 0  # did not run


def test_disabled_is_complete_noop(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    _seed_mirror(root, "memory", ["a", "a"])
    dp = DreamProcessor(root, enabled=False, threshold=1, staleness_days=7)

    dp.record_signal(5)
    res = dp.maybe_consolidate("threshold", now=NOW)

    assert res.ran is False
    assert res.skipped == "disabled"
    assert not (root / ".dream_state.json").exists()
    assert not (root / ".dream.lock").exists()
    # Mirror left untouched (no dedup).
    body = (root / "_mirror" / "memory.md").read_text(encoding="utf-8")
    assert body.count("<!--") == 2


# -- provider integration ----------------------------------------------------

def test_provider_threshold_via_on_memory_write(tmp_path):
    provider = Mem4MemoryProvider(
        {"backend": "local-file", "dream": {"enabled": True, "threshold": 3, "staleness_days": 7}}
    )
    provider.initialize("s1", hermes_home=str(tmp_path))

    for i in range(3):
        provider.on_memory_write("add", "memory", f"fact number {i}")

    state = json.loads((tmp_path / "mem4" / ".dream_state.json").read_text(encoding="utf-8"))
    assert state["consolidation_count"] == 1
    assert state["signals_since_last"] == 0


def test_provider_dream_never_touches_builtin(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("BUILTIN MEMORY", encoding="utf-8")
    (memories / "USER.md").write_text("BUILTIN USER", encoding="utf-8")
    before = {p.name: p.read_text(encoding="utf-8") for p in memories.iterdir()}

    provider = Mem4MemoryProvider(
        {"backend": "local-file", "dream": {"enabled": True, "threshold": 2, "staleness_days": 7}}
    )
    provider.initialize("s1", hermes_home=str(tmp_path))

    # Two identical writes → threshold 2 → consolidation dedups mem4 mirror.
    provider.on_memory_write("add", "memory", "duplicate entry")
    provider.on_memory_write("add", "memory", "duplicate entry")

    # Built-in hot zone is byte-for-byte unchanged; no strays in memories/.
    assert {p.name: p.read_text(encoding="utf-8") for p in memories.iterdir()} == before
    assert sorted(p.name for p in memories.iterdir()) == ["MEMORY.md", "USER.md"]

    # Consolidation ran (on mem4-owned files) and archived the original.
    state = json.loads((tmp_path / "mem4" / ".dream_state.json").read_text(encoding="utf-8"))
    assert state["consolidation_count"] >= 1
    assert (tmp_path / "mem4" / "_mirror" / "_archive").is_dir()


def test_provider_dream_disabled_no_trigger(tmp_path):
    provider = Mem4MemoryProvider(
        {"backend": "local-file", "dream": {"enabled": False, "threshold": 1, "staleness_days": 7}}
    )
    provider.initialize("s1", hermes_home=str(tmp_path))

    for i in range(5):
        provider.on_memory_write("add", "memory", f"f{i}")
    provider.on_session_end([])

    # Dream never fired: no state, no lock.
    assert not (tmp_path / "mem4" / ".dream_state.json").exists()
    assert not (tmp_path / "mem4" / ".dream.lock").exists()
    # But mirroring (independent of Dream) still happened.
    assert (tmp_path / "mem4" / "_mirror" / "memory.md").is_file()


def test_provider_session_end_idle_skips(tmp_path):
    provider = Mem4MemoryProvider(
        {"backend": "local-file", "dream": {"enabled": True, "threshold": 3, "staleness_days": 7}}
    )
    provider.initialize("s1", hermes_home=str(tmp_path))

    # No writes → no pending signal → session end consolidates nothing.
    provider.on_session_end([])

    assert not (tmp_path / "mem4" / ".dream_state.json").exists()


# -- bootstrap + turn-signal + pre_compress (the fix: Dream really fires) -----

def test_bootstrap_staleness_fires_first_consolidation(tmp_path):
    # Never consolidated before, but a pending signal has been waiting > staleness.
    # The old code required last_consolidation_at to be set → could never bootstrap.
    root = tmp_path / "mem4"
    root.mkdir()
    _seed_mirror(root, "memory", ["a", "a"])
    dp = DreamProcessor(root, enabled=True, threshold=10, staleness_days=7)
    dp.save(DreamState(
        signals_since_last=1,
        pending_since=(NOW - timedelta(days=8)).isoformat(),
    ))
    res = dp.maybe_consolidate("session_start", now=NOW)
    assert res.ran is True and res.reason == "staleness"
    st = dp.load()
    assert st.consolidation_count == 1
    assert st.pending_since is None and st.signals_since_last == 0


def test_record_signal_stamps_pending_since(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    dp = DreamProcessor(root, enabled=True, threshold=25, staleness_days=7)
    dp.record_signal(1)
    assert dp.load().pending_since is not None


def test_provider_turns_then_session_end_consolidates(tmp_path):
    # Turns (not just rare memory writes) accumulate the signal; a boundary event
    # then fires the consolidation — this is what makes "定期做夢整理" actually run.
    provider = Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": True, "threshold": 3, "staleness_days": 7},
        "audit": {"enabled": False},
    })
    provider.initialize("s1", hermes_home=str(tmp_path))
    for i in range(3):
        provider.sync_turn(f"這是第 {i} 個測試對話輪次，內容夠長可被索引", "好的收到")
    st = json.loads((tmp_path / "mem4" / ".dream_state.json").read_text(encoding="utf-8"))
    assert st["signals_since_last"] == 3            # turns fed the signal
    provider.on_session_end([])                     # boundary → threshold met
    st = json.loads((tmp_path / "mem4" / ".dream_state.json").read_text(encoding="utf-8"))
    assert st["consolidation_count"] >= 1 and st["signals_since_last"] == 0


def test_provider_pre_compress_triggers_consolidation(tmp_path):
    provider = Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": True, "threshold": 2, "staleness_days": 7},
        "audit": {"enabled": False},
    })
    provider.initialize("s1", hermes_home=str(tmp_path))
    provider.sync_turn("第一個對話輪次內容夠長可索引", "回覆一")
    provider.sync_turn("第二個對話輪次內容夠長可索引", "回覆二")
    provider.on_pre_compress([])                    # compression = consolidate moment
    st = json.loads((tmp_path / "mem4" / ".dream_state.json").read_text(encoding="utf-8"))
    assert st["consolidation_count"] >= 1
