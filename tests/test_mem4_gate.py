"""Tests for C-① prefetch gating (mem4.gate) + provider wiring.

Covers the three deterministic gates that make prefetch precise:
  * is_low_signal_query — skip image/document/background envelopes + empty text
  * lexical_overlap / gate_hit relevance — drop hits that barely match the query
  * redundancy_vs_l0 — drop hits already resident in the L0 hot zone

Plus provider-level wiring: prefetch() skips envelope turns, and
_compose_prefetch() drops irrelevant and L0-duplicate hits while keeping the
pre-C-① behaviour when called without a query.
"""

import time

from mem4 import Mem4MemoryProvider
from mem4.backend import SearchHit
from mem4 import gate


# -- is_low_signal_query -----------------------------------------------------

def test_envelope_image_turn_is_low_signal():
    q = ("[The user sent an image~ Here's what I can see:\n"
         "This image shows a receipt held by a person's left hand.]")
    assert gate.is_low_signal_query(q) is True


def test_envelope_document_turn_is_low_signal():
    q = ("[The user sent a document: 'img_9702691bc7d5.jpg'. It is saved at: "
         "/root/.hermes/image_cache/img_9702691bc7d5.jpg. Its text is not inlined.]")
    assert gate.is_low_signal_query(q) is True


def test_background_process_envelope_is_low_signal():
    assert gate.is_low_signal_query("[IMPORTANT: Background process proc_f4118a13 finished]") is True


def test_empty_or_punctuation_is_low_signal():
    assert gate.is_low_signal_query("") is True
    assert gate.is_low_signal_query("   ") is True
    assert gate.is_low_signal_query("？！。、") is True


def test_real_chinese_question_is_not_low_signal():
    assert gate.is_low_signal_query("盤中速報對產業情報分析有價值嗎？") is False


def test_channel_wrapped_real_question_survives():
    # A genuine question wrapped in channel-message envelopes must NOT be skipped.
    q = ("[Recent channel messages]\n[Sam Liu] 類似n8n的做法，才能模組化\n\n"
         "[New message]\n[Sam Liu] 但是走方案一")
    assert gate.is_low_signal_query(q) is False


def test_role_prefix_only_is_low_signal():
    # Just a name tag with no content.
    assert gate.is_low_signal_query("[Sam Liu]") is True


# -- terms (CJK-aware) -------------------------------------------------------

def test_terms_ascii_tokens_min_length():
    t = gate.terms("Baserow API key a")
    assert "baserow" in t and "api" in t and "key" in t
    assert "a" not in t          # single ascii char dropped


def test_terms_cjk_bigrams():
    t = gate.terms("產業情報")
    assert {"產業", "業情", "情報"} <= t


# -- lexical_overlap / relevance --------------------------------------------

def test_overlap_full_and_zero():
    assert gate.lexical_overlap("baserow schema", "the baserow schema is fixed") == 1.0
    assert gate.lexical_overlap("峇里島 記帳", "完全無關的英文 content here") == 0.0


def test_overlap_partial_fraction():
    # query terms: {"cron", "job"} ; text has only "cron" → 0.5
    assert gate.lexical_overlap("cron job", "the cron ran fine") == 0.5


def test_gate_hit_drops_irrelevant_turn():
    keep, reason = gate.gate_hit(
        "峇里島 行程 規劃", "feed_pool table 909 schema title source url",
        is_microfile=False, l0_terms=set())
    assert keep is False and reason == "drop-relevance"


def test_gate_hit_microfile_lower_bar_keeps_partial():
    # query terms = {nas, cpu, 規格, 容量} (4); text matches only 'nas' → overlap
    # 0.25, which lands between the micro bar (0.15) and the turn bar (0.30): a
    # microfile is kept, an equivalent turn snippet is dropped.
    q = "nas 規格 cpu 容量"
    text = "DS918+ nas 四核心"
    assert gate.lexical_overlap(q, text) == 0.25
    keep_turn, _ = gate.gate_hit(q, text, is_microfile=False, l0_terms=set())
    keep_micro, _ = gate.gate_hit(q, text, is_microfile=True, l0_terms=set())
    assert keep_turn is False
    assert keep_micro is True


