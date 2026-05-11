# Voice Pipeline Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Phase A's voice engines into the live daemon over WebRTC. A WS client sends a `webrtc_offer`, daemon spins up a per-connection `VoiceSession` (aiortc peer + ASR + TTS engines from the active character pack), bidirectional audio flows. Doll's `Say` output transparently feeds TTS into the outbound audio track. Inbound utterance audio (bounded by client `utterance_start` / `utterance_end` markers) runs through ASR and fires `UserTextEvent` into the dispatcher.

**Architecture:**
- New `src/dollos/voice/session.py` — `VoiceSession` class per WS client. Wraps `aiortc.RTCPeerConnection`, manages inbound utterance buffer + ASR call + outbound track push + TTS streaming.
- New `src/dollos/voice/codec.py` — PCM ↔ `aiortc.MediaStreamTrack` AudioFrame helpers + scipy-based 48k ↔ 16k resample.
- Extended `src/dollos/ipc/messages.py` — `WebRTCOffer / WebRTCAnswer / ICECandidate / UtteranceStart / UtteranceEnd` schemas added to ClientMessage + ServerMessage discriminators.
- `src/dollos/ipc/server.py` — `_on_connect` passes sink to `on_disconnect` so per-connection state can be cleaned up; binary frames remain unsupported (audio rides aiortc, not WS).
- `src/dollos/kernel.py` — adds a `voice_sessions: dict[id(sink), VoiceSession]` map; on first `webrtc_offer` from a connection, lazily builds the session from the character pack's voice config; on disconnect, closes + removes.
- New `TTSObservingSink(asyncio.Queue)` in `src/dollos/voice/sink.py` — sink subclass that triggers TTS as a side effect of `put_nowait(TextChunk(...))`. Kernel uses this subclass for every WS connection's sink so Say transparently speaks (when a voice session exists).

**Tech Stack:** Python 3.13, asyncio, aiortc (WebRTC), pydantic, scipy.signal.resample_poly, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-11-voice-pipeline-design.md` — see "Architecture", "Signal flow", "Per-client VoiceSession lifecycle", "Sink interceptor", "WS signaling schema additions".

**Phase placement:** Phase B of 3. Builds on Phase A engines (already merged, step 26). Phase C adds the local-audio-bridge client + live E2E smoke.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add `aiortc>=1.9`, `scipy>=1.13` deps. |
| `src/dollos/voice/codec.py` | Create | PCM byte ↔ `aiortc.AudioFrame` conversion; resample helpers. |
| `src/dollos/voice/session.py` | Create | `VoiceSession` class — owns peer + engines + audio routing for one WS client. |
| `src/dollos/voice/sink.py` | Create | `TTSObservingSink` — Queue subclass that triggers TTS on TextChunk put. |
| `src/dollos/ipc/messages.py` | Modify | Add WebRTC signaling + utterance message schemas. |
| `src/dollos/ipc/server.py` | Modify | `on_disconnect` hook receives the sink (so kernel can clean up per-connection state). |
| `src/dollos/kernel.py` | Modify | Build voice engines from character pack; per-connection VoiceSession map; route signaling messages; substitute TTSObservingSink for the connection sink. |
| `src/dollos/dispatcher.py` | (no change expected) | Sink type widened to `asyncio.Queue` superclass; TTSObservingSink works transparently. |
| `tests/voice/test_codec.py` | Create | PCM round-trip, resample correctness. |
| `tests/voice/test_session.py` | Create | VoiceSession with mocked aiortc peer + mocked engines; covers offer/answer/ice/utterance flow + close. |
| `tests/voice/test_sink.py` | Create | TTSObservingSink fires TTS on TextChunk; passes others through. |
| `tests/voice/test_signaling_messages.py` | Create | Round-trip schema validation for the new message types. |
| `tests/test_ipc.py` | Modify (if exists) | `on_disconnect` receives sink. (If file does not exist, behavior is exercised by the kernel integration test below.) |
| `tests/test_kernel.py` | Modify | New tests: signaling route → VoiceSession; engine build from character pack voice config. |
| `tests/test_e2e.py` | Modify | New E2E test: WS client sends offer → daemon answers, mocked aiortc + mocked engines, full round-trip. |
| `docs/roadmap.md` | Modify | Add step 27 entry. |
| `CLAUDE.md` | Modify | Update completed table + 下一個. |

---

## Task 1: Add aiortc + scipy dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add deps**

In `pyproject.toml` under `[project] dependencies = [...]`, append:

```toml
"aiortc>=1.9",
"scipy>=1.13",
```

- [ ] **Step 2: Sync**

```bash
uv sync
```

Expected: installs aiortc (pulls cryptography, pyee, av, pylibsrtp, aioice, dnspython, google-crc32c) + scipy. No conflicts.

If aiortc native deps fail (libopus / libsrtp / libavcodec missing on host), STOP and report. Linux apt names: `libopus-dev libsrtp2-dev libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev libavutil-dev libswresample-dev libswscale-dev`.

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "
import aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.mediastreams import AudioStreamTrack
import scipy.signal
print('aiortc', aiortc.__version__, 'scipy ok')
"
```

