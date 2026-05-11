"""Bridge utterance state machine — VAD speech_prob → utterance_start/end."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dollos.voice.bridge.controller import UtteranceStateMachine


@pytest.mark.asyncio
async def test_start_fires_after_speech_threshold_crossed():
    """Speech probability rising above threshold for 1+ chunk → utterance_start."""
    signaling = MagicMock()
    signaling.send_utterance_start = AsyncMock()
    signaling.send_utterance_end = AsyncMock()
    fsm = UtteranceStateMachine(
        signaling=signaling, sample_rate=16000,
        speech_threshold=0.5, silence_chunks_to_end=10,
    )
    await fsm.on_chunk(speech_prob=0.1)
    signaling.send_utterance_start.assert_not_awaited()
    await fsm.on_chunk(speech_prob=0.8)
    signaling.send_utterance_start.assert_awaited_once_with(sample_rate=16000)


@pytest.mark.asyncio
async def test_end_fires_after_silence_window():
    signaling = MagicMock()
    signaling.send_utterance_start = AsyncMock()
    signaling.send_utterance_end = AsyncMock()
    fsm = UtteranceStateMachine(
        signaling=signaling, sample_rate=16000,
        speech_threshold=0.5, silence_chunks_to_end=3,
    )
    await fsm.on_chunk(speech_prob=0.9)  # start
    # 2 silence chunks: still in utterance
    await fsm.on_chunk(speech_prob=0.1)
    await fsm.on_chunk(speech_prob=0.1)
    signaling.send_utterance_end.assert_not_awaited()
    # 3rd silence chunk: end
    await fsm.on_chunk(speech_prob=0.1)
    signaling.send_utterance_end.assert_awaited_once()


@pytest.mark.asyncio
async def test_silence_resets_during_utterance():
    """Speech during silence window cancels the pending end."""
    signaling = MagicMock()
    signaling.send_utterance_start = AsyncMock()
    signaling.send_utterance_end = AsyncMock()
    fsm = UtteranceStateMachine(
        signaling=signaling, sample_rate=16000,
        speech_threshold=0.5, silence_chunks_to_end=3,
    )
    await fsm.on_chunk(speech_prob=0.9)  # start
    await fsm.on_chunk(speech_prob=0.1)  # silence 1
    await fsm.on_chunk(speech_prob=0.9)  # speech again → reset
    await fsm.on_chunk(speech_prob=0.1)
    await fsm.on_chunk(speech_prob=0.1)
    signaling.send_utterance_end.assert_not_awaited()
    await fsm.on_chunk(speech_prob=0.1)
    signaling.send_utterance_end.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_only_fires_once_per_utterance():
    """Speech probability staying high doesn't refire start."""
    signaling = MagicMock()
    signaling.send_utterance_start = AsyncMock()
    signaling.send_utterance_end = AsyncMock()
    fsm = UtteranceStateMachine(
        signaling=signaling, sample_rate=16000,
        speech_threshold=0.5, silence_chunks_to_end=10,
    )
    for _ in range(5):
        await fsm.on_chunk(speech_prob=0.9)
    signaling.send_utterance_start.assert_awaited_once()