# -- redundancy_vs_l0 --------------------------------------------------------

def test_redundancy_drops_l0_duplicate():
    l0 = gate.terms("產業情報 pipeline v10：A→B→C 全線 opus-4-8")
    # A hit that is essentially the same content already in L0.
    keep, reason = gate.gate_hit(
        "產業情報 pipeline", "產業情報 pipeline v10 A B C opus",
        is_microfile=False, l0_terms=l0)
    assert keep is False and reason == "drop-l0dup"


def test_novel_cold_material_survives_dedup():
    l0 = gate.terms("產業情報 pipeline v10")
    keep, reason = gate.gate_hit(
        "峇里島 記帳 匯率", "峇里島 記帳 匯率 560 IDR 刷卡 Visa 現金",
        is_microfile=False, l0_terms=l0)
    assert keep is True and reason == "kept"


# -- provider wiring ---------------------------------------------------------

def _provider(tmp_path):
    p = Mem4MemoryProvider({"backend": "local-file"})
    p.initialize("s-1", hermes_home=str(tmp_path))
    return p


def test_prefetch_skips_envelope_turn(tmp_path):
    # A microfile that WOULD match on the caption keywords.
    root = tmp_path / "mem4"
    root.mkdir(exist_ok=True)
    (root / "sys.md").write_text("receipt image document processing notes", encoding="utf-8")
    p = _provider(tmp_path)
    p._recall.index("turn:1", "receipt image document processing", "turn", time.time())

    envelope = "[The user sent an image~ Here's what I can see: a receipt document]"
    assert p.prefetch(envelope) == ""          # skipped, nothing injected


def test_prefetch_injects_on_real_query(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir(exist_ok=True)
    (root / "nas.md").write_text("DS918+ Tailscale 100.98.31.81 port 6322", encoding="utf-8")
    p = _provider(tmp_path)
    out = p.prefetch("NAS 的 Tailscale port 是多少")
    assert "nas" in out.lower() or "ds918" in out.lower()


def test_compose_drops_irrelevant_and_l0_dup(tmp_path):
    p = _provider(tmp_path)
    now = time.time()
    # L0 already holds the bali accounting detail.
    l0 = gate.terms("峇里島 記帳 匯率 560 IDR 刷卡 Visa 現金 Aryaduta")
    hits = [
        # relevant to query AND novel (泛舟 not in L0) → kept
        SearchHit(ref="turn:1", snippet="峇里島 泛舟 行程 四人均分 費用", kind="turn", ts=now),
        # off-topic → dropped by relevance
        SearchHit(ref="turn:2", snippet="totally unrelated english snippet here", kind="turn", ts=now),
        # relevant but essentially all terms already resident in L0 → dropped by dedup
        SearchHit(ref="turn:3", snippet="峇里島 記帳 匯率 560 IDR 刷卡", kind="turn", ts=now),
    ]
    text, _n = p._compose_prefetch(hits, query="峇里島 記帳 匯率", l0_terms=l0)
    assert "泛舟" in text                          # relevant + novel → kept
    assert "unrelated" not in text                # irrelevant → dropped
    assert "560" not in text                      # redundant with L0 → dropped


def test_gate_keeps_long_query_with_two_shared_terms(tmp_path):
    # A long query dilutes per-term fraction, but 2 shared content terms is a
    # solid on-topic signal → kept despite fraction below the turn bar.
    q = "重新 review 目前 pipeline 設計 是否 還有 bug 例如 baserow mcp"
    text = "pipeline baserow 設定 已修好"
    q_terms = gate.terms(q)
    assert gate.lexical_overlap(q, text) < gate.REL_MIN_FRAC_TURN   # low fraction
    keep, reason = gate.gate_hit(q, text, is_microfile=False, l0_terms=set())
    assert keep is True and reason == "kept"


def test_compose_without_query_preserves_legacy_behaviour(tmp_path):
    p = _provider(tmp_path)
    now = time.time()
    hits = [SearchHit(ref="turn:1", snippet="anything at all", kind="turn", ts=now)]
    text, _n = p._compose_prefetch(hits)          # no query → no gating
    assert "anything at all" in text