Expected: prints version, no errors.

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 354 passed (unchanged).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add aiortc + scipy for voice pipeline Phase B"
```

---

## Task 2: Codec helpers (PCM ↔ AudioFrame, resample)

**Files:**
- Create: `src/dollos/voice/codec.py`
- Test: `tests/voice/test_codec.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/test_codec.py`:

```python
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
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest tests/voice/test_codec.py -v
```

- [ ] **Step 3: Implement codec.py**

`src/dollos/voice/codec.py`:

```python
"""PCM ↔ av.AudioFrame conversion + resample helpers for the voice pipeline.

aiortc tracks pass av.AudioFrame objects. Engines emit raw int16 PCM
bytes. This module bridges the two and resamples between the WebRTC
48 kHz default and the ASR-preferred 16 kHz.
"""
from __future__ import annotations

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
    # Find an integer ratio reduction.
    from math import gcd
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

    If the frame is not s16/mono, downmix + convert.
    """
    if frame.format.name == "s16" and frame.layout.name == "mono":
        return frame.to_ndarray().tobytes()
    # Fallback: convert via av's reformatter.
    array = frame.to_ndarray()
    if array.ndim == 2 and array.shape[0] > 1:
        # Downmix multi-channel to mono by averaging.
        array = array.mean(axis=0, keepdims=True).astype(array.dtype)
    if array.dtype != np.int16:
        # Convert float to int16 with clip.
        if np.issubdtype(array.dtype, np.floating):
            array = np.clip(array * 32767.0, -32768, 32767).astype(np.int16)
        else:
            array = array.astype(np.int16)
    return array.tobytes()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_codec.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 359 passed (354 + 5).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/codec.py tests/voice/test_codec.py
git commit -m "feat(voice): codec helpers — PCM ↔ AudioFrame + scipy resample"
```

---

## Task 3: Signaling message schemas

**Files:**
- Modify: `src/dollos/ipc/messages.py`
- Test: `tests/voice/test_signaling_messages.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/test_signaling_messages.py`:

```python
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
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest tests/voice/test_signaling_messages.py -v
```

- [ ] **Step 3: Extend messages.py**

In `src/dollos/ipc/messages.py`, add NEW classes alongside existing ones:

```python
# ===== Client → Server (additions) =====

class WebRTCOfferIn(BaseModel):
    type: Literal["webrtc_offer"] = "webrtc_offer"
    sdp: str


class ICECandidateIn(BaseModel):
    type: Literal["ice_candidate"] = "ice_candidate"
    candidate: str
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None


class UtteranceStart(BaseModel):
    type: Literal["utterance_start"] = "utterance_start"
    sample_rate: int


class UtteranceEnd(BaseModel):
    type: Literal["utterance_end"] = "utterance_end"
```

Update the existing `ClientMessage` union to include the new types:

```python
ClientMessage = Annotated[
    TextInput | WebRTCOfferIn | ICECandidateIn | UtteranceStart | UtteranceEnd,
    Field(discriminator="type"),
]
```

Add server-side variants:

```python
# ===== Server → Client (additions) =====

class WebRTCAnswerOut(BaseModel):
    type: Literal["webrtc_answer"] = "webrtc_answer"
    sdp: str


class ICECandidateOut(BaseModel):
    type: Literal["ice_candidate"] = "ice_candidate"
    candidate: str
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None
```

Update `ServerMessage`:

```python
ServerMessage = Annotated[
    TextChunk | TurnEnd | ErrorMsg | WebRTCAnswerOut | ICECandidateOut,
    Field(discriminator="type"),
]
```

Rebuild the type adapter line if not auto-rebuilt:

```python
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_signaling_messages.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 365 passed (359 + 6).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/ipc/messages.py tests/voice/test_signaling_messages.py
git commit -m "feat(ipc): WebRTC + utterance signaling message schemas"
```

---

## Task 4: TTSObservingSink

**Files:**
- Create: `src/dollos/voice/sink.py`
- Test: `tests/voice/test_sink.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/test_sink.py`:

```python
"""TTSObservingSink — Queue subclass that fires TTS on TextChunk put."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dollos.ipc.messages import TextChunk, TurnEnd, ErrorMsg
from dollos.voice.sink import TTSObservingSink


