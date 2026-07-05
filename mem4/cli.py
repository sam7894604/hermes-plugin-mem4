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


def cmd_audit(args) -> None:
    """Query the local SQLite audit store (② real-traffic measurement).

    Reads ``$HERMES_HOME/mem4/audit.db`` and prints the per-event rollup plus
    the paired counterfactual (Layer 2). This is the simple query interface over
    the audit store; for ad-hoc slicing use ``Auditor.query(sql)`` in Python.
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home
    _ensure_importable()
    from mem4.audit import Auditor, AUDIT_DB_FILENAME
    from mem4.eval.harness import paired_counterfactual

    db = Path(get_hermes_home()) / "mem4" / AUDIT_DB_FILENAME
    events = Auditor(db).read_events()
    print("\nmem4 audit  (" + str(db) + ")\n" + "─" * 40)
    if not events:
        print("  no events yet (enable memory.mem4.audit.enabled: true)\n")
        return
    s = Auditor.summarize(events)
    print(f"  events:                {s['n_events']}  "
          f"(search={s['n_search']} route={s['n_route']} prefetch={s['n_prefetch']})")
    print(f"  model-initiated tools: {s['n_tool_calls']}  "
          f"(the model-decision path; prefetch is automatic)")
    print(f"  search / route hit:    {s['search_hit_rate']:.0%} / {s['route_hit_rate']:.0%}")
    print(f"  prefetch trigger rate: {s['prefetch_trigger_rate']:.0%}")
    print(f"  route distribution:    {s['route_distribution']}")
    print(f"  avg injected chars:    {s['avg_injected_chars']}")
    print(f"  median paired diff:    {s['median_paired_diff_tokens']} tokens "
          f"(baseline−mem4; counterfactual — see note)")
    paired = paired_counterfactual(events)
    if paired.get("n"):
        pd = paired["paired_diff_tokens"]
        print(f"  paired diff min/med/max: {pd['min']:.0f}/{pd['median']:.0f}/{pd['max']:.0f}  "
              f"(mem4 cheaper {paired['mem4_cheaper_fraction']:.0%} of queries)")
    print()


def cmd_refine(args) -> None:
    """§3 縮限式放寬 — 精煉 MEMORY.md 熱區。

    預設 dry-run 只出提案；``--apply`` 需顯式且會先備份、可 ``--restore`` 還原；
    自動路徑（bootstrap / Dream④）永不走這條 apply。
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home
    _ensure_importable()
    from mem4.refine import RefinePlanner
    from mem4.audit import Auditor, AUDIT_DB_FILENAME

    home = get_hermes_home()
    auditor = Auditor(
        Path(home) / "mem4" / AUDIT_DB_FILENAME,
        enabled=True, arm="experiment", session_id="cli-refine",
    )
    planner = RefinePlanner(home, auditor=auditor)

    if getattr(args, "restore", False):
        result = planner.restore(getattr(args, "ts", None))
        print("\nmem4 refine --restore\n" + "─" * 32)
        if result.get("restored"):
            print(f"  已還原 MEMORY.md ← {result['from']}\n")
        else:
            print(f"  未還原：{result.get('reason')}\n")
            backups = planner.list_backups()
            if backups:
                print("  可用備份：")
                for p in backups:
                    print(f"    {p.name}")
                print()
        return

    plan = planner.plan()
    print(planner.render_plan(plan))

    if getattr(args, "apply", False):
        result = planner.apply(plan)
        print("mem4 refine --apply\n" + "─" * 32)
        if result.get("applied"):
            print(f"  已改寫 MEMORY.md（{result['before_bytes']} → "
                  f"{result['after_bytes']} bytes）")
            print(f"  微檔：{result['microfiles']}  "
                  f"（覆寫既有並備份：{result['overwritten_microfiles']}）")
            print(f"  備份：{result['backup']}")
            print(f"  還原：hermes mem4 refine --restore {result['stamp']}\n")
        else:
            print(f"  未套用：{result.get('reason')}\n")


