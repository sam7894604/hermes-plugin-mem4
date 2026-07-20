"""C-① prefetch gating — deterministic relevance + L0-dedup + non-text skip.

Pure functions, zero external deps, no model. Used by the provider's
``prefetch()`` to make the one mechanism that actually delivers value on
toothless (automatic recall injection) *precise* instead of firing on every
turn. The v2 verification (2026-07-15) found prefetch was injecting ~220 tok on
**every** turn — including image/document turns whose "query" is a host-injected
caption, and large amounts of material already resident in MEMORY.md/USER.md.
These gates are the fix, and they are fully deterministic (no reliance on the
weak model deciding anything):

  * ``is_low_signal_query()`` — skip non-text / envelope turns (image / document
    / background-process wrappers) and content-free queries. Replaying memory on
    a "here's what I can see in the image" turn helps nothing, so we don't pay
    the tokens.
  * ``lexical_overlap()`` — deterministic relevance proxy: the fraction of the
    query's terms that actually appear in a candidate hit. recall's own score is
    a *positional / time-decay* proxy (``recall.py _rerank_with_decay``), not
    lexical relevance, so a hit can rank high while sharing almost nothing with
    the query. This gate drops those.
  * ``redundancy_vs_l0()`` — the fraction of a hit's terms already present in the
    resident hot zone (MEMORY.md + USER.md). High ⇒ we would just be replaying
    L0, so drop it and spend the char budget on genuinely-cold material.

CJK-aware: ``terms()`` emits ASCII word tokens **and** CJK bigrams, so Chinese
overlap is measured on 2-gram units (single-char overlap is too noisy; bigrams
match the way the trigram recall index already thinks about CJK).
"""

from __future__ import annotations

import re
from typing import Iterable, Set, Tuple

# ---------------------------------------------------------------------------
# Tunables (deterministic thresholds — no model, no config gambling)
# ---------------------------------------------------------------------------

#: Relevance rule (calibrated on toothless: 4,679 real query×hit pairs, 2026-07).
#: A hit is kept when it shares at least ``REL_MIN_SHARED`` content terms with the
#: query OR covers at least this fraction of the query's terms. The dual rule is
#: deliberately length-robust: a fraction-only bar over-penalises long queries
#: (which naturally dilute per-term overlap), while a shared-count-only bar misses
#: short focused queries. The intent (v2 blueprint) is to drop *low*-relevance
#: incidental single-token matches — not to keep only high-relevance hits.
REL_MIN_SHARED = 2          # ≥2 shared content terms ⇒ on-topic regardless of length
REL_MIN_FRAC_TURN = 0.30    # …or a strong single-token match on a short query
#: Microfiles are curated cold knowledge — valuable when matched at all — so a
#: single shared term clears them at a lower fraction than raw turn snippets.
REL_MIN_FRAC_MICRO = 0.15
#: If this fraction (or more) of a hit's terms are already in the L0 hot zone it
#: is redundant replay and is dropped. Measured L0 term-redundancy is low
#: (toothless p90≈0.26), so this is a light safety net for near-duplicates, not a
#: major pruner — set where it catches genuine echoes (~top 0.5% of hits) without
#: touching normal cold material.
L0_DEDUP_MAX = 0.50
#: Minimum residual meaningful characters (after wrapper/role stripping) for a
#: query to be worth a recall injection.
MIN_SIGNAL_CHARS = 6


# ---------------------------------------------------------------------------
# Non-text / envelope turn detection
# ---------------------------------------------------------------------------

#: Substrings marking a turn whose "query" is a host-injected wrapper around a
#: non-text payload (image caption, document attach, background-process notice),
#: not a real user question. Matched case-insensitively anywhere in the query.
#: These are exactly the envelopes the toothless audit showed being hard-injected
#: with ~316 tokens of unrelated memory each.
_ENVELOPE_MARKERS: Tuple[str, ...] = (
    "the user sent an image",
    "the user sent a document",
    "the user sent a video",
    "the user sent an audio",
    "the user sent a file",
    "here's what i can see",
    "here is what i can see",
    "[image]",
    "[important: background process",
)

#: Wrapper fragments stripped before judging residual signal length, so a real
#: question wrapped in "[Recent channel messages] … [New message] …" is still
#: evaluated on its actual text (those wrappers are NOT skip markers).
_WRAPPER_STRIP: Tuple[str, ...] = (
    "[recent channel messages]",
    "[new message]",
    "[continued]",
)

