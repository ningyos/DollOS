"""SpeakerPlayer — consumes a remote audio track, writes to sounddevice."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from dollos.voice.bridge.speaker import SpeakerPlayer


@pytest.mark.asyncio
async def test_speaker_player_starts_output_stream():
    with patch("dollos.voice.bridge.speaker.sd") as sd_mod:
        stream_mock = MagicMock()
        sd_mod.OutputStream = MagicMock(return_value=stream_mock)

        player = SpeakerPlayer(sample_rate=48000)
        kwargs = sd_mod.OutputStream.call_args.kwargs
        assert kwargs["samplerate"] == 48000
        assert kwargs["channels"] == 1
        stream_mock.start.assert_called_once()
        player.stop()
        stream_mock.stop.assert_called_once()


@pytest.mark.asyncio
async def test_speaker_player_consume_writes_frames_to_stream():
    """consume_track() reads frames from a track and writes PCM to sounddevice."""
    with patch("dollos.voice.bridge.speaker.sd") as sd_mod:
        stream_mock = MagicMock()
        write_calls: list = []
        stream_mock.write = MagicMock(side_effect=lambda d: write_calls.append(d.copy()))
        sd_mod.OutputStream = MagicMock(return_value=stream_mock)

        from av import AudioFrame
        # Build a fake frame of 480 samples (10ms @ 48k) of float32.
        # The actual conversion in speaker.py converts to float32 for sd.
        samples_i16 = (np.arange(480, dtype=np.int16) * 10)
        frame = AudioFrame.from_ndarray(samples_i16.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000

        class _FakeTrack:
            def __init__(self):
                self._sent = False

            async def recv(self):
                if not self._sent:
                    self._sent = True
                    return frame
                # Signal end by raising; consume_track must handle.
                raise asyncio.CancelledError()

        player = SpeakerPlayer(sample_rate=48000)
        task = asyncio.create_task(player.consume_track(_FakeTrack()))
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        assert len(write_calls) >= 1
        # write was called with a numpy array
        assert write_calls[0].shape[0] > 0
        player.stop()
