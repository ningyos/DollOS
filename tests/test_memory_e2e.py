"""End-to-end test: real SQLite + StubEmbedder, exercise the full Memory API."""

from pathlib import Path

import pytest

from dollos.memory import Memory, StubEmbedder


@pytest.mark.asyncio
async def test_full_memory_lifecycle(tmp_path: Path):
    db = tmp_path / "memory.db"
    mem = Memory(db_path=db, embedder=StubEmbedder())
    await mem.initialize()

    # Seed data: 10 shared, 10 char_a, 10 char_b
    shared_ids: list[int] = []
    a_ids: list[int] = []
    b_ids: list[int] = []
    for i in range(10):
        shared_ids.append(await mem.write(f"shared fact {i} about colors"))
        a_ids.append(await mem.write(f"private to a fact {i}", character_id="char_a"))
        b_ids.append(await mem.write(f"private to b fact {i}", character_id="char_b"))

    # === Scoping: None sees only shared ===
    res_none = await mem.search("fact", mode="hybrid", character_id=None, top_k=50)
    res_none_ids = {r.fact.id for r in res_none}
    assert set(shared_ids).issubset(res_none_ids)
    assert not (set(a_ids) & res_none_ids)
    assert not (set(b_ids) & res_none_ids)

    # === Scoping: char_a sees shared + a, not b ===
    res_a = await mem.search("fact", mode="hybrid", character_id="char_a", top_k=50)
    res_a_ids = {r.fact.id for r in res_a}
    assert set(shared_ids).issubset(res_a_ids)
    assert set(a_ids).issubset(res_a_ids)
    assert not (set(b_ids) & res_a_ids)

    # === Hybrid retrieval surfaces specific keyword ===
    target = await mem.write("the unique purple flamingo word")
    keyword_results = await mem.search("flamingo", mode="hybrid", top_k=5)
    assert target in {r.fact.id for r in keyword_results}

    # === Delete works end-to-end ===
    assert await mem.delete(target) is True
    assert await mem.read(target) is None

    # === Persistence: close and reopen, data still there ===
    await mem.close()
    mem2 = Memory(db_path=db, embedder=StubEmbedder())
    await mem2.initialize()
    fact = await mem2.read(shared_ids[0])
    assert fact is not None
    assert "shared fact 0" in fact.text
    await mem2.close()


@pytest.mark.asyncio
async def test_rebuild_after_reopen(tmp_path: Path):
    db = tmp_path / "memory.db"
    mem = Memory(db_path=db, embedder=StubEmbedder())
    await mem.initialize()
    for i in range(5):
        await mem.write(f"item {i}")
    await mem.close()

    mem2 = Memory(db_path=db, embedder=StubEmbedder())
    await mem2.initialize()
    n = await mem2.rebuild_embeddings()
    assert n == 5
    await mem2.close()