def cmd_usermind(args) -> None:
    """§11 — Dream USER 心智/偏好摘要(啟發式、零 LLM)。

    預設 dry-run 只出提案;``--apply`` 才寫進 USER.md 的受管區塊(先備份、原子、
    可 ``--restore`` 還原);自動路徑(Dream④)永不走這條 apply。
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home
    _ensure_importable()
    from mem4.usermind import UserMindSummarizer
    from mem4.recall import RecallStore

    home = get_hermes_home()

    # --restore reads backups only; it needs no recall store.
    if getattr(args, "restore", False):
        result = UserMindSummarizer(home).restore(getattr(args, "ts", None))
        print("\nmem4 usermind --restore\n" + "─" * 32)
        print(f"  已還原 USER.md ← {result['from']}\n" if result.get("restored")
              else f"  未還原：{result.get('reason')}\n")
        return

    # Wire the recall store so the summarizer reads dialogue turns — the PRIMARY
    # source (the USER-write mirror alone is usually empty). Without this the CLI
    # would extract nothing even when the recall DB is full of turns.
    recall = None
    recall_db = Path(home) / "mem4" / "recall.db"
    if recall_db.is_file():
        recall = RecallStore(recall_db)
    try:
        smz = UserMindSummarizer(home, recall=recall)
        items, summary = smz.plan()
        print("\nmem4 usermind — USER 心智/偏好摘要 (dry-run)\n" + "─" * 44)
        if not summary:
            print("  近期對話/鏡射中未抽到顯式偏好陳述 —— 無提案。\n")
            return
        print(f"  抽出偏好項：{len(items)}")
        print("  ┈┈┈ 提案內容 ┈┈┈")
        for line in summary.splitlines():
            print("  " + line)
        print("  ┈┈┈┈┈┈┈┈┈┈┈┈┈┈")

        if getattr(args, "apply", False):
            result = smz.apply()
            print("\nmem4 usermind --apply\n" + "─" * 32)
            if result.get("applied"):
                print(f"  已寫入 USER.md 受管區塊（{result['items']} 項）")
                print(f"  備份：{result['backup']}")
                print(f"  還原：hermes mem4 usermind --restore {result['stamp']}\n")
            else:
                print(f"  未套用：{result.get('reason')}\n")
        else:
            print("  這是提案（dry-run），未改動 USER.md。--apply 才寫入（會先備份、可 --restore）。\n")
    finally:
        if recall is not None:
            recall.close()


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
    audit_p = sub.add_parser(
        "audit",
        help="Query the local SQLite audit store (real-traffic measurement).",
    )
    audit_p.set_defaults(func=cmd_audit)
    refine_p = sub.add_parser(
        "refine",
        help="Refine (縮限式放寬) MEMORY.md: propose/apply hot-zone slimming.",
    )
    refine_p.add_argument("--dry-run", action="store_true",
                          help="Preview the proposal only (default).")
    refine_p.add_argument("--apply", action="store_true",
                          help="Apply: backup, extract microfiles, rewrite MEMORY.md.")
    refine_p.add_argument("--restore", action="store_true",
                          help="Restore MEMORY.md from a refine backup.")
    refine_p.add_argument("ts", nargs="?", default=None,
                          help="Optional backup timestamp for --restore (default: latest).")
    refine_p.set_defaults(func=cmd_refine)
    usermind_p = sub.add_parser(
        "usermind",
        help="Heuristic USER mind/preference summary from Dream (§11); proposal by default.",
    )
    usermind_p.add_argument("--dry-run", action="store_true",
                            help="Preview the summary proposal only (default).")
    usermind_p.add_argument("--apply", action="store_true",
                            help="Write the summary into USER.md's managed block (backup first).")
    usermind_p.add_argument("--restore", action="store_true",
                            help="Restore USER.md from a usermind backup.")
    usermind_p.add_argument("ts", nargs="?", default=None,
                            help="Optional backup timestamp for --restore (default: latest).")
    usermind_p.set_defaults(func=cmd_usermind)


def mem4_command(args) -> None:
    """Default handler when ``hermes mem4`` is run with no subcommand."""
    if getattr(args, "mem4_cmd", None) is None:
        print("\nmem4 — four-tier routed memory provider\n")
        print("  hermes mem4 rebuild   Rebuild the FTS5 recall index from source files")
        print("  hermes mem4 eval      Run the recall A/B harness (synthetic fixture)")
        print("  hermes mem4 audit     Query the local SQLite audit store (real traffic)")
        print("  hermes mem4 refine    Propose/apply MEMORY.md hot-zone slimming (§3)")
        print("  hermes mem4 usermind  Propose/apply a USER mind/preference summary (§11)\n")
