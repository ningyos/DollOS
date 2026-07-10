"""Tests for the per-call `on_usage` callback threaded through Provider.stream().

Mirrors the respx/httpx SSE-mock harness used in tests/test_llm_transport.py
(e.g. test_llamacpp_provider_records_telemetry_on_success) — same fake SSE
body shape, same LlamaCppProvider construction, no recorder needed since
on_usage fires independently of telemetry recording.
"""

import httpx
import pytest
import respx

from dollos.llm.transport import LlamaCppProvider


@pytest.mark.asyncio
async def test_provider_stream_calls_on_usage_with_tokens():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    # llama.cpp emits tokens_evaluated / tokens_predicted on the final SSE
    # (stop=true) payload.
    sse_body = (
        'data: {"content": "Hello", "stop": false}\n\n'
        'data: {"content": "", "stop": true, "tokens_evaluated": 128, "tokens_predicted": 42}\n\n'
    )

    got = []

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )
        async for _ in provider.stream(
            prompt="hi",
            stop=None,
            max_tokens=128,
            on_usage=lambda p, c: got.append((p, c)),
        ):
            pass

    assert got == [(128, 42)]


@pytest.mark.asyncio
async def test_provider_stream_on_usage_none_when_no_final_payload():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    # Backend's final SSE payload omits tokens_evaluated / tokens_predicted
    # entirely (e.g. a server that doesn't report usage).
    sse_body = 'data: {"content": "", "stop": true}\n\n'

    got = []

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )
        async for _ in provider.stream(
            prompt="hi",
            stop=None,
            max_tokens=128,
            on_usage=lambda p, c: got.append((p, c)),
        ):
            pass

    assert got == [(None, None)]


@pytest.mark.asyncio
async def test_provider_stream_on_usage_default_none_is_a_noop():
    """Existing callers that don't pass on_usage must be unaffected."""
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    sse_body = 'data: {"content": "", "stop": true, "tokens_evaluated": 1, "tokens_predicted": 1}\n\n'

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )
        async for _ in provider.stream(prompt="hi", stop=None, max_tokens=128):
            pass


@pytest.mark.asyncio
async def test_provider_stream_on_usage_raising_does_not_break_turn():
    """A raising on_usage callback must be swallowed (guarded), mirroring the
    recorder's own try/except — a bad callback must not break the turn."""
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    sse_body = 'data: {"content": "", "stop": true, "tokens_evaluated": 1, "tokens_predicted": 1}\n\n'

    def boom(p, c):
        raise RuntimeError("on_usage blew up")

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
            prompt="hi", stop=None, max_tokens=128, on_usage=boom
        ):
            chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].done is True
