"""PCM ↔ av.AudioFrame conversion + resample helpers for the voice pipeline.

aiortc tracks pass av.AudioFrame objects. Engines emit raw int16 PCM
bytes. This module bridges the two and resamples between the WebRTC
48 kHz default and the ASR-preferred 16 kHz.
"""
from __future__ import annotations

from math import gcd

import numpy as np
from av import AudioFrame
from scipy.signal import resample_poly


def resample_pcm_int16(pcm: bytes, *, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono int16 little-endian PCM bytes.

    Uses scipy.signal.resample_poly (polyphase filter — clean for
    integer ratios like 48000/16000 = 3).
    """
    if src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    g = gcd(src_rate, dst_rate)
    up = dst_rate // g
    down = src_rate // g
    resampled = resample_poly(samples, up=up, down=down)
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


def audio_frame_from_pcm(pcm: bytes, *, sample_rate: int) -> AudioFrame:
    """Build an av.AudioFrame from mono int16 PCM bytes.

    Layout: mono (1 channel). Format: s16.
    """
    samples = np.frombuffer(pcm, dtype=np.int16)
    # av AudioFrame.from_ndarray expects shape (channels, n_samples).
    array = samples.reshape(1, -1)
    frame = AudioFrame.from_ndarray(array, format="s16", layout="mono")
    frame.sample_rate = sample_rate
    return frame


def pcm_from_audio_frame(frame: AudioFrame) -> bytes:
    """Extract mono int16 PCM bytes from an av.AudioFrame.

    If the frame is not s16/mono, downmix + format-convert as needed.
    """
    if frame.format.name == "s16" and frame.layout.name == "mono":
        return frame.to_ndarray().tobytes()
    # Convert via ndarray: downmix multi-channel to mono by averaging.
    array = frame.to_ndarray()
    if array.ndim == 2 and array.shape[0] > 1:
        array = array.mean(axis=0, keepdims=True).astype(array.dtype)
    if array.dtype != np.int16:
        if np.issubdtype(array.dtype, np.floating):
            array = np.clip(array * 32767.0, -32768, 32767).astype(np.int16)
        else:
            array = array.astype(np.int16)
    return array.tobytes()
