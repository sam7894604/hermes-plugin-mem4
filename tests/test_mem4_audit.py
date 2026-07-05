"""Tests for mem4 ② — Auditor instrumentation, A/B arm, QA harness, Baserow sink."""

import json

from mem4 import Mem4MemoryProvider
from mem4.audit import Auditor, estimate_tokens
from mem4.eval.harness import (
    run_all, load_fixture, load_history_samples, gate, dist,
    deterministic_replay, paired_counterfactual, resident_cost,
)


def _audit_provider(tmp_path, arm="experiment"):
    return Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": False},
        "audit": {"enabled": True},
        "arm": arm,
    })


# -- instrumentation ---------------------------------------------------------

def test_search_event_recorded(tmp_path):
    p = _audit_provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("the deploy target host is toothless on lightnode", "ok")
    p.handle_tool_call("mem_search", {"query": "toothless lightnode"})
    p.handle_tool_call("mem_search", {"query": "zzz nonexistent term qqq"})

    events = p._auditor.read_events()
    searches = [e for e in events if e["kind"] == "search"]
    assert len(searches) == 2
    hit, miss = searches[0], searches[1]
    assert hit["hit"] is True and hit["route"] in {"fts", "trigram", "like"}
    assert hit["injected_chars"] > 0 and hit["hit_estimated"] is False
    assert miss["hit"] is False and miss["route"] == ""
    p.shutdown()


def test_route_and_prefetch_events_recorded(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    (root / "sys.md").write_text("host toothless lightnode vps tokyo", encoding="utf-8")
    p = _audit_provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))

    p.handle_tool_call("mem_route", {"code": "sys"})       # hit
    p.handle_tool_call("mem_route", {"code": "nope"})      # miss
    p.prefetch("toothless lightnode vps tokyo host")       # prefetch hit

    events = p._auditor.read_events()
    routes = [e for e in events if e["kind"] == "route"]
    prefetches = [e for e in events if e["kind"] == "prefetch"]
    assert [r["hit"] for r in routes] == [True, False]
    assert routes[0]["route"] == "microfile"
    assert prefetches and prefetches[0]["prefetch_triggered"] is True
    # injected_tokens is present and derived from chars
    assert prefetches[0]["injected_tokens"] == estimate_tokens(prefetches[0]["injected_chars"])
    p.shutdown()


def test_audit_disabled_writes_nothing(tmp_path):
    p = Mem4MemoryProvider({"backend": "local-file", "dream": {"enabled": False}})
    p.initialize("s1", hermes_home=str(tmp_path))  # audit default off
    p.sync_turn("remember the deploy host is toothless", "ok")
    p.handle_tool_call("mem_search", {"query": "toothless"})
    assert not (tmp_path / "mem4" / "audit.jsonl").exists()
    p.shutdown()


# -- A/B arm -----------------------------------------------------------------

def test_baseline_arm_disables_agent_surfaces(tmp_path):
    p = _audit_provider(tmp_path, arm="baseline")
    p.initialize("s1", hermes_home=str(tmp_path))
    assert p._is_baseline() is True
    # No tools, no system-prompt injection, no prefetch injection.
    assert p.get_tool_schemas() == []
    assert p.system_prompt_block() == ""
    p.sync_turn("deploy host toothless lightnode server", "ok")
    assert p.prefetch("toothless lightnode server host deploy") == ""
    p.shutdown()


def test_experiment_arm_enables_surfaces(tmp_path):
    p = _audit_provider(tmp_path, arm="experiment")
    p.initialize("s1", hermes_home=str(tmp_path))
    assert [s["name"] for s in p.get_tool_schemas()] == ["mem_route", "mem_search"]
    assert "mem_search" in p.system_prompt_block()
    p.shutdown()


# -- summarize + Baserow sink (mock) -----------------------------------------

def test_summarize_and_baserow_row_uses_existing_columns(tmp_path):
    auditor = Auditor(tmp_path / "audit.jsonl", enabled=True, arm="experiment", session_id="s1")
    auditor.record_search("q1", route="fts", hit=True, injected_chars=100)
    auditor.record_search("q2", route="like", hit=True, injected_chars=60)
    auditor.record_search("q3", route="", hit=False, injected_chars=0)

    summary = auditor.summarize(auditor.read_events())
    assert summary["n_search"] == 3
    assert summary["search_hit_rate"] == round(2 / 3, 3)
    assert summary["route_distribution"] == {"fts": 1, "like": 1, "none": 1}

    captured = {}

    def mock_writer(table_id, rows):
        captured["table_id"] = table_id
        captured["rows"] = rows

    row = auditor.export_to_baserow(mock_writer, date_str="2026-07-05", name="mem4 audit test")
    assert captured["table_id"] == 907
    # Only columns that exist on table 907.
    existing_907 = {
        "Name", "Notes", "Active", "date", "type", "mem_chars", "entry_count",
        "mem_pct", "hot_hit_rate", "est_tokens_saved", "dead_links",
        "items_archived", "items_removed", "notes",
    }
    assert set(row).issubset(existing_907)
    assert row["type"] == "audit"
    assert json.loads(row["notes"])["arm"] == "experiment"