@pytest.mark.asyncio
async def test_sink_fires_tts_on_text_chunk():
    session = MagicMock()
    session.speak = AsyncMock()
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TextChunk(text="hello"))
    # Yield to let the scheduled task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.speak.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_sink_skips_tts_when_no_session():
    sink = TTSObservingSink(voice_session_provider=lambda: None)
    sink.put_nowait(TextChunk(text="hello"))
    # Should not crash; nothing else to assert structurally — just that
    # the put_nowait succeeded.
    item = await sink.get()
    assert isinstance(item, TextChunk)


@pytest.mark.asyncio
async def test_sink_passes_non_text_chunks_through():
    session = MagicMock()
    session.speak = AsyncMock()
    sink = TTSObservingSink(voice_session_provider=lambda: session)
    sink.put_nowait(TurnEnd())
    sink.put_nowait(ErrorMsg(message="x"))
    sink.put_nowait(None)
    items = [await sink.get() for _ in range(3)]
    assert isinstance(items[0], TurnEnd)
    assert isinstance(items[1], ErrorMsg)
    assert items[2] is None
    await asyncio.sleep(0)
    session.speak.assert_not_called()


@pytest.mark.asyncio
async def test_sink_acts_as_normal_queue():
    sink = TTSObservingSink(voice_session_provider=lambda: None)
    sink.put_nowait(TextChunk(text="a"))
    sink.put_nowait(TextChunk(text="b"))
    a = await sink.get()
    b = await sink.get()
    assert a.text == "a"
    assert b.text == "b"
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest tests/voice/test_sink.py -v
```

- [ ] **Step 3: Implement sink.py**

`src/dollos/voice/sink.py`:

```python
"""TTSObservingSink — a Queue subclass that triggers TTS as a side
effect of `put_nowait(TextChunk(...))`.

The sink is otherwise a plain asyncio.Queue: items still flow to the
IPC pump unchanged. The voice session reference is fetched lazily via
a provider callable so sessions can be attached and detached without
re-wiring the sink at the call sites.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from dollos.ipc.messages import TextChunk, ServerMessage

if TYPE_CHECKING:
    from dollos.voice.session import VoiceSession

logger = logging.getLogger(__name__)


class TTSObservingSink(asyncio.Queue):
    """asyncio.Queue subclass that fires `voice_session.speak(text)` whenever
    a `TextChunk` is put into the queue. Other items pass through unchanged.
    """

    def __init__(
        self,
        *,
        voice_session_provider: Callable[[], "VoiceSession | None"],
        maxsize: int = 0,
    ) -> None:
        super().__init__(maxsize=maxsize)
        self._voice_session_provider = voice_session_provider

    def put_nowait(self, item: Any) -> None:
        super().put_nowait(item)
        if isinstance(item, TextChunk):
            session = self._voice_session_provider()
            if session is not None:
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(session.speak(item.text))
                except RuntimeError:
                    logger.warning(
                        "TTSObservingSink: TextChunk put_nowait outside event "
                        "loop; TTS not scheduled (text=%r)", item.text,
                    )
                except Exception:
                    logger.exception("scheduling speak() failed")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_sink.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 369 passed (365 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/sink.py tests/voice/test_sink.py
git commit -m "feat(voice): TTSObservingSink — fires TTS on TextChunk put"
```

---

## Task 5: VoiceSession skeleton + mocked tests

**Files:**
- Create: `src/dollos/voice/session.py`
- Test: `tests/voice/test_session.py`

- [ ] **Step 1: Write tests (mocked aiortc + mocked engines)**

`tests/voice/test_session.py`:

```python
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
```

- [ ] **Step 2: Run, expect ImportError**

```bash
uv run pytest tests/voice/test_session.py -v
```

- [ ] **Step 3: Implement session.py**

`src/dollos/voice/session.py`:

```python
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
from aiortc.contrib.media import MediaPlayer  # noqa: F401 — kept for parity

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

    aiortc 1.9 exposes a parser at aiortc.sdp; fall back to manual fields
    if the parser is unavailable.
    """
    from aiortc.sdp import candidate_from_sdp
    cand = candidate_from_sdp(candidate.replace("candidate:", "", 1))
    cand.sdpMid = sdpMid
    cand.sdpMLineIndex = sdpMLineIndex
    return cand
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_session.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 375 passed (369 + 6).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/session.py tests/voice/test_session.py
git commit -m "feat(voice): VoiceSession — aiortc peer + engine orchestration"
```

---

## Task 6: IPC server — on_disconnect receives sink

**Files:**
- Modify: `src/dollos/ipc/server.py`
- Test: extend `tests/voice/test_ipc_disconnect.py` (new test file — there is no existing test_ipc.py)

- [ ] **Step 1: Write failing test**

`tests/voice/test_ipc_disconnect.py`:

```python
"""IPC server: on_disconnect hook now receives the sink."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from dollos.ipc.server import WebSocketServer
from dollos.ipc.messages import TextInput


@pytest.mark.asyncio
async def test_on_disconnect_receives_sink():
    captured_sink_on_connect = None
    captured_sink_on_disconnect = None

    async def on_connect(sink):
        nonlocal captured_sink_on_connect
        captured_sink_on_connect = sink

    async def on_disconnect(sink):
        nonlocal captured_sink_on_disconnect
        captured_sink_on_disconnect = sink

    async def handler(msg, sink):
        pass

    server = WebSocketServer(
        host="127.0.0.1", port=0, handler=handler,
        on_connect=on_connect, on_disconnect=on_disconnect,
    )
    await server.start()
    try:
        port = server.port
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            pass  # connect then disconnect immediately
        # Give the server a tick to run the disconnect hook.
        for _ in range(20):
            if captured_sink_on_disconnect is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert captured_sink_on_connect is not None
    assert captured_sink_on_disconnect is captured_sink_on_connect
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/voice/test_ipc_disconnect.py -v
```

Failure: `on_disconnect` doesn't currently take a sink argument.

- [ ] **Step 3: Update server.py**

In `src/dollos/ipc/server.py`:

Change the type alias:
```python
on_disconnect: (
    Callable[["asyncio.Queue[ServerMessage | None]"], Awaitable[None]]
    | None
) = None,
```

In `_on_connect`, change the disconnect hook call:
```python
if self._on_disconnect_hook is not None:
    try:
        await self._on_disconnect_hook(sink)
    except Exception:
        logger.exception("on_disconnect hook failed")
```

Other existing callers (`kernel._handle_disconnect`) need their signature updated — that's part of Task 8.

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/voice/test_ipc_disconnect.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Full suite (kernel test may break — that's expected; fix in Task 8)**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: a small number of failures in test_kernel.py / kernel-related tests because `_handle_disconnect` signature is now wrong. We'll fix those in Task 8. If failure count is more than ~5, STOP and investigate.

Actually we should fix kernel here in one move — the signature breakage propagates. Do this:

In `src/dollos/kernel.py`, find `_handle_disconnect`:
```python
async def _handle_disconnect(self) -> None:
```
Change to:
```python
async def _handle_disconnect(self, sink: "asyncio.Queue[ServerMessage | None]") -> None:
```
The current body uses no parameters, so simply accepting the new arg (and ignoring it for now) makes things green again.

- [ ] **Step 6: Run full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 376 passed (375 + 1).

- [ ] **Step 7: Commit**

```bash
git add src/dollos/ipc/server.py src/dollos/kernel.py tests/voice/test_ipc_disconnect.py
git commit -m "feat(ipc): on_disconnect hook receives the sink"
```

---

## Task 7: Kernel integration — engines + per-connection VoiceSession

**Files:**
- Modify: `src/dollos/kernel.py`
- Test: `tests/test_kernel.py`

- [ ] **Step 1: Write integration tests**

Append to `tests/test_kernel.py`:

```python
@pytest.mark.asyncio
async def test_kernel_builds_voice_engines_from_pack(tmp_path: Path, monkeypatch):
    """When character pack has voice/engine.toml, kernel loads engines."""
    from dollos.voice import engines as eng_mod
    from dollos.voice import pack as pack_mod
    from dollos.voice.engines import ASREngine, TTSEngine, register_asr, register_tts

    # Fake engines registered for this test.
    class _FakeASR(ASREngine):
        def __init__(self, **kw): pass
        async def transcribe(self, audio_pcm, sample_rate): return ""
        async def aclose(self): pass

    class _FakeTTS(TTSEngine):
        sample_rate = 48000
        def __init__(self, **kw): pass
        async def synthesize(self, text):
            yield b""
        async def aclose(self): pass

    eng_mod.ASR_REGISTRY["fake-asr"] = _FakeASR
    eng_mod.TTS_REGISTRY["fake-tts"] = _FakeTTS

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="t"\nname="T"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )
    voice_dir = pack_dir / "voice"
    voice_dir.mkdir()
    (voice_dir / "engine.toml").write_text(
        '[asr]\nengine="fake-asr"\n\n[tts]\nengine="fake-tts"\n'
    )

    # Build a kernel with this pack (similar to test_kernel patterns).
    # Implementation should expose a method like `_build_voice_engines(pack_dir)`
    # that returns (asr, tts) or raises if config missing.

    from dollos.kernel import build_voice_engines
    asr, tts = build_voice_engines(pack_dir, data_root=tmp_path / "data")
    assert isinstance(asr, _FakeASR)
    assert isinstance(tts, _FakeTTS)

    # Cleanup the test registrations.
    del eng_mod.ASR_REGISTRY["fake-asr"]
    del eng_mod.TTS_REGISTRY["fake-tts"]


@pytest.mark.asyncio
async def test_kernel_no_voice_when_pack_has_none(tmp_path: Path):
    from dollos.kernel import build_voice_engines

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="t"\nname="T"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )

    out = build_voice_engines(pack_dir, data_root=tmp_path / "data")
    assert out is None
```

- [ ] **Step 2: Run, expect AttributeError**

```bash
uv run pytest tests/test_kernel.py::test_kernel_builds_voice_engines_from_pack -v
```

- [ ] **Step 3: Implement `build_voice_engines` + VoiceSession lifecycle wiring**

In `src/dollos/kernel.py`:

1. Add imports:

```python
from dollos.voice.engines import ASR_REGISTRY, TTS_REGISTRY, ASREngine, TTSEngine
from dollos.voice.pack import load_voice_config
from dollos.voice.session import VoiceSession
from dollos.voice.sink import TTSObservingSink
```

2. Add module-level builder function:

```python
def build_voice_engines(
    pack_dir: Path, *, data_root: Path,
) -> tuple[ASREngine, TTSEngine] | None:
    """Construct ASR+TTS engines from a character pack's voice config.

    Returns None if the pack has no voice/engine.toml. Raises ValueError
    if the config references an unregistered engine.
    """
    cfg = load_voice_config(pack_dir)
    if cfg.asr is None or cfg.tts is None:
        return None

    asr_name = cfg.asr["engine"]
    if asr_name not in ASR_REGISTRY:
        raise ValueError(f"unknown ASR engine in voice/engine.toml: {asr_name!r}")
    tts_name = cfg.tts["engine"]
    if tts_name not in TTS_REGISTRY:
        raise ValueError(f"unknown TTS engine in voice/engine.toml: {tts_name!r}")

    asr_kwargs = {k: v for k, v in cfg.asr.items() if k != "engine"}
    asr_kwargs.setdefault("data_root", data_root)
    tts_kwargs = {k: v for k, v in cfg.tts.items() if k != "engine"}
    tts_kwargs.setdefault("data_root", data_root)

    asr = ASR_REGISTRY[asr_name](**asr_kwargs)
    tts = TTS_REGISTRY[tts_name](**tts_kwargs)
    return asr, tts
```

3. In `DollOS.__init__`, track voice sessions per sink:

```python
self._voice_sessions: dict[int, VoiceSession] = {}  # keyed by id(sink)
self._pack_dir = Path(settings.character.pack)
self._data_root = settings.data.root
```

4. Replace the existing sink construction during `_handle_connect`. Currently the server creates the sink inside `_on_connect`. We want each new sink to be a `TTSObservingSink`. Simplest: change `WebSocketServer._on_connect` to use a sink factory. The kernel passes its factory.

In `src/dollos/ipc/server.py`, change the constructor to accept an optional sink factory:

```python
sink_factory: Callable[[], "asyncio.Queue[ServerMessage | None]"] | None = None,
```

In `_on_connect`:
```python
sink: asyncio.Queue[ServerMessage | None] = (
    self._sink_factory() if self._sink_factory else asyncio.Queue()
)
```

5. In kernel, supply the factory:

```python
self.server = WebSocketServer(
    host=settings.ipc.host,
    port=settings.ipc.port,
    handler=self._handle_message,  # renamed; see below
    on_connect=self._handle_connect,
    on_disconnect=self._handle_disconnect,
    sink_factory=self._make_sink,
)


def _make_sink(self) -> "asyncio.Queue[ServerMessage | None]":
    """Build a TTSObservingSink that fetches the session at speak-time."""
    sink: TTSObservingSink = TTSObservingSink(
        voice_session_provider=lambda: self._voice_sessions.get(id(sink)),  # type: ignore[name-defined]
    )
    # The lambda above closes over `sink` BEFORE it's bound — Python
    # raises NameError. Use a placeholder pattern:
    holder: dict = {}
    sink = TTSObservingSink(
        voice_session_provider=lambda: self._voice_sessions.get(holder["id"]),
    )
    holder["id"] = id(sink)
    return sink
```

6. Update `_handle_message` (was `_handle_text_input`) to dispatch by message type:

```python
async def _handle_message(self, msg, sink) -> None:
    if isinstance(msg, TextInput):
        self.dispatcher.dispatch(
            UserTextEvent(text=msg.text, response_sink=sink)
        )
    elif isinstance(msg, WebRTCOfferIn):
        answer_sdp = await self._handle_offer(msg.sdp, sink)
        sink.put_nowait(WebRTCAnswerOut(sdp=answer_sdp))
    elif isinstance(msg, ICECandidateIn):
        session = self._voice_sessions.get(id(sink))
        if session is not None:
            await session.handle_ice_candidate(
                candidate=msg.candidate, sdpMid=msg.sdpMid, sdpMLineIndex=msg.sdpMLineIndex,
            )
    elif isinstance(msg, UtteranceStart):
        session = self._voice_sessions.get(id(sink))
        if session is not None:
            await session.handle_utterance_start(sample_rate=msg.sample_rate)
    elif isinstance(msg, UtteranceEnd):
        session = self._voice_sessions.get(id(sink))
        if session is not None:
            await session.handle_utterance_end()
    else:
        logger.warning("unhandled message type: %r", type(msg).__name__)


async def _handle_offer(self, offer_sdp: str, sink) -> str:
    engines = build_voice_engines(self._pack_dir, data_root=self._data_root)
    if engines is None:
        raise RuntimeError(
            "voice not configured for the active character pack; "
            f"missing {self._pack_dir}/voice/engine.toml"
        )
    asr, tts = engines

    async def _on_user_text(text: str) -> None:
        self.dispatcher.dispatch(
            UserTextEvent(text=text, response_sink=sink)
        )

    session = VoiceSession(asr=asr, tts=tts, on_user_text=_on_user_text)
    self._voice_sessions[id(sink)] = session
    return await session.handle_offer(offer_sdp)
```

7. Update `_handle_disconnect` to close + remove the session:

```python
async def _handle_disconnect(self, sink) -> None:
    session = self._voice_sessions.pop(id(sink), None)
    if session is not None:
        try:
            await session.close()
        except Exception:
            logger.exception("voice session close raised")
    if self._active_sink is sink:
        self._active_sink = None
```

8. Add the new imports at the top of kernel.py:

```python
from dollos.ipc.messages import (
    TextInput, WebRTCOfferIn, WebRTCAnswerOut, ICECandidateIn,
    UtteranceStart, UtteranceEnd,
)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_kernel.py -v
```

Expected: all pass (including 2 new + existing).

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 378 passed (376 + 2).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/kernel.py src/dollos/ipc/server.py tests/test_kernel.py
git commit -m "feat(kernel): per-connection VoiceSession + signaling dispatch"
```

---

## Task 8: E2E test — WS client offer → daemon answer (mocked aiortc + engines)

**Files:**
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Append E2E test**

```python
@pytest.mark.asyncio
async def test_voice_session_offer_answer_with_mocked_aiortc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A WS client sends webrtc_offer → daemon answers; aiortc is fully mocked.

    Verifies: signaling messages are routed, VoiceSession is created with
    the correct engines, answer SDP comes back over WS.
    """
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid="gura"\nname="Gura"\n[identity]\nself="x"\npersonality="x"\ntaboos="x"\n'
    )
    voice_dir = pack_dir / "voice"
    voice_dir.mkdir()
    (voice_dir / "engine.toml").write_text(
        '[asr]\nengine="e2e-asr"\n\n[tts]\nengine="e2e-tts"\n'
    )

    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp", template="qwen3-thinking",
            base_url="http://test.local:8001", model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="ERROR"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003", timeout_s=5.0,
        ),
    )

    async def _stub_recall(self, q, **kw): return ""
    async def _stub_process(self, e): return ""
    async def _stub_compact(self, *, perception, cascade_messages): return ""
    async def _noop_index(self): return None

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_process)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact)
    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

    from dollos.voice.engines import ASR_REGISTRY, TTS_REGISTRY, ASREngine, TTSEngine

    class _E2EASR(ASREngine):
        def __init__(self, **kw): pass
        async def transcribe(self, audio_pcm, sample_rate): return ""
        async def aclose(self): pass

    class _E2ETTS(TTSEngine):
        sample_rate = 48000
        def __init__(self, **kw): pass
        async def synthesize(self, text):
            if False: yield b""
        async def aclose(self): pass

    ASR_REGISTRY["e2e-asr"] = _E2EASR
    TTS_REGISTRY["e2e-tts"] = _E2ETTS

    fake_peer = MagicMock()
    fake_peer.setRemoteDescription = AsyncMock()
    fake_peer.createAnswer = AsyncMock(return_value=MagicMock(sdp="answer-sdp"))
    fake_peer.setLocalDescription = AsyncMock()
    fake_peer.localDescription = MagicMock(sdp="answer-sdp")
    fake_peer.addIceCandidate = AsyncMock()
    fake_peer.close = AsyncMock()
    fake_peer.addTrack = MagicMock()
    fake_peer.on = MagicMock()
    monkeypatch.setattr("dollos.voice.session.RTCPeerConnection", lambda: fake_peer)

    dollos = DollOS(settings)
    from datetime import date as _date
    dollos._bootstrapped_dates.add(_date.today())

    try:
        await dollos.memsearch.index()
        await dollos.server.start()
        uri = f"ws://127.0.0.1:{dollos.server.port}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "webrtc_offer", "sdp": "offer-sdp"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert msg["type"] == "webrtc_answer"
            assert msg["sdp"] == "answer-sdp"
    finally:
        await dollos.server.stop()
        del ASR_REGISTRY["e2e-asr"]
        del TTS_REGISTRY["e2e-tts"]
