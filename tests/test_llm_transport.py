"""Tests for Provider ABC + LlamaCppProvider."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from dollos.llm.transport import LlamaCppProvider, Provider
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder


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


@pytest.mark.asyncio
async def test_llamacpp_provider_includes_grammar_when_passed():
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
            stop=None,
            max_tokens=128,
            grammar="root ::= \"hi\"",
        ):
            pass

    assert captured["body"]["grammar"] == "root ::= \"hi\""


@pytest.mark.asyncio
async def test_llamacpp_provider_omits_grammar_when_none():
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

    assert "grammar" not in captured["body"]


@pytest.mark.asyncio
async def test_llamacpp_provider_records_telemetry_on_success(tmp_path: Path):
    recorder = TelemetryRecorder(tmp_path / "telemetry")
    provider = LlamaCppProvider(
        base_url="http://test.local:8001",
        timeout_s=5.0,
        recorder=recorder,
        model_alias="Qwen3.6",
        max_context_tokens=131_072,
    )

    # llama.cpp emits tokens_evaluated / tokens_predicted on the final SSE.
    sse_body = (
        'data: {"content": "Hello", "stop": false}\n\n'
        'data: {"content": "", "stop": true, "tokens_evaluated": 1234, "tokens_predicted": 56}\n\n'
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )
        async for _ in provider.stream(
            prompt="hello", stop=None, max_tokens=128, purpose="cascade"
        ):
            pass

    records = recorder.read_today()
    assert len(records) == 1
    r = records[0]
    assert r.model == "Qwen3.6"
    assert r.prompt_tokens == 1234
    assert r.completion_tokens == 56
    assert r.latency_total_ms is not None and r.latency_total_ms >= 0
    assert r.latency_ttft_ms is not None
    assert r.context_pct is not None
    assert r.error is None
    assert r.call_purpose == "cascade"


@pytest.mark.asyncio
async def test_llamacpp_provider_records_telemetry_on_http_error(tmp_path: Path):
    recorder = TelemetryRecorder(tmp_path / "telemetry")
    provider = LlamaCppProvider(
        base_url="http://test.local:8001",
        timeout_s=5.0,
        recorder=recorder,
        model_alias="Qwen3.6",
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(return_value=httpx.Response(429))
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in provider.stream(prompt="x", stop=None, max_tokens=128):
                pass

    records = recorder.read_today()
    assert len(records) == 1
    assert records[0].error == "http_429"
    assert records[0].prompt_tokens is None     # no fake data


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_generations(monkeypatch):
    """Semaphore: at most max_concurrency HTTP connections are in-flight at once.

    Injects a fake httpx.AsyncClient whose stream() context manager increments
    an inflight counter on entry and decrements it on exit.  A gate (asyncio.Event)
    holds all in-flight connections open until all 5 consumers have started, so we
    can measure the peak before any slot is released.  With max_concurrency=2 the
    semaphore must prevent the peak from exceeding 2, even when 5 consumers race.
    """
    peak = 0
    inflight = 0
    gate = asyncio.Event()

    class FakeStreamResponse:
        async def __aenter__(self):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            # Yield to event loop so other tasks can reach the semaphore boundary
            await asyncio.sleep(0)
            # Hold the connection open until the gate opens
            await gate.wait()
            return self

        async def __aexit__(self, *args):
            nonlocal inflight
            inflight -= 1

        def raise_for_status(self):
            pass

        def aiter_lines(self):
            async def _lines():
                yield 'data: {"content": "", "stop": true}'
            return _lines()

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url, **kwargs):
            return FakeStreamResponse()

    monkeypatch.setattr("dollos.llm.transport.httpx.AsyncClient", FakeAsyncClient)

    provider = LlamaCppProvider(base_url="http://test.local:8001", max_concurrency=2)

    completed = 0

    async def consume(i: int) -> None:
        nonlocal completed
        async for _ in provider.stream(prompt=f"p{i}", stop=None, max_tokens=1):
            pass
        completed += 1

    tasks = [asyncio.create_task(consume(i)) for i in range(5)]

    # Give all 5 tasks time to queue up against the semaphore and gate
    await asyncio.sleep(0.01)

    # Open the gate: let the (at most 2) in-flight connections complete
    gate.set()
    await asyncio.gather(*tasks)

    assert peak <= 2, f"peak concurrent HTTP connections was {peak}, expected ≤ 2"
    assert completed == 5, "all 5 consumers should have completed successfully"
