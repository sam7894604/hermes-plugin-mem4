"""Tests for the mem4 provider ⑤-minimal chassis.

Covers: registration, availability gating, mem_route reads (freshness tags +
graceful miss + traversal guard), the built-in-memory-untouched mirror
invariant, degrade-when-inactive, and idempotent init with a version marker.
"""

import json

from mem4 import Mem4MemoryProvider, register
from mem4.backend import normalize_code


# -- registration -----------------------------------------------------------

def test_register_captures_provider():
    captured = {}

    class Ctx:
        def register_memory_provider(self, provider):
            captured["provider"] = provider

    register(Ctx())
    assert isinstance(captured["provider"], Mem4MemoryProvider)
    assert captured["provider"].name == "mem4"


def test_tool_schemas_route_and_search():
    schemas = Mem4MemoryProvider({"backend": "local-file"}).get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert names == ["mem_route", "mem_search"]  # mem_search live since feature ①


# -- availability gating -----------------------------------------------------

def test_is_available_local_file():
    assert Mem4MemoryProvider({"backend": "local-file"}).is_available() is True


def test_is_available_unimplemented_backend_is_false():
    # remote-vault is a reserved topology, not in ⑤-minimal → degrade to
    # built-in rather than half-load a broken provider.
    assert Mem4MemoryProvider({"backend": "remote-vault"}).is_available() is False


# -- mem_route reads ---------------------------------------------------------

def test_mem_route_hit_carries_freshness_tag(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    (root / "sys.md").write_text("host=toothless\nvps=lightnode", encoding="utf-8")

    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    out = json.loads(provider.handle_tool_call("mem_route", {"code": "§sys"}))
    assert out["found"] is True
    assert out["source"] == "local-file"
    assert out["stale"] is False
    assert "[fresh: local-file]" in out["result"]
    assert "host=toothless" in out["result"]


def test_mem_route_miss_falls_back_not_errors(tmp_path):
    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    out = json.loads(provider.handle_tool_call("mem_route", {"code": "vlt"}))
    assert out["found"] is False
    assert "built-in memory remains authoritative" in out["result"]


def test_mem_route_rejects_path_traversal(tmp_path):
    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    res = provider.handle_tool_call("mem_route", {"code": "../../etc/passwd"})
    assert "invalid route code" in res.lower()


def test_normalize_code_guards():
    assert normalize_code("§sys") == "sys"
    assert normalize_code("  ADR ") == "adr"
    assert normalize_code("../secret") is None
    assert normalize_code("a/b") is None
    assert normalize_code("") is None


# -- built-in memory is never touched (design spike §3 / §8.3) ---------------

def test_on_memory_write_mirrors_without_touching_builtin(tmp_path):
    # Pre-existing built-in memory files.
    memories = tmp_path / "memories"
    memories.mkdir()
    memory_md = memories / "MEMORY.md"
    user_md = memories / "USER.md"
    memory_md.write_text("BUILTIN MEMORY", encoding="utf-8")
    user_md.write_text("BUILTIN USER", encoding="utf-8")
    before = {p.name: p.read_text(encoding="utf-8") for p in (memory_md, user_md)}

    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    provider.on_memory_write("add", "user", "User prefers concise replies")
    provider.on_memory_write("add", "memory", "Fork origin is sam7894604")

    # Built-in files are byte-for-byte unchanged, and no new files appeared
    # in the memories/ directory.
    assert {p.name: p.read_text(encoding="utf-8") for p in (memory_md, user_md)} == before
    assert sorted(p.name for p in memories.iterdir()) == ["MEMORY.md", "USER.md"]

    # The writes landed in mem4-owned mirror files instead.
    mirror_user = tmp_path / "mem4" / "_mirror" / "user.md"
    mirror_memory = tmp_path / "mem4" / "_mirror" / "memory.md"
    assert mirror_user.is_file() and "concise" in mirror_user.read_text(encoding="utf-8")
    assert mirror_memory.is_file() and "sam7894604" in mirror_memory.read_text(encoding="utf-8")


def test_mirror_skips_remove_and_empty(tmp_path):
    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    provider.on_memory_write("remove", "memory", "gone")
    provider.on_memory_write("add", "memory", "   ")

    assert not (tmp_path / "mem4" / "_mirror").exists()


# -- degrade when inactive (removing/failing provider = built-in only) -------

def test_inactive_backend_degrades_gracefully(tmp_path):
    provider = Mem4MemoryProvider({"backend": "remote-vault"})  # unimplemented
    provider.initialize("session-1", hermes_home=str(tmp_path))
    assert provider._active is False

    # Reads degrade to a marker, never an exception.
    out = json.loads(provider.handle_tool_call("mem_route", {"code": "sys"}))
    assert out["found"] is False
    assert "inactive" in out["result"]

    # System prompt contributes nothing when inactive.
    assert provider.system_prompt_block() == ""

    # Writes are no-ops — no mem4 storage is created at all.
    provider.on_memory_write("add", "user", "should not persist")
    assert not (tmp_path / "mem4" / "_mirror").exists()


def test_active_provider_contributes_system_prompt(tmp_path):
    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))
    block = provider.system_prompt_block()
    assert "mem_route" in block
    assert block.strip()


# -- idempotent init + version marker (design spike §10) ---------------------

def test_idempotent_init_marker(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    (root / "sys.md").write_text("x", encoding="utf-8")
    (root / "adr.md").write_text("y", encoding="utf-8")

    p1 = Mem4MemoryProvider({"backend": "local-file"})
    p1.initialize("session-1", hermes_home=str(tmp_path))
    assert p1._ran_migration is True

    marker = root / ".mem4_state.json"
    assert marker.is_file()
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["migration_complete"] is True
    # ① is wired: with no history source, backfill completes at init (microfiles
    # are indexed synchronously) — the cursor seam is still present.
    assert state["backfill_complete"] is True
    assert "backfill_cursor" in state
    assert state["counts"]["microfiles"] == 2        # adopted, not rebuilt

    marker_snapshot = marker.read_text(encoding="utf-8")

    # Second init on the same home: marker already current → no migration,
    # marker untouched (byte-for-byte).
    p2 = Mem4MemoryProvider({"backend": "local-file"})
    p2.initialize("session-2", hermes_home=str(tmp_path))
    assert p2._ran_migration is False
    assert marker.read_text(encoding="utf-8") == marker_snapshot


def test_init_adopts_existing_microfiles_not_rebuild(tmp_path):
    root = tmp_path / "mem4"
    root.mkdir()
    original = "ADOPTED CONTENT — do not touch"
    (root / "vlt.md").write_text(original, encoding="utf-8")

    provider = Mem4MemoryProvider({"backend": "local-file"})
    provider.initialize("session-1", hermes_home=str(tmp_path))

    # The adopted microfile is readable and unchanged.
    assert (root / "vlt.md").read_text(encoding="utf-8") == original
    out = json.loads(provider.handle_tool_call("mem_route", {"code": "vlt"}))
    assert out["found"] is True
    assert original in out["result"]
