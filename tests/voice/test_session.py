"""VoiceSession unit tests with mocked aiortc + mocked engines.

aiortc's network behavior is not exercised here — it's well-tested
upstream. We verify the orchestration logic: offer/answer dance,
utterance buffer, ASR fire, TTS push.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dollos.voice.session import VoiceSession


def _mock_asr():
    asr = MagicMock()
    asr.transcribe = AsyncMock(return_value="hello world")
    asr.aclose = AsyncMock()
    return asr


def _mock_tts():
    tts = MagicMock()
    tts.sample_rate = 48000

    async def _gen(text):
        # 20ms of silence @ 48k = 960 samples × 2 bytes = 1920 bytes
        for _ in range(3):
            yield b"\x00" * 1920

    tts.synthesize = _gen
    tts.aclose = AsyncMock()
    return tts


@pytest.mark.asyncio
async def test_session_construction_no_peer_yet():
    asr = _mock_asr()
    tts = _mock_tts()
    on_user_text = AsyncMock()
    s = VoiceSession(asr=asr, tts=tts, on_user_text=on_user_text)
    assert s.peer is None
    assert s.is_open is False
    await s.close()


@pytest.mark.asyncio
async def test_session_handle_offer_creates_peer_and_returns_answer():
    asr = _mock_asr()
    tts = _mock_tts()
    on_user_text = AsyncMock()
    s = VoiceSession(asr=asr, tts=tts, on_user_text=on_user_text)

    # Stub the RTCPeerConnection used inside session.py.
    fake_peer = MagicMock()
    fake_peer.setRemoteDescription = AsyncMock()
    fake_peer.createAnswer = AsyncMock(return_value=MagicMock(sdp="answer-sdp"))
    fake_peer.setLocalDescription = AsyncMock()
    fake_peer.localDescription = MagicMock(sdp="answer-sdp")
    fake_peer.addIceCandidate = AsyncMock()
    fake_peer.close = AsyncMock()
    fake_peer.addTrack = MagicMock()
    fake_peer.on = MagicMock()

    with patch("dollos.voice.session.RTCPeerConnection", return_value=fake_peer):
        answer = await s.handle_offer("offer-sdp")
    assert answer == "answer-sdp"
    assert s.peer is fake_peer
    assert s.is_open
    fake_peer.setRemoteDescription.assert_awaited_once()
    fake_peer.createAnswer.assert_awaited_once()
    fake_peer.setLocalDescription.assert_awaited_once()
    await s.close()
    fake_peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_handle_ice_candidate_routes_to_peer():
    s = VoiceSession(asr=_mock_asr(), tts=_mock_tts(), on_user_text=AsyncMock())
    fake_peer = MagicMock()
    fake_peer.addIceCandidate = AsyncMock()
    s._peer = fake_peer
    await s.handle_ice_candidate(
        candidate="candidate:1 1 udp 2122 192.168.0.1 50000 typ host",
        sdpMid="0",
        sdpMLineIndex=0,
    )
    fake_peer.addIceCandidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_utterance_collects_then_transcribes():
    asr = _mock_asr()
    tts = _mock_tts()
    on_user_text = AsyncMock()
    s = VoiceSession(asr=asr, tts=tts, on_user_text=on_user_text)
    await s.handle_utterance_start(sample_rate=16000)
    s._utterance_buffer.append(b"\x00\x10" * 16000)  # 1s of audio
    s._utterance_buffer.append(b"\x00\x10" * 16000)
    await s.handle_utterance_end()
    asr.transcribe.assert_awaited_once()
    args, kwargs = asr.transcribe.call_args
    # Audio fed at 16 kHz with 2 seconds of samples.
    assert kwargs.get("sample_rate", args[1] if len(args) > 1 else None) == 16000
    on_user_text.assert_awaited_once_with("hello world")
    await s.close()


@pytest.mark.asyncio
async def test_session_speak_pushes_pcm_to_outbound_track():
    s = VoiceSession(asr=_mock_asr(), tts=_mock_tts(), on_user_text=AsyncMock())
    pushed = []

    async def _capture(pcm_chunk: bytes, sample_rate: int):
        pushed.append((pcm_chunk, sample_rate))

    s._push_outbound = _capture  # type: ignore[method-assign]
    await s.speak("hi")
    assert len(pushed) == 3
    assert all(len(p[0]) == 1920 for p in pushed)
    assert all(p[1] == 48000 for p in pushed)
    await s.close()


@pytest.mark.asyncio
async def test_session_close_releases_engines():
    asr = _mock_asr()
    tts = _mock_tts()
    s = VoiceSession(asr=asr, tts=tts, on_user_text=AsyncMock())
    await s.close()
    asr.aclose.assert_awaited_once()
    tts.aclose.assert_awaited_once()
    assert not s.is_open
