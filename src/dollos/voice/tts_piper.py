"""PiperVITSEngine — TTS via Piper VITS distilled single-speaker ONNX.

Each voice is a (model.onnx, model.onnx.json) pair shipped per-character.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from dollos.voice.engines import TTSEngine, register_tts

logger = logging.getLogger(__name__)

_FRAME_MS = 20


def _chunk_pcm_bytes(raw: bytes, sample_rate: int) -> Iterator[bytes]:
    samples_per_chunk = sample_rate * _FRAME_MS // 1000
    chunk_bytes = samples_per_chunk * 2
    for i in range(0, len(raw), chunk_bytes):
        c = raw[i : i + chunk_bytes]
        if len(c) < chunk_bytes:
            c = c + b"\x00" * (chunk_bytes - len(c))
        yield c


@register_tts("piper-vits")
class PiperVITSEngine(TTSEngine):
    """TTS engine wrapping Piper VITS single-speaker ONNX."""

    def __init__(
        self,
        *,
        voice_onnx_path: Path,
        voice_config_path: Path,
    ) -> None:
        for p in (voice_onnx_path, voice_config_path):
            if not p.exists():
                raise FileNotFoundError(f"piper file not found: {p}")
        from piper import PiperVoice  # lazy

        self._voice = PiperVoice.load(str(voice_onnx_path), str(voice_config_path))
        self.sample_rate: int = int(self._voice.config.sample_rate)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        # piper is sync; run on a worker thread and feed an asyncio.Queue.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None | BaseException] = asyncio.Queue(maxsize=64)

        def producer() -> None:
            try:
                for raw in self._voice.synthesize_stream_raw(text):
                    for chunk in _chunk_pcm_bytes(raw, self.sample_rate):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as e:
                logger.exception("piper synthesize error")
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        self._voice = None
