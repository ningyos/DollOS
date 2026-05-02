"""Tests for Memory.__init__ and initialize()."""

import datetime as dt
from pathlib import Path

import pytest

from dollos.memory.embedder import StubEmbedder
from dollos.memory.store import Memory


@pytest.mark.asyncio
async def test_initialize_creates_schema_and_meta(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    embedder = StubEmbedder()
    mem = Memory(db_path=db_path, embedder=embedder)
    await mem.initialize()

    # File exists
    assert db_path.exists()

    # memory_meta has the embedder model_id and dim
    assert mem.get_meta("embedding_model_id") == "stub"
    assert mem.get_meta("embedding_dim") == "32"
    assert mem.get_meta("schema_version") == "1"

    await mem.close()


@pytest.mark.asyncio
async def test_initialize_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "subdir" / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    assert db_path.exists()
    await mem.close()


@pytest.mark.asyncio
async def test_initialize_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    await mem.close()

    # Reopen — should not error, meta unchanged
    mem2 = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem2.initialize()
    assert mem2.get_meta("embedding_model_id") == "stub"
    await mem2.close()


@pytest.mark.asyncio
async def test_initialize_warns_on_model_mismatch(tmp_path: Path, caplog):
    db_path = tmp_path / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    await mem.close()

    # Open with a different embedder model_id — must warn but not crash
    class OtherStub(StubEmbedder):
        @property
        def model_id(self) -> str:
            return "different-stub"

    import logging
    with caplog.at_level(logging.WARNING, logger="dollos.memory.store"):
        mem2 = Memory(db_path=db_path, embedder=OtherStub())
        await mem2.initialize()

    assert any("memory was built with model" in r.message for r in caplog.records)
    await mem2.close()


@pytest.mark.asyncio
async def test_methods_before_initialize_raise(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    with pytest.raises(RuntimeError):
        await mem.write("hello")


@pytest.mark.asyncio
async def test_write_then_read_returns_same_fact(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("the sky is blue", metadata={"source": "test"})
    fact = await mem.read(fact_id)

    assert fact is not None
    assert fact.id == fact_id
    assert fact.text == "the sky is blue"
    assert fact.character_id is None
    assert fact.metadata == {"source": "test"}
    assert isinstance(fact.created_at, dt.datetime)
    await mem.close()


@pytest.mark.asyncio
async def test_write_with_character_id(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("private thought", character_id="gura")
    fact = await mem.read(fact_id)
    assert fact is not None
    assert fact.character_id == "gura"
    await mem.close()


@pytest.mark.asyncio
async def test_read_nonexistent_returns_none(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    assert await mem.read(99999) is None
    await mem.close()


@pytest.mark.asyncio
async def test_delete_removes_from_facts_and_vec_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("ephemeral")
    assert await mem.delete(fact_id) is True
    assert await mem.read(fact_id) is None

    # Confirm vec_facts row is gone too
    cur = mem._conn.execute("SELECT COUNT(*) FROM vec_facts WHERE fact_id = ?", (fact_id,))
    assert cur.fetchone()[0] == 0

    # Confirm FTS row is gone too (auto by trigger)
    cur = mem._conn.execute("SELECT COUNT(*) FROM facts_fts WHERE rowid = ?", (fact_id,))
    assert cur.fetchone()[0] == 0

    await mem.close()


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    assert await mem.delete(99999) is False
    await mem.close()


@pytest.mark.asyncio
async def test_metadata_default_is_empty_dict(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    fact_id = await mem.write("no metadata")
    fact = await mem.read(fact_id)
    assert fact is not None
    assert fact.metadata == {}
    await mem.close()


@pytest.mark.asyncio
async def test_search_vector_mode_returns_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    a = await mem.write("alpha")
    b = await mem.write("bravo")
    c = await mem.write("charlie")

    results = await mem.search("alpha", mode="vector", top_k=10)
    ids = {r.fact.id for r in results}
    assert {a, b, c}.issubset(ids)
    await mem.close()


@pytest.mark.asyncio
async def test_search_fts_mode_finds_keyword(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    target = await mem.write("the quick brown fox")
    other = await mem.write("nothing related here")

    results = await mem.search("brown fox", mode="fts", top_k=10)
    ids = [r.fact.id for r in results]
    assert target in ids
    # FTS may not return `other` at all
    await mem.close()


@pytest.mark.asyncio
async def test_search_hybrid_mode_combines_both(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    a = await mem.write("the quick brown fox")
    b = await mem.write("a slow green turtle")
    c = await mem.write("brown bears in the forest")

    results = await mem.search("brown", mode="hybrid", top_k=10)
    ids = {r.fact.id for r in results}
    assert a in ids and c in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_character_scope_none_excludes_private_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    shared = await mem.write("shared knowledge")
    private = await mem.write("gura's secret", character_id="gura")

    results = await mem.search("knowledge secret", mode="hybrid", character_id=None)
    ids = {r.fact.id for r in results}
    assert shared in ids
    assert private not in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_character_scope_includes_private_for_that_character(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    shared = await mem.write("shared knowledge")
    private_gura = await mem.write("gura's secret", character_id="gura")
    private_rin = await mem.write("rin's secret", character_id="rin")

    results = await mem.search(
        "knowledge secret", mode="hybrid", character_id="gura"
    )
    ids = {r.fact.id for r in results}
    assert shared in ids
    assert private_gura in ids
    assert private_rin not in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_top_k_limits_results(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    for i in range(20):
        await mem.write(f"fact number {i}")

    results = await mem.search("fact", mode="hybrid", top_k=5)
    assert len(results) <= 5
    await mem.close()
