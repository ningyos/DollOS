"""Tests for LlamaCppAdapter."""

import json

import httpx
import pytest
import respx

from dollos.llm.llamacpp import LlamaCppAdapter


@pytest.mark.asyncio
async def test_stream_completion_basic():
    """Adapter streams chunks until done."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    sse_body = (
        'data: {"content": "Hello", "stop": false}\n\n'
        'data: {"content": " world", "stop": false}\n\n'
        'data: {"content": "", "stop": true}\n\n'
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )

        chunks = []
        async for chunk in adapter.stream_completion(
            system="You are helpful.",
            user="Hi",
            prefill="",
        ):
            chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].text == "Hello"
    assert chunks[0].done is False
    assert chunks[1].text == " world"
    assert chunks[1].done is False
    assert chunks[2].done is True


@pytest.mark.asyncio
async def test_stream_completion_includes_prefill_in_prompt():
    """Adapter sends prefill as part of the prompt field."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture_request)

        async for _ in adapter.stream_completion(
            system="SYS",
            user="USR",
            prefill="RECALL: x\nGOAL: ",
        ):
            pass

    prompt = captured["body"]["prompt"]
    assert "SYS" in prompt
    assert "USR" in prompt
    # Renderer always opens <think> after the assistant marker
    assert "<|im_start|>assistant\n<think>\n" in prompt
    assert "RECALL: x\nGOAL: " in prompt
    # Prefill must come AFTER <think>\n
    assert prompt.endswith("<think>\nRECALL: x\nGOAL: ")


@pytest.mark.asyncio
async def test_stream_completion_passes_stop_sequences():
    """Stop sequences are forwarded to the backend."""
    adapter = LlamaCppAdapter(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture_request)

        async for _ in adapter.stream_completion(
            system="s",
            user="u",
            prefill="",
            stop=["<|im_end|>"],
            max_tokens=512,
        ):
            pass

    assert captured["body"]["stop"] == ["<|im_end|>"]
    assert captured["body"]["n_predict"] == 512
    assert captured["body"]["stream"] is True