```

Add the imports at the top of `test_e2e.py` if missing:
```python
from unittest.mock import AsyncMock, MagicMock
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/test_e2e.py -v
```

Expected: 3 passed.

- [ ] **Step 3: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 379 passed (378 + 1).

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(e2e): WS webrtc_offer → daemon answer round-trip with mocked aiortc"
```

---

## Task 9: Docs — roadmap + CLAUDE.md

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: roadmap.md**

Insert above step 26:

```markdown
### 27. Voice pipeline Phase B — WebRTC + VoiceSession + IPC integration  ✅ Merged

**範圍**：
- aiortc 接入；新 `src/dollos/voice/session.py` 包含 `VoiceSession`（per-WS-client）
- 新 `src/dollos/voice/codec.py`：PCM ↔ AudioFrame + scipy resample
- 新 `src/dollos/voice/sink.py`：`TTSObservingSink`（put_nowait TextChunk → 觸發 voice_session.speak）
- 擴展 IPC messages：`webrtc_offer/answer`、`ice_candidate`、`utterance_start/end`
- IPC server `on_disconnect` hook 多收一個 sink 參數（給 kernel 清理 per-connection state）
- Kernel build engines from character pack voice config；per-connection VoiceSession map keyed by `id(sink)`
- E2E：WS webrtc_offer → daemon answer round-trip（mocked aiortc + engines）

**設計選擇**：
- VoiceSession 只負責 orchestration，aiortc 的網路機制 trusted upstream
- TTS 在 sink layer 攔截，Doll.Say 路徑零改動
- ASR 結果 fire UserTextEvent，跟文字輸入走同一個 dispatcher 流程
- 每個 WS 連線一個 VoiceSession；第一個 webrtc_offer 才 lazy build engines

**Phase 後續**：
- C (next plan)：local-audio-bridge process + 真實 WebRTC 端對端 smoke
```

