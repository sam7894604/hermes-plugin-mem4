"""Tests for C-② refine heuristic candidate selector (preservation-oriented).

The selector promotes long *un-coded* core entries into L2 microfiles + a hot-zone
pointer, so cold detail survives when the memory tool later evicts to stay under
the char cap. It is deterministic (length/fill-gated, capped per run), conservative
off the hot path (passive unless the hot zone is near its cap), and never grows the
hot zone. char_limit is injected to make the fill ratio deterministic in tests.
"""

import re

from mem4 import Mem4MemoryProvider
from mem4.refine import RefinePlanner, ENTRY_DELIMITER, _POINTER_MARK


def _pad(prefix: str, target: int, tail: str) -> str:
    """Build an entry of at least ``target`` chars ending in ``tail`` (distinctive
    token beyond the 80-char summary cutoff)."""
    filler = "內容細節說明文字補充"
    s = prefix
    while len(s) + len(tail) < target:
        s += filler
    return s + tail


# A long (>300 char) un-coded core entry; ASCII "pipeline v10" ⇒ readable slug.
CORE_BIG = _pad("產業情報 pipeline v10 完整鏈路 ", 340, "尾端保存標記_獨特XYZ")
CORE_SMALL = "叔鼠偏好精簡直接、先結論"


def _mid(tag: str) -> str:
    """A mid-length (~170 char) core entry: above the aggressive bar (140), below
    the passive bar (300)."""
    return _pad(f"中長條目 {tag} 需要保存的冷知識 ", 170, f"尾標_{tag}")


def _write_memory(tmp_path, text):
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "MEMORY.md"
    p.write_text(text, encoding="utf-8")
    return p


# -- passive mode: only the runaway (>=300) entry is preserved ---------------

def test_passive_promotes_only_runaway(tmp_path):
    assert len(CORE_BIG) >= 300
    _write_memory(tmp_path, ENTRY_DELIMITER.join([CORE_BIG, CORE_SMALL]))
    # large char_limit ⇒ low fill ⇒ passive (min 300, max 1)
    plan = RefinePlanner(tmp_path, char_limit=8000).plan()
    assert plan.aggressive is False
    assert len(plan.promoted) == 1
    assert plan.after_bytes < plan.before_bytes            # never grows
    assert _POINTER_MARK in plan.condensed
    assert CORE_SMALL in plan.condensed                    # short core kept inline
    assert "尾端保存標記_獨特XYZ" not in plan.condensed     # detail left the hot zone


def test_passive_ignores_midsize_entries(tmp_path):
    # A mid (~150 char) entry is below the passive threshold (300) → left inline.
    mid = _mid("A")
    assert 140 <= len(mid) < 300
    _write_memory(tmp_path, ENTRY_DELIMITER.join([mid, CORE_SMALL]))
    plan = RefinePlanner(tmp_path, char_limit=8000).plan()
    assert plan.promoted == []
    assert plan.changed is False                           # nothing to do


# -- aggressive mode (near cap): preserve mid+ entries, capped at 3 ----------

def test_aggressive_promotes_up_to_cap(tmp_path):
    mids = [_mid("A"), _mid("B"), _mid("C"), _mid("D")]
    body = ENTRY_DELIMITER.join(mids + [CORE_SMALL])
    # small char_limit ⇒ fill >= 0.80 ⇒ aggressive (min 140, max 3)
    planner = RefinePlanner(tmp_path, char_limit=int(len(body) / 0.85))
    _write_memory(tmp_path, body)
    plan = planner.plan()
    assert plan.aggressive is True
    assert len(plan.promoted) == 3                         # capped, longest-first
    assert plan.after_bytes < plan.before_bytes


def test_apply_preserves_detail_and_is_searchable(tmp_path):
    body = ENTRY_DELIMITER.join([CORE_BIG, CORE_SMALL])
    planner = RefinePlanner(tmp_path, char_limit=int(len(body) / 0.85))
    p = _write_memory(tmp_path, body)
    result = planner.apply()
    assert result["applied"] is True and result["microfiles"] >= 1
    # hot zone now holds a pointer; the detail is gone from MEMORY.md …
    new = p.read_text(encoding="utf-8")
    assert _POINTER_MARK in new and "尾端保存標記_獨特XYZ" not in new
    # … but preserved verbatim in an L2 microfile on disk …
    code = planner.plan().promoted or []  # already applied → recompute not needed
    mf = list((tmp_path / "mem4").glob("*.md"))
    mf_text = "\n".join(f.read_text(encoding="utf-8") for f in mf
                        if not f.name.startswith("_"))
    assert "尾端保存標記_獨特XYZ" in mf_text
    # … and a fresh provider indexes that microfile so recall can find it.
    prov = Mem4MemoryProvider({"backend": "local-file"})
    prov.initialize("s1", hermes_home=str(tmp_path))
    hits = prov._recall.search("獨特XYZ", limit=5, now=__import__("time").time())
    assert any(h.kind == "microfile" for h in hits)


# -- invariant: refine never grows the hot zone ------------------------------

def test_never_grows_hotzone_even_aggressive(tmp_path):
    body = ENTRY_DELIMITER.join([_mid(x) for x in "ABCDE"])
    planner = RefinePlanner(tmp_path, char_limit=int(len(body) / 0.9))
    _write_memory(tmp_path, body)
    plan = planner.plan()
    assert plan.after_bytes <= plan.before_bytes


# -- CJK-only entries get a stable content-hash code -------------------------

def test_cjk_entry_gets_valid_hash_code(tmp_path):
    _write_memory(tmp_path, ENTRY_DELIMITER.join([CORE_BIG, CORE_SMALL]))
    plan = RefinePlanner(tmp_path, char_limit=8000).plan()
    assert len(plan.promoted) == 1
    code = plan.promoted[0]
    # CORE_BIG summary has ASCII ("pipeline v10") so a readable slug is fine too;
    # either way the code must be a valid route code (extractable, traversal-safe).
    from mem4.backend import normalize_code
    assert normalize_code(code) == code


def test_pure_cjk_entry_uses_hash_code(tmp_path):
    # No ASCII anywhere ⇒ slug is empty ⇒ hash-code fallback.
    pure = _pad("純中文長條目測試無任何英數字元 ", 170, "尾標記結束")
    assert len(pure) >= 140
    _write_memory(tmp_path, pure)
    plan = RefinePlanner(tmp_path, char_limit=int(len(pure) / 0.85)).plan()
    assert len(plan.promoted) == 1
    assert re.match(r"^m[0-9a-f]{6}$", plan.promoted[0])   # hash fallback code


# -- idempotency: after preserving, re-refine is a no-op ---------------------

def test_idempotent_after_promote(tmp_path):
    body = ENTRY_DELIMITER.join([CORE_BIG, CORE_SMALL])
    planner = RefinePlanner(tmp_path, char_limit=int(len(body) / 0.85))
    _write_memory(tmp_path, body)
    assert planner.apply()["applied"] is True
    plan2 = planner.plan()
    assert plan2.promoted == []
    assert plan2.changed is False
