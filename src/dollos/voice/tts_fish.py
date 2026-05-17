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
_SAMPLES_PER_CHUNK = _SAMPLE_RATE * _FRAME_MS // 1000  # 882
_CHUNK_BYTES = _SAMPLES_PER_CHUNK * 2  # 1764


def _chunk_pcm_bytes(raw_pcm: bytes) -> Iterator[bytes]:
    """Re-chunk a stream of int16 PCM bytes into 20ms frames."""
    for i in range(0, len(raw_pcm), _CHUNK_BYTES):
        chunk = raw_pcm[i:i + _CHUNK_BYTES]
        if len(chunk) < _CHUNK_BYTES:
            chunk = chunk + b"\x00" * (_CHUNK_BYTES - len(chunk))
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
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

        def producer():
            try:
                # Re-pin our references each call — singleton may have been
                # used by another character since our __init__.
                self._synth.set_references(self._profiles)
                for raw in self._synth.synthesize_stream(text):
                    for chunk in _chunk_pcm_bytes(raw):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as e:
                logger.exception("fish-tts synthesize error")
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        import threading
        threading.Thread(target=producer, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        # Singleton stays alive for the process — just drop our references.
        self._synth = None
        self._profiles = None
