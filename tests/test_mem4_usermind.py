"""Tests for mem4 §11 — Dream USER mind/preference summary (heuristic, zero-LLM).

Covers: heuristic extraction of explicit preferences, proposal-only refresh
(never touches USER.md), optional LLM condensation (off by default), and the
apply/restore write path with refine-style backup + atomic safety.
"""

import os

from mem4 import Mem4MemoryProvider
from mem4.usermind import UserMindSummarizer, extract_preferences, build_summary


def _memories(tmp_path):
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- heuristic extraction ----------------------------------------------------

def test_extract_picks_only_preference_statements():
    texts = [
        "User: 我偏好繁體中文，語氣精簡\nAssistant: 好的",
        "User: 今天天氣如何\nAssistant: 晴天",  # no preference cue → skipped
        "幫我填表時直接填好不要反問·時區用 Asia/Taipei",
        "I always prefer short bullet answers",
    ]
    items = extract_preferences(texts)
    joined = " | ".join(items)
    assert "偏好繁體中文" in joined
    assert "幫我填表" in joined and "時區用 Asia/Taipei" in joined
    assert "prefer short bullet" in joined.lower()
    assert "今天天氣如何" not in joined  # non-preference filtered out


def test_extract_dedups_and_caps():
    texts = ["偏好深色模式"] * 5 + [f"習慣 pattern {i}" for i in range(30)]
    items = extract_preferences(texts)
    assert items.count("偏好深色模式") == 1        # deduped
    assert len(items) <= 12                          # capped


def test_build_summary_heuristic():
    items = ["偏好繁體中文", "幫我直接填好"]
    plain = build_summary(items)
    assert "偏好繁體中文" in plain and "候選" in plain and "啟發式" in plain
    assert build_summary([]) == ""


def test_extract_drops_assistant_side_and_questions():
    texts = [
        "User: 我偏好用繁體中文\nAssistant: 需要我幫你設成預設嗎",
        "User: 這個要不要幫我留著",   # user-side but a question → dropped
    ]
    joined = " | ".join(extract_preferences(texts))
    assert "偏好用繁體中文" in joined       # user-side preference kept
    assert "幫你設成預設" not in joined      # assistant-side speech dropped
    assert "留著" not in joined             # user-side question dropped


def test_condense_with_llm_and_fallbacks():
    from mem4.usermind import condense_with_llm
    items = ["偏好繁體中文", "幫我一次填好"]
    # llm receives OpenAI messages, returns condensed text
    got = condense_with_llm(items, lambda msgs: "- 偏好繁體中文")
    assert got == "- 偏好繁體中文"
    # model says "no stable preference" → treated as empty (no proposal)
    assert condense_with_llm(items, lambda msgs: "(無穩定偏好)") == ""
    # raising / None llm → '' so the caller falls back to heuristic
    def boom(msgs):
        raise RuntimeError("x")
    assert condense_with_llm(items, boom) == ""
    assert condense_with_llm(items, None) == ""


def test_plan_llm_mode_and_degrade(tmp_path):
    _memories(tmp_path)
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好繁體中文·不要反問直接做·需要我幫你確認嗎",
                                     encoding="utf-8")
    # llm mode with a mock returning a clean list ⇒ effective llm
    smz = UserMindSummarizer(
        tmp_path, mode="llm",
        llm=lambda msgs: "- 偏好繁體中文\n- 不要反問，直接做")
    _items, summary = smz.plan()
    assert smz.last_effective_mode == "llm"
    assert "LLM 濃縮" in summary and "偏好繁體中文" in summary
    # llm returns the empty sentinel ⇒ degrade to heuristic (never hard-fails)
    smz2 = UserMindSummarizer(tmp_path, mode="llm", llm=lambda msgs: "(無穩定偏好)")
    _i2, s2 = smz2.plan()
    assert smz2.last_effective_mode == "heuristic" and "啟發式" in s2


# -- proposal-only refresh (never touches USER.md) ---------------------------

def test_refresh_proposal_never_touches_user_md(tmp_path):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\n原有內容\n", encoding="utf-8")
    original = user.read_text(encoding="utf-8")
    # mirror log of observed USER writes provides the source
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好繁體中文·不要反問直接做", encoding="utf-8")

    smz = UserMindSummarizer(tmp_path)
    summary = smz.refresh_proposal()
    assert summary and "偏好繁體中文" in summary
    assert smz.proposal_path.is_file()
    # USER.md is untouched by the proposal path
    assert user.read_text(encoding="utf-8") == original


# -- apply / restore (refine-style safety) -----------------------------------

def test_apply_backs_up_and_writes_managed_block(tmp_path):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\n既有 profile\n", encoding="utf-8")
    original = user.read_text(encoding="utf-8")
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好深色模式·習慣深夜工作", encoding="utf-8")

    smz = UserMindSummarizer(tmp_path)
    result = smz.apply()
    assert result["applied"] is True
    new_text = user.read_text(encoding="utf-8")
    assert "既有 profile" in new_text                 # existing content preserved
    assert "mem4:user-mind-summary" in new_text        # managed block markers
    assert "偏好深色模式" in new_text
    backups = smz.list_backups()
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original