- [ ] **Step 2: CLAUDE.md**

Append to "已完成" table:
```
| Roadmap step 27 — Voice pipeline Phase B (WebRTC + VoiceSession + IPC) | Merged |
```

Update "下一個":
```
- **Voice pipeline Phase C**：local-audio-bridge process + 真實 WebRTC E2E smoke
- **Drone**（persistent agents — 跟 Subagent 對偶；Monitor 是無大腦版，Drone 是有大腦版）
- **Wake gating** — 等 voice / drone events 進來才有 ROI
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: roadmap step 27 — voice pipeline Phase B"
```

---

## Self-Review Checklist

- [x] **Spec coverage**:
  - aiortc dependency → Task 1
  - Codec helpers → Task 2
  - WS signaling schemas → Task 3
  - TTSObservingSink → Task 4
  - VoiceSession class → Task 5
  - IPC on_disconnect with sink → Task 6
  - Kernel integration (build engines + per-WS session map + signaling dispatch) → Task 7
  - E2E test → Task 8
  - Docs → Task 9
- [x] **No placeholders**: all code blocks complete; bash commands have expected outputs.
- [x] **Type consistency**:
  - `VoiceSession.__init__(*, asr, tts, on_user_text)` consistent in test + impl
  - `VoiceSession.handle_offer(sdp) -> str` consistent
  - `TTSObservingSink(*, voice_session_provider, maxsize=0)` consistent
  - `build_voice_engines(pack_dir, *, data_root) -> tuple[ASR, TTS] | None` consistent
  - Message classes follow `<Verb>In` / `<Verb>Out` convention to disambiguate direction
