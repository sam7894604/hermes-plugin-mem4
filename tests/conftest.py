"""Standalone test harness for hermes-plugin-mem4.

mem4 is a *host plugin*: at runtime it imports a few Hermes-internal modules
(``agent.memory_provider.MemoryProvider`` and ``tools.registry.tool_error``).
Those only exist inside a running Hermes install. So that mem4's own unit tests
can run in a plain checkout (CI, a laptop with no Hermes), this conftest:

  1. Puts the repo root on ``sys.path`` so ``import mem4`` resolves to ``./mem4``.
  2. Installs *lightweight stubs* for the two host modules — but ONLY if the real
     Hermes package is not importable. Inside a real Hermes tree the genuine
     modules win, so these tests double as a host-integration smoke check.
  3. Isolates ``HERMES_HOME`` to a per-test tempdir and pins TZ/locale/hashseed
     for determinism (the same hermetic invariants the upstream suite enforces).

No mem4 source is modified — the stubs live entirely in the test harness.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# 1. Make ``import mem4`` resolve to the package directory in this repo.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# 2. Provide host stubs unless the real Hermes modules are importable.
def _install_host_stubs() -> None:
    import types

    try:  # Real Hermes present? Use it and register nothing.
        import agent.memory_provider  # noqa: F401
        import tools.registry  # noqa: F401
        return
    except Exception:
        pass

    # -- agent.memory_provider.MemoryProvider ------------------------------
    if "agent.memory_provider" not in sys.modules:
        agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
        agent_pkg.__path__ = []  # mark as package
        sys.modules["agent"] = agent_pkg

        mp_mod = types.ModuleType("agent.memory_provider")

        class MemoryProvider:  # minimal base; mem4 overrides everything it uses
            """Stub of the Hermes MemoryProvider base class."""

            def __init__(self, *args, **kwargs) -> None:  # pragma: no cover
                pass

        mp_mod.MemoryProvider = MemoryProvider
        sys.modules["agent.memory_provider"] = mp_mod
        agent_pkg.memory_provider = mp_mod

    # -- tools.registry.tool_error -----------------------------------------
    if "tools.registry" not in sys.modules:
        tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")
        tools_pkg.__path__ = []
        sys.modules["tools"] = tools_pkg

        reg_mod = types.ModuleType("tools.registry")

        def tool_error(message: str) -> str:
            """Stub matching the shape mem4 relies on: a JSON error string."""
            return json.dumps({"error": str(message)}, ensure_ascii=False)

        reg_mod.tool_error = tool_error
        sys.modules["tools.registry"] = reg_mod
        tools_pkg.registry = reg_mod


_install_host_stubs()


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    """Per-test isolated HERMES_HOME + deterministic locale/timezone."""
    home = tmp_path / "hermes_home"
    for sub in ("memories", "sessions", "skills"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")
