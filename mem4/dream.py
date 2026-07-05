"""Dream consolidation for mem4 — fully in-provider, zero external dependency.

Design decision (i′), 2026-07-04: mem4's Dream is self-contained pure code. It
does NOT assume or coordinate with any external cron. (The toothless "Dream
cron" was a user-set jobs.json entry, not a Hermes default — in the upstream
context there is no cron to coordinate with. When mem4 ships with Dream, that
user cron becomes redundant and should be retired; see README deployment note.)

Triggers (all in-provider):
  * Event / threshold — on_memory_write accumulates a signal count; crossing the
    threshold triggers consolidation.
  * Staleness floor — at session boundaries (start via initialize, end via
    on_session_end) consolidate if it has been longer than ``staleness_days``
    since the last consolidation AND there is pending signal.
  * Idle skip — no pending signal ⇒ nothing to consolidate ⇒ skip. Pure idle
    (no sessions at all) needs no timer; consolidation only matters when there
    is new material. (Known non-goal: idle-time periodic consolidation. A future
    optional external scheduler hook could add it, but it is not needed and not
    the default.)

Invariants:
  * v1 consolidates ONLY mem4-owned L2/L3 (the mirror logs); it NEVER writes the
    built-in MEMORY.md/USER.md hot zone (design spike §3 / decision 4).
  * Consolidation is non-destructive: the pre-compaction content is archived to
    an L3 cold file before the mirror is rewritten (Fable 5 review: "整併是
    資訊丟失重災區" — archive originals before compacting).
  * A marker (``.dream_state.json``) + a lock (``.dream.lock``) make the event
    and staleness paths mutually exclusive so one startup never double-runs.
  * Disabled (feature-flag off) ⇒ every method is an immediate no-op. The flag
    also makes Dream cedeable if Hermes ships an official background-memory
    agent (upstream issue #553).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .backend import MIRROR_DIRNAME

logger = logging.getLogger(__name__)

#: Marker filename under the mem4 root.
DREAM_STATE_FILENAME = ".dream_state.json"
#: Lock filename under the mem4 root (best-effort cross-process exclusion).
DREAM_LOCK_FILENAME = ".dream.lock"
#: Archive subdir (under _mirror) for pre-compaction originals (L3 cold).
DREAM_ARCHIVE_DIRNAME = "_archive"
#: A lock older than this is treated as stale (crashed run) and stolen.
LOCK_STALE_SECONDS = 3600

#: Defaults (overridable via memory.mem4.dream.* in config.yaml).
DEFAULT_ENABLED = True
DEFAULT_THRESHOLD = 25          # new mirror entries before an event trigger
DEFAULT_STALENESS_DAYS = 7      # floor between consolidations

# Parse the mirror entry format written by the provider:
#   \n<!-- <ts> <action> -->\n<content>\n
_BLOCK_RE = re.compile(
    r"<!--\s*(?P<hdr>.*?)\s*-->\n(?P<body>.*?)(?=\n<!--|\Z)",
    re.DOTALL,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DreamState:
    last_consolidation_at: Optional[str] = None
    consolidation_count: int = 0
    signals_since_last: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "DreamState":
        return cls(
            last_consolidation_at=d.get("last_consolidation_at"),
            consolidation_count=int(d.get("consolidation_count", 0)),
            signals_since_last=int(d.get("signals_since_last", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "last_consolidation_at": self.last_consolidation_at,
            "consolidation_count": self.consolidation_count,
            "signals_since_last": self.signals_since_last,
        }


@dataclass
class DreamResult:
    ran: bool
    reason: str
    skipped: str = ""          # why it skipped, when ran is False
    targets: Optional[dict] = None   # per-target (before, after) entry counts


class DreamProcessor:
    """In-provider Dream consolidation over mem4-owned L2/L3 files."""

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = DEFAULT_ENABLED,
        threshold: int = DEFAULT_THRESHOLD,
        staleness_days: int = DEFAULT_STALENESS_DAYS,
    ):
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.threshold = max(1, int(threshold))
        self.staleness = timedelta(days=max(0, int(staleness_days)))
        self._state_path = self.root / DREAM_STATE_FILENAME
        self._lock_path = self.root / DREAM_LOCK_FILENAME
        self._mirror_dir = self.root / MIRROR_DIRNAME

    # -- state ---------------------------------------------------------------

    def load(self) -> DreamState:
        if not self._state_path.is_file():
            return DreamState()
        try:
            return DreamState.from_dict(json.loads(self._state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return DreamState()

    def save(self, state: DreamState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record_signal(self, n: int = 1) -> None:
        """Accumulate new-material signal (called on each built-in memory write)."""
        if not self.enabled or n <= 0:
            return
        state = self.load()
        state.signals_since_last += n
        self.save(state)

    # -- trigger decision ----------------------------------------------------

    def _should_run(self, state: DreamState, now: datetime) -> Tuple[bool, str]:
        # Idle skip: nothing new to consolidate.
        if state.signals_since_last <= 0:
            return False, "idle: no pending signal"
        # Event / threshold.
        if state.signals_since_last >= self.threshold:
            return True, "threshold"
        # Staleness floor (needs a baseline — the first consolidation is
        # threshold-driven; staleness governs cadence thereafter).
        if state.last_consolidation_at:
            try:
                last = datetime.fromisoformat(state.last_consolidation_at)
            except ValueError:
                last = None
            if last is not None and (now - last) >= self.staleness:
                return True, "staleness"
        return False, "below threshold, within staleness window"

    def maybe_consolidate(self, reason: str, *, now: Optional[datetime] = None) -> DreamResult:
        """Consolidate if a trigger condition holds and the lock is free."""
        if not self.enabled:
            return DreamResult(ran=False, reason=reason, skipped="disabled")
        now = now or _now()
        state = self.load()
        should, why = self._should_run(state, now)
        if not should:
            return DreamResult(ran=False, reason=reason, skipped=why)

        # Mutual exclusion: only acquire the lock once we actually intend to run,
        # so the event and staleness paths can't double-run in one startup.
        if not self._acquire_lock(now):
            return DreamResult(ran=False, reason=reason, skipped="locked")
        try:
            targets = self._consolidate(now)
            state.last_consolidation_at = now.isoformat()
            state.consolidation_count += 1
            state.signals_since_last = 0
            self.save(state)
            logger.info("mem4 Dream consolidated (reason=%s, why=%s, targets=%s)",
                        reason, why, targets)
            return DreamResult(ran=True, reason=why, targets=targets)
        finally:
            self._release_lock()

    # -- lock ----------------------------------------------------------------

    def _acquire_lock(self, now: datetime) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            # Steal a stale lock left by a crashed run.
            try:
                age = now.timestamp() - self._lock_path.stat().st_mtime
            except OSError:
                return False
            if age > LOCK_STALE_SECONDS:
                try:
                    self._lock_path.unlink()
                    fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    return True
                except OSError:
                    return False
            return False
        except OSError:
            return False

    def _release_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            pass

    # -- consolidation (mem4-owned L2/L3 only) -------------------------------

    def _consolidate(self, now: datetime) -> dict:
        """Compact the mirror logs: dedup entries, archiving originals first.

        HARD INVARIANT: only touches files under ``root/_mirror/``. The built-in
        MEMORY.md/USER.md are never read-for-write here.
        """
        results: dict = {}
        if not self._mirror_dir.is_dir():
            return results
        for path in sorted(self._mirror_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            before, after = self._compact_mirror_file(path, now)
            results[path.stem] = {"before": before, "after": after}
        return results

    def _compact_mirror_file(self, path: Path, now: datetime) -> Tuple[int, int]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return (0, 0)
        blocks = [(m.group("hdr"), m.group("body").strip()) for m in _BLOCK_RE.finditer(raw)]
        if not blocks:
            return (0, 0)
        # Dedup by body content, last occurrence wins, order preserved.
        deduped: "dict[str, Tuple[str, str]]" = {}
        for hdr, body in blocks:
            if body:
                deduped[body] = (hdr, body)
        if len(deduped) == len(blocks):
            return (len(blocks), len(blocks))  # already minimal — no rewrite

        # Archive the pre-compaction original to L3 cold before rewriting, so no
        # information is ever lost to consolidation.
        archive_dir = self._mirror_dir / DREAM_ARCHIVE_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        (archive_dir / f"{path.stem}-{stamp}.md").write_text(raw, encoding="utf-8")

        rendered = "".join(f"\n<!-- {hdr} -->\n{body}\n" for hdr, body in deduped.values())
        path.write_text(rendered, encoding="utf-8")
        return (len(blocks), len(deduped))
