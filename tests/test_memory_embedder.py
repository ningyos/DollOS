"""Tests for Embedder ABC + StubEmbedder."""

import pytest

from dollos.memory.embedder import Embedder, StubEmbedder


@pytest.mark.asyncio
async def test_stub_initialize_idempotent():
    e = StubEmbedder()
    await e.initialize()
    await e.initialize()  # second call must not raise
    assert e.dimensions == 32
    assert e.model_id == "stub"


@pytest.mark.asyncio
async def test_stub_embed_deterministic():
    e = StubEmbedder()
    await e.initialize()
    v1 = await e.embed("hello")
    v2 = await e.embed("hello")
    assert v1 == v2
    assert len(v1) == 32
    assert all(isinstance(x, float) for x in v1)


@pytest.mark.asyncio
async def test_stub_embed_distinct_inputs_produce_distinct_vectors():
    e = StubEmbedder()
    await e.initialize()
    v1 = await e.embed("hello")
    v2 = await e.embed("world")
    assert v1 != v2


@pytest.mark.asyncio
async def test_stub_embed_batch_equals_single_calls():
    e = StubEmbedder()
    await e.initialize()
    texts = ["a", "b", "c"]
    batch = await e.embed_batch(texts)
    singles = [await e.embed(t) for t in texts]
    assert batch == singles


def test_stub_dimensions_before_initialize_raises():
    e = StubEmbedder()
    with pytest.raises(RuntimeError):
        _ = e.dimensions


def test_embedder_is_abstract():
    with pytest.raises(TypeError):
        Embedder()  # type: ignore[abstract]