# -- QA harness --------------------------------------------------------------

def test_fixture_has_enough_items_incl_paraphrase_and_cjk():
    items = load_fixture()
    assert len(items) >= 20
    assert any(it["paraphrase"] for it in items)
    assert any(it["lang"] == "zh" for it in items)
    assert any(it["lang"] == "en" for it in items)


def test_history_samples_loadable_and_injectable():
    # Default synthetic file.
    assert len(load_history_samples()) >= 5
    # Injectable source overrides the file.
    injected = [{"id": "x", "lang": "en", "paraphrase": False,
                 "query": "q", "knowledge": "k", "expect_substr": "k"}]
    assert load_history_samples(source=lambda: injected) == injected


def test_dist_reports_distribution_not_single_number():
    d = dist([10, 20, 30, 40])
    assert d["n"] == 4 and d["min"] == 10.0 and d["max"] == 40.0
    assert d["median"] == 25.0
    assert "p25" in d and "p75" in d


# -- Layer 1: deterministic replay -------------------------------------------

def test_layer1_deterministic_replay_precise_and_mem4_beats_baseline():
    r = deterministic_replay(load_fixture())
    assert "PRECISE" in r["precision"]
    assert r["gold_accuracy_baseline"] == 0.0     # cold knowledge, no recall
    assert r["gold_accuracy_mem4"] > 0.5
    assert set(r["route_distribution"]).issuperset({"fts", "trigram", "like"})
    # mem4 injects far fewer tokens per query than a flat resident MEMORY.md.
    assert r["inject_tokens_mem4"]["median"] < r["inject_tokens_baseline"]["median"]


def test_layer1_is_deterministic():
    a = deterministic_replay(load_fixture())
    b = deterministic_replay(load_fixture())
    assert a["gold_accuracy_mem4"] == b["gold_accuracy_mem4"]
    assert a["route_distribution"] == b["route_distribution"]


# -- Layer 2: paired counterfactual ------------------------------------------

def test_layer2_paired_counterfactual_from_events():
    events = [
        {"kind": "search", "baseline_inject_tokens": 500, "mem4_inject_tokens": 90},
        {"kind": "search", "baseline_inject_tokens": 500, "mem4_inject_tokens": 120},
        {"kind": "prefetch", "baseline_inject_tokens": 500, "mem4_inject_tokens": 70},
    ]
    p = paired_counterfactual(events)
    assert p["n"] == 3
    assert p["paired_diff_tokens"]["median"] > 0     # mem4 cheaper
    assert p["mem4_cheaper_fraction"] == 1.0


def test_layer2_provider_records_paired_tokens(tmp_path):
    # A resident built-in memory exists → baseline_inject_tokens > 0.
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("X" * 2000, encoding="utf-8")
    p = _audit_provider(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path))
    p.sync_turn("the recall database file is recall.db under mem4", "ok")
    p.handle_tool_call("mem_search", {"query": "recall.db mem4"})
    ev = [e for e in p._auditor.read_events() if e["kind"] == "search"][0]
    assert ev["baseline_inject_tokens"] > 0
    assert ev["mem4_inject_tokens"] > 0
    assert ev["baseline_inject_tokens"] > ev["mem4_inject_tokens"]  # mem4 cheaper
    p.shutdown()


# -- Layer 3: resident cost --------------------------------------------------

def test_layer3_resident_cost_reduction():
    res = resident_cost([1500, 1800, 2175, 2400, 2600], mem4_legend_chars=280)
    assert res["baseline_resident_tokens"]["median"] > res["mem4_resident_tokens"]["median"]
    assert res["median_reduction_fraction"] > 0.5


# -- Gate --------------------------------------------------------------------

def test_run_all_gate_ships_on_fixture():
    report = run_all()
    g = report["gate"]
    assert g["passed"] is True
    assert g["recall_win"] > 0.3
    # All three layers present.
    assert report["layer1_replay_fixture"]["n"] >= 20
    assert report["layer3_resident"]["median_reduction_fraction"] > 0


def test_gate_rolls_back_without_recall_advantage():
    replay = {
        "gold_accuracy_mem4": 0.05, "gold_accuracy_baseline": 0.0,
        "inject_tokens_mem4": {"median": 90}, "inject_tokens_baseline": {"median": 500},
    }
    resident = {
        "mem4_resident_tokens": {"median": 70}, "baseline_resident_tokens": {"median": 500},
        "median_reduction_fraction": 0.86,
    }
    g = gate(replay, resident)
    assert g["passed"] is False               # recall win below threshold
    assert "ROLL BACK" in g["verdict"]
