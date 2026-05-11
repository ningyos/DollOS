"""BridgeSignaling — WS connect + aiortc client peer with mocked aiortc."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_signaling_sends_offer_and_handles_answer():
    from dollos.voice.bridge.signaling import BridgeSignaling

    # Fake WS connection.
    ws_send_calls: list = []
    incoming_messages: list = [
        json.dumps({"type": "webrtc_answer", "sdp": "answer-sdp"}),
    ]

    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock(side_effect=lambda d: ws_send_calls.append(json.loads(d)))

    async def _recv():
        if incoming_messages:
            return incoming_messages.pop(0)
        await asyncio.sleep(10)
        return ""  # never reached

    fake_ws.recv = AsyncMock(side_effect=_recv)

    fake_peer = MagicMock()
    fake_peer.createOffer = AsyncMock(return_value=MagicMock(sdp="offer-sdp"))
    fake_peer.setLocalDescription = AsyncMock()
    fake_peer.localDescription = MagicMock(sdp="offer-sdp")
    fake_peer.setRemoteDescription = AsyncMock()
    fake_peer.addIceCandidate = AsyncMock()
    fake_peer.close = AsyncMock()
    fake_peer.addTrack = MagicMock()
    fake_peer.on = MagicMock()
    fake_peer.addTransceiver = MagicMock()

    with patch("dollos.voice.bridge.signaling.RTCPeerConnection", return_value=fake_peer):
        sig = BridgeSignaling(ws=fake_ws)
        local_track = MagicMock()
        on_remote_track = AsyncMock()
        await sig.connect(local_audio_track=local_track, on_remote_track=on_remote_track)
        # Verify the offer was sent.
        assert any(m["type"] == "webrtc_offer" for m in ws_send_calls)
        offer = next(m for m in ws_send_calls if m["type"] == "webrtc_offer")
        assert offer["sdp"] == "offer-sdp"
        fake_peer.setRemoteDescription.assert_awaited()
        await sig.close()
        fake_peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_signaling_send_utterance_markers():
    from dollos.voice.bridge.signaling import BridgeSignaling

    ws_send_calls: list = []
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock(side_effect=lambda d: ws_send_calls.append(json.loads(d)))

    sig = BridgeSignaling(ws=fake_ws)
    await sig.send_utterance_start(sample_rate=16000)
    await sig.send_utterance_end()

    types = [m["type"] for m in ws_send_calls]
    assert types == ["utterance_start", "utterance_end"]
    assert ws_send_calls[0]["sample_rate"] == 16000


@pytest.mark.asyncio
async def test_signaling_routes_ice_candidate_from_server():
    """Incoming ice_candidate messages are added to the peer."""
    from dollos.voice.bridge.signaling import BridgeSignaling

    incoming = [
        json.dumps({"type": "webrtc_answer", "sdp": "answer-sdp"}),
        json.dumps({
            "type": "ice_candidate",
            "candidate": "candidate:1 1 udp 2122 1.2.3.4 50000 typ host",
            "sdpMid": "0", "sdpMLineIndex": 0,
        }),
    ]
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()

    async def _recv():
        if incoming:
            return incoming.pop(0)
        await asyncio.sleep(10)
        return ""

    fake_ws.recv = AsyncMock(side_effect=_recv)

    fake_peer = MagicMock()
    fake_peer.createOffer = AsyncMock(return_value=MagicMock(sdp="offer-sdp"))
    fake_peer.setLocalDescription = AsyncMock()
    fake_peer.localDescription = MagicMock(sdp="offer-sdp")
    fake_peer.setRemoteDescription = AsyncMock()
    fake_peer.addIceCandidate = AsyncMock()
    fake_peer.close = AsyncMock()
    fake_peer.addTrack = MagicMock()
    fake_peer.on = MagicMock()
    fake_peer.addTransceiver = MagicMock()

    with patch("dollos.voice.bridge.signaling.RTCPeerConnection", return_value=fake_peer):
        sig = BridgeSignaling(ws=fake_ws)
        await sig.connect(local_audio_track=MagicMock(), on_remote_track=AsyncMock())
        # Give the recv loop one tick.
        await asyncio.sleep(0.1)
        fake_peer.addIceCandidate.assert_awaited()
        await sig.close()
