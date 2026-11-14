"""Streaming resamplers between Discord's 48 kHz stereo and dollcore's mono rates.

- The owner's voice: 48 kHz stereo s16le (what Opus decodes to) → 16 kHz mono s16le for ASR.
- Her voice: 24 kHz mono s16le (Qwen3-TTS) → 48 kHz stereo s16le for Discord's Opus encoder.

Both are a windowed-sinc FIR on the upsampled stream, kept stateful across chunks so that
cutting a stream into 20 ms pieces gives the same samples as resampling it in one go. The output
length is exact: `len(in) * up / down` samples per chunk once the phase is accounted for, so
play-position accounting in 24 kHz samples can be done on the 48 kHz side by halving.
"""

from __future__ import annotations

import numpy as np

#: Bytes per s16le sample.
SAMPLE_BYTES = 2
#: FIR length. 63 taps at 48 kHz is a 0.65 ms group delay, inaudible for conversation.
TAPS = 63


class StreamResampler:
    """Rational resampler by `up / down` for one mono stream, float in, float out."""

    def __init__(self, up: int, down: int, taps: int = TAPS) -> None:
        if up < 1 or down < 1:
            raise ValueError(f"resampling factors must be positive, got {up}/{down}")
        # Cut a little below the lower Nyquist so the transition band stays out of the alias zone.
        cutoff = 0.5 / max(up, down) * 0.9          # cycles per sample of the upsampled stream
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        self.up, self.down = up, down
        self._h = h / h.sum() * up                  # zero-stuffing divides the level by `up`
        self._hist = np.zeros(taps - 1)
        self._phase = 0                             # index of the next kept sample in the next block

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            # np.convolve swaps its arguments when the first is the shorter, so 62 samples of
            # history against 63 taps would give 2 outputs for no input (an empty TTS chunk
            # became one 24 kHz sample more played than sent, 2026-11-14 live).
            return np.zeros(0)
        if self.up > 1:
            u = np.zeros(len(x) * self.up)
            u[:: self.up] = x
        else:
            u = np.asarray(x, dtype=np.float64)
        buf = np.concatenate((self._hist, u))
        y = np.convolve(buf, self._h, mode="valid")  # one output per upsampled input
        self._hist = buf[len(buf) - len(self._hist):]
        out = y[self._phase :: self.down]
        self._phase = (self._phase - len(u)) % self.down
        return out


def pcm_samples(pcm: bytes, what: str) -> np.ndarray:
    """s16le bytes → float64 samples. An odd byte count is a malformed stream, not a short read."""
    if len(pcm) % SAMPLE_BYTES:
        raise ValueError(f"{what}: {len(pcm)} bytes is not a whole number of s16le samples")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def to_pcm(y: np.ndarray) -> bytes:
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


class OwnerDownsampler:
    """48 kHz stereo s16le → 16 kHz mono s16le (stereo averaged to mono, then /3)."""

    def __init__(self) -> None:
        self._r = StreamResampler(1, 3)

    def process(self, pcm48_stereo: bytes) -> bytes:
        x = pcm_samples(pcm48_stereo, "owner audio (48 kHz stereo)")
        if len(x) % 2:
            raise ValueError(f"owner audio: {len(x)} samples is not a whole number of stereo frames")
        return to_pcm(self._r.process(x.reshape(-1, 2).mean(axis=1)))


class HerUpsampler:
    """24 kHz mono s16le → 48 kHz stereo s16le (×2, the same signal on both channels)."""

    def __init__(self) -> None:
        self._r = StreamResampler(2, 1)

    def process(self, pcm24_mono: bytes) -> bytes:
        y = self._r.process(pcm_samples(pcm24_mono, "her audio (24 kHz mono)"))
        return to_pcm(np.repeat(y, 2))
