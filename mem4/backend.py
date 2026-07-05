"""Storage backends for the mem4 four-tier memory provider.

mem4 needs exactly two runtime data operations (see design spike §9.5):

  1. Read an L2/L3 *microfile* by route code  (§sys -> ``sys.md``) — a plain
     key -> file lookup.
  2. Full-text *recall* over conversation history — deferred to feature ①
     (SQLite FTS5); the interface method exists here as a stub so the chassis
     is shaped for it now.

Both operations are abstracted behind :class:`StorageBackend` so the deployment
topology is switchable (design spike §9.3):

  * ``local-file`` — read microfiles straight from ``$HERMES_HOME/mem4/``
    (★ default; zero remote dependency, matches the "four tiers only need file
    reads + FTS5" reality).
  * ``remote-vault`` / ``local-vault`` — reserved for later; not implemented in
    the ⑤-minimal chassis.

Every read carries a **freshness tag** (design spike §9.2) so the agent never
mistakes a stale cached snapshot for a live read.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# A route code is a short key: letters/digits, plus '_' and '-'. The leading
# section sign (§) is optional and stripped. This pattern is also the path
# traversal guard — no '/', '\\', '.', or '..' can ever reach the filesystem.
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_code(code: str) -> Optional[str]:
    """Normalize and validate a route code, or return None if invalid.

    Strips an optional leading ``§`` and surrounding whitespace, lowercases,
    then validates against :data:`_CODE_RE`. Returning None for anything with
    a path separator or ``..`` is the traversal guard for file-backed stores.
    """
    if not code:
        return None
    cleaned = code.strip()
    if cleaned.startswith("§"):
        cleaned = cleaned[1:]
    cleaned = cleaned.strip().lower()
    if not _CODE_RE.match(cleaned):
        return None
    return cleaned


@dataclass
class MicrofileResult:
    """The content of one microfile plus provenance/freshness metadata."""

    code: str
    content: str
    source: str            # "local-file" | "cache" | "vault" | "builtin"
    stale: bool = False
    cached_at: Optional[str] = None

    def freshness_tag(self) -> str:
        """Human/agent-readable freshness marker (design spike §9.2)."""
        if self.source == "vault":
            return "[fresh: vault]"
        if self.source == "local-file":
            return "[fresh: local-file]"
        if self.stale:
            ts = self.cached_at or "unknown"
            return f"[STALE: cached {ts}, live source unreachable]"
        if self.source == "builtin":
            return "[built-in only]"
        return f"[{self.source}]"

    def render(self) -> str:
        """Content prefixed with its freshness tag, for tool output."""
        return f"{self.freshness_tag()}\n{self.content}"


@dataclass
class SearchHit:
    """One recall hit from the FTS5 recall store (feature ①)."""

    ref: str
    snippet: str
    score: float = 0.0
    kind: str = ""          # "turn" | "microfile"
    ts: float = 0.0         # unix seconds
    route: str = ""         # which path answered: "fts" | "trigram" | "like"


class StorageBackend(ABC):
    """Abstract mem4 storage backend.

    Three operations, matching design spike §9.3's interface:
    ``read_microfile`` / ``write_microfile`` / ``search``.
    """

    #: Backend identifier, used in freshness tags and logging.
    name: str = "abstract"

    @abstractmethod
    def read_microfile(self, code: str) -> Optional[MicrofileResult]:
        """Read the microfile for ``code``, or None on miss / invalid code."""

    @abstractmethod
    def write_microfile(self, code: str, content: str) -> None:
        """Write/replace the microfile for ``code`` (mem4-owned storage only)."""

    @abstractmethod
    def search(self, query: str, *, limit: int = 5) -> List[SearchHit]:
        """Full-text recall. Stubbed in ⑤-minimal; FTS5 lands in feature ①."""

    def is_ready(self) -> bool:
        """Whether the backend can serve reads. Never blocks on a remote."""
        return True

    def list_codes(self) -> List[str]:
        """Route codes with an existing microfile (for counts / legend)."""
        return []


class LocalFileBackend(StorageBackend):
    """Default backend: microfiles are plain ``.md`` files under a mem4 root.

    Storage layout (all under ``root`` = ``$HERMES_HOME/mem4/``):
      * ``<code>.md``            — L2/L3 microfiles (human-readable, git/Obsidian
                                   friendly; the hard requirement from §9.5).
      * ``_mirror/<target>.md``  — append-only mirror of built-in memory writes
                                   (mem4-owned; the built-in files are NEVER
                                   touched — design spike §3 / §8.3).
      * ``.mem4_state.json``     — idempotent-init version marker (§10.1).

    Reserved names (``_``-prefixed dirs, the state marker) are excluded from
    microfile enumeration so mirrors/markers never masquerade as microfiles.
    """

    name = "local-file"

    def __init__(self, root: Path):
        self.root = Path(root)
        # Attached by the provider once feature ①'s recall store is built. When
        # None, search() returns [] (⑤-minimal behaviour).
        self.recall = None

    def attach_recall(self, store) -> None:
        self.recall = store

    # -- microfiles ----------------------------------------------------------

    def _microfile_path(self, code: str) -> Optional[Path]:
        norm = normalize_code(code)
        if norm is None:
            return None
        return self.root / f"{norm}.md"

    def read_microfile(self, code: str) -> Optional[MicrofileResult]:
        path = self._microfile_path(code)
        if path is None or not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return MicrofileResult(
            code=normalize_code(code) or code,
            content=content,
            source="local-file",
        )

    def write_microfile(self, code: str, content: str) -> None:
        path = self._microfile_path(code)
        if path is None:
            raise ValueError(f"invalid mem4 route code: {code!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def list_codes(self) -> List[str]:
        if not self.root.is_dir():
            return []
        codes = []
        for p in sorted(self.root.glob("*.md")):
            if p.name.startswith("_"):
                continue
            codes.append(p.stem)
        return codes

    # -- recall (deferred to feature ①) --------------------------------------

    def search(self, query: str, *, limit: int = 5) -> List[SearchHit]:
        # Feature ①: delegate to the attached FTS5 recall store (mem4's own
        # dual-table DB — design spike §10.8 decision B). Without a store
        # attached (⑤-minimal), return [] rather than a fake claim of recall.
        if self.recall is None:
            return []
        import time
        return self.recall.search(query, limit=limit, now=time.time())

    def is_ready(self) -> bool:
        # Local file I/O has no remote dependency; ready as long as the root is
        # creatable. We don't create it here (is_available must not have side
        # effects); initialize() does the mkdir.
        return True


#: Mirror subdirectory name under the mem4 root.
MIRROR_DIRNAME = "_mirror"

#: Version-marker filename under the mem4 root (design spike §10.1).
STATE_FILENAME = ".mem4_state.json"

#: Known backend identifiers. Only ``local-file`` is implemented in ⑤-minimal.
KNOWN_BACKENDS = ("local-file", "remote-vault", "local-vault")


def build_backend(kind: str, root: Path) -> Optional[StorageBackend]:
    """Construct a backend by ``mem4.backend`` config value.

    Returns None for a recognized-but-unimplemented backend (remote/local
    vault) so the caller degrades gracefully rather than crashing.
    """
    if kind == "local-file":
        return LocalFileBackend(root)
    # remote-vault / local-vault are reserved topologies (§9.3 a/c) — not in
    # the ⑤-minimal chassis. Unknown or unimplemented -> no backend.
    return None