#: Role/name prefixes like "[Sam Liu]" or "User:" that carry no query signal.
_ROLE_PREFIX_RE = re.compile(
    r"\[[^\]]{1,40}\]|(?:^|\n)\s*(?:user|assistant|使用者|助理)\s*[:：]",
    re.IGNORECASE,
)

#: Keep letters/digits/CJK for the length judgement; drop punctuation/space.
_NONMEANINGFUL_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def is_low_signal_query(query: str) -> bool:
    """True ⇒ prefetch should inject nothing this turn.

    Deterministic. Fires on (a) explicit non-text envelopes, or (b) queries with
    fewer than ``MIN_SIGNAL_CHARS`` meaningful characters after wrapper/role
    stripping. Real questions wrapped in channel-message envelopes survive.
    """
    if not query:
        return True
    low = query.lower()
    for marker in _ENVELOPE_MARKERS:
        if marker in low:
            return True
    residual = low
    for wrap in _WRAPPER_STRIP:
        residual = residual.replace(wrap, " ")
    residual = _ROLE_PREFIX_RE.sub(" ", residual)
    meaningful = _NONMEANINGFUL_RE.sub("", residual)
    return len(meaningful) < MIN_SIGNAL_CHARS


# ---------------------------------------------------------------------------
# Tokenization (CJK-aware) + overlap metrics
# ---------------------------------------------------------------------------

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_cjk(cp: int) -> bool:
    return (
        0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
        0x3400 <= cp <= 0x4DBF or    # CJK Extension A
        0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
        0x3040 <= cp <= 0x309F or    # Hiragana
        0x30A0 <= cp <= 0x30FF or    # Katakana
        0xAC00 <= cp <= 0xD7AF       # Hangul Syllables
    )


def _cjk_runs(text: str) -> Iterable[str]:
    run: list = []
    for ch in text:
        if _is_cjk(ord(ch)):
            run.append(ch)
        elif run:
            yield "".join(run)
            run = []
    if run:
        yield "".join(run)


def terms(text: str) -> Set[str]:
    """Deterministic term set: lowercased ASCII tokens (len ≥ 2) + CJK bigrams.

    ASCII words shorter than 2 chars are dropped as noise. Each CJK run becomes
    its set of overlapping 2-grams; a lone CJK char contributes itself.
    """
    if not text:
        return set()
    low = text.lower()
    out: Set[str] = set()
    for tok in _ASCII_TOKEN_RE.findall(low):
        if len(tok) >= 2:
            out.add(tok)
    for run in _cjk_runs(low):
        if len(run) == 1:
            out.add(run)
        else:
            for i in range(len(run) - 1):
                out.add(run[i:i + 2])
    return out


def lexical_overlap(query: str, text: str) -> float:
    """Fraction of the query's terms that appear in ``text`` (0.0–1.0)."""
    q = terms(query)
    if not q:
        return 0.0
    t = terms(text)
    if not t:
        return 0.0
    return len(q & t) / len(q)


def redundancy_vs_l0(text: str, l0_terms: Set[str]) -> float:
    """Fraction of ``text``'s terms already present in the L0 hot zone (0.0–1.0)."""
    t = terms(text)
    if not t or not l0_terms:
        return 0.0
    return len(t & l0_terms) / len(t)


def gate_hit(
    query: str,
    text: str,
    *,
    is_microfile: bool,
    l0_terms: Set[str],
) -> Tuple[bool, str]:
    """Decide whether a single recall hit should be injected.

    Keep when the hit shares ≥ ``REL_MIN_SHARED`` terms with the query OR covers
    a strong fraction of it; then drop it if it is redundant with L0. Returns
    ``(keep, reason)`` where reason is ``kept`` / ``drop-relevance`` /
    ``drop-l0dup`` — the reason feeds the offline eval and the ``mem4 doctor``
    metric.
    """
    q = terms(query)
    t = terms(text)
    if not q or not t:
        return False, "drop-relevance"
    shared = len(q & t)
    frac = shared / len(q)
    frac_min = REL_MIN_FRAC_MICRO if is_microfile else REL_MIN_FRAC_TURN
    if shared < REL_MIN_SHARED and frac < frac_min:
        return False, "drop-relevance"
    if (len(t & l0_terms) / len(t)) >= L0_DEDUP_MAX:
        return False, "drop-l0dup"
    return True, "kept"
