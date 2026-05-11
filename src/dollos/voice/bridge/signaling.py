"""BridgeSignaling — client-side WebRTC negotiation over the daemon's WS.

The bridge is the offerer: builds an RTCPeerConnection, attaches local
mic track, opens a recv-only transceiver for the daemon's outbound
track, createOffer → send to daemon → setRemote(answer) → exchange ICE
candidates → audio flows.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Optional

from aiortc import (
    MediaStreamTrack,
    RTCIceCandidate,
    RTCPeerConnection,
    RTCSessionDescription,
)

logger = logging.getLogger(__name__)


class BridgeSignaling:
    def __init__(self, *, ws) -> None:
        self._ws = ws
        self._peer: Optional[RTCPeerConnection] = None
        self._recv_loop_task: Optional[asyncio.Task] = None
        self._on_remote_track: Optional[Callable[[MediaStreamTrack], Awaitable[None]]] = None

    async def connect(
        self,
        *,
        local_audio_track: MediaStreamTrack,
        on_remote_track: Callable[[MediaStreamTrack], Awaitable[None]],
    ) -> None:
        self._on_remote_track = on_remote_track
        self._peer = RTCPeerConnection()
        self._peer.addTrack(local_audio_track)
        self._peer.addTransceiver("audio", direction="recvonly")

        @self._peer.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                asyncio.create_task(self._on_remote_track(track))

        @self._peer.on("icecandidate")
        async def _on_ice(event) -> None:
            cand = getattr(event, "candidate", None)
            if cand is None:
                return
            cand_str = cand.candidate
            if not cand_str.startswith("candidate:"):
                cand_str = "candidate:" + cand_str
            await self._send_json({
                "type": "ice_candidate",
                "candidate": cand_str,
                "sdpMid": cand.sdpMid,
                "sdpMLineIndex": cand.sdpMLineIndex,
            })

        offer = await self._peer.createOffer()
        await self._peer.setLocalDescription(offer)
        await self._send_json({"type": "webrtc_offer", "sdp": self._peer.localDescription.sdp})
        self._recv_loop_task = asyncio.create_task(self._recv_loop(), name="bridge-recv-loop")
        # Yield control so the recv loop can process the first message (e.g.
        # webrtc_answer) before connect() returns to the caller.
        await asyncio.sleep(0)

    async def _recv_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "webrtc_answer":
                    answer = RTCSessionDescription(sdp=msg["sdp"], type="answer")
                    await self._peer.setRemoteDescription(answer)
                elif t == "ice_candidate":
                    cand = _parse_ice_candidate_string(
                        msg["candidate"],
                        msg.get("sdpMid"),
                        msg.get("sdpMLineIndex"),
                    )
                    await self._peer.addIceCandidate(cand)
                else:
                    logger.debug("bridge recv: ignoring %s", t)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bridge recv loop ended")

    async def send_utterance_start(self, *, sample_rate: int) -> None:
        await self._send_json({"type": "utterance_start", "sample_rate": sample_rate})

    async def send_utterance_end(self) -> None:
        await self._send_json({"type": "utterance_end"})

    async def send_text_input(self, text: str) -> None:
        """Optional: send text directly when bridge user types instead of speaks."""
        await self._send_json({"type": "text_input", "text": text})

    async def _send_json(self, payload: dict) -> None:
        await self._ws.send(json.dumps(payload))

    async def close(self) -> None:
        if self._recv_loop_task is not None:
            self._recv_loop_task.cancel()
            try:
                await self._recv_loop_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._peer is not None:
            try:
                await self._peer.close()
            except Exception:
                logger.exception("peer.close raised")
            self._peer = None


def _parse_ice_candidate_string(
    candidate: str, sdpMid: str | None, sdpMLineIndex: int | None,
) -> RTCIceCandidate:
    from aiortc.sdp import candidate_from_sdp
    cand = candidate_from_sdp(candidate.replace("candidate:", "", 1))
    cand.sdpMid = sdpMid
    cand.sdpMLineIndex = sdpMLineIndex
    return cand
