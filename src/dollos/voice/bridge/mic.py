"""MicrophoneTrack — aiortc audio track sourced from sounddevice InputStream."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
import sounddevice as sd
from aiortc import MediaStreamTrack

from dollos.voice.codec import audio_frame_from_pcm

logger = logging.getLogger(__name__)


class MicrophoneTrack(MediaStreamTrack):
    """Capture mono float32 audio from the system mic, yield int16 AudioFrames.

    Each sounddevice callback fires from a worker thread; we put the
    frames into an asyncio.Queue (thread-safe via call_soon_threadsafe)
    consumed by recv().
    """

    kind = "audio"

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        blocksize: int = 512,
        device: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._loop = asyncio.get_event_loop()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            callback=self._sd_callback,
            device=device,
        )
        self._stream.start()

    def _sd_callback(self, indata, frames, time, status) -> None:
        if status:
            logger.debug("InputStream status: %s", status)
        # indata shape (frames, 1) float32 in [-1, 1]
        samples_f32 = indata.reshape(-1)
        pcm_i16 = np.clip(samples_f32 * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        frame = audio_frame_from_pcm(pcm_i16, sample_rate=self._sample_rate)
        # Queue must be modified on the asyncio thread.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    async def recv(self):
        return await self._queue.get()

    def stop(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("mic stream stop failed")
        super().stop()
