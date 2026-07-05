"""§3 縮限式放寬 — MEMORY.md 精煉（refine）：熱區縮小的提案／套用引擎。

mem4 的鐵律是「日常永不回寫內建 MEMORY.md/USER.md」（design spike §3 / §8.3）。
本模組是那條鐵律**唯一**的、顯式的、可還原的放寬：只有使用者明確執行
``hermes mem4 refine --apply`` 才會改寫 MEMORY.md，而且：

  * 預設 ``--dry-run`` 只產出提案，不碰任何內建檔。
  * ``--apply`` 前一定先把 MEMORY.md 備份到
    ``$HERMES_HOME/mem4/_refine_backups/MEMORY-<UTCts>.md``；任何會被覆寫的既有
    微檔也一併備份 —— **絕不靜默覆蓋**。
  * 改寫用原子寫入（先寫 ``.tmp`` 再 ``os.replace``）：中途失敗則原檔零改動。
  * ``--restore [<ts>]`` 可從備份還原 MEMORY.md。
  * on_memory_write / Dream④ 等自動路徑**永遠**只呼叫 :meth:`refresh_proposal`
    （寫 mem4-owned 的提案檔），從不 apply。

精煉策略 = 啟發式優先、零 LLM、零依賴：
  1. 動態解析 MEMORY.md 的 ``§<code>`` 段落標記（不寫死段名）。
  2. 每段抽成 ``$HERMES_HOME/mem4/<code>.md`` 的 L2 微檔。
  3. 濃縮後的 MEMORY.md = 短標頭 + 無 § 歸屬的必要核心 + 路由索引
     （每個 §code 一行摘要，指向對應微檔）。
  4. 無 § 標記時降級：先用 markdown 標題分段，再無則用大小分塊。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .backend import normalize_code
from .audit import estimate_tokens

#: 精煉產物落地位置（皆在 mem4-owned 樹下；內建檔不在此列）。
BACKUP_DIRNAME = "_refine_backups"
PROPOSAL_FILENAME = "_refine_proposal.md"

#: § 段落標記：行首（可含 0-6 個 ``#`` 與少量縮排）出現 ``§<code>``。code 取
#: 其後的 token（字母數字起頭，可含 ``_`` ``-``）。標記行其餘文字當標題。
_SECTION_RE = re.compile(r"^\s{0,3}#{0,6}\s*§\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*(.*)$")

#: 降級用的 markdown 標題（## 以下；一級 # 常是整檔標題，略過）。
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*#*$")

#: 大小分塊降級時每塊的目標字元數。
_CHUNK_CHARS = 1500


def _slugify(text: str) -> str:
    """把任意標題轉成 ascii route code 候選（CJK 等非 ascii 會被清成空字串）。"""
    t = text.strip().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t


def _make_code(raw: str, index: int, used: set) -> str:
    """產生唯一且合法的 route code；非 ascii 標題退化成 ``s<N>``。"""
    base = normalize_code(raw) or normalize_code(_slugify(raw)) or f"s{index + 1}"
    code = base
    n = 2
    while code in used:
        code = f"{base}-{n}"
        n += 1
    used.add(code)
    return code


def _summarize(body: str, title: str = "", limit: int = 80) -> str:
    """一行摘要：優先用標記行的標題，否則取 body 首個非空行。"""
    text = title.strip()
    if not text:
        for line in body.splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                text = s
                break
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(無摘要)"


@dataclass
class Section:
    code: str
    summary: str
    body: str

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))


@dataclass
class RefinePlan:
    source_exists: bool
    mode: str  # "section" | "heading" | "chunk" | "empty"
    preamble: str
    sections: List[Section] = field(default_factory=list)
    condensed: str = ""
    before_bytes: int = 0
    after_bytes: int = 0

    @property
    def before_tokens(self) -> int:
        # token 估計以字元數為準（estimate_tokens 吃 char 數）。
        return estimate_tokens(len(self._before_text_cached))

    _before_text_cached: str = ""

    @property
    def after_tokens(self) -> int:
        return estimate_tokens(len(self.condensed))

    @property
    def n_microfiles(self) -> int:
        return len(self.sections)

    @property
    def bytes_saved(self) -> int:
        return self.before_bytes - self.after_bytes


class RefinePlanner:
    """精煉引擎。純檔案操作、無網路、無 LLM。"""

    def __init__(self, hermes_home, *, auditor=None):
        self.home = Path(hermes_home)
        self.memory_path = self.home / "memories" / "MEMORY.md"
        self.mem4_root = self.home / "mem4"
        self.backup_dir = self.mem4_root / BACKUP_DIRNAME
        self.proposal_path = self.mem4_root / PROPOSAL_FILENAME
        self.auditor = auditor

    # -- 解析 ---------------------------------------------------------------

    def _read_source(self) -> Optional[str]:
        if not self.memory_path.is_file():
            return None
        try:
            return self.memory_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _parse(self, text: str) -> "tuple[str, str, List[Section]]":
        """回傳 (mode, preamble, sections)。動態選段落文法。"""
        lines = text.splitlines()

        # 1) § 段落模式（優先）：只要出現至少一個 § 標記。code 來自 § 後的 token；
        #    標記行 § code 之後的**同行文字是內容**（手寫 MEMORY.md 常把整條記憶
        #    寫在 §code 那一行），必須併入 body，不能只當標題。
        #    entry = (line_index, code_hint, inline_content, title_for_summary)
        section_entries = [
            (i, m.group(1), m.group(2), "") for i, line in enumerate(lines)
            if (m := _SECTION_RE.match(line))
        ]
        if section_entries:
            return self._sections_from_entries(lines, section_entries, mode="section")

        # 2) markdown 標題降級：至少兩個 ## 標題才值得拆。code 由標題文字衍生，
        #    標題文字是 body 上方的標題（非內容），故 inline 為空、標題只餵摘要。
        heading_entries = [
            (i, m.group(2), "", m.group(2)) for i, line in enumerate(lines)
            if (m := _HEADING_RE.match(line))
        ]
        if len(heading_entries) >= 2:
            return self._sections_from_entries(lines, heading_entries, mode="heading")

        # 3) 大小分塊降級。
        return self._sections_from_chunks(text)

    @staticmethod
    def _clean_body(text: str) -> str:
        """清掉手寫 MEMORY.md 常見的裸 ``§`` 分隔行等噪音，讓微檔 body 乾淨。"""
        kept = [ln for ln in text.splitlines() if ln.strip() != "§"]
        return "\n".join(kept).strip()

    def _base_code(self, code_hint: str, index: int) -> str:
        """段落 code 的基底（未加去重後綴）；非 ascii 標題退化為 ``s<N>``。"""
        return (normalize_code(code_hint) or normalize_code(_slugify(code_hint))
                or f"s{index + 1}")

    def _sections_from_entries(self, lines, entries, *, mode) -> "tuple[str, str, List[Section]]":
        """entries = [(line_index, code_hint, inline_content, title)]，依序切段。

        每段 body = 標記行的同行內容（inline）＋其下方到下一標記前的行（清掉裸 §
        噪音）。同一 route code 的重複段落會**合併**成單一微檔（route code → 單檔
        的模型才成立），依首次出現順序排列、body 依序串接。
        """
        preamble = self._clean_body("\n".join(lines[: entries[0][0]]))
        order: List[str] = []
        merged: "dict[str, list]" = {}  # code -> [title, [bodies]]
        for n, (i, code_hint, inline, title) in enumerate(entries):
            end = entries[n + 1][0] if n + 1 < len(entries) else len(lines)
            below = self._clean_body("\n".join(lines[i + 1 : end]))
            body = "\n".join(p for p in (inline.strip(), below) if p)
            code = self._base_code(code_hint, n)
            if code not in merged:
                merged[code] = [title, []]
                order.append(code)
            if body:
                merged[code][1].append(body)
        sections: List[Section] = []
        for code in order:
            title, bodies = merged[code]
            body = "\n\n".join(bodies)
            sections.append(Section(code=code, summary=_summarize(body, title=title), body=body))
        return mode, preamble, sections

    def _sections_from_chunks(self, text: str) -> "tuple[str, str, List[Section]]":
        stripped = text.strip()
        if not stripped:
            return "empty", "", []
        used: set = set()
        paras = re.split(r"\n\s*\n", stripped)
        sections: List[Section] = []
        buf: List[str] = []
        size = 0

        def flush():
            nonlocal buf, size
            if not buf:
                return
            body = "\n\n".join(buf).strip()
            idx = len(sections)
            code = _make_code(f"part{idx + 1}", idx, used)
            sections.append(Section(code=code, summary=_summarize(body), body=body))
            buf, size = [], 0

        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= _CHUNK_CHARS:
                flush()
        flush()
        # 分塊模式沒有「無歸屬核心」概念，全部進微檔。
        return "chunk", "", sections

    # -- 濃縮輸出 -----------------------------------------------------------

    @staticmethod
    def _strip_leading_h1(text: str) -> str:
        """濃縮檔已有自己的 H1；去掉 preamble 開頭殘留的原始一級標題避免重複。"""
        lines = text.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and re.match(r"^\s{0,3}#\s+\S", lines[0]):
            lines.pop(0)
        return "\n".join(lines).strip()

    def _build_condensed(self, preamble: str, sections: List[Section]) -> str:
        out = [
            "# MEMORY.md（mem4 精煉索引）",
            "",
            "> 本檔已由 `hermes mem4 refine` 精煉：長段落移入 mem4 冷區微檔，"
            "此處僅保留常駐核心與路由索引。",
            "> 需完整內容用 `mem_route(<code>)` 讀微檔；需回復原檔用 "
            "`hermes mem4 refine --restore`。",
            "",
        ]
        core = self._strip_leading_h1(preamble)
        if core.strip():
            out += ["## 核心（常駐）", "", core.strip(), ""]
        if sections:
            out += ["## 路由索引", ""]
            for s in sections:
                out.append(
                    f"- §{s.code} — {s.summary} → 微檔 `{s.code}.md`"
                    f"（`mem_route({s.code})`）"
                )
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    # -- 提案（dry-run 核心） ----------------------------------------------

    def plan(self) -> RefinePlan:
        text = self._read_source()
        if text is None:
            return RefinePlan(source_exists=False, mode="empty", preamble="")
        mode, preamble, sections = self._parse(text)
        condensed = self._build_condensed(preamble, sections) if sections else text
        plan = RefinePlan(
            source_exists=True,
            mode=mode,
            preamble=preamble,
            sections=sections,
            condensed=condensed,
            before_bytes=len(text.encode("utf-8")),
            after_bytes=len(condensed.encode("utf-8")),
        )
        plan._before_text_cached = text
        return plan

    def render_plan(self, plan: RefinePlan) -> str:
        lines = ["", "mem4 refine — 精煉提案 (dry-run)", "─" * 40]
        if not plan.source_exists:
            lines.append(f"  找不到 MEMORY.md：{self.memory_path}")
            lines.append("")
            return "\n".join(lines)
        if not plan.sections:
            lines.append("  MEMORY.md 無可拆分的段落 —— 不需精煉。")
            lines.append("")
            return "\n".join(lines)
        pct = (100 * plan.bytes_saved / plan.before_bytes) if plan.before_bytes else 0
        lines += [
            f"  來源：{self.memory_path}",
            f"  分段模式：{plan.mode}   微檔數：{plan.n_microfiles}",
            f"  MEMORY.md：{plan.before_bytes} → {plan.after_bytes} bytes "
            f"（縮小 {plan.bytes_saved} bytes / {pct:.0f}%）",
            f"  token 估計：{plan.before_tokens} → {plan.after_tokens} tokens",
            "",
            "  將抽出的微檔：",
        ]
        for s in plan.sections:
            lines.append(f"    §{s.code:<12} {s.body_bytes:>6} bytes  {s.summary}")
        lines += [
            "",
            "  這是提案（dry-run），未改動任何檔案。",
            "  套用：hermes mem4 refine --apply  （會先備份、可 --restore 還原）",
            "",
        ]
        return "\n".join(lines)

    def refresh_proposal(self) -> Optional[RefinePlan]:
        """把最新提案寫到 mem4-owned 提案檔。**永不** apply、永不碰內建檔。

        由首次 bootstrap 與 Dream④ 呼叫。任何錯誤都吞掉（不得影響一輪對話）。
        """
        try:
            plan = self.plan()
            if not plan.source_exists or not plan.sections:
                return plan
            self.mem4_root.mkdir(parents=True, exist_ok=True)
            self.proposal_path.write_text(self.render_plan(plan), encoding="utf-8")
            return plan
        except Exception:
            return None

    # -- 套用（唯一會改寫 MEMORY.md 的路徑，需顯式 --apply） ----------------

    def _utc_stamp(self) -> str:
        # 避免用 datetime.now()（測試/決定性考量交由呼叫端），這裡直接取檔案系統時鐘。
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def apply(self, plan: Optional[RefinePlan] = None) -> dict:
        """備份 → 寫微檔 → 原子改寫 MEMORY.md。失敗則原檔零改動。"""
        plan = plan or self.plan()
        if not plan.source_exists:
            return {"applied": False, "reason": "no MEMORY.md"}
        if not plan.sections:
            return {"applied": False, "reason": "nothing to refine"}

        self.mem4_root.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._utc_stamp()

        # 1) 備份 MEMORY.md（第一優先，任何後續失敗都留有原檔）。
        backup_path = self.backup_dir / f"MEMORY-{stamp}.md"
        backup_path.write_text(plan._before_text_cached, encoding="utf-8")

        # 2) 寫微檔；會覆寫既有微檔時先備份到同一時戳目錄 —— 絕不靜默覆蓋。
        overwritten = 0
        mf_backup_dir = self.backup_dir / f"microfiles-{stamp}"
        for s in plan.sections:
            target = self.mem4_root / f"{s.code}.md"
            if target.is_file():
                try:
                    old = target.read_text(encoding="utf-8")
                except OSError:
                    old = ""
                if old.strip() != s.body.strip():
                    mf_backup_dir.mkdir(parents=True, exist_ok=True)
                    (mf_backup_dir / f"{s.code}.md").write_text(old, encoding="utf-8")
                    overwritten += 1
            target.write_text(s.body, encoding="utf-8")

        # 3) 原子改寫 MEMORY.md（tmp → os.replace）；失敗清 tmp、原檔不動。
        tmp = self.memory_path.with_suffix(".md.refine-tmp")
        try:
            tmp.write_text(plan.condensed, encoding="utf-8")
            os.replace(str(tmp), str(self.memory_path))
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"applied": False, "reason": f"atomic write failed: {e}",
                    "backup": str(backup_path)}

        archived = 1 + overwritten  # MEMORY.md + 被覆寫的微檔
        if self.auditor is not None:
            try:
                self.auditor.record_refine(
                    before_bytes=plan.before_bytes, after_bytes=plan.after_bytes,
                    microfiles=plan.n_microfiles, archived=archived,
                    before_tokens=plan.before_tokens, after_tokens=plan.after_tokens,
                    applied=True,
                )
            except Exception:
                pass

        return {
            "applied": True,
            "backup": str(backup_path),
            "microfiles": plan.n_microfiles,
            "overwritten_microfiles": overwritten,
            "before_bytes": plan.before_bytes,
            "after_bytes": plan.after_bytes,
            "stamp": stamp,
        }

    # -- 還原 ---------------------------------------------------------------

    def list_backups(self) -> List[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(self.backup_dir.glob("MEMORY-*.md"))

    def restore(self, ts: Optional[str] = None) -> dict:
        """從備份還原 MEMORY.md。ts 省略時取最新。"""
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
        tmp = self.memory_path.with_suffix(".md.restore-tmp")
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            os.replace(str(tmp), str(self.memory_path))
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"restored": False, "reason": f"atomic write failed: {e}"}
        return {"restored": True, "from": str(src)}
