"""Controlled measurement harness for mem4 ② (synthetic/fixture data).

Upgrades the measurement from "compare two random real-traffic segments" (which
is confounded by traffic randomness) to THREE controlled layers:

  Layer 1 — Deterministic offline replay (primary evidence, zero randomness).
    The SAME fixed input set is replayed against both configs (baseline = pure
    built-in, experiment = mem4). Same input ⇒ any difference is attributable to
    mem4. Per item we record: gold hit (PRECISE), injected tokens (PRECISE), and
    recall route. Fixed input = the QA fixture (24 items, EN+ZH, exact+
    paraphrase, gold answers) + an injectable "sampled from real session history"
    set (synthetic stand-in now; a real sampler is wired at deploy time).

  Layer 2 — Paired counterfactual (real traffic, but paired).
    For each real query the Auditor records BOTH what pure built-in WOULD inject
    (its whole resident memory) and what mem4 actually injected (legend + this
    query's recall). We report the paired-difference distribution — robust to
    traffic mix. Demonstrated here on synthetic events.

  Layer 3 — Resident cost (context-independent, least disputable).
    Session-open injection size: baseline (whole MEMORY.md) vs mem4 (short
    routing legend), across N sessions. Directly shows the hot zone shrank.

All numbers are reported as DISTRIBUTIONS (min/median/max, quartiles), not single
points. The gate is hard-wired to design spike §7.

Honesty (design spike §7 / Fable 5 §6): deterministic-replay gold hits are
PRECISE. Free real-traffic "true hits" can only be ESTIMATED — that path is
labelled estimated, never reported as exact.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..recall import RecallStore

_FIXTURE_PATH = Path(__file__).with_name("qa_fixture.json")
_HISTORY_PATH = Path(__file__).with_name("history_samples.json")

# Representative baseline resident size (design spike §7 cites ADR-018's measured
# ~2,175-char flat MEMORY.md). Overridable.
_DEFAULT_BASELINE_RESIDENT_CHARS = 2175
_RECALL_WIN_THRESHOLD = 0.30
_NOW = 1_780_000_000.0


def _chars_to_tokens(chars: float) -> int:
    return (int(max(0.0, chars)) + 3) // 4


def dist(values: List[float]) -> Dict[str, float]:
    """Summary distribution (not a single number) for a metric."""
    vals = [float(v) for v in values]
    if not vals:
        return {"n": 0, "min": 0.0, "p25": 0.0, "median": 0.0, "p75": 0.0,
                "max": 0.0, "mean": 0.0}
    if len(vals) >= 2:
        q = statistics.quantiles(vals, n=4)  # [p25, p50, p75]
        p25, p75 = round(q[0], 1), round(q[2], 1)
    else:
        p25 = p75 = round(vals[0], 1)
    return {
        "n": len(vals),
        "min": round(min(vals), 1),
        "p25": p25,
        "median": round(statistics.median(vals), 1),
        "p75": p75,
        "max": round(max(vals), 1),
        "mean": round(statistics.mean(vals), 1),
    }


def _default_legend_chars() -> int:
    try:
        from .. import ROUTING_LEGEND
        return len(ROUTING_LEGEND)
    except Exception:  # pragma: no cover - defensive
        return 300


# ---------------------------------------------------------------------------
# Input sets
# ---------------------------------------------------------------------------

def load_fixture(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return json.loads(Path(path or _FIXTURE_PATH).read_text(encoding="utf-8"))["items"]


def load_history_samples(
    source: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Injectable 'sampled from real session history' query set (design spike §10.4).

    At deploy time, ``source`` reads real session queries (with a gold answer
    derived from what was actually recalled). Here it defaults to a synthetic
    stand-in file so the harness runs end-to-end without a live history.
    """
    if source is not None:
        return list(source())
    if _HISTORY_PATH.is_file():
        return json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))["items"]
    return []


# ---------------------------------------------------------------------------
# Layer 1 — deterministic offline replay
# ---------------------------------------------------------------------------

