"""Tests for Memory.__init__ and initialize()."""

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
