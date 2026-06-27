"""FishTTSEngine — TTS via fish-tts (PyTorch, with torch.compile warmup).

Wraps the fish_tts singleton so multiple characters share the loaded model.
First call triggers torch.compile warmup (~4 min); subsequent calls only swap
`set_references`. Yields 20ms int16 PCM chunks at 44.1 kHz from fish-tts's
`synthesize_stream` (true streaming, first chunk ~1s for short text).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from dollos.voice.engines import TTSEngine, register_tts

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 44100
_FRAME_MS = 20
_SAMPLES_PER_CHUNK = _SAMPLE_RATE * _FRAME_MS // 1000  # 1764
_CHUNK_BYTES = _SAMPLES_PER_CHUNK * 2  # 3528


class _PCMReChunker:
    """Buffer arbitrary-sized PCM byte fragments into exact 20ms frames.

    fish-tts's synthesize_stream yields fragments of arbitrary size. The
    earlier per-fragment pad-with-zero approach inserted silence between
    fragments, stretching effective duration ~2x. This buffers across
    fragments and only zero-pads the final flush.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, raw: bytes) -> Iterator[bytes]:
        self._buf.extend(raw)
        while len(self._buf) >= _CHUNK_BYTES:
            chunk = bytes(self._buf[:_CHUNK_BYTES])
            del self._buf[:_CHUNK_BYTES]
            yield chunk

    def flush(self) -> Iterator[bytes]:
        if self._buf:
            chunk = bytes(self._buf) + b"\x00" * (_CHUNK_BYTES - len(self._buf))
            self._buf.clear()
            yield chunk


@register_tts("fish-tts")
class FishTTSEngine(TTSEngine):
    """TTS engine wrapping fish-tts. Uses the package's process singleton."""

    sample_rate = _SAMPLE_RATE

    def __init__(
        self,
        *,
        voice_profile_path: Path | None = None,
        transcript: str | None = None,
        voice_profile_paths: list[Path] | None = None,
        transcripts: list[str] | None = None,
    ) -> None:
        # Resolve paths/transcripts from either singular or plural form.
        if voice_profile_paths is not None:
            if not voice_profile_paths or len(voice_profile_paths) != len(transcripts or []):
                raise ValueError(
                    "voice_profile_paths and transcripts must be non-empty lists of equal length"
                )
            paths: list[Path] = voice_profile_paths
            texts: list[str] = transcripts  # type: ignore[assignment]
        elif voice_profile_path is not None:
            if transcript is None:
                raise ValueError("voice_profile_path requires transcript")
            paths = [voice_profile_path]
            texts = [transcript]
        else:
            raise ValueError(
                "must provide either voice_profile_path+transcript or "
                "voice_profile_paths+transcripts"
            )

        for p in paths:
            if not p.exists():
                raise FileNotFoundError(f"fish-tts voice profile not found: {p}")

        try:
            from fish_tts import get_instance, VoiceProfile  # lazy: torch heavy
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "fish-tts backend requires the `fish` optional dependency. "
                "Install with: `uv sync --extra fish` (or "
                "`uv pip install 'dollos[fish]'` in a downstream project)."
            ) from e
        self._synth = get_instance()  # singleton, blocks on first call
        self._profiles = [
            VoiceProfile.load(str(p), text=t) for p, t in zip(paths, texts)
        ]
        self._synth.set_references(self._profiles)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        # fish-tts's synthesize_stream is a sync generator. Run on a worker
        # thread and yield as chunks land.
        import threading
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        # MINOR: stop flag so the producer thread does not keep calling
        # call_soon_threadsafe(queue.put_nowait, ...) after the async generator
        # is cancelled, which would raise QueueFull in the event loop.
        stop_flag = threading.Event()

        def _safe_put(item) -> None:
            """Schedule queue.put_nowait only while the consumer is alive."""
            if stop_flag.is_set():
                return
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass  # consumer gone; discard

        def producer():
            try:
                # Re-pin our references each call — singleton may have been
                # used by another character since our __init__.
                self._synth.set_references(self._profiles)
                rechunker = _PCMReChunker()
                for raw in self._synth.synthesize_stream(text):
                    if stop_flag.is_set():
                        break
                    for chunk in rechunker.push(raw):
                        if stop_flag.is_set():
                            break
                        loop.call_soon_threadsafe(_safe_put, chunk)
                if not stop_flag.is_set():
                    for chunk in rechunker.flush():
                        if stop_flag.is_set():
                            break
                        loop.call_soon_threadsafe(_safe_put, chunk)
            except BaseException as e:
                logger.exception("fish-tts synthesize error")
                loop.call_soon_threadsafe(_safe_put, e)
            finally:
                # Signal the consumer only if it is still waiting.
                loop.call_soon_threadsafe(_safe_put, None)

        threading.Thread(target=producer, daemon=True).start()

        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop_flag.set()

    async def aclose(self) -> None:
        # Singleton stays alive for the process — just drop our references.
        self._synth = None
        self._profiles = None