def test_apply_is_idempotent_on_managed_block(tmp_path):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\nbase\n", encoding="utf-8")
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好繁體中文", encoding="utf-8")
    smz = UserMindSummarizer(tmp_path)
    smz.apply()
    smz.apply()  # second apply replaces the managed block, not appends a 2nd
    text = user.read_text(encoding="utf-8")
    assert text.count("mem4:user-mind-summary BEGIN") == 1


def test_restore_recovers_original(tmp_path):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\norig\n", encoding="utf-8")
    original = user.read_text(encoding="utf-8")
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好簡短回答", encoding="utf-8")
    smz = UserMindSummarizer(tmp_path)
    smz.apply()
    assert user.read_text(encoding="utf-8") != original
    assert smz.restore()["restored"] is True
    assert user.read_text(encoding="utf-8") == original


def test_apply_atomic_failure_leaves_user_md_intact(tmp_path, monkeypatch):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\nkeep me\n", encoding="utf-8")
    original = user.read_text(encoding="utf-8")
    mirror = tmp_path / "mem4" / "_mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "user.md").write_text("偏好繁體中文", encoding="utf-8")
    smz = UserMindSummarizer(tmp_path)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    result = smz.apply()
    assert result["applied"] is False
    assert user.read_text(encoding="utf-8") == original  # untouched
    assert smz.list_backups()                            # backup still taken


def test_no_preferences_is_graceful(tmp_path):
    _memories(tmp_path)
    smz = UserMindSummarizer(tmp_path)  # no recall, no mirror
    assert smz.refresh_proposal() is None
    assert smz.apply()["applied"] is False


# -- provider Dream④ trigger (proposal-only, never writes USER.md) -----------

def test_cli_usermind_wires_recall_store(tmp_path, monkeypatch, capsys):
    # Regression: cmd_usermind must construct UserMindSummarizer WITH a
    # RecallStore, so the CLI sees dialogue turns (the primary source). The bug
    # passed no recall store, so the CLI extracted nothing even when recall.db
    # was full of preference-bearing turns.
    import sys
    import types
    from mem4.recall import RecallStore

    (tmp_path / "memories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mem4").mkdir(parents=True, exist_ok=True)
    store = RecallStore(tmp_path / "mem4" / "recall.db")
    store.index(ref="turn:s1", content="User: 我偏好繁體中文，回答精簡條列",
                kind="turn", ts=1_780_000_000.0)
    store.close()

    hc = types.ModuleType("hermes_constants")
    hc.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", hc)

    from mem4.cli import cmd_usermind
    cmd_usermind(types.SimpleNamespace(restore=False, apply=False, ts=None))
    out = capsys.readouterr().out
    assert "抽出偏好項" in out                 # a proposal was produced
    assert "偏好繁體中文" in out                # extracted from the recall turn
    # dry-run must not have written USER.md
    assert not (tmp_path / "memories" / "USER.md").exists()


def test_cli_usermind_llm_mode(tmp_path, monkeypatch, capsys):
    # --mode llm builds a PluginLlm(plugin_id="mem4") and condenses via it; the
    # host facade is stubbed so this runs offline. Still proposal-only.
    import sys
    import types
    from mem4.recall import RecallStore

    (tmp_path / "memories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mem4").mkdir(parents=True, exist_ok=True)
    store = RecallStore(tmp_path / "mem4" / "recall.db")
    store.index(ref="turn:s1", content="User: 我偏好繁體中文，回答精簡\nAssistant: 好",
                kind="turn", ts=1_780_000_000.0)
    store.close()

    hc = types.ModuleType("hermes_constants")
    hc.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", hc)

    # Stub agent.plugin_llm.PluginLlm (host-owned LLM facade).
    pl = types.ModuleType("agent.plugin_llm")

    class _Res:
        text = "- 偏好繁體中文\n- 回答精簡"

    class PluginLlm:
        def __init__(self, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            return _Res()

    pl.PluginLlm = PluginLlm
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", pl)

    from mem4.cli import cmd_usermind
    cmd_usermind(types.SimpleNamespace(restore=False, apply=False, ts=None, mode="llm"))
    out = capsys.readouterr().out
    assert "實際=llm" in out
    assert "偏好繁體中文" in out and "LLM 濃縮" in out
    assert not (tmp_path / "memories" / "USER.md").exists()  # dry-run, proposal only


def test_provider_dream_refreshes_user_summary_proposal(tmp_path):
    _memories(tmp_path)
    user = tmp_path / "memories" / "USER.md"
    user.write_text("# USER\nprofile\n", encoding="utf-8")
    original = user.read_text(encoding="utf-8")
    provider = Mem4MemoryProvider({
        "backend": "local-file",
        "dream": {"enabled": True},
        "audit": {"enabled": False},
    })
    provider.initialize("s1", hermes_home=str(tmp_path))
    # a dialogue turn that expresses a preference feeds the heuristic
    provider.sync_turn("請你以後都用繁體中文，我偏好精簡條列", "好的")
    provider._refresh_user_summary(str(tmp_path))
    provider.shutdown()
    prop = tmp_path / "mem4" / "_user_summary_proposal.md"
    assert prop.is_file() and "偏好" in prop.read_text(encoding="utf-8")
    # USER.md never rewritten by the auto path
    assert user.read_text(encoding="utf-8") == original
