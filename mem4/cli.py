"""CLI for the mem4 memory plugin: ``hermes mem4 rebuild``.

Exposes the "derived layers are always reconstructible" guarantee (Fable 5
review §5) as a command: rebuild mem4's FTS5 recall index from the
source-of-truth files (microfiles + mirror logs), never touching the built-in
MEMORY.md/USER.md.
"""

from __future__ import annotations


def _ensure_importable() -> None:
    """Make ``import mem4.*`` work whether mem4 is bundled or a user plugin.

    Bundled it imports as ``plugins.memory.mem4``; installed under
    ``$HERMES_HOME/plugins/`` it loads via a synthetic namespace, so the
    absolute path ``plugins.memory.mem4`` is NOT importable. Adding the
    directory that contains the ``mem4`` package to sys.path lets the harness
    and provider be imported as top-level ``mem4`` in both layouts.
    """
    import sys
    from pathlib import Path

    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def cmd_rebuild(args) -> None:
    from hermes_constants import get_hermes_home
    _ensure_importable()
    from mem4 import Mem4MemoryProvider

    provider = Mem4MemoryProvider()
    provider.initialize("cli-rebuild", hermes_home=str(get_hermes_home()), platform="cli")
    if not provider._active:
        print("  mem4 is not active (check memory.mem4.backend). Nothing to rebuild.\n")
        return
    result = provider.rebuild()
    provider.shutdown()
    print("\nmem4 recall rebuild\n" + "─" * 32)
    print(f"  indexed (microfiles/mirror): {result.get('indexed', 0)}")
    print(f"  total recall docs:           {result.get('recall_docs', 0)}\n")


def cmd_eval(args) -> None:
    _ensure_importable()
    from mem4.eval.harness import run_all, format_full_report

    print(format_full_report(run_all()))


def register_cli(subparser) -> None:
    """Add mem4 subcommands to the ``hermes mem4`` parser."""
    sub = subparser.add_subparsers(dest="mem4_cmd")
    rebuild_p = sub.add_parser(
        "rebuild",
        help="Rebuild mem4's FTS5 recall index from source files (non-destructive).",
    )
    rebuild_p.set_defaults(func=cmd_rebuild)
    eval_p = sub.add_parser(
        "eval",
        help="Run the recall A/B harness on the synthetic QA fixture (② measurement).",
    )
    eval_p.set_defaults(func=cmd_eval)


def mem4_command(args) -> None:
    """Default handler when ``hermes mem4`` is run with no subcommand."""
    if getattr(args, "mem4_cmd", None) is None:
        print("\nmem4 — four-tier routed memory provider\n")
        print("  hermes mem4 rebuild   Rebuild the FTS5 recall index from source files")
        print("  hermes mem4 eval      Run the recall A/B harness (synthetic fixture)\n")
