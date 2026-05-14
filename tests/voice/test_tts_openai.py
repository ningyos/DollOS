"""OpenAICompatTTSEngine tests with mocked httpx."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import sys
import pytest

# Pre-import dollos.voice so huggingface_hub (a real httpx user) is loaded
# with the real httpx *before* the fake_httpx fixture swaps sys.modules.
import dollos.voice  # noqa: F401


@pytest.fixture
def fake_httpx(monkeypatch):
    """Patch httpx.AsyncClient with a fake that yields chunked bytes."""
    fake_module = MagicMock()
    fake_response = MagicMock()
    async def aiter_bytes(_chunk_size=None):
        for chunk in [b"\x00\x01" * 480, b"\x02\x03" * 480]:
            yield chunk
    fake_response.aiter_bytes = aiter_bytes
    fake_response.raise_for_status = MagicMock()
    fake_response.__aenter__ = AsyncMock(return_value=fake_response)
    fake_response.__aexit__ = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_module.AsyncClient = MagicMock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, "httpx", fake_module)
    return fake_module, fake_client


async def test_openai_compat_streams_chunks(fake_httpx):
    from dollos.voice.tts_openai import OpenAICompatTTSEngine
    eng = OpenAICompatTTSEngine(
        base_url="https://api.openai.com/v1",
        api_key="sk-xxx",
        model="tts-1",
        voice="alloy",
    )
    chunks = [c async for c in eng.synthesize("hello")]
    assert len(chunks) >= 2
    assert eng.sample_rate == 24000


async def test_openai_compat_no_api_key_ok(fake_httpx):
    from dollos.voice.tts_openai import OpenAICompatTTSEngine
    eng = OpenAICompatTTSEngine(
        base_url="http://localhost:8000/v1",
        api_key=None,
        model="kokoro",
        voice="af_bella",
    )
    chunks = [c async for c in eng.synthesize("hi")]
    assert len(chunks) > 0
