<!-- Language: **English** · [繁體中文](./README_zh-TW.md) -->

# hermes-plugin-mem4

> **English** · [繁體中文 (Traditional Chinese)](./README_zh-TW.md)

**mem4** is a four-tier routed **memory provider** for [Hermes](https://github.com/webdevtodayjason/hermes-plugin-template) that *augments* the built-in agent memory instead of replacing it. It keeps the always-loaded hot zone small, pushes detail into on-demand cold-tier microfiles, and adds a Chinese-capable full-text recall index — all from local files, with **zero external dependencies** and **zero residue** when you turn it off.

It is packaged as a standalone, installable Hermes plugin so a `hermes update` of your core install never overwrites it (plugins live outside the core tree).

---

## What is mem4?

Hermes ships with built-in memory: a `memory` tool writes `MEMORY.md` / `USER.md` under `$HERMES_HOME/memories/`, and those files are injected into the system prompt at the start of every session. That "always-loaded hot zone" is authoritative, but it grows, and every turn pays for its full size in tokens.

mem4 wraps a **four-tier routed-memory design** (ADR-018) as a Hermes `MemoryProvider` in **coexist / augment mode**:

| Tier | Role | Where it lives |
|------|------|----------------|
| **L0 — hot** | Always-loaded, authoritative memory injected into the system prompt every session. | Built-in `MEMORY.md` / `USER.md` (owned by Hermes, **never touched** by mem4) |
| **L1 — cache** | Recent snapshots of what the cold tiers read, kept local for fast re-reads. | Local cache under `$HERMES_HOME` |
| **L2 — microfile** | On-demand cold knowledge, read by *route code* only when L0 lacks the detail. | `$HERMES_HOME/mem4/<code>.md` (human-readable, git/Obsidian-friendly) |
| **L3 — cold** | Batch-consolidated / archived material. | mem4-owned files, compacted by Dream (see below) |

The core idea: **L0 stays small** (just a routing legend beyond the built-in files), and the model pulls L2/L3 detail on demand via tools, or gets it pre-fetched from a local recall index. Built-in memory remains the single source of truth; mem4 is a purely *additive* read-enhancement + recall layer.

---

## Features

- 🟢 **Coexist / augment, never replace.** mem4 *only ever reads* the built-in `MEMORY.md` / `USER.md` and mirrors observed writes into its **own** files under `$HERMES_HOME/mem4/`. It never writes, moves, or deletes the built-in memory. This is a hard invariant, enforced in code (a path-traversal guard makes the mirror physically unable to escape the mem4 directory).
- 🟢 **Disable = clean degrade, zero residue.** Remove `memory.provider: mem4` from `config.yaml` and Hermes falls straight back to pure built-in memory. The `$HERMES_HOME/mem4/` directory becomes inert and can be deleted at any time. "Better" is therefore a *low-risk, additive* claim: keep it if measured benefit is real, roll back if not — the built-in path is always intact.
- 🟢 **Local-file backend, zero external dependencies.** The default (and, in this minimal chassis, only) backend uses the Python **stdlib + SQLite** — no pip packages, and **no MCP / network calls on the turn hot path**.
- 🟢 **Chinese-capable dual-table FTS5 recall.** mem4 owns its own SQLite FTS5 database and indexes both conversation turns and cold-tier microfiles. It uses a **dual-table** scheme so Chinese search actually works (see [Recall](#-recall-mem_search-prefetch) and [Limitations](#limitations)).
- 🟢 **Dream consolidation, event-triggered, no external cron.** Consolidation of mem4-owned cold tiers runs entirely inside the provider as pure code — triggered by a write-count threshold or a session-boundary staleness floor. No timer, no background daemon to babysit.
- 🟢 **Always rebuildable.** Every derived layer (the FTS5 index, mirror snapshots) can be rebuilt from the source-of-truth files with `hermes mem4 rebuild` — non-destructive, and it never reads the built-in files for writing.
- 🟢 **Measured, not asserted.** Ships with an auditor + A/B arm + a controlled-measurement harness so value is proven with data, with a hard SHIP/ROLLBACK gate. See [Measurement](#ab-controlled-measurement).

---

## Installation

mem4 is an **opt-in** plugin. Install it, enable it, then point Hermes' memory provider at it.

```bash
# 1. Install from GitHub
hermes plugins install sam7894604/hermes-plugin-mem4

# 2. Enable the plugin
hermes plugins enable mem4
```

Then turn it on in `config.yaml`:

```yaml
memory:
  provider: mem4
  mem4:
    backend: local-file      # default; the only backend in the minimal chassis
    dream:
      enabled: true          # event-triggered consolidation (default on)
      threshold: 25          # new memory writes before an event-triggered consolidation
      staleness_days: 7      # consolidate at a session boundary if overdue by this
    recall:
      prefetch_cap: 2000     # max characters prefetch may inject per turn (total)
      prefetch_limit: 5      # max recall hits considered per prefetch
      microfile_chars: 500   # per-microfile inject cap — matched cold-tier L2 microfiles
                             # are surfaced fuller (not a 240-char snippet) and ranked first,
                             # so a weak model gets cold facts WITHOUT calling mem_route
    audit:
      enabled: false         # opt-in: log one JSONL line per recall/route event
    arm: experiment          # experiment | baseline (see Measurement)
```

To **disable**, simply remove `memory.provider: mem4`. Nothing is left behind.

> **Requirements.** mem4 runs inside a Hermes host (it imports `agent.memory_provider` / `tools.registry` from the running process). The default backend needs only the Python stdlib — no pip installs. Python ≥ 3.10.

---

## Tools

When active (and not in the `baseline` A/B arm), mem4 advertises two tools and a tiny always-resident routing legend in the system prompt.

### `mem_route(code)`

Read an L2/L3 cold-tier microfile by **route code** when the always-loaded L0 lacks the detail.

- Codes: `sys` (system/environment), `fam` (people/family), `vlt` (vault/knowledge), `adr` (architecture decisions), `proto` (protocols/workflows). The leading `§` is optional.
- Reads `$HERMES_HOME/mem4/<code>.md`. Every result is prefixed with a **freshness tag** (`[fresh: local-file]`, `[STALE: …]`, or `[built-in only]`).
- A miss is **never an error** — it returns a marker (`built-in memory remains authoritative`) so the agent falls back to L0 gracefully.

### `mem_search(query, limit=5)`

Full-text recall over the mem4 index — past conversation turns **and** cold-tier microfiles — for knowledge that has left the always-loaded hot zone. Works for **English and Chinese**. Returns ranked snippets with their source, with a `[backfill in progress]` note while history indexing is still catching up.

---

## Recall (`mem_search`, `prefetch`)

mem4 owns its own SQLite FTS5 database (`$HERMES_HOME/mem4/recall.db`) indexing both turns and microfiles. It reuses the upstream dual-table pattern so CJK search works:

- **`docs_fts` (unicode61)** for English / BM25 relevance, plus **`docs_fts_trigram` (trigram)** for CJK.
- A `_contains_cjk()` check routes CJK queries to the trigram table. Any CJK token shorter than 3 characters (trigram's minimum) falls back to a per-token `LIKE` scan.
- If the local SQLite build lacks the trigram tokenizer, CJK queries degrade to `LIKE` — **never a hard failure**.
- Ranking layers a **time-decay weight** (30-day half-life) over relevance so recent material outranks equally-relevant older material.

Two surfaces beyond the `mem_search` tool:

- **`prefetch(query)`** — turn-start recall, **microfile-aware**. **Local I/O only** (SQLite + files, never MCP/network — it runs synchronously on the hot path) and capped at `recall.prefetch_cap` characters (default 2000). When the query matches a cold-tier **L2 microfile**, its curated content is injected **more fully** (up to `recall.microfile_chars`) and **ranked ahead** of noisier conversation-turn snippets — so a weak model gets the moved-out facts automatically, without having to call `mem_route`. (The toothless decisive experiment showed weak models rely on this auto-injection, not on proactively routing — design spike §11.)
- **`sync_turn(...)`** — indexes each completed turn, filtered (min length, tool-output stripped; the store dedups by content hash).

History backfill is **resumable** via a cursor persisted in `.mem4_state.json`, so a restart resumes mid-stream. A real deployment injects a session-history source; without one, only microfiles/mirror are indexed.

---

## Dream consolidation

Dream runs entirely inside the provider as **pure code — no external cron**. Triggers:

- **Event / threshold** — each built-in memory write is a signal; crossing `dream.threshold` new writes triggers a consolidation.
- **Staleness floor** — at a session boundary, if it has been longer than `dream.staleness_days` since the last consolidation *and* there is pending signal, consolidate.
- **Idle skip** — no pending signal ⇒ nothing to consolidate ⇒ skip. Pure idle needs no timer.

Consolidation compacts the mem4-owned mirror logs (dedup), **archiving the pre-compaction original to `_mirror/_archive/` before rewriting** so nothing is lost. It **only touches mem4-owned L2/L3** — never the built-in hot zone. Setting `dream.enabled: false` makes Dream a complete no-op.

> **Deployment note.** If you previously ran Dream as a standalone cron / `jobs.json` entry, retire that entry once mem4 ships Dream in-provider — otherwise you double-run against the same L2/L3.

---

## Relationship to built-in memory

This is the whole design, in one paragraph: **built-in memory is the single source of truth; mem4 is an additive read-enhancement + recall layer that only observes and reads it, and never writes it back.** Concretely:

- mem4 **reads** `MEMORY.md` / `USER.md` (e.g. to measure the resident hot-zone size) but **never writes** them.
- Built-in memory writes are **mirrored** into `$HERMES_HOME/mem4/_mirror/<target>.md` (mem4-owned) so recall can cover them — the originals are byte-for-byte untouched.
- When mem4 is unavailable, misconfigured (e.g. an unimplemented backend), or disabled, the provider goes inactive and Hermes uses pure built-in memory. **Degrade is the default failure mode**, not a broken half-load.

---

## `hermes mem4 rebuild`

```bash
hermes mem4 rebuild
```

Clears and rebuilds the recall index from the source-of-truth files (microfiles + mirror logs), then re-runs history backfill. Non-destructive; never reads the built-in memory files for writing. This is the "derived layers are always reconstructible" guarantee — the recall index and other derived state can always be rebuilt, so mirror drift is self-healing.

---

## `hermes mem4 refine` — §3 縮限式放寬 (hot-zone slimming)

```bash
hermes mem4 refine                 # dry-run: print the proposal only (default)
hermes mem4 refine --apply         # backup → extract microfiles → atomically rewrite MEMORY.md
hermes mem4 refine --restore [ts]  # restore MEMORY.md from a backup (latest if ts omitted)
```

The one **explicit, reversible** exception to the "never write the built-in `MEMORY.md`" rule. It parses the `§<code>` sections of `MEMORY.md` (heuristic-only, zero LLM, zero deps), extracts each into an `$HERMES_HOME/mem4/<code>.md` cold-tier microfile, and condenses `MEMORY.md` to a short header + un-attributed core + a routing index — measurably shrinking the always-loaded hot zone.

Safety guarantees: the daily `on_memory_write` path **never** writes back (unchanged); the auto paths (first bootstrap, Dream④) only refresh a proposal at `mem4/_refine_proposal.md` and **never** apply; `--apply` backs up `MEMORY.md` to `mem4/_refine_backups/MEMORY-<UTCts>.md` first, never silently overwrites an existing microfile (backs it up too), writes atomically (`.tmp` → `os.replace`; on failure the original is untouched), and is fully reversible with `--restore`. Falls back to markdown headings, then size-chunking, when no `§` markers exist. See [`docs/refine-section3-policy.md`](docs/refine-section3-policy.md).

---

## `hermes mem4 usermind` — §11 USER mind/preference summary (Honcho-lite)

```bash
hermes mem4 usermind                 # dry-run: heuristic proposal only (default)
hermes mem4 usermind --mode llm      # dry-run, but condense the candidates with the host's active model
hermes mem4 usermind --apply         # backup → write the summary into USER.md's managed block
hermes mem4 usermind --restore [ts]  # restore USER.md from a usermind backup
```

A distillation of the user's *explicit* preference statements (「偏好/不要/幫我/風格…」, `prefer/always/never/…`) from recent dialogue turns + observed built-in USER writes — the Honcho theory-of-mind idea, kept within mem4's zero-dependency principle (design spike §11).

Two modes (`--mode`, or `memory.mem4.user_summary.mode`; default `heuristic`):

- **`heuristic`** (default, zero-LLM, zero-dependency) — extract candidates and use them directly. The extractor pre-filters for signal: **User-side text only** (assistant speech dropped), question/offer lines excluded, deduped.
- **`llm`** — the same heuristic pre-filter, then **one bare completion** to keep only genuinely stable preferences. It reuses the host's own LLM facade (`agent.plugin_llm` / `ctx.llm` → the user's **active model + auth**) with **no tools/skills/mcp and no new key/service/DB** — mem4 never brings its own model. If the facade is unavailable, or the model returns nothing usable, it **degrades to heuristic** (never a hard failure).

Safety mirrors `refine`: the Dream④ auto path only refreshes a proposal at `mem4/_user_summary_proposal.md` and **never** writes `USER.md`; `--apply` (opt-in) backs up `USER.md` first, writes only a marker-delimited **managed block** (idempotent — re-apply replaces, never appends a second), atomically, and is reversible with `--restore`. Config: `memory.mem4.user_summary.enabled` (default true — proposal generation only), `memory.mem4.user_summary.mode` (`heuristic` | `llm`).

---

## A/B controlled measurement

> **Honesty first.** The numbers below come from **synthetic / fixture** data — they are a *mechanism proof*, not production results. Real hit rates require deploying to a real workload and collecting actual usage; the same harness then runs against real data. Numbers are labelled **示範 (demonstrative)** where they are not measured on your traffic.

Value is measured with data, not estimates. Three tools ship in the box:

1. **Auditor** (`memory.mem4.audit.enabled: true`) — one row per recall/route/prefetch event in a local SQLite store (`$HERMES_HOME/mem4/audit.db`, table `audit_events`): query, arm, route (`fts`/`trigram`/`like`), hit/hit_estimated, tool_called, injected chars/tokens, the paired baseline/mem4 inject tokens, and `paired_diff`. Query it with `hermes mem4 audit`, `Auditor.query(sql)`, or any SQLite client. A tool-call *miss* is **precise**; the L0-hit rate (turns using no tool) is **estimated** offline and labelled as such. *(Baserow 907 export is retired/off-by-default; a legacy `audit.jsonl` is imported once into `audit.db`.)*
2. **A/B arm** (`memory.mem4.arm: experiment | baseline`). In `baseline`, mem4 is loaded but **all agent-facing surfaces are off** (no tools, no system-prompt legend, no prefetch injection), so the hot-zone/tool surface matches pure built-in while the recall store stays measurable. Run the same workload under each arm and compare.
3. **Controlled harness** (`hermes mem4 eval`) — three layers so results aren't confounded by traffic randomness, reporting **distributions** (min/median/max), not single numbers:
   - **Deterministic offline replay** (primary, zero randomness) — the same fixed input set replayed against baseline vs mem4; per item: gold hit (**precise**), injected tokens (**precise**), route.
   - **Paired counterfactual** — per query, both what pure built-in *would* inject and what mem4 *did*, reported as a paired-difference distribution.
   - **Resident cost** — session-open injection size: baseline (whole `MEMORY.md`) vs mem4 (short legend).

### Fixture / demonstrative results

- **Resident hot-zone size** — the ADR-018 design measured a reduction of **2,175 → 665 characters, ≈ −69 %** on its fixture dataset. *(示範 / fixture measurement — your reduction depends on how large your `MEMORY.md` is; the mechanism is "inject a short legend instead of the whole hot zone.")*
- **Cold-knowledge recall** — the expected direction is **experiment > baseline** (baseline ≈ 0, since baseline can't recall what left the hot zone). No production hit-rate number is claimed here.
- **Per-query token delta** — reported as a paired-difference *distribution* by the harness, not a single figure.

### SHIP / ROLLBACK gate

The harness hard-wires a gate (`gate()` prints PASS/FAIL per criterion). **SHIP** only if **all** hold:

1. mem4 recalls cold knowledge the baseline can't (**gold Δ ≥ 30 %**), **and**
2. net per-query tokens shrank (tool-read + prefetch cost did **not** eat the hot-zone savings), **and**
3. the resident hot zone shrank.

Otherwise **ROLL BACK** (remove `memory.provider`). Latency/errors that hurt turn stability are also a rollback trigger. The governing rule: *all claims stand on measured data; nothing is claimed until it passes.*

---

## Limitations

- **Chinese two-character words are the weak spot.** SQLite FTS5's default `unicode61` tokenizer does no CJK segmentation, so the trigram table carries CJK search — but **trigram queries need ≥ 3 characters**. A two-character Chinese word (e.g. 「部署」) falls back to a `LIKE` scan, which is a substring match, not ranked relevance. Recall still works, but two-char queries are less precise than three-plus-char ones. (Real-corpus hit-rate for the LIKE fallback is a pending measurement.)
- **No trigram tokenizer → CJK degrades to `LIKE`.** If your SQLite build lacks the trigram tokenizer, CJK search still works via `LIKE`, just without trigram ranking.
- **History backfill needs a source.** Without an injected session-history source, only microfiles and mirror logs are indexed — not your full past conversation history.
- **Minimal chassis.** Only the `local-file` backend is implemented; remote-vault / local-vault topologies are reserved. Real-data measurement is operator-gated (deploy + collect usage).

---

## Development & tests

The plugin's `mem4` package is imported under its canonical top-level name `mem4`. The `mem4` source is unmodified from its origin; the standalone repo adds only packaging, docs, and a test harness.

```bash
# Run the full suite (each test file in its own isolated subprocess).
python run_tests.py

# Or a single file directly.
python -m pytest tests/test_mem4_recall.py -q
```

**Why the runner?** The tests exercise SQLite FTS5 virtual tables and a background backfill thread, whose native/global state doesn't reset cleanly between tests in one long-lived interpreter — running every file together in a single process can crash the interpreter. `run_tests.py` runs **one process per file** (mirroring the upstream Hermes suite) and exits non-zero if any file fails.

The test harness (`tests/conftest.py`) installs **lightweight stubs** for the two Hermes host modules (`agent.memory_provider`, `tools.registry`) *only if the real Hermes package isn't importable* — so the unit tests run in a plain checkout with no Hermes install, and double as a host-integration smoke check inside a real Hermes tree. No mem4 source is modified for testing.

Current status: **54 tests, all green** (per-file isolation).

---

## License

[MIT](./LICENSE) © 2026 sam7894604.

Design basis: ADR-018 four-tier routed-memory architecture and the *2026-07-04 "Wrapping four-tier memory as a Hermes Provider" design spike*. Plugin structure follows the [hermes-plugin-template](https://github.com/webdevtodayjason/hermes-plugin-template) conventions.
