"""Codec helpers — PCM byte ↔ AudioFrame round-trip + resample."""
from __future__ import annotations

import numpy as np
import pytest


def test_resample_48k_to_16k_length_correct():
    from dollos.voice.codec import resample_pcm_int16

    # 1 second of 48 kHz int16 mono = 48000 samples.
    samples_48k = np.zeros(48000, dtype=np.int16)
    samples_48k[::100] = 10000  # sparse marker
    pcm_48k = samples_48k.tobytes()
    pcm_16k = resample_pcm_int16(pcm_48k, src_rate=48000, dst_rate=16000)
    # 1 second of 16 kHz = 16000 samples = 32000 bytes
    assert len(pcm_16k) == 32000


def test_resample_16k_to_48k_length_correct():
    from dollos.voice.codec import resample_pcm_int16

    samples_16k = np.zeros(16000, dtype=np.int16)
    pcm_16k = samples_16k.tobytes()
    pcm_48k = resample_pcm_int16(pcm_16k, src_rate=16000, dst_rate=48000)
    assert len(pcm_48k) == 96000  # 48000 samples × 2 bytes


def test_resample_passthrough_when_rates_equal():
    from dollos.voice.codec import resample_pcm_int16

    pcm = b"\x00" * 1000
    out = resample_pcm_int16(pcm, src_rate=16000, dst_rate=16000)
    assert out == pcm


def test_audio_frame_round_trip_48k():
    """PCM bytes → AudioFrame → PCM bytes preserves samples."""
    from dollos.voice.codec import audio_frame_from_pcm, pcm_from_audio_frame

    # 20ms of 48k mono PCM, ramp 0..959.
    samples = np.arange(960, dtype=np.int16) * 10
    pcm_in = samples.tobytes()
    frame = audio_frame_from_pcm(pcm_in, sample_rate=48000)
    assert frame.sample_rate == 48000
    assert frame.samples == 960
    pcm_out = pcm_from_audio_frame(frame)
    assert pcm_out == pcm_in


def test_audio_frame_has_correct_layout():
    from dollos.voice.codec import audio_frame_from_pcm

    pcm = b"\x00\x00" * 480  # 10ms @ 48k mono
    frame = audio_frame_from_pcm(pcm, sample_rate=48000)
    # aiortc / av expects layout: "mono"
    assert frame.layout.name == "mono"
    assert frame.format.name == "s16"
