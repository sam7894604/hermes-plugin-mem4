"""hermes-plugin-mem4 — standalone Hermes plugin entry point.

Hermes discovers a plugin by importing its package and calling ``register(ctx)``.
The mem4 implementation lives in the nested ``mem4`` package (``./mem4/``) so it
stays importable under its canonical top-level name ``mem4`` — the CLI
(``hermes mem4 rebuild``/``eval``) and the tests both import it as ``mem4``.

This top-level shim re-exports mem4's entry point so Hermes can find it. It uses
an absolute ``import mem4`` (after making this directory importable) rather than
a relative ``from .mem4`` so it works whether Hermes loads it as a package or a
loose module.
"""

from __future__ import annotations

import os
import sys

# Ensure ``import mem4`` resolves to the nested package in this directory,
# regardless of how the host loaded this shim.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mem4 import Mem4MemoryProvider, register  # noqa: E402,F401

__all__ = ["Mem4MemoryProvider", "register"]
