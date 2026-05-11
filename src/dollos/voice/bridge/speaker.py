"""SpeakerPlayer — consumes an aiortc remote audio track + writes to sounddevice."""
from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

from dollos.voice.codec import pcm_from_audio_frame, resample_pcm_int16

logger = logging.getLogger(__name__)


class SpeakerPlayer:
    """Open an output stream; consume frames from a track + write PCM."""

    def __init__(self, *, sample_rate: int = 48000) -> None:
        self._sample_rate = sample_rate
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        self._stream.start()

    async def consume_track(self, track) -> None:
        """Read AudioFrames from `track`, write to the speaker.

        Ends when the track raises (e.g. peer disconnected).
        """
        try:
            while True:
                frame = await track.recv()
                pcm_i16 = pcm_from_audio_frame(frame)
                # Resample if frame.sample_rate differs from our output rate.
                if frame.sample_rate != self._sample_rate:
                    pcm_i16 = resample_pcm_int16(
                        pcm_i16,
                        src_rate=frame.sample_rate,
                        dst_rate=self._sample_rate,
                    )
                # int16 LE → float32 in [-1, 1]
                samples_i16 = np.frombuffer(pcm_i16, dtype=np.int16)
                samples_f32 = samples_i16.astype(np.float32) / 32768.0
                # sounddevice.OutputStream.write expects float32 in (frames, channels).
                try:
                    self._stream.write(samples_f32.reshape(-1, 1).copy())
                except Exception:
                    logger.exception("speaker write failed")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("speaker consume_track ended")

    def stop(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("speaker stream stop failed")
