"""VoiceSession — per-WS-client WebRTC peer + ASR + TTS orchestration.

Lifecycle:
    1. Construct with asr/tts engines + on_user_text callback (fires
       when ASR transcribes an inbound utterance).
    2. handle_offer(sdp): create aiortc RTCPeerConnection, attach our
       outbound audio track + inbound audio track listener, exchange
       SDP, return answer SDP.
    3. handle_ice_candidate / handle_utterance_start / handle_utterance_end:
       per-client signaling messages.
    4. speak(text): TTS the text, push frames into outbound track.
    5. close(): drop peer, release engine resources.

Tests mock aiortc — the orchestration logic is what we own; the
peer-connection machinery is delegated.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiortc import (
    MediaStreamTrack,
    RTCIceCandidate,
    RTCPeerConnection,
    RTCSessionDescription,
)

from dollos.voice.codec import (
    audio_frame_from_pcm,
    pcm_from_audio_frame,
    resample_pcm_int16,
)

if TYPE_CHECKING:
    from dollos.voice.engines import ASREngine, TTSEngine

logger = logging.getLogger(__name__)


_ASR_RATE = 16000  # what we feed the ASR engine
_OUT_FRAME_MS = 20  # 20ms outbound frames


class _OutboundAudioTrack(MediaStreamTrack):
    """A MediaStreamTrack that emits AudioFrames from an asyncio.Queue.

    aiortc reads frames via `recv()`. We feed PCM via push_pcm; the
    track converts to AudioFrame and exposes via recv().
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._frames: asyncio.Queue = asyncio.Queue()

    async def push_pcm(self, pcm_chunk: bytes, sample_rate: int) -> None:
        frame = audio_frame_from_pcm(pcm_chunk, sample_rate=sample_rate)
        await self._frames.put(frame)

    async def recv(self):
        return await self._frames.get()


class VoiceSession:
    """Per-WS-client voice session."""

    def __init__(
        self,
        *,
        asr: "ASREngine",
        tts: "TTSEngine",
        on_user_text: Callable[[str], Awaitable[None]],
    ) -> None:
        self._asr = asr
        self._tts = tts
        self._on_user_text = on_user_text
        self._peer: RTCPeerConnection | None = None
        self._outbound_track: _OutboundAudioTrack | None = None
        self._utterance_buffer: list[bytes] = []
        self._utterance_rate: int = 16000
        self._inbound_consumer_task: asyncio.Task | None = None
        self._is_open: bool = False

    @property
    def peer(self) -> RTCPeerConnection | None:
        return self._peer

    @property
    def is_open(self) -> bool:
        return self._is_open

    async def handle_offer(self, sdp: str) -> str:
        """Process a webrtc_offer; return the SDP answer."""
        self._peer = RTCPeerConnection()
        self._outbound_track = _OutboundAudioTrack()
        self._peer.addTrack(self._outbound_track)

        @self._peer.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._inbound_consumer_task = asyncio.create_task(
                    self._consume_inbound(track), name="voice-inbound",
                )

        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self._peer.setRemoteDescription(offer)
        answer = await self._peer.createAnswer()
        await self._peer.setLocalDescription(answer)
        self._is_open = True
        return self._peer.localDescription.sdp

    async def handle_ice_candidate(
        self, *, candidate: str, sdpMid: str | None, sdpMLineIndex: int | None,
    ) -> None:
        if self._peer is None:
            logger.warning("ice_candidate received before peer; ignoring")
            return
        cand = _parse_ice_candidate_string(candidate, sdpMid, sdpMLineIndex)
        await self._peer.addIceCandidate(cand)

    async def handle_utterance_start(self, *, sample_rate: int) -> None:
        self._utterance_buffer.clear()
        self._utterance_rate = sample_rate

    async def handle_utterance_end(self) -> None:
        if not self._utterance_buffer:
            return
        pcm = b"".join(self._utterance_buffer)
        self._utterance_buffer.clear()
        # Resample to ASR-preferred rate if needed.
        if self._utterance_rate != _ASR_RATE:
            pcm = resample_pcm_int16(
                pcm, src_rate=self._utterance_rate, dst_rate=_ASR_RATE,
            )
            sr = _ASR_RATE
        else:
            sr = self._utterance_rate
        text = await self._asr.transcribe(pcm, sample_rate=sr)
        if text:
            await self._on_user_text(text)

    async def speak(self, text: str) -> None:
        """Run TTS, push PCM chunks to the outbound track."""
        async for chunk in self._tts.synthesize(text):
            await self._push_outbound(chunk, self._tts.sample_rate)

    async def _push_outbound(self, pcm_chunk: bytes, sample_rate: int) -> None:
        if self._outbound_track is None:
            logger.warning("speak() with no outbound track; dropping audio")
            return
        await self._outbound_track.push_pcm(pcm_chunk, sample_rate)

    async def _consume_inbound(self, track: MediaStreamTrack) -> None:
        """Read inbound AudioFrames; append to utterance buffer (between
        utterance_start/end markers managed by handle_utterance_*)."""
        try:
            while True:
                frame = await track.recv()
                pcm = pcm_from_audio_frame(frame)
                # Resample to the rate the client declared.
                if frame.sample_rate != self._utterance_rate:
                    pcm = resample_pcm_int16(
                        pcm,
                        src_rate=frame.sample_rate,
                        dst_rate=self._utterance_rate,
                    )
                self._utterance_buffer.append(pcm)
        except Exception:
            logger.debug("inbound consumer ended")

    async def close(self) -> None:
        self._is_open = False
        if self._inbound_consumer_task is not None:
            self._inbound_consumer_task.cancel()
            try:
                await self._inbound_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._peer is not None:
            try:
                await self._peer.close()
            except Exception:
                logger.exception("peer.close raised")
            self._peer = None
        try:
            await self._asr.aclose()
        except Exception:
            logger.exception("asr.aclose raised")
        try:
            await self._tts.aclose()
        except Exception:
            logger.exception("tts.aclose raised")


def _parse_ice_candidate_string(
    candidate: str, sdpMid: str | None, sdpMLineIndex: int | None,
) -> RTCIceCandidate:
    """Parse the SDP-style candidate string into an RTCIceCandidate.

    aiortc 1.14.0 exposes a parser at aiortc.sdp; strip the 'candidate:'
    prefix before passing to candidate_from_sdp.
    """
    from aiortc.sdp import candidate_from_sdp
    cand = candidate_from_sdp(candidate.replace("candidate:", "", 1))
    cand.sdpMid = sdpMid
    cand.sdpMLineIndex = sdpMLineIndex
    return cand
