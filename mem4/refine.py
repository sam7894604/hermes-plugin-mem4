"""§3 縮限式放寬 — MEMORY.md 精煉（refine）：熱區縮小 + **持久化**引擎。

mem4 的鐵律是「日常永不回寫內建 MEMORY.md/USER.md」（design spike §3 / §8.3）。
本模組是那條鐵律**唯一**的、顯式的、可還原的放寬。

## 為什麼輸出用 memory 工具原生的 ``\\n§\\n`` entry 格式（2026-07-06 修正）

內建 ``tools/memory_tool.py`` 把 MEMORY.md 當成「entries 用 ``\\n§\\n`` 串接」的清單，
啟動時載入記憶體、之後每次 add/replace **從清單整檔重寫**，且有 drift 偵測（① 用
``\\n§\\n`` 拆再拼 ≠ 原檔，或 ② 任一 entry 超過字元上限 → 備份並拒絕寫入）。

早期 refine 把 MEMORY.md 改成 markdown 索引 → memory 工具把整份索引當成 1 個不透明
entry，一寫新記憶就從快取清單整檔重寫、把精煉成果蓋回 inline（**零持久**）。

修正版：refine 輸出 = **``\\n§\\n`` 分隔的短 entry**：每個 ``§code`` 條目抽進 L2 微檔、
換成一行「路由指標 entry」（帶 :data:`_POINTER_MARK` sentinel）；無 § 歸屬的條目原樣保留
為核心 entry。這樣 memory 工具讀回是乾淨離散短 entry（round-trip 一致、不觸發 drift、
各 entry < 上限），**會保留**；新記憶當新 entry 附加，Dream④ re-refine 再把新的抽走。
如此熱區才真正持續保持小。

安全：``--apply`` 前備份 MEMORY.md（時間戳、可 ``--restore``）；微檔用**合併**（不覆蓋、
會覆寫時先備份）；原子寫入（``.tmp`` → ``os.replace``，失敗原檔零改動）。冪等：已是精煉
態（無新 inline entry）時 re-refine 是 no-op。自動路徑預設只出提案；只有顯式 ``--apply``
或開啟 ``refine.persist_on_dream`` 的 Dream④ 才會改寫。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .backend import normalize_code
from .audit import estimate_tokens

#: 精煉產物落地位置（皆在 mem4-owned 樹下；內建檔不在此列）。
BACKUP_DIRNAME = "_refine_backups"
PROPOSAL_FILENAME = "_refine_proposal.md"
STATE_FILENAME = "_refine_state.json"

#: **必須與 tools/memory_tool.py 的 ENTRY_DELIMITER 一致。** MEMORY.md 是 memory 工具
#: 的 entries 用此分隔符串接的清單。
ENTRY_DELIMITER = "\n§\n"

#: 路由指標 entry 的 sentinel（辨識「已抽出的指標」以達冪等；正常使用者記憶不會出現）。
_POINTER_MARK = "⟪mem4⟫"

#: 一個 ``§code content`` 條目（entry 開頭是 §code，其後為內容；內容可跨行但不含分隔符）。
_CODED_ENTRY_RE = re.compile(r"^§\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s+(.+)$", re.DOTALL)

#: 摘要／指標長度上限（越短熱區越小；遠低於 memory 工具的整檔字元上限，不觸發 drift）。
_SUMMARY_CHARS = 80


def _slugify(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return t


def _summarize(body: str, limit: int = _SUMMARY_CHARS) -> str:
    """一行摘要：body 首個非空行、壓成單行、截短。"""
    text = ""
    for line in body.splitlines():
        s = line.strip().lstrip("#-*> ").strip()
        if s:
            text = s
            break
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(見微檔)"


def _pointer_entry(code: str, summary: str) -> str:
    """一行路由指標 entry：``§code ⟪mem4⟫ <summary> · mem_route(code)``。

    單行、含 sentinel、不含 ``\\n§\\n`` → memory 工具讀回為乾淨短 entry、可被冪等辨識。
    """
    return f"§{code} {_POINTER_MARK} {_summarize(summary)} · mem_route({code})"


def _is_pointer(entry: str) -> bool:
    return _POINTER_MARK in entry


@dataclass
class Section:
    """本輪要寫進微檔的一個 code（body = 合併後的完整微檔內容，供報告/寫入）。"""

    code: str
    summary: str
    body: str

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))


@dataclass
class RefinePlan:
    source_exists: bool
    mode: str  # "entry" | "empty"
    sections: List[Section] = field(default_factory=list)  # codes with NEW content this round
    condensed: str = ""
    before_bytes: int = 0
    after_bytes: int = 0
    _before_text_cached: str = ""

    @property
    def before_tokens(self) -> int:
        return estimate_tokens(len(self._before_text_cached))

    @property
    def after_tokens(self) -> int:
        return estimate_tokens(len(self.condensed))

    @property
    def n_microfiles(self) -> int:
        return len(self.sections)

    @property
    def bytes_saved(self) -> int:
        return self.before_bytes - self.after_bytes

    @property
    def changed(self) -> bool:
        """精煉輸出是否與現況不同（相同 ⇒ 已是精煉態，apply 為 no-op）。"""
        return self.source_exists and self.condensed != self._before_text_cached


class RefinePlanner:
    """精煉引擎。純檔案操作、無網路、無 LLM。"""

    def __init__(self, hermes_home, *, auditor=None):
        self.home = Path(hermes_home)
        self.memory_path = self.home / "memories" / "MEMORY.md"
        self.mem4_root = self.home / "mem4"
        self.backup_dir = self.mem4_root / BACKUP_DIRNAME
        self.proposal_path = self.mem4_root / PROPOSAL_FILENAME
        self.state_path = self.mem4_root / STATE_FILENAME
        self.auditor = auditor

    # -- 讀取 ---------------------------------------------------------------

    def _read_source(self) -> Optional[str]:
        if not self.memory_path.is_file():
            return None
        try:
            return self.memory_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _microfile_path(self, code: str) -> Path:
        return self.mem4_root / f"{code}.md"

    def _read_microfile_text(self, code: str) -> str:
        p = self._microfile_path(code)
        if not p.is_file():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _merge_microfile(existing: str, new_bodies: List[str]) -> str:
        """把新內容併入既有微檔（依段落去重，保序）。"""
        parts = [p.strip() for p in existing.split("\n\n")] if existing.strip() else []
        parts = [p for p in parts if p]
        seen = set(parts)
        for b in new_bodies:
            b = b.strip()
            if b and b not in seen:
                seen.add(b)
                parts.append(b)
        return "\n\n".join(parts)

    @staticmethod
    def _code_of(raw: str) -> Optional[str]:
        return normalize_code(raw) or normalize_code(_slugify(raw))

    # -- 解析 + 濃縮（entry 模型的核心） ------------------------------------

    def plan(self) -> RefinePlan:
        text = self._read_source()
        if text is None:
            return RefinePlan(source_exists=False, mode="empty")

        entries = [e.strip() for e in text.split(ENTRY_DELIMITER)]
        entries = [e for e in entries if e]

        # -- pass 1：分類每個 entry，收集各 code 的新內容與既有指標 --------------
        # items 保序：('core', text) | ('ptr', code, text) | ('coded', code, body, text)
        items: List[tuple] = []
        new_bodies: Dict[str, List[str]] = {}
        has_ptr: set = set()
        for e in entries:
            m = _CODED_ENTRY_RE.match(e)
            code = self._code_of(m.group(1)) if m else None
            if m and code:
                if _is_pointer(e):
                    has_ptr.add(code)
                    items.append(("ptr", code, e))
                else:
                    body = m.group(2).strip()
                    new_bodies.setdefault(code, []).append(body)
                    items.append(("coded", code, body, e))
            else:
                items.append(("core", None, e))

        # -- 抽取決策：**只有 pointer 比原內容短（真的省熱區）才抽**；已 cold 的 code
        #    （有既有指標）一律把新內容併進微檔。短 §entry 保留 inline，避免越精煉越大。
        extract: set = set()
        for code, bodies in new_bodies.items():
            merged_new = "\n\n".join(bodies)
            if code in has_ptr or len(_pointer_entry(code, _summarize(bodies[0]))) < len(merged_new):
                extract.add(code)

        # -- pass 2：保序組出濃縮輸出 + 微檔清單 -------------------------------
        order: List[tuple] = []   # ('keep', text) | ('ptr', code)
        emitted: set = set()
        for it in items:
            if it[0] == "core":
                order.append(("keep", it[2]))
            elif it[0] == "ptr":
                if it[1] not in emitted:   # 既有指標：原樣保留、去重
                    emitted.add(it[1])
                    order.append(("keep", it[2]))
            else:  # coded
                code, text_e = it[1], it[3]
                if code in extract:
                    if code not in emitted:
                        emitted.add(code)
                        order.append(("ptr", code))   # 這格放新指標（若無既有指標）
                    # 已有指標或已佔格：內容併進微檔、此 entry 收掉
                else:
                    order.append(("keep", text_e))     # 太短、抽了不划算 → 保留 inline

        sections: List[Section] = []
        merged: Dict[str, str] = {}
        for code in new_bodies:
            if code not in extract:
                continue
            content = self._merge_microfile(self._read_microfile_text(code), new_bodies[code])
            merged[code] = content
            sections.append(Section(code=code, summary=_summarize(new_bodies[code][0]), body=content))

        parts: List[str] = []
        for kind, payload in order:
            if kind == "keep":
                parts.append(payload)
            else:  # ptr — 只有無既有指標的 extract code 會走到（既有指標已 keep）
                parts.append(_pointer_entry(payload, _summarize(new_bodies[payload][0])))
        condensed = ENTRY_DELIMITER.join(parts)

        plan = RefinePlan(
            source_exists=True,
            mode="entry",
            sections=sections,
            condensed=condensed,
            before_bytes=len(text.encode("utf-8")),
            after_bytes=len(condensed.encode("utf-8")),
        )
        plan._before_text_cached = text
        # 供 apply 使用的合併內容掛在 plan 上（避免 apply 再算一次）。
        plan._merged = merged  # type: ignore[attr-defined]
        return plan

    def render_plan(self, plan: RefinePlan) -> str:
        lines = ["", "mem4 refine — 精煉提案 (dry-run)", "─" * 40]
        if not plan.source_exists:
            lines += [f"  找不到 MEMORY.md：{self.memory_path}", ""]
            return "\n".join(lines)
        if not plan.changed:
            lines += ["  MEMORY.md 已是精煉態（無新 inline 內容）—— 無需精煉。", ""]
            return "\n".join(lines)
        pct = (100 * plan.bytes_saved / plan.before_bytes) if plan.before_bytes else 0
        lines += [
            f"  來源：{self.memory_path}",
            f"  格式：memory 工具原生 \\n§\\n entry（持久化）",
            f"  本輪抽出/更新微檔：{plan.n_microfiles}",
            f"  MEMORY.md：{plan.before_bytes} → {plan.after_bytes} bytes "
            f"（縮小 {plan.bytes_saved} bytes / {pct:.0f}%）",
            f"  token 估計：{plan.before_tokens} → {plan.after_tokens} tokens",
        ]
        if plan.sections:
            lines += ["", "  微檔（合併後大小）："]
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
        """把最新提案寫到 mem4-owned 提案檔。**永不** apply、永不碰內建檔。"""
        try:
            plan = self.plan()
            if not plan.source_exists or not plan.changed:
                return plan
            self.mem4_root.mkdir(parents=True, exist_ok=True)
            self.proposal_path.write_text(self.render_plan(plan), encoding="utf-8")
            return plan
        except Exception:
            return None

    # -- 狀態（hash 冪等） --------------------------------------------------

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, applied_hash: str) -> None:
        try:
            self.mem4_root.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"last_applied_hash": applied_hash}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    # -- 套用（唯一會改寫 MEMORY.md 的路徑） --------------------------------

    def _utc_stamp(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def apply(self, plan: Optional[RefinePlan] = None) -> dict:
        """備份 → 合併寫微檔 → 原子改寫 MEMORY.md。失敗則原檔零改動。"""
        plan = plan or self.plan()
        if not plan.source_exists:
            return {"applied": False, "reason": "no MEMORY.md"}
        if not plan.changed:
            # 已是精煉態：把 hash 記下（讓 apply_if_changed 快取），視為 no-op。
            self._save_state(self._hash(plan._before_text_cached))
            return {"applied": False, "reason": "already refined (no-op)"}

        self.mem4_root.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._utc_stamp()

        # 1) 備份 MEMORY.md（第一優先）。
        backup_path = self.backup_dir / f"MEMORY-{stamp}.md"
        backup_path.write_text(plan._before_text_cached, encoding="utf-8")

        # 2) 合併寫微檔；內容有變的既有微檔先備份 —— 絕不靜默覆蓋。
        merged: Dict[str, str] = getattr(plan, "_merged", {})
        overwritten = 0
        mf_backup_dir = self.backup_dir / f"microfiles-{stamp}"
        for s in plan.sections:
            content = merged.get(s.code, s.body)
            target = self._microfile_path(s.code)
            if target.is_file():
                try:
                    old = target.read_text(encoding="utf-8")
                except OSError:
                    old = ""
                if old.strip() != content.strip():
                    mf_backup_dir.mkdir(parents=True, exist_ok=True)
                    (mf_backup_dir / f"{s.code}.md").write_text(old, encoding="utf-8")
                    overwritten += 1
            target.write_text(content, encoding="utf-8")

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

        self._save_state(self._hash(plan.condensed))
        if self.auditor is not None:
            try:
                self.auditor.record_refine(
                    before_bytes=plan.before_bytes, after_bytes=plan.after_bytes,
                    microfiles=plan.n_microfiles, archived=1 + overwritten,
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

    def apply_if_changed(self) -> dict:
        """Dream④ 週期性 re-refine 用：只有 MEMORY.md 自上次精煉後有變才 apply。

        hash 快取先擋（沒變就 no-op、零解析）；再以 plan.changed 為準。
        """
        text = self._read_source()
        if text is None:
            return {"applied": False, "reason": "no MEMORY.md"}
        if self._load_state().get("last_applied_hash") == self._hash(text):
            return {"applied": False, "reason": "unchanged since last refine"}
        plan = self.plan()
        if not plan.changed:
            self._save_state(self._hash(text))
            return {"applied": False, "reason": "already refined (no-op)"}
        return self.apply(plan)

    # -- 還原 ---------------------------------------------------------------

    def list_backups(self) -> List[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(self.backup_dir.glob("MEMORY-*.md"))

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
        # 還原後清掉 last_applied_hash（下次 Dream 會依現況重新判斷）。
        self._save_state("")
        return {"restored": True, "from": str(src)}