- [x] **No fallback**: missing engine in registry → ValueError; webrtc_offer with no voice config → RuntimeError surfaced to client.

## Notes for Reviewer

- **Single-client assumption preserved**: `_active_sink` is still a single slot; voice sessions are keyed by `id(sink)` so multi-connection in theory works, but other parts of the kernel (scheduler bootstrap routing) still pick one active sink. Multi-client voice is a future concern.
- **aiortc deps on host**: libopus / libsrtp / libavcodec must be present. If not, `uv sync` works but `import aiortc` fails. Document for new dev machines.
- **No outbound media until first speak()**: VoiceSession's outbound track yields frames only after Doll.Say fires TTS. That's the intended behavior — silent track until daemon has something to say.
- **Test mocking note**: `dollos.voice.session.RTCPeerConnection` is the canonical mock target. Tests that don't mock it will hit real aiortc and try to negotiate ICE locally (slow but not broken).

## Out of scope (Phase C)

- local-audio-bridge process (Python aiortc client + sounddevice)
- Real live E2E smoke against a running daemon
- Voice activity feedback to client (e.g., "Doll is speaking" indicator)
- Multi-client concurrent voice sessions (architecture supports it; needs scheduler refactor first)
- Mid-utterance interrupt of TTS playback (would require track flushing)
