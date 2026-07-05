"""mem4 — four-tier routed memory as a Hermes MemoryProvider (⑤-minimal chassis).

Wraps the L0/L1/L2/L3 routed-memory design (ADR-018) as a pluggable
``MemoryProvider`` in **coexist / augment** mode: it strengthens the built-in
``MEMORY.md``/``USER.md`` but never replaces them. Disabling it (removing
``memory.provider: mem4`` from config.yaml) degrades cleanly back to pure
built-in memory, with zero residue.

This module is the ⑤-minimal *chassis*. It implements:
  * provider identity + availability + idempotent one-time init (§10)
  * the ``mem_route`` tool (route code -> L2/L3 microfile read, with freshness
    tags and graceful miss handling)
  * a routing legend in the system prompt and pre-compression summary
  * mirroring of built-in memory writes into mem4-owned files (never the
    built-in files)
  * a switchable storage backend, defaulting to local-file (see backend.py)

Feature ④ (Dream consolidation) is wired in via dream.py — event/threshold +
session-boundary staleness triggers over mem4-owned L2/L3, fully in-provider
with no external cron dependency. See dream.py and the README deployment note.

Feature ① (FTS5 recall) is wired in via recall.py — mem4's own dual-table FTS5
database (unicode61 + trigram, with CJK routing and LIKE fallback) indexing both
conversation turns (via ``sync_turn``) and the L2/L3 microfiles. It powers the
``mem_search`` tool and ``prefetch`` (local-I/O-only, char-capped). Backfill of
existing history is resumable via the ``.mem4_state.json`` cursor (§10.4).

See design spike: 技術/架構決策/2026-07-04_四層記憶包裝為Hermes-Provider設計spike.md
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .backend import (
    STATE_FILENAME,
    MIRROR_DIRNAME,
    LocalFileBackend,
    StorageBackend,
    build_backend,
    normalize_code,
)
from .dream import (
    DreamProcessor,
    DEFAULT_ENABLED,
    DEFAULT_THRESHOLD,
    DEFAULT_STALENESS_DAYS,
)
from .recall import RecallStore
from .audit import Auditor, AUDIT_LOG_FILENAME

logger = logging.getLogger(__name__)

#: A/B arms (design spike §7). "experiment" = full mem4; "baseline" = provider
#: loaded but all agent-facing surfaces off (no tools, no injection) so hot-zone
#: cost matches pure built-in while the recall store can still be measured.
ARM_EXPERIMENT = "experiment"
ARM_BASELINE = "baseline"

#: Default cap on characters injected by prefetch() (design spike / Fable 5 §2).
DEFAULT_PREFETCH_CAP = 2000
#: sync_turn filter: minimum user-content length worth indexing.
_MIN_INDEX_LEN = 12
#: Backfill batches processed per background worker (bounded per run).
_BACKFILL_BATCH_SIZE = 200


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default

#: State-marker schema version (design spike §10.1). Bump when the on-disk
#: layout changes; ``_migrate`` walks from the stored version up to this.
SCHEMA_VERSION = 1

#: Default backend when ``memory.mem4.backend`` is unset (design spike §9.3 b).
DEFAULT_BACKEND = "local-file"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

MEM_ROUTE_SCHEMA = {
    "name": "mem_route",
    "description": (
        "Read a mem4 cold-tier microfile by route code when built-in memory "
        "(the always-loaded MEMORY.md/USER.md) lacks the detail. Codes: "
        "sys (system/environment), fam (people/family), vlt (vault/knowledge), "
        "adr (architecture decisions), proto (protocols/workflows). The leading "
        "§ is optional. Returns the microfile content prefixed with a freshness "
        "tag; a miss falls back to built-in memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Route code, e.g. 'sys', 'fam', 'vlt', 'adr', 'proto'.",
            },
        },
        "required": ["code"],
    },
}

#: The tiny always-resident routing legend (system_prompt_block). Kept as a
#: module constant so its size can be measured for the ② resident-cost metric.
ROUTING_LEGEND = (
    "# mem4 記憶路由（補強層）\n"
    "內建 MEMORY.md/USER.md 為權威 L0；mem4 提供按需的冷區微檔讀取與召回。\n"
    "路由碼：§sys 系統/環境 · §fam 人物/家庭 · §vlt vault/知識 · "
    "§adr 架構決策 · §proto 協定/流程。\n"
    "L0 缺該細節時：用 mem_route(code) 讀對應微檔；"
    "用 mem_search(query) 全文召回舊對話/冷知識（支援中文）。"
)

MEM_SEARCH_SCHEMA = {
    "name": "mem_search",
    "description": (
        "Full-text search mem4's recall index (past conversation turns and "
        "cold-tier microfiles) for knowledge that has left the always-loaded "
        "hot zone. Works for English and Chinese (CJK). Use when you need to "
        "recall something discussed or recorded earlier that is not in "
        "MEMORY.md. Returns ranked snippets with their source."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Max hits (default 5)."},
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class Mem4MemoryProvider(MemoryProvider):
    """Four-tier routed memory provider — ⑤-minimal chassis."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = dict(config) if config else None
        self._backend: Optional[StorageBackend] = None
        self._root: Optional[Path] = None
        self._session_id = ""
        self._platform = "cli"
        self._agent_context = "primary"
        self._active = False
        self._ran_migration = False
        self._state: Dict[str, Any] = {}
        self._dream: Optional[DreamProcessor] = None
        # ① FTS5 recall
        self._recall: Optional[RecallStore] = None
        self._prefetch_cap = DEFAULT_PREFETCH_CAP
        # Injectable history source for backfill: fetch(since_rowid, batch_size)
        # -> iterable of (rowid, ref, content, ts). None ⇒ no history backfill
        # (only microfiles are indexed). Real deployments wire a session-store
        # reader; tests inject a fake. See set_backfill_source().
        self._backfill_source: Optional[Callable[[int, int], Iterable[Tuple[int, str, str, float]]]] = None
        self._backfill_thread: Optional[threading.Thread] = None
        # ② Auditor + A/B arm + measurement baselines
        self._auditor: Optional[Auditor] = None
        self._arm = ARM_EXPERIMENT
        self._builtin_chars = 0   # resident built-in memory size (MEMORY.md+USER.md)
        self._legend_chars = len(ROUTING_LEGEND)

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "mem4"

    def _resolve_backend_kind(self) -> str:
        """Read ``memory.mem4.backend`` (default local-file). No network."""
        if self._config and self._config.get("backend"):
            return str(self._config["backend"])
        try:
            from hermes_cli.config import load_config

            config = load_config()
            memory = config.get("memory", {}) if isinstance(config, dict) else {}
            m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
            if isinstance(m4, dict) and m4.get("backend"):
                return str(m4["backend"])
        except Exception:
            pass
        return DEFAULT_BACKEND

    def is_available(self) -> bool:
        """Ready if the configured backend is one ⑤-minimal implements.

        No network calls (design spike §9.2). ⑤-minimal only ships the
        local-file backend; a config pointing at an unimplemented remote/local
        vault returns False so the agent degrades to pure built-in memory
        rather than half-loading a broken provider.
        """
        return self._resolve_backend_kind() == "local-file"

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")

        hermes_home = kwargs.get("hermes_home") or _default_hermes_home()
        self._root = Path(hermes_home) / "mem4"

        kind = self._resolve_backend_kind()
        self._backend = build_backend(kind, self._root)
        if self._backend is None:
            logger.warning("mem4: backend %r not implemented — provider inactive", kind)
            self._active = False
            return

        try:
            self._ran_migration = self._ensure_bootstrap()
            self._prefetch_cap = self._resolve_prefetch_cap()
            # ① Build the FTS5 recall store and attach it to the backend so
            # backend.search() is live. Index existing microfiles synchronously
            # (fast, needed for immediate recall); history backfill runs in the
            # background (resumable via the marker cursor).
            self._recall = RecallStore(self._root / "recall.db")
            if isinstance(self._backend, LocalFileBackend):
                self._backend.attach_recall(self._recall)
            self._index_microfiles()
            self._start_backfill()
            # ② Auditor + A/B arm + measurement baselines
            self._arm = self._resolve_arm()
            self._builtin_chars = self._read_builtin_memory_chars(hermes_home)
            self._legend_chars = len(ROUTING_LEGEND)
            self._auditor = Auditor(
                self._root / AUDIT_LOG_FILENAME,
                enabled=self._resolve_audit_enabled(),
                arm=self._arm, session_id=session_id,
            )
            self._dream = self._build_dream()
            self._active = True
            logger.info(
                "mem4 active (backend=%s, microfiles=%d, recall_docs=%d, trigram=%s, dream=%s)",
                kind, self._state.get("counts", {}).get("microfiles", 0),
                self._recall.count(), self._recall.trigram_available,
                self._dream.enabled if self._dream else False,
            )
        except Exception as e:
            logger.warning("mem4 initialize failed — provider inactive: %s", e)
            self._active = False
            return

        # ④ Dream — session-start staleness floor: consolidate if overdue (and
        # there is pending signal). Non-fatal; never blocks the turn.
        if self._dream:
            try:
                self._dream.maybe_consolidate("session_start")
            except Exception as e:
                logger.debug("mem4 dream (session_start) failed (non-fatal): %s", e)

    def shutdown(self) -> None:
        # Let a running backfill finish briefly, then close the recall DB.
        if self._backfill_thread and self._backfill_thread.is_alive():
            self._backfill_thread.join(timeout=2.0)
        if self._recall is not None:
            self._recall.close()

    def set_backfill_source(
        self, fetch: Callable[[int, int], Iterable[Tuple[int, str, str, float]]]
    ) -> None:
        """Inject a history source for backfill (real deployment / tests)."""
        self._backfill_source = fetch

    def _resolve_prefetch_cap(self) -> int:
        override = (self._config or {}).get("prefetch_cap") if self._config else None
        if override:
            try:
                return max(200, int(override))
            except (TypeError, ValueError):
                pass
        try:
            from hermes_cli.config import load_config

            config = load_config()
            memory = config.get("memory", {}) if isinstance(config, dict) else {}
            m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
            recall = m4.get("recall", {}) if isinstance(m4, dict) else {}
            if isinstance(recall, dict) and recall.get("prefetch_cap"):
                return max(200, int(recall["prefetch_cap"]))
        except Exception:
            pass
        return DEFAULT_PREFETCH_CAP

    def _resolve_arm(self) -> str:
        """Resolve the A/B arm (memory.mem4.arm). Default experiment."""
        val = None
        if self._config and self._config.get("arm"):
            val = str(self._config["arm"])
        else:
            try:
                from hermes_cli.config import load_config

                config = load_config()
                memory = config.get("memory", {}) if isinstance(config, dict) else {}
                m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
                if isinstance(m4, dict) and m4.get("arm"):
                    val = str(m4["arm"])
            except Exception:
                pass
        return ARM_BASELINE if (val or "").strip().lower() == ARM_BASELINE else ARM_EXPERIMENT

    def _resolve_audit_enabled(self) -> bool:
        """Resolve memory.mem4.audit.enabled. Default False (opt-in)."""
        override = (self._config or {}).get("audit") if self._config else None
        if isinstance(override, dict) and "enabled" in override:
            return _coerce_bool(override.get("enabled"), False)
        try:
            from hermes_cli.config import load_config

            config = load_config()
            memory = config.get("memory", {}) if isinstance(config, dict) else {}
            m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
            audit = m4.get("audit", {}) if isinstance(m4, dict) else {}
            if isinstance(audit, dict) and "enabled" in audit:
                return _coerce_bool(audit.get("enabled"), False)
        except Exception:
            pass
        return False

    def _is_baseline(self) -> bool:
        return self._arm == ARM_BASELINE

    @staticmethod
    def _read_builtin_memory_chars(hermes_home) -> int:
        """Total chars of the resident built-in memory (MEMORY.md + USER.md).

        This is what pure built-in injects on EVERY turn — the baseline side of
        the paired counterfactual (② layer 2) and the resident-cost metric
        (② layer 3). Read-only; never writes the built-in files.
        """
        total = 0
        mem_dir = Path(hermes_home) / "memories"
        for fname in ("MEMORY.md", "USER.md"):
            p = mem_dir / fname
            if p.is_file():
                try:
                    total += len(p.read_text(encoding="utf-8"))
                except OSError:
                    pass
        return total

    def _paired_tokens(self, recall_chars: int) -> Tuple[int, int]:
        """(baseline_inject_tokens, mem4_inject_tokens) for one query.

        Built-in injects its whole resident memory every turn; mem4 injects the
        small legend plus this query's recall. Paired per-query (② layer 2).
        """
        from .audit import estimate_tokens
        baseline = estimate_tokens(self._builtin_chars)
        mem4 = estimate_tokens(self._legend_chars + max(0, recall_chars))
        return baseline, mem4

    # -- idempotent init / migration (design spike §10) ----------------------

    def _state_path(self) -> Path:
        assert self._root is not None
        return self._root / STATE_FILENAME

    def _read_state(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: Dict[str, Any]) -> None:
        assert self._root is not None
        self._root.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure_bootstrap(self) -> bool:
        """Idempotent one-time init guarded by a version marker (§10.1).

        Returns True if a migration ran on this call, False if the marker was
        already current (the common warm-start path — no rewrite).
        """
        assert self._root is not None
        self._root.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        current_v = int(state.get("schema_version", 0)) if state else 0
        if state and state.get("migration_complete") and current_v == SCHEMA_VERSION:
            self._state = state
            return False
        self._state = self._migrate(current_v, SCHEMA_VERSION, prior=state)
        return True

    def _migrate(self, from_v: int, to_v: int, *, prior: Dict[str, Any]) -> Dict[str, Any]:
        """Stepwise migration entry. ⑤-minimal implements only v0 -> v1.

        v0 -> v1: create the mem4 dir, ADOPT any existing microfiles (never
        rebuild — §10.2), reserve the backfill cursor (filled by feature ①),
        and write the marker. Non-destructive: only mem4-owned paths are
        created; the built-in memory files are never read-for-write here.
        """
        state = dict(prior or {})
        if from_v < 1 <= to_v:
            adopted = self._backend.list_codes() if self._backend else []
            # §10.6 verification: microfile count == actual .md count. Here the
            # adopted list *is* the on-disk enumeration, so it holds by
            # construction; we still record it for the audit trail.
            state.update(
                {
                    "schema_version": 1,
                    "migrated_at": _now_iso(),
                    "backfill_cursor": None,      # feature ① (§10.4)
                    "backfill_complete": False,   # FTS5 backfill deferred to ①
                    "counts": {"microfiles": len(adopted)},
                    "migration_complete": True,
                }
            )
        self._write_state(state)
        return state

    # -- ④ Dream config / construction ---------------------------------------

    def _resolve_dream_config(self) -> Dict[str, Any]:
        """Resolve memory.mem4.dream.{enabled,threshold,staleness_days}."""
        enabled, threshold, staleness = (
            DEFAULT_ENABLED, DEFAULT_THRESHOLD, DEFAULT_STALENESS_DAYS,
        )
        # Constructor override (tests / programmatic use) wins.
        override = self._config.get("dream") if self._config else None
        if isinstance(override, dict):
            return {
                "enabled": _coerce_bool(override.get("enabled"), enabled),
                "threshold": int(override.get("threshold", threshold)),
                "staleness_days": int(override.get("staleness_days", staleness)),
            }
        try:
            from hermes_cli.config import load_config

            config = load_config()
            memory = config.get("memory", {}) if isinstance(config, dict) else {}
            m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
            dream = m4.get("dream", {}) if isinstance(m4, dict) else {}
            if isinstance(dream, dict):
                if "enabled" in dream:
                    enabled = _coerce_bool(dream.get("enabled"), enabled)
                if dream.get("threshold"):
                    threshold = int(dream["threshold"])
                if dream.get("staleness_days"):
                    staleness = int(dream["staleness_days"])
        except Exception:
            pass
        return {"enabled": enabled, "threshold": threshold, "staleness_days": staleness}

    def _build_dream(self) -> Optional[DreamProcessor]:
        assert self._root is not None
        cfg = self._resolve_dream_config()
        return DreamProcessor(
            self._root,
            enabled=cfg["enabled"],
            threshold=cfg["threshold"],
            staleness_days=cfg["staleness_days"],
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """④ Dream — session boundary: event threshold OR staleness floor.

        Fires the same consolidation gate as on_memory_write, but at a natural
        boundary. Idle sessions (no pending signal) skip. Non-fatal.
        """
        if not self._active or not self._dream:
            return
        try:
            self._dream.maybe_consolidate("session_end")
        except Exception as e:
            logger.debug("mem4 dream (session_end) failed (non-fatal): %s", e)

    # -- ① FTS5 recall: indexing / backfill ----------------------------------

    def _index_microfiles(self) -> int:
        """Index all L2/L3 microfiles into recall (also indexes mirror logs)."""
        if self._recall is None or self._backend is None:
            return 0
        n = 0
        for code in self._backend.list_codes():
            result = self._backend.read_microfile(code)
            if result and self._recall.index(
                ref=f"microfile:{code}", content=result.content,
                kind="microfile", ts=time.time(),
            ):
                n += 1
        # Mirror logs too, so recall covers observed built-in writes.
        mirror_dir = self._root / MIRROR_DIRNAME
        if mirror_dir.is_dir():
            for path in sorted(mirror_dir.glob("*.md")):
                if path.name.startswith("_"):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if self._recall.index(
                    ref=f"mirror:{path.stem}", content=text,
                    kind="microfile", ts=time.time(),
                ):
                    n += 1
        return n

    def _backfill_in_progress(self) -> bool:
        state = self._read_state()
        return bool(state.get("schema_version")) and not state.get("backfill_complete", False)

    def _start_backfill(self) -> None:
        """Kick off resumable history backfill in the background (non-blocking).

        No source ⇒ nothing to backfill from; mark complete (microfiles already
        indexed synchronously). With a source, a daemon thread processes batches
        and persists the cursor after each, so a restart resumes mid-stream
        (design spike §10.4). Runs off the hot path; never blocks a turn.
        """
        if self._recall is None:
            return
        if self._backfill_source is None:
            self._mark_backfill_complete()
            return
        self._backfill_thread = threading.Thread(
            target=self._backfill_worker, name="mem4-backfill", daemon=True,
        )
        self._backfill_thread.start()

    def _backfill_worker(self, max_batches: Optional[int] = None) -> int:
        """Process backfill batches until the source is exhausted. Resumable.

        Returns the number of docs indexed this run. ``max_batches`` bounds the
        run (used by tests to assert resumption); None runs to completion.
        """
        if self._recall is None or self._backfill_source is None:
            return 0
        total = 0
        batches = 0
        while max_batches is None or batches < max_batches:
            state = self._read_state()
            cursor = int(state.get("backfill_cursor") or 0)
            indexed, new_cursor, has_more = self._recall.backfill_batch(
                self._backfill_source, since_rowid=cursor,
                batch_size=_BACKFILL_BATCH_SIZE,
            )
            total += indexed
            batches += 1
            state["backfill_cursor"] = new_cursor
            if not has_more:
                state["backfill_complete"] = True
                self._write_state(state)
                break
            self._write_state(state)
        return total

    def _mark_backfill_complete(self) -> None:
        state = self._read_state()
        state["backfill_complete"] = True
        self._write_state(state)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # A/B baseline arm hides all tools so the model can't augment — the
        # hot-zone + tool surface matches pure built-in (design spike §7). The
        # recall store still exists for offline measurement via the harness.
        if self._is_baseline():
            return []
        # ① is live: mem_search is backed by the FTS5 recall store, advertised
        # alongside mem_route.
        return [MEM_ROUTE_SCHEMA, MEM_SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "mem_route":
            return self._tool_route(args or {})
        if tool_name == "mem_search":
            return self._tool_search(args or {})
        return tool_error(f"Unknown tool: {tool_name}")

    def _tool_route(self, args: dict) -> str:
        code = args.get("code", "")
        if not code:
            return tool_error("code is required")
        if not self._active or self._backend is None:
            return json.dumps(
                {"code": code, "found": False,
                 "result": "[mem4 inactive: built-in memory remains authoritative]"},
                ensure_ascii=False,
            )
        norm = normalize_code(code)
        if norm is None:
            return tool_error(f"invalid route code: {code!r}")
        result = self._backend.read_microfile(norm)
        if result is None:
            if self._auditor is not None:
                self._auditor.record_route(norm, hit=False, injected_chars=0)
            # Graceful miss: never an error — the built-in L0 is still there.
            return json.dumps(
                {"code": norm, "found": False,
                 "result": f"[mem4 miss: no microfile '{norm}' — built-in memory remains authoritative]"},
                ensure_ascii=False,
            )
        if self._auditor is not None:
            self._auditor.record_route(norm, hit=True, injected_chars=len(result.content))
        return json.dumps(
            {"code": norm, "found": True, "source": result.source,
             "stale": result.stale, "result": result.render()},
            ensure_ascii=False,
        )

    def _tool_search(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        if not self._active or self._recall is None:
            return json.dumps({"query": query, "hits": []}, ensure_ascii=False)
        try:
            limit = int(args.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))
        hits = self._recall.search(query, limit=limit, now=time.time())
        if self._auditor is not None:
            _rc = sum(len(h.snippet) for h in hits)
            _base_tok, _mem4_tok = self._paired_tokens(_rc)
            self._auditor.record_search(
                query, route=(hits[0].route if hits else ""),
                hit=bool(hits), injected_chars=_rc,
                baseline_inject_tokens=_base_tok, mem4_inject_tokens=_mem4_tok,
            )
        payload = {
            "query": query,
            "hits": [
                {"ref": h.ref, "kind": h.kind, "route": h.route, "snippet": h.snippet}
                for h in hits
            ],
        }
        # Honesty during backfill: recall may not yet cover old history.
        if self._backfill_in_progress():
            payload["note"] = "[backfill in progress: older history may not be indexed yet]"
        return json.dumps(payload, ensure_ascii=False)

    # -- ① recall: prefetch (turn-start, local-only, capped) -----------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall context for the upcoming turn — LOCAL FTS5 ONLY.

        Guardrail (Fable 5 review §2): prefetch runs synchronously on the turn's
        hot path, so it must NEVER make an MCP/network call — it only reads the
        local SQLite recall store and local files. The injected text is capped at
        ``self._prefetch_cap`` characters to bound token cost.
        """
        # Baseline arm injects nothing (design spike §7 A/B).
        if not self._active or self._recall is None or self._is_baseline():
            return ""
        if not query or len(query.strip()) < _MIN_INDEX_LEN:
            return ""
        try:
            hits = self._recall.search(query.strip(), limit=5, now=time.time())
        except Exception as e:
            logger.debug("mem4 prefetch failed (non-fatal): %s", e)
            return ""
        if not hits:
            if self._auditor is not None:
                _b, _m = self._paired_tokens(0)
                self._auditor.record_prefetch(
                    query.strip(), injected_chars=0,
                    baseline_inject_tokens=_b, mem4_inject_tokens=_m,
                )
            return ""
        lines = ["## mem4 recall"]
        for h in hits:
            lines.append(f"- ({h.kind}) {h.snippet}")
        text = "\n".join(lines)
        if len(text) > self._prefetch_cap:
            suffix = " …[truncated]"
            keep = max(0, self._prefetch_cap - len(suffix))
            text = text[:keep].rstrip() + suffix
        if self._auditor is not None:
            _b, _m = self._paired_tokens(len(text))
            self._auditor.record_prefetch(
                query.strip(), injected_chars=len(text),
                baseline_inject_tokens=_b, mem4_inject_tokens=_m,
            )
        return text

    # -- ① recall: sync_turn (filtered, deduped indexing) --------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Index a completed turn into the recall store, filtered.

        Filtering (Fable 5 review §5): skip trivially short turns and tool-output
        noise; dedup is handled by the recall store's content hash. Time-decay in
        ranking is applied at search time, not here.
        """
        if not self._active or self._recall is None:
            return
        user_content = (user_content or "").strip()
        if len(user_content) < _MIN_INDEX_LEN:
            return
        assistant_content = (assistant_content or "").strip()
        # Strip obvious tool-output scaffolding from the assistant side.
        if assistant_content.startswith("{") and '"tool' in assistant_content[:200]:
            assistant_content = ""
        combined = f"User: {user_content}"
        if assistant_content:
            combined += f"\nAssistant: {assistant_content[:2000]}"
        try:
            self._recall.index(
                ref=f"turn:{session_id}", content=combined,
                kind="turn", ts=time.time(),
            )
        except Exception as e:
            logger.debug("mem4 sync_turn index failed (non-fatal): %s", e)

    # -- ① recall: rebuild (fifth non-negotiable guarantee) ------------------

    def rebuild(self) -> Dict[str, int]:
        """Rebuild all derived state (recall FTS5) from source-of-truth files.

        Fable 5 review §5 / fifth guarantee: derived layers are always
        reconstructible. Clears the recall index and re-indexes from the
        mem4-owned microfiles + mirror logs, then re-runs history backfill.
        Returns counts for verification. Never reads-for-write the built-in
        memory files.
        """
        if not self._active or self._recall is None:
            return {"indexed": 0}
        self._recall.clear()
        # Reset the backfill cursor so history is re-indexed from the start.
        state = self._read_state()
        state["backfill_cursor"] = 0
        state["backfill_complete"] = False
        self._write_state(state)
        indexed = self._index_microfiles()
        if self._backfill_source is not None:
            self._backfill_worker()
        else:
            self._mark_backfill_complete()
        return {"indexed": indexed, "recall_docs": self._recall.count()}

    # -- system prompt / compression -----------------------------------------

    def system_prompt_block(self) -> str:
        if not self._active or self._is_baseline():
            return ""
        # Deliberately tiny: do NOT re-inject L0 (built-in already loaded
        # MEMORY.md). Just the routing legend so the model knows mem_route
        # exists and what the codes mean (design spike §2).
        return ROUTING_LEGEND

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        # Feed the routing legend (and available codes) into the compression
        # summary so the map survives context compression (design spike §2 —
        # the direct benefit of ⑤). Free text only.
        if not self._active or self._backend is None:
            return ""
        legend = (
            "mem4 路由碼：§sys 系統 · §fam 人物 · §vlt 知識 · §adr 決策 · "
            "§proto 協定；用 mem_route(code) 按需讀冷區微檔。"
        )
        codes = self._backend.list_codes()
        if codes:
            legend += " 現有微檔：" + ", ".join(f"§{c}" for c in codes) + "。"
        return legend

    # -- built-in memory mirror (design spike §3 / §8.3) ---------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Observe built-in memory writes and mirror them into mem4-owned files.

        HARD INVARIANT: this only ever writes under ``$HERMES_HOME/mem4/`` — it
        never writes, moves, or deletes the built-in MEMORY.md/USER.md, which
        remain the sole source of truth. Removing the provider drops the mirror
        with zero effect on built-in memory.
        """
        if not self._active or self._root is None:
            return
        if action not in {"add", "replace"} or not content or not content.strip():
            return
        mirror_target = target if target in {"memory", "user"} else "memory"
        try:
            self._mirror_write(mirror_target, action, content)
        except Exception as e:
            logger.debug("mem4 mirror write failed (non-fatal): %s", e)

        # ④ Dream — count this write as new material; a threshold crossing
        # triggers consolidation (of mem4-owned L2/L3 only). Non-fatal.
        if self._dream:
            try:
                self._dream.record_signal(1)
                self._dream.maybe_consolidate("threshold")
            except Exception as e:
                logger.debug("mem4 dream (on_memory_write) failed (non-fatal): %s", e)

    def _mirror_write(self, target: str, action: str, content: str) -> None:
        assert self._root is not None
        mirror_dir = self._root / MIRROR_DIRNAME
        mirror_dir.mkdir(parents=True, exist_ok=True)
        path = mirror_dir / f"{target}.md"
        # Traversal guard: the write target must stay inside the mem4 mirror
        # dir. ``target`` is constrained to {"memory","user"} by the caller,
        # but assert the resolved path anyway so this can never escape to the
        # built-in memories directory.
        if path.resolve().parent != mirror_dir.resolve():
            raise ValueError(f"mem4 mirror target escaped root: {path!r}")
        entry = f"\n<!-- {_now_iso()} {action} -->\n{content.strip()}\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register mem4 as a memory provider plugin."""
    ctx.register_memory_provider(Mem4MemoryProvider())
