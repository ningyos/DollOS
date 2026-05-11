"""BridgeController + UtteranceStateMachine — drive utterance markers from VAD."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from dollos.voice.bridge.signaling import BridgeSignaling
    from dollos.voice.bridge.vad import SileroVAD

logger = logging.getLogger(__name__)


class UtteranceStateMachine:
    """Drives utterance_start / utterance_end markers from chunk-by-chunk VAD output.

    State transitions:
        SILENCE → SPEECH (on first chunk with prob >= threshold) → fires utterance_start
        SPEECH  → SILENCE (after `silence_chunks_to_end` consecutive low-prob chunks)
                  fires utterance_end, reset to SILENCE
        SPEECH  → SPEECH (any high-prob chunk resets the silence counter)
    """

    def __init__(
        self,
        *,
        signaling: "BridgeSignaling",
        sample_rate: int,
        speech_threshold: float = 0.5,
        silence_chunks_to_end: int = 25,  # 25 × 32ms = 800ms silence
    ) -> None:
        self._signaling = signaling
        self._sample_rate = sample_rate
        self._threshold = speech_threshold
        self._silence_chunks_to_end = silence_chunks_to_end
        self._in_utterance = False
        self._silence_chunks = 0

    async def on_chunk(self, *, speech_prob: float) -> None:
        is_speech = speech_prob >= self._threshold
        if not self._in_utterance:
            if is_speech:
                self._in_utterance = True
                self._silence_chunks = 0
                await self._signaling.send_utterance_start(sample_rate=self._sample_rate)
        else:
            if is_speech:
                self._silence_chunks = 0
            else:
                self._silence_chunks += 1
                if self._silence_chunks >= self._silence_chunks_to_end:
                    self._in_utterance = False
                    self._silence_chunks = 0
                    await self._signaling.send_utterance_end()


class BridgeController:
    """Wires mic + VAD + signaling + speaker into a running bridge.

    Owns the asyncio tasks: a consume-from-mic-track loop that runs VAD
    and pushes markers, and the speaker consumer driven by `on_remote_track`.
    """

    def __init__(
        self,
        *,
        signaling: "BridgeSignaling",
        vad: "SileroVAD",
        sample_rate: int = 16000,
    ) -> None:
        self._signaling = signaling
        self._vad = vad
        self._sample_rate = sample_rate
        self._fsm = UtteranceStateMachine(
            signaling=signaling, sample_rate=sample_rate,
        )
        self._mic_loop_task: asyncio.Task | None = None

    async def run_mic_loop(self, mic_track) -> None:
        """Read frames from the mic track, run VAD, drive utterance state."""
        try:
            while True:
                frame = await mic_track.recv()
                # frame is int16 mono at our sample_rate; convert to float32 [-1,1]
                pcm_i16 = np.frombuffer(frame.to_ndarray().tobytes(), dtype=np.int16)
                samples_f32 = pcm_i16.astype(np.float32) / 32768.0
                # Split into VAD chunks of SAMPLES_PER_CHUNK
                chunk_size = self._vad.SAMPLES_PER_CHUNK
                for i in range(0, len(samples_f32) - chunk_size + 1, chunk_size):
                    chunk = samples_f32[i:i + chunk_size]
                    prob = self._vad.speech_probability(chunk)
                    await self._fsm.on_chunk(speech_prob=prob)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mic loop ended")
