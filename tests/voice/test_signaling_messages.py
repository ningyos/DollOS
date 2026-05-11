"""WebRTC signaling + utterance markers — schema round-trip."""
from __future__ import annotations

import json

import pytest

from dollos.ipc.messages import (
    decode_client_message,
    encode_server_message,
    WebRTCOfferIn,
    WebRTCAnswerOut,
    ICECandidateIn,
    ICECandidateOut,
    UtteranceStart,
    UtteranceEnd,
)


def test_decode_webrtc_offer():
    raw = json.dumps({"type": "webrtc_offer", "sdp": "v=0\r\n..."})
    msg = decode_client_message(raw)
    assert isinstance(msg, WebRTCOfferIn)
    assert msg.sdp.startswith("v=0")


def test_decode_ice_candidate_in():
    raw = json.dumps({
        "type": "ice_candidate",
        "candidate": "candidate:1 1 udp 2122 192.168.0.1 50000 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    })
    msg = decode_client_message(raw)
    assert isinstance(msg, ICECandidateIn)
    assert msg.sdpMLineIndex == 0


def test_decode_utterance_start_end():
    s = decode_client_message(json.dumps({"type": "utterance_start", "sample_rate": 16000}))
    assert isinstance(s, UtteranceStart)
    assert s.sample_rate == 16000
    e = decode_client_message(json.dumps({"type": "utterance_end"}))
    assert isinstance(e, UtteranceEnd)


def test_encode_webrtc_answer():
    msg = WebRTCAnswerOut(sdp="v=0\r\n...")
    s = encode_server_message(msg)
    parsed = json.loads(s)
    assert parsed["type"] == "webrtc_answer"
    assert parsed["sdp"].startswith("v=0")


def test_encode_ice_candidate_out():
    msg = ICECandidateOut(
        candidate="candidate:2 1 udp 2122 1.2.3.4 50001 typ host",
        sdpMid="0",
        sdpMLineIndex=0,
    )
    parsed = json.loads(encode_server_message(msg))
    assert parsed["type"] == "ice_candidate"
    assert parsed["sdpMid"] == "0"


def test_decode_unknown_type_raises():
    with pytest.raises(ValueError):
        decode_client_message(json.dumps({"type": "bogus"}))
