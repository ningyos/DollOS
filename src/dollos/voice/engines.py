"""ASR / TTS engine ABCs + decorator-based registries.

Adding a new engine = write a class implementing the ABC, decorate with
@register_asr("<name>") or @register_tts("<name>"). The class becomes
available to character pack voice configs via that name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class ASREngine(ABC):
    """Speech recognition. Utterance-batch input → final transcript string."""

    @abstractmethod
    async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
        """Return the transcript for one utterance.

        audio_pcm: mono int16 little-endian PCM bytes.
        sample_rate: the engine resamples internally if it differs from
            the model's native rate.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (model handles, threads, etc.)."""


class TTSEngine(ABC):
    """Text-to-speech. Text in → streaming PCM chunks out at self.sample_rate."""

    sample_rate: int  # output sample rate in Hz; concrete classes must set this

    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mono int16 little-endian PCM chunks for the spoken text.

        Each yielded chunk is ~10-20ms of audio so callers can pipe into
        a streaming sink (WebRTC track, file writer) without buffering
        the whole utterance.
        """

    @abstractmethod
    async def aclose(self) -> None: ...


ASR_REGISTRY: dict[str, type[ASREngine]] = {}
TTS_REGISTRY: dict[str, type[TTSEngine]] = {}


def register_asr(name: str):
    """Decorator: register an ASREngine subclass under `name`."""
    def decorate(cls: type[ASREngine]) -> type[ASREngine]:
        if not issubclass(cls, ASREngine):
            raise TypeError(f"{cls.__name__} must subclass ASREngine")
        ASR_REGISTRY[name] = cls
        return cls
    return decorate


def register_tts(name: str):
    """Decorator: register a TTSEngine subclass under `name`."""
    def decorate(cls: type[TTSEngine]) -> type[TTSEngine]:
        if not issubclass(cls, TTSEngine):
            raise TypeError(f"{cls.__name__} must subclass TTSEngine")
        TTS_REGISTRY[name] = cls
        return cls
    return decorate
