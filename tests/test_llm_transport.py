"""Tests for Provider ABC + LlamaCppProvider."""

import json

import httpx
import pytest
import respx

from dollos.llm.transport import LlamaCppProvider, Provider


def test_provider_is_abstract():
    with pytest.raises(TypeError):
        Provider()  # type: ignore[abstract]


def test_llamacpp_provider_supports_prefill_is_true():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)
    assert provider.supports_prefill is True


@pytest.mark.asyncio
async def test_llamacpp_provider_streams_chunks_until_done():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

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
        async for chunk in provider.stream(
            prompt="hello prompt",
            stop=None,
            max_tokens=128,
        ):
            chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].text == "Hello"
    assert chunks[0].done is False
    assert chunks[1].text == " world"
    assert chunks[1].done is False
    assert chunks[2].done is True


@pytest.mark.asyncio
async def test_llamacpp_provider_forwards_prompt_verbatim():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(
            prompt="THE EXACT PROMPT STRING",
            stop=None,
            max_tokens=128,
        ):
            pass

    # Provider should NOT mutate the prompt string at all.
    assert captured["body"]["prompt"] == "THE EXACT PROMPT STRING"


@pytest.mark.asyncio
async def test_llamacpp_provider_forwards_stop_and_max_tokens():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(
            prompt="x",
            stop=["<|im_end|>"],
            max_tokens=512,
        ):
            pass

    assert captured["body"]["stop"] == ["<|im_end|>"]
    assert captured["body"]["n_predict"] == 512
    assert captured["body"]["stream"] is True
    assert captured["body"]["cache_prompt"] is True


@pytest.mark.asyncio
async def test_llamacpp_provider_default_stop_when_none_passed():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(prompt="x", stop=None, max_tokens=128):
            pass

    # Acknowledged tech-debt (spec §10 Open Questions): default stop is
    # ChatML-flavored `<|im_end|>` even though stop is conceptually
    # template's concern. Future plans will revisit.
    assert captured["body"]["stop"] == ["<|im_end|>"]
