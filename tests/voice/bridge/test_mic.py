"""MicrophoneTrack — sounddevice InputStream → aiortc audio track.

sounddevice is mocked. Real audio device tests are voice_integration.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from dollos.voice.bridge.mic import MicrophoneTrack


@pytest.mark.asyncio
async def test_mic_track_kind_is_audio():
    with patch("dollos.voice.bridge.mic.sd") as sd_mod:
        sd_mod.InputStream = MagicMock()
        track = MicrophoneTrack(sample_rate=16000)
        assert track.kind == "audio"
        track.stop()


@pytest.mark.asyncio
async def test_mic_track_starts_input_stream_on_construction():
    with patch("dollos.voice.bridge.mic.sd") as sd_mod:
        stream_mock = MagicMock()
        sd_mod.InputStream = MagicMock(return_value=stream_mock)
        track = MicrophoneTrack(sample_rate=16000)
        sd_mod.InputStream.assert_called_once()
        kwargs = sd_mod.InputStream.call_args.kwargs
        assert kwargs["samplerate"] == 16000
        assert kwargs["channels"] == 1
        assert kwargs["dtype"] == "float32"
        stream_mock.start.assert_called_once()
        track.stop()
        stream_mock.stop.assert_called_once()
        stream_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_mic_track_recv_returns_audio_frame_from_callback_data():
    """sounddevice calls our callback with mic data; recv() yields it as AudioFrame."""
    with patch("dollos.voice.bridge.mic.sd") as sd_mod:
        callback_holder = {}

        def _capture_callback(callback, **kw):
            callback_holder["fn"] = callback
            return MagicMock()

        sd_mod.InputStream = MagicMock(side_effect=lambda *a, **kw: _capture_callback(
            callback=kw["callback"], **{k: v for k, v in kw.items() if k != "callback"}
        ))

        track = MicrophoneTrack(sample_rate=16000)
        # Simulate sounddevice firing the callback once with 512 samples of audio.
        samples = (np.arange(512, dtype=np.float32) / 512.0)
        callback_holder["fn"](
            indata=samples.reshape(-1, 1),
            frames=512,
            time=None,
            status=None,
        )

        frame = await asyncio.wait_for(track.recv(), timeout=1.0)
        assert frame.sample_rate == 16000
        # 512 samples in the frame
        assert frame.samples == 512
        track.stop()
