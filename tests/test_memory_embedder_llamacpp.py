"""Tests for LlamaCppEmbedder."""

import json

import httpx
import pytest
import respx

from dollos.memory.embedder_llamacpp import LlamaCppEmbedder


@pytest.mark.asyncio
async def test_initialize_sets_dimensions_via_probe():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="bge-base-en-v1.5",
        timeout_s=5.0,
    )
    with respx.mock(base_url="http://test.local:8002") as m:
        m.post("/embedding").mock(
            return_value=httpx.Response(
                200, json={"embedding": [0.1] * 768}
            )
        )
        await emb.initialize()
    assert emb.dimensions == 768
    assert emb.model_id == "bge-base-en-v1.5"


@pytest.mark.asyncio
async def test_embed_single():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    with respx.mock(base_url="http://test.local:8002") as m:
        # initialize probe + actual embed share same mock
        m.post("/embedding").mock(
            return_value=httpx.Response(
                200, json={"embedding": [0.1, 0.2, 0.3]}
            )
        )
        await emb.initialize()
        v = await emb.embed("hello")
    assert v == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_batch_makes_one_request_per_input():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embedding": [0.5, 0.5, 0.5]})

    with respx.mock(base_url="http://test.local:8002") as m:
        m.post("/embedding").mock(side_effect=handler)
        await emb.initialize()
        results = await emb.embed_batch(["a", "b"])

    # 1 probe call + 2 batch calls = 3 captured
    assert len(captured_bodies) == 3
    assert captured_bodies[1]["content"] == "a"
    assert captured_bodies[2]["content"] == "b"
    assert results == [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]]


@pytest.mark.asyncio
async def test_dimensions_before_initialize_raises():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    with pytest.raises(RuntimeError):
        _ = emb.dimensions
