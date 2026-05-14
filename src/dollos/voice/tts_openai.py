"""OpenAICompatTTSEngine — TTS via OpenAI-compatible /v1/audio/speech endpoint.

Targets: OpenAI tts-1, lemonfox, kokoro, any /v1/audio/speech server.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from dollos.voice.engines import TTSEngine, register_tts

logger = logging.getLogger(__name__)

_FRAME_MS = 20


def _chunk_pcm_bytes(raw: bytes, sample_rate: int) -> Iterator[bytes]:
    samples_per_chunk = sample_rate * _FRAME_MS // 1000
    chunk_bytes = samples_per_chunk * 2
    for i in range(0, len(raw), chunk_bytes):
        c = raw[i:i + chunk_bytes]
        if len(c) < chunk_bytes:
            c = c + b"\x00" * (chunk_bytes - len(c))
        yield c


@register_tts("openai-compat-tts")
class OpenAICompatTTSEngine(TTSEngine):
    """Streaming TTS via any OpenAI-compatible /v1/audio/speech endpoint."""

    sample_rate = 24000  # OpenAI tts-1 default; pcm response is always 24k

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        voice: str,
        response_format: str = "pcm",
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._response_format = response_format
        self._timeout = timeout

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import httpx  # lazy
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "voice": self._voice,
            "input": text,
            "response_format": self._response_format,
        }
        url = f"{self._base_url}/audio/speech"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                buffer = b""
                async for raw in resp.aiter_bytes():
                    buffer += raw
                    # Emit complete 20ms frames from buffer
                    samples_per_chunk = self.sample_rate * _FRAME_MS // 1000
                    chunk_bytes = samples_per_chunk * 2
                    while len(buffer) >= chunk_bytes:
                        yield buffer[:chunk_bytes]
                        buffer = buffer[chunk_bytes:]
                # Final partial frame (pad with zeros)
                if buffer:
                    chunk_bytes = (self.sample_rate * _FRAME_MS // 1000) * 2
                    yield buffer + b"\x00" * (chunk_bytes - len(buffer))

    async def aclose(self) -> None:
        pass  # no resources to release
