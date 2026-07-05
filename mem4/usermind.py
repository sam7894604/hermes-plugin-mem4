"""§11 Honcho 借鏡 — Dream④ 產出的 USER 心智/偏好摘要(啟發式優先、零 LLM)。

mem4 的立身之本是**零外部依賴**(design spike §9.4)。Honcho 那種「使用者心智
建模(theory-of-mind、偏好/立場)」若引入本體服務/DB/外呼 LLM 就違背此原則,故
本模組:

  * **預設純啟發式、零 LLM、零依賴**:從最近的對話 turn(recall store)與觀察到的
    內建 USER 寫入鏡射中,抽取**顯式偏好陳述**(「偏好/不要/幫我/風格…」等線索),
    去重、截短、組成一段極短摘要。
  * **LLM 濃縮為選配、預設 OFF**:只有呼叫端注入既有的 LLM callback 且顯式開啟時
    才用(延續 §11 不可退讓原則:不新增服務/DB,只複用既有 LLM pass)。
  * **預設只產「提案」不覆蓋**:`refresh_proposal()`(Dream④ 呼叫)只寫 mem4-owned
    的 `_user_summary_proposal.md`,**永不**碰內建 USER.md。
  * **若真要寫 USER.md**:`apply()` 走與 refine 相同的安全套路 —— 先備份、原子寫入
    (失敗原檔零改動)、以標記界定的「受管區塊」冪等覆寫、可 `restore()` 還原。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

#: mem4-owned 產物(皆不在內建記憶樹下)。
USER_SUMMARY_PROPOSAL = "_user_summary_proposal.md"
USER_BACKUP_DIRNAME = "_user_backups"

#: USER.md 內由 mem4 管理的受管區塊界標(apply 只動這對標記之間)。
_BLOCK_BEGIN = "<!-- mem4:user-mind-summary BEGIN -->"
_BLOCK_END = "<!-- mem4:user-mind-summary END -->"

#: 顯式偏好線索(繁中 + 英文);ascii 部分大小寫不敏感。
_CUES = [
    "偏好", "喜歡", "不喜歡", "討厭", "習慣", "總是", "從不", "不要", "別",
    "要我", "幫我", "請你", "風格", "語氣", "稱呼", "時區", "作息",
    "prefer", "likes", "dislike", "always", "never", "hate", "style", "tone",
]
_CUE_RE = re.compile("|".join(re.escape(c) for c in _CUES), re.IGNORECASE)
#: 把一段文字切成候選陳述(中英標點 + 常見分隔號 ·)。
_SPLIT_RE = re.compile(r"[·\n。;；!！?？]+")
#: 去掉 turn 記錄的角色前綴。
_ROLE_RE = re.compile(r"^\s*(User|Assistant|使用者|助理)\s*[:：]\s*", re.IGNORECASE)

_MAX_ITEMS = 12
_MAX_ITEM_CHARS = 100
_MAX_SUMMARY_CHARS = 900


def extract_preferences(texts: Iterable[str]) -> List[str]:
    """從文字集啟發式抽取顯式偏好陳述(零 LLM);去重、截短、封頂。"""
    seen: set = set()
    out: List[str] = []
    for t in texts:
        if not t:
            continue
        for seg in _SPLIT_RE.split(t):
            s = _ROLE_RE.sub("", seg).strip().lstrip("-*#> ").strip()
            if len(s) < 4 or not _CUE_RE.search(s):
                continue
            key = re.sub(r"\s+", "", s.lower())
            if key in seen:
                continue
            seen.add(key)
            if len(s) > _MAX_ITEM_CHARS:
                s = s[: _MAX_ITEM_CHARS - 1].rstrip() + "…"
            out.append(s)
            if len(out) >= _MAX_ITEMS:
                return out
    return out


def build_summary(items: List[str], *, llm: Optional[Callable[[str], str]] = None) -> str:
    """把偏好項組成極短摘要;llm 有給才用(選配、由呼叫端決定是否開)。"""
    if not items:
        return ""
    body = "\n".join(f"- {it}" for it in items)
    if llm is not None:
        try:
            condensed = llm(body)
            if condensed and condensed.strip():
                body = condensed.strip()
        except Exception:
            pass  # LLM 失敗就退回啟發式清單,絕不讓它弄壞 Dream
    text = (
        "## USER 心智/偏好摘要（mem4 Dream 候選提案，未套用）\n\n"
        "> 啟發式抽取自近期對話與內建 USER 寫入；僅為候選，未回寫內建 USER.md。\n\n"
        f"{body}\n"
    )
    return text[:_MAX_SUMMARY_CHARS]


class UserMindSummarizer:
    """Dream④ 的 USER 心智摘要器。純本機讀取、預設零 LLM、預設只產提案。"""

    def __init__(self, hermes_home, *, recall=None, backend=None,
                 llm: Optional[Callable[[str], str]] = None, llm_enabled: bool = False):
        self.home = Path(hermes_home)
        self.user_path = self.home / "memories" / "USER.md"
        self.mem4_root = self.home / "mem4"
        self.mirror_dir = self.mem4_root / "_mirror"
        self.proposal_path = self.mem4_root / USER_SUMMARY_PROPOSAL
        self.backup_dir = self.mem4_root / USER_BACKUP_DIRNAME
        self.recall = recall
        self.backend = backend
        self.llm = llm
        self.llm_enabled = bool(llm_enabled)

    # -- 來源蒐集(純本機) --------------------------------------------------

    def _gather_texts(self, turn_limit: int = 200) -> List[str]:
        texts: List[str] = []
        # 主要訊號:近期對話 turn(Honcho-lite —— 偏好從對話浮現,而非重述 USER.md)。
        if self.recall is not None:
            try:
                texts.extend(self.recall.recent(kind="turn", limit=turn_limit))
            except Exception:
                pass
        # 次要:觀察到的內建 USER 寫入鏡射。
        mp = self.mirror_dir / "user.md"
        if mp.is_file():
            try:
                texts.append(mp.read_text(encoding="utf-8"))
            except OSError:
                pass
        return texts

    def plan(self) -> Tuple[List[str], str]:
        items = extract_preferences(self._gather_texts())
        use_llm = self.llm if (self.llm_enabled and self.llm is not None) else None
        return items, build_summary(items, llm=use_llm)

    # -- Dream④ 觸發:只產提案,永不碰 USER.md ------------------------------

    def refresh_proposal(self) -> Optional[str]:
        """寫 mem4-owned 提案檔。任何錯誤都吞掉(不得影響一輪對話)。"""
        try:
            _items, summary = self.plan()
            if not summary:
                return None
            self.mem4_root.mkdir(parents=True, exist_ok=True)
            self.proposal_path.write_text(summary, encoding="utf-8")
            return summary
        except Exception:
            return None

    # -- 選配寫回(走 refine 那套備份/原子/還原) ---------------------------

    def _utc_stamp(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _splice_block(self, original: str, summary: str) -> str:
        """把摘要放進 USER.md 的受管區塊:存在則冪等覆寫,否則附加於末。"""
        block = f"{_BLOCK_BEGIN}\n{summary.rstrip()}\n{_BLOCK_END}"
        if _BLOCK_BEGIN in original and _BLOCK_END in original:
            pattern = re.compile(
                re.escape(_BLOCK_BEGIN) + r".*?" + re.escape(_BLOCK_END), re.DOTALL)
            return pattern.sub(lambda _m: block, original)
        base = original.rstrip()
        return (base + "\n\n" + block + "\n") if base else (block + "\n")

    def apply(self) -> dict:
        """把摘要寫進 USER.md 的受管區塊。先備份、原子寫入,失敗原檔零改動。"""
        _items, summary = self.plan()
        if not summary:
            return {"applied": False, "reason": "no preferences extracted"}
        original = ""
        if self.user_path.is_file():
            try:
                original = self.user_path.read_text(encoding="utf-8")
            except OSError as e:
                return {"applied": False, "reason": f"cannot read USER.md: {e}"}
        self.mem4_root.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._utc_stamp()
        backup_path = self.backup_dir / f"USER-{stamp}.md"
        backup_path.write_text(original, encoding="utf-8")

        new_text = self._splice_block(original, summary)
        tmp = self.user_path.with_suffix(".md.usermind-tmp")
        try:
            self.user_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(str(tmp), str(self.user_path))
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"applied": False, "reason": f"atomic write failed: {e}",
                    "backup": str(backup_path)}
        return {"applied": True, "backup": str(backup_path), "stamp": stamp,
                "items": len(_items)}

    def list_backups(self) -> List[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(self.backup_dir.glob("USER-*.md"))

    def restore(self, ts: Optional[str] = None) -> dict:
        backups = self.list_backups()
        if not backups:
            return {"restored": False, "reason": "no backups found"}
        if ts:
            match = [p for p in backups if ts in p.name]
            if not match:
                return {"restored": False, "reason": f"no backup matching {ts!r}"}
            src = match[-1]
        else:
            src = backups[-1]
        try:
            content = src.read_text(encoding="utf-8")
        except OSError as e:
            return {"restored": False, "reason": f"cannot read backup: {e}"}
        tmp = self.user_path.with_suffix(".md.usermind-restore-tmp")
        try:
            self.user_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            os.replace(str(tmp), str(self.user_path))
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"restored": False, "reason": f"atomic write failed: {e}"}
        return {"restored": True, "from": str(src)}