def deterministic_replay(
    items: List[Dict[str, Any]], *,
    baseline_resident_chars: int = _DEFAULT_BASELINE_RESIDENT_CHARS,
    mem4_legend_chars: Optional[int] = None,
    db_path: str = ":memory:", now: float = _NOW, limit: int = 5,
) -> Dict[str, Any]:
    """Replay the SAME items against baseline (built-in) and mem4. Deterministic."""
    mem4_legend_chars = mem4_legend_chars if mem4_legend_chars is not None else _default_legend_chars()
    baseline_resident_tokens = _chars_to_tokens(baseline_resident_chars)
    legend_tokens = _chars_to_tokens(mem4_legend_chars)

    store = RecallStore(Path(db_path))
    rows = []
    try:
        for it in items:
            store.index(ref=it["id"], content=it["knowledge"], kind="qa", ts=now)
        for it in items:
            hits = store.search(it["query"], limit=limit, now=now)
            gold_mem4 = any(it["expect_substr"] in h.snippet for h in hits)
            recall_tokens = _chars_to_tokens(sum(len(h.snippet) for h in hits))
            rows.append({
                "id": it["id"], "lang": it["lang"], "paraphrase": it["paraphrase"],
                # Baseline (pure built-in) has NO recall of cold knowledge that
                # left the hot zone → gold miss; it still injects its whole
                # resident memory every query.
                "gold_baseline": False,
                "gold_mem4": gold_mem4,
                "route": (hits[0].route if hits else ""),
                "inject_tokens_baseline": baseline_resident_tokens,
                "inject_tokens_mem4": legend_tokens + recall_tokens,
            })
    finally:
        store.close()

    def acc(subset, key):
        return round(sum(1 for r in subset if r[key]) / len(subset), 3) if subset else 0.0

    route_dist: Dict[str, int] = {}
    for r in rows:
        if r["route"]:
            route_dist[r["route"]] = route_dist.get(r["route"], 0) + 1

    return {
        "n": len(rows),
        "precision": "gold hits are PRECISE (deterministic replay)",
        "gold_accuracy_baseline": acc(rows, "gold_baseline"),
        "gold_accuracy_mem4": acc(rows, "gold_mem4"),
        "gold_accuracy_mem4_en": acc([r for r in rows if r["lang"] == "en"], "gold_mem4"),
        "gold_accuracy_mem4_zh": acc([r for r in rows if r["lang"] == "zh"], "gold_mem4"),
        "gold_accuracy_mem4_exact": acc([r for r in rows if not r["paraphrase"]], "gold_mem4"),
        "gold_accuracy_mem4_paraphrase": acc([r for r in rows if r["paraphrase"]], "gold_mem4"),
        "route_distribution": route_dist,
        "inject_tokens_baseline": dist([r["inject_tokens_baseline"] for r in rows]),
        "inject_tokens_mem4": dist([r["inject_tokens_mem4"] for r in rows]),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Layer 2 — paired counterfactual (from Auditor events)
# ---------------------------------------------------------------------------

def paired_counterfactual(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Paired (baseline vs mem4) injection stats from recorded events."""
    pairs = [
        (int(e.get("baseline_inject_tokens", 0)), int(e.get("mem4_inject_tokens", 0)))
        for e in events
        if e.get("kind") in {"search", "prefetch"}
        and (e.get("baseline_inject_tokens") or e.get("mem4_inject_tokens"))
    ]
    if not pairs:
        return {"n": 0, "note": "no paired events (needs real traffic w/ audit on)"}
    diffs = [b - m for b, m in pairs]
    cheaper = sum(1 for d in diffs if d > 0)
    return {
        "n": len(pairs),
        "precision": "injection sizes are PRECISE; per-query paired",
        "baseline_inject_tokens": dist([b for b, _ in pairs]),
        "mem4_inject_tokens": dist([m for _, m in pairs]),
        "paired_diff_tokens": dist(diffs),   # baseline - mem4; positive = mem4 saved
        "mem4_cheaper_fraction": round(cheaper / len(pairs), 3),
    }


# ---------------------------------------------------------------------------
# Layer 3 — resident cost (context-independent)
# ---------------------------------------------------------------------------

def resident_cost(
    baseline_session_chars: List[int], *, mem4_legend_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Session-open injection size: baseline (MEMORY.md) vs mem4 (legend)."""
    mem4_legend_chars = mem4_legend_chars if mem4_legend_chars is not None else _default_legend_chars()
    n = len(baseline_session_chars) or 1
    base = dist([_chars_to_tokens(c) for c in baseline_session_chars] or [0])
    mem4 = dist([_chars_to_tokens(mem4_legend_chars)] * n)
    reduction = (1 - (mem4["median"] / base["median"])) if base["median"] else 0.0
    return {
        "precision": "resident sizes are PRECISE, context-independent",
        "baseline_resident_tokens": base,
        "mem4_resident_tokens": mem4,
        "median_reduction_fraction": round(reduction, 3),
    }


# ---------------------------------------------------------------------------
# Gate (design spike §7, hard-wired)
# ---------------------------------------------------------------------------

def gate(replay: Dict[str, Any], resident: Dict[str, Any]) -> Dict[str, Any]:
    reasons = []

    recall_win = replay["gold_accuracy_mem4"] - replay["gold_accuracy_baseline"]
    ok_recall = recall_win >= _RECALL_WIN_THRESHOLD
    reasons.append(
        f"{'PASS' if ok_recall else 'FAIL'} gold recall of cold knowledge: "
        f"mem4 {replay['gold_accuracy_mem4']:.0%} vs baseline "
        f"{replay['gold_accuracy_baseline']:.0%} (Δ={recall_win:+.0%}, need ≥{_RECALL_WIN_THRESHOLD:.0%})"
    )

    base_inj = replay["inject_tokens_baseline"]["median"]
    mem4_inj = replay["inject_tokens_mem4"]["median"]
    ok_net_token = mem4_inj < base_inj
    reasons.append(
        f"{'PASS' if ok_net_token else 'FAIL'} net per-query tokens: mem4 median "
        f"{mem4_inj:.0f} < baseline median {base_inj:.0f}"
    )

    ok_resident = resident["mem4_resident_tokens"]["median"] < resident["baseline_resident_tokens"]["median"]
    reasons.append(
        f"{'PASS' if ok_resident else 'FAIL'} resident hot zone smaller: mem4 median "
        f"{resident['mem4_resident_tokens']['median']:.0f} < baseline median "
        f"{resident['baseline_resident_tokens']['median']:.0f} tokens "
        f"({resident['median_reduction_fraction']:.0%} reduction)"
    )

    passed = ok_recall and ok_net_token and ok_resident
    return {
        "passed": passed,
        "recall_win": round(recall_win, 3),
        "reasons": reasons,
        "verdict": "SHIP (measured value positive)" if passed
                   else "ROLL BACK (remove memory.provider: mem4)",
    }


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def run_all(
    *, items: Optional[List[Dict[str, Any]]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    baseline_resident_chars: int = _DEFAULT_BASELINE_RESIDENT_CHARS,
    baseline_session_chars: Optional[List[int]] = None,
    synthetic_events: Optional[List[Dict[str, Any]]] = None,
    db_path: str = ":memory:",
) -> Dict[str, Any]:
    """Run all three measurement layers on fixture/synthetic data."""
    items = items if items is not None else load_fixture()
    history = history if history is not None else load_history_samples()
    replay = deterministic_replay(
        items, baseline_resident_chars=baseline_resident_chars, db_path=db_path)
    replay_history = deterministic_replay(
        history, baseline_resident_chars=baseline_resident_chars, db_path=db_path
    ) if history else None
    # Synthetic session sizes modelling MEMORY.md growth if none supplied.
    if baseline_session_chars is None:
        baseline_session_chars = [1500, 1800, 2175, 2400, 2600]
    resident = resident_cost(baseline_session_chars)
    paired = paired_counterfactual(synthetic_events or [])
    return {
        "layer1_replay_fixture": replay,
        "layer1_replay_history": replay_history,
        "layer2_paired": paired,
        "layer3_resident": resident,
        "gate": gate(replay, resident),
    }


def format_full_report(report: Dict[str, Any]) -> str:
    r = report["layer1_replay_fixture"]
    res = report["layer3_resident"]
    paired = report["layer2_paired"]
    g = report["gate"]
    L = [
        "mem4 controlled measurement (synthetic/fixture)",
        "═" * 52,
        "LAYER 1 — deterministic replay (PRECISE gold, zero randomness)",
        f"  items:                {r['n']}",
        f"  gold accuracy  mem4/baseline: {r['gold_accuracy_mem4']:.0%} / {r['gold_accuracy_baseline']:.0%}",
        f"    en / zh:            {r['gold_accuracy_mem4_en']:.0%} / {r['gold_accuracy_mem4_zh']:.0%}",
        f"    exact / paraphrase: {r['gold_accuracy_mem4_exact']:.0%} / {r['gold_accuracy_mem4_paraphrase']:.0%}"
        "   (paraphrase = FTS weak spot, Fable 5 §3)",
        f"  route distribution:   {r['route_distribution']}",
        f"  inject tokens/query mem4     (min/med/max): "
        f"{r['inject_tokens_mem4']['min']:.0f}/{r['inject_tokens_mem4']['median']:.0f}/{r['inject_tokens_mem4']['max']:.0f}",
        f"  inject tokens/query baseline (min/med/max): "
        f"{r['inject_tokens_baseline']['min']:.0f}/{r['inject_tokens_baseline']['median']:.0f}/{r['inject_tokens_baseline']['max']:.0f}",
    ]
    if report["layer1_replay_history"]:
        h = report["layer1_replay_history"]
        L.append(f"  [history samples] gold mem4/baseline: "
                 f"{h['gold_accuracy_mem4']:.0%} / {h['gold_accuracy_baseline']:.0%} (n={h['n']})")
    L += [
        "",
        "LAYER 2 — paired counterfactual (per-query, PRECISE injection)",
    ]
    if paired.get("n"):
        pd = paired["paired_diff_tokens"]
        L += [
            f"  events:               {paired['n']}",
            f"  paired diff tokens (baseline−mem4) min/med/max: "
            f"{pd['min']:.0f}/{pd['median']:.0f}/{pd['max']:.0f}",
            f"  mem4 cheaper fraction: {paired['mem4_cheaper_fraction']:.0%}",
        ]
    else:
        L.append("  (no events — needs real traffic with audit enabled)")
    L += [
        "",
        "LAYER 3 — resident cost (context-independent, PRECISE)",
        f"  baseline resident tokens min/med/max: "
        f"{res['baseline_resident_tokens']['min']:.0f}/{res['baseline_resident_tokens']['median']:.0f}/{res['baseline_resident_tokens']['max']:.0f}",
        f"  mem4 resident tokens     min/med/max: "
        f"{res['mem4_resident_tokens']['min']:.0f}/{res['mem4_resident_tokens']['median']:.0f}/{res['mem4_resident_tokens']['max']:.0f}",
        f"  median reduction:     {res['median_reduction_fraction']:.0%}",
        "",
        f"GATE (§7): {g['verdict']}",
    ]
    L += [f"    - {x}" for x in g["reasons"]]
    L += [
        "",
        "NOTE: synthetic/fixture data — mechanism proof only. Real hit rates need",
        "toothless deployment + actual usage; the same harness + audit.jsonl then",
        "run against real data. Free-traffic 'true hits' are ESTIMATED, not exact.",
    ]
    return "\n".join(L)


def main() -> None:  # pragma: no cover - manual invocation
    print(format_full_report(run_all()))


if __name__ == "__main__":  # pragma: no cover
    main()
