# Voice Pipeline Phase C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local-audio-bridge client process — captures mic, runs silero VAD to detect utterances, streams audio to the daemon over WebRTC, plays daemon's TTS audio through speakers. End-to-end voice loop on a single machine: user speaks → ASR → Doll cascade → TTS → user hears.

**Architecture:** New `src/dollos/voice/bridge/` sub-package. `python -m dollos.voice.bridge --daemon ws://localhost:9876` runs the process. SileroVAD wrapper (onnxruntime, no torch) drives an utterance state machine. Mic audio routes through sounddevice → aiortc audio track → daemon. Daemon's outbound track → sounddevice playback. WS signaling client negotiates the WebRTC peer with the daemon.

**Tech Stack:** Python 3.13, asyncio, aiortc (already a dep), sounddevice, onnxruntime (already), huggingface_hub (already), websockets (already).

**Spec:** `docs/superpowers/specs/2026-05-11-voice-pipeline-design.md` — see "Component map" → local-audio-bridge column, "Signal flow → Voice in" / "Voice out".

**Phase placement:** Phase C of 3. Closes the voice pipeline loop. After this, future work moves to product features (multi-client voice, voice activity UI, etc.).

**Out of scope** (separate plans):
- Zero-shot wake word (KWS) — VAD-only triggers utterances; wake word is a research item
- Speaker ID — single-user assumption
- Phone app + Tauri UI — separate clients with the same WS protocol

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add `sounddevice>=0.5`. |
| `src/dollos/voice/bridge/__init__.py` | Create | Package marker. |
| `src/dollos/voice/bridge/__main__.py` | Create | CLI entry: `python -m dollos.voice.bridge`. |
| `src/dollos/voice/bridge/vad.py` | Create | `SileroVAD` — wraps `silero_vad.onnx` (auto-downloaded). |
| `src/dollos/voice/bridge/mic.py` | Create | `MicrophoneTrack` — aiortc audio track sourced from a sounddevice InputStream. |
| `src/dollos/voice/bridge/speaker.py` | Create | `SpeakerPlayer` — consumes a remote audio track, writes to sounddevice OutputStream. |
| `src/dollos/voice/bridge/signaling.py` | Create | `BridgeSignaling` — connects WS, exchanges SDP offer/answer + ICE candidates. |
| `src/dollos/voice/bridge/controller.py` | Create | `BridgeController` — wires VAD + mic + speaker + signaling + state machine for utterance markers. |
| `tests/voice/bridge/__init__.py` | Create | Test package marker. |
| `tests/voice/bridge/test_vad.py` | Create | SileroVAD: chunk shape, probability output, model loads. |
| `tests/voice/bridge/test_mic.py` | Create | MicrophoneTrack with mocked sounddevice. |
| `tests/voice/bridge/test_speaker.py` | Create | SpeakerPlayer with mocked sounddevice. |
| `tests/voice/bridge/test_signaling.py` | Create | BridgeSignaling with mocked aiortc + mocked websockets. |
| `tests/voice/bridge/test_controller.py` | Create | BridgeController state machine (VAD speech_prob sequence → utterance_start/end emissions). |
| `docs/roadmap.md` | Modify | Add step 28 entry. |
| `CLAUDE.md` | Modify | Update completed plans table. |

---

## Task 1: Add sounddevice dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dep**

In `[project] dependencies = [...]`, append:

```toml
"sounddevice>=0.5",
```

- [ ] **Step 2: Sync**

```bash
uv sync
```

Expect: installs sounddevice + cffi. Native PortAudio is required at runtime — on Linux: `sudo apt install libportaudio2`. If missing, `import sounddevice` succeeds (lazy load) but actual device access fails — bridge will surface a clear error. Document in step 5 if it surfaces here.

- [ ] **Step 3: Verify import**

```bash
uv run python -c "import sounddevice; print('sounddevice', sounddevice.__version__)"
```

Expect: prints version. If error about libportaudio2, STOP and report — host needs the system package.

- [ ] **Step 4: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 379 passed (unchanged).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add sounddevice for local-audio-bridge"
```

---

## Task 2: SileroVAD wrapper

**Files:**
- Create: `src/dollos/voice/bridge/__init__.py`, `src/dollos/voice/bridge/vad.py`
- Test: `tests/voice/bridge/__init__.py`, `tests/voice/bridge/test_vad.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/bridge/__init__.py` empty.

`tests/voice/bridge/test_vad.py`:

```python
"""SileroVAD wrapper tests.

The model is loaded via onnxruntime on first call (auto-download from
HuggingFace into <data_root>/voice/vad/). Integration tests are marked
voice_integration; structural tests stand alone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


def test_silero_vad_chunk_size_constant():
    from dollos.voice.bridge.vad import SileroVAD
    # Silero v5 expects 512-sample chunks at 16 kHz (32 ms).
    assert SileroVAD.SAMPLES_PER_CHUNK == 512
    assert SileroVAD.SAMPLE_RATE == 16000


@pytest.mark.voice_integration
def test_silero_vad_runs_on_silence(tmp_path: Path):
    from dollos.voice.bridge.vad import SileroVAD

    vad = SileroVAD(data_root=tmp_path)
    # 32ms of silence
    silence = np.zeros(SileroVAD.SAMPLES_PER_CHUNK, dtype=np.float32)
    prob = vad.speech_probability(silence)
    assert 0.0 <= prob <= 1.0
    # Silence should have low speech probability
    assert prob < 0.3
    vad.close()


@pytest.mark.voice_integration
def test_silero_vad_runs_on_noise(tmp_path: Path):
    from dollos.voice.bridge.vad import SileroVAD

    vad = SileroVAD(data_root=tmp_path)
    rng = np.random.default_rng(0)
    # Loud noise → not necessarily speech, but pipeline should return a probability.
    noise = (rng.standard_normal(SileroVAD.SAMPLES_PER_CHUNK) * 0.3).astype(np.float32)
    prob = vad.speech_probability(noise)
    assert 0.0 <= prob <= 1.0
    vad.close()


def test_silero_vad_rejects_wrong_chunk_size(tmp_path: Path, monkeypatch):
    """Wrong chunk length should raise ValueError before any model run."""
    from dollos.voice.bridge import vad as vad_mod

    # Stub _ensure_model to no-op + skip session init.
    class _FakeSession:
        def run(self, *a, **kw): raise AssertionError("should not be called")

    monkeypatch.setattr(vad_mod, "_ensure_model", lambda data_root: tmp_path / "fake.onnx")
    monkeypatch.setattr(vad_mod.ort, "InferenceSession", lambda *a, **kw: _FakeSession())

    vad = vad_mod.SileroVAD(data_root=tmp_path)
    bad = np.zeros(100, dtype=np.float32)
    with pytest.raises(ValueError, match="chunk size"):
        vad.speech_probability(bad)
    vad.close()
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/voice/bridge/test_vad.py -v -m "not voice_integration"
```

Expect: ImportError on `dollos.voice.bridge.vad`.

- [ ] **Step 3: Create bridge package + vad.py**

`src/dollos/voice/bridge/__init__.py`:
```python
"""Local audio bridge — captures mic, plays speaker, talks WebRTC to the daemon."""
```

`src/dollos/voice/bridge/vad.py`:

```python
"""SileroVAD — voice activity detection via silero_vad.onnx (no torch).

Auto-downloads silero_vad.onnx (~2.3 MB) from HuggingFace into
<data_root>/voice/vad/ on first construction. The model state is a small
LSTM hidden state that must be threaded between chunks; we keep it
inside the instance.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download

logger = logging.getLogger(__name__)


_HF_REPO = "onnx-community/silero-vad"
_HF_FILENAME = "onnx/model.onnx"
_LOCAL_FILENAME = "silero_vad.onnx"


def _ensure_model(data_root: Path) -> Path:
    """Download silero_vad.onnx to <data_root>/voice/vad/ if missing."""
    vad_dir = data_root / "voice" / "vad"
    vad_dir.mkdir(parents=True, exist_ok=True)
    target = vad_dir / _LOCAL_FILENAME
    if target.exists() and target.stat().st_size > 0:
        return target
    logger.info("downloading %s → %s", _HF_FILENAME, target)
    downloaded = hf_hub_download(
        repo_id=_HF_REPO,
        filename=_HF_FILENAME,
        local_dir=str(vad_dir),
    )
    # hf_hub_download preserves the relative path "onnx/model.onnx" inside
    # local_dir — symlink or rename to our canonical name.
    src = Path(downloaded)
    if src.resolve() != target.resolve():
        target.unlink(missing_ok=True)
        target.symlink_to(src.resolve())
    return target


class SileroVAD:
    """Stateful VAD: feed 32 ms chunks of mono float32 16 kHz audio."""

    SAMPLE_RATE: int = 16000
    SAMPLES_PER_CHUNK: int = 512  # 32 ms at 16 kHz

    def __init__(self, *, data_root: Path) -> None:
        self._model_path = _ensure_model(data_root)
        self._session = ort.InferenceSession(
            str(self._model_path),
            providers=["CPUExecutionProvider"],
        )
        # LSTM hidden state — silero v5 uses a single 'state' tensor of
        # shape (2, 1, 128).
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(self.SAMPLE_RATE, dtype=np.int64)

    def reset(self) -> None:
        """Drop carrier state — start of a new utterance / silence period."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def speech_probability(self, chunk: np.ndarray) -> float:
        """Return speech probability for one chunk in [0, 1]."""
        if chunk.shape != (self.SAMPLES_PER_CHUNK,):
            raise ValueError(
                f"chunk size must be {self.SAMPLES_PER_CHUNK} samples; got {chunk.shape}"
            )
        if chunk.dtype != np.float32:
            chunk = chunk.astype(np.float32)
        inputs = {
            "input": chunk.reshape(1, -1),
            "state": self._state,
            "sr": self._sr,
        }
        outputs = self._session.run(["output", "stateN"], inputs)
        prob = float(outputs[0].squeeze())
        self._state = outputs[1]
        return prob

    def close(self) -> None:
        self._session = None
```

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest tests/voice/bridge/test_vad.py -v -m "not voice_integration"
```

Expect: 2 passed (chunk size constant + rejects wrong size).

- [ ] **Step 5: Run voice_integration tests (downloads model on first run)**

```bash
uv run pytest tests/voice/bridge/test_vad.py -v -m voice_integration
```

Expect: 2 passed (silence + noise). First run takes ~5s for download.

- [ ] **Step 6: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 381 passed (379 + 2).

- [ ] **Step 7: Commit**

```bash
git add src/dollos/voice/bridge/__init__.py src/dollos/voice/bridge/vad.py tests/voice/bridge/
git commit -m "feat(bridge): SileroVAD wrapper — onnxruntime, no torch, auto-download"
```

---

## Task 3: MicrophoneTrack

**Files:**
- Create: `src/dollos/voice/bridge/mic.py`
- Test: `tests/voice/bridge/test_mic.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/bridge/test_mic.py`:

```python
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
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/voice/bridge/test_mic.py -v
```

- [ ] **Step 3: Implement mic.py**

`src/dollos/voice/bridge/mic.py`:

```python
"""MicrophoneTrack — aiortc audio track sourced from sounddevice InputStream."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
import sounddevice as sd
from aiortc import MediaStreamTrack

from dollos.voice.codec import audio_frame_from_pcm

logger = logging.getLogger(__name__)


class MicrophoneTrack(MediaStreamTrack):
    """Capture mono float32 audio from the system mic, yield int16 AudioFrames.

    Each sounddevice callback fires from a worker thread; we put the
    frames into an asyncio.Queue (thread-safe via call_soon_threadsafe)
    consumed by recv().
    """

    kind = "audio"

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        blocksize: int = 512,
        device: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._loop = asyncio.get_event_loop()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            callback=self._sd_callback,
            device=device,
        )
        self._stream.start()

    def _sd_callback(self, indata, frames, time, status) -> None:
        if status:
            logger.debug("InputStream status: %s", status)
        # indata shape (frames, 1) float32 in [-1, 1]
        samples_f32 = indata.reshape(-1)
        pcm_i16 = np.clip(samples_f32 * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        frame = audio_frame_from_pcm(pcm_i16, sample_rate=self._sample_rate)
        # Queue must be modified on the asyncio thread.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    async def recv(self):
        return await self._queue.get()

    def stop(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("mic stream stop failed")
        super().stop()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/bridge/test_mic.py -v
```

Expect: 3 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 384 passed.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/bridge/mic.py tests/voice/bridge/test_mic.py
git commit -m "feat(bridge): MicrophoneTrack — sounddevice → aiortc audio track"
```

---

## Task 4: SpeakerPlayer

**Files:**
- Create: `src/dollos/voice/bridge/speaker.py`
- Test: `tests/voice/bridge/test_speaker.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/bridge/test_speaker.py`:

```python
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
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/voice/bridge/test_speaker.py -v
```

- [ ] **Step 3: Implement speaker.py**

`src/dollos/voice/bridge/speaker.py`:

```python
"""SpeakerPlayer — consumes an aiortc remote audio track + writes to sounddevice."""
from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

from dollos.voice.codec import pcm_from_audio_frame, resample_pcm_int16

logger = logging.getLogger(__name__)


class SpeakerPlayer:
    """Open an output stream; consume frames from a track + write PCM."""

    def __init__(self, *, sample_rate: int = 48000) -> None:
        self._sample_rate = sample_rate
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        self._stream.start()

    async def consume_track(self, track) -> None:
        """Read AudioFrames from `track`, write to the speaker.

        Ends when the track raises (e.g. peer disconnected).
        """
        try:
            while True:
                frame = await track.recv()
                pcm_i16 = pcm_from_audio_frame(frame)
                # Resample if frame.sample_rate differs from our output rate.
                if frame.sample_rate != self._sample_rate:
                    pcm_i16 = resample_pcm_int16(
                        pcm_i16,
                        src_rate=frame.sample_rate,
                        dst_rate=self._sample_rate,
                    )
                # int16 LE → float32 in [-1, 1]
                samples_i16 = np.frombuffer(pcm_i16, dtype=np.int16)
                samples_f32 = samples_i16.astype(np.float32) / 32768.0
                # sounddevice.OutputStream.write expects float32 in (frames, channels).
                try:
                    self._stream.write(samples_f32.reshape(-1, 1).copy())
                except Exception:
                    logger.exception("speaker write failed")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("speaker consume_track ended")

    def stop(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            logger.exception("speaker stream stop failed")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/bridge/test_speaker.py -v
```

Expect: 2 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 386 passed.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/bridge/speaker.py tests/voice/bridge/test_speaker.py
git commit -m "feat(bridge): SpeakerPlayer — aiortc track → sounddevice OutputStream"
```

---

## Task 5: BridgeSignaling — WS + aiortc client peer

**Files:**
- Create: `src/dollos/voice/bridge/signaling.py`
- Test: `tests/voice/bridge/test_signaling.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/bridge/test_signaling.py`:

```python
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
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/voice/bridge/test_signaling.py -v
```

- [ ] **Step 3: Implement signaling.py**

`src/dollos/voice/bridge/signaling.py`:

```python
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
            await self._send_json({
                "type": "ice_candidate",
                "candidate": "candidate:" + cand.candidate if not cand.candidate.startswith("candidate:") else cand.candidate,
                "sdpMid": cand.sdpMid,
                "sdpMLineIndex": cand.sdpMLineIndex,
            })

        offer = await self._peer.createOffer()
        await self._peer.setLocalDescription(offer)
        await self._send_json({"type": "webrtc_offer", "sdp": self._peer.localDescription.sdp})
        self._recv_loop_task = asyncio.create_task(self._recv_loop(), name="bridge-recv-loop")

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
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/bridge/test_signaling.py -v
```

Expect: 3 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 389 passed.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/bridge/signaling.py tests/voice/bridge/test_signaling.py
git commit -m "feat(bridge): BridgeSignaling — WS + aiortc client peer"
```

---

## Task 6: BridgeController — VAD-driven utterance state machine

**Files:**
- Create: `src/dollos/voice/bridge/controller.py`
- Test: `tests/voice/bridge/test_controller.py`

- [ ] **Step 1: Write failing tests**

`tests/voice/bridge/test_controller.py`:

```python
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
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest tests/voice/bridge/test_controller.py -v
```

- [ ] **Step 3: Implement controller.py**

`src/dollos/voice/bridge/controller.py`:

```python
"""BridgeController + UtteranceStateMachine — drive utterance markers from VAD."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from dollos.voice.bridge.signaling import BridgeSignaling
    from dollos.voice.bridge.vad import SileroVAD

logger = logging.getLogger(__name__)


class UtteranceStateMachine:
    """Drives utterance_start / utterance_end markers from chunk-by-chunk VAD output.

    State transitions:
        SILENCE → SPEECH (on first chunk with prob >= threshold) → fires utterance_start
        SPEECH  → SILENCE (after `silence_chunks_to_end` consecutive low-prob chunks)
                  fires utterance_end, reset to SILENCE
        SPEECH  → SPEECH (any high-prob chunk resets the silence counter)
    """

    def __init__(
        self,
        *,
        signaling: "BridgeSignaling",
        sample_rate: int,
        speech_threshold: float = 0.5,
        silence_chunks_to_end: int = 25,  # 25 × 32ms = 800ms silence
    ) -> None:
        self._signaling = signaling
        self._sample_rate = sample_rate
        self._threshold = speech_threshold
        self._silence_chunks_to_end = silence_chunks_to_end
        self._in_utterance = False
        self._silence_chunks = 0

    async def on_chunk(self, *, speech_prob: float) -> None:
        is_speech = speech_prob >= self._threshold
        if not self._in_utterance:
            if is_speech:
                self._in_utterance = True
                self._silence_chunks = 0
                await self._signaling.send_utterance_start(sample_rate=self._sample_rate)
        else:
            if is_speech:
                self._silence_chunks = 0
            else:
                self._silence_chunks += 1
                if self._silence_chunks >= self._silence_chunks_to_end:
                    self._in_utterance = False
                    self._silence_chunks = 0
                    await self._signaling.send_utterance_end()


class BridgeController:
    """Wires mic + VAD + signaling + speaker into a running bridge.

    Owns the asyncio tasks: a consume-from-mic-track loop that runs VAD
    and pushes markers, and the speaker consumer driven by `on_remote_track`.
    """

    def __init__(
        self,
        *,
        signaling: "BridgeSignaling",
        vad: "SileroVAD",
        sample_rate: int = 16000,
    ) -> None:
        self._signaling = signaling
        self._vad = vad
        self._sample_rate = sample_rate
        self._fsm = UtteranceStateMachine(
            signaling=signaling, sample_rate=sample_rate,
        )
        self._mic_loop_task: asyncio.Task | None = None

    async def run_mic_loop(self, mic_track) -> None:
        """Read frames from the mic track, run VAD, drive utterance state."""
        try:
            while True:
                frame = await mic_track.recv()
                # frame is int16 mono at our sample_rate; convert to float32 [-1,1]
                pcm_i16 = np.frombuffer(frame.to_ndarray().tobytes(), dtype=np.int16)
                samples_f32 = pcm_i16.astype(np.float32) / 32768.0
                # Split into VAD chunks of SAMPLES_PER_CHUNK
                chunk_size = self._vad.SAMPLES_PER_CHUNK
                for i in range(0, len(samples_f32) - chunk_size + 1, chunk_size):
                    chunk = samples_f32[i:i + chunk_size]
                    prob = self._vad.speech_probability(chunk)
                    await self._fsm.on_chunk(speech_prob=prob)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mic loop ended")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/bridge/test_controller.py -v
```

Expect: 4 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 393 passed.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/bridge/controller.py tests/voice/bridge/test_controller.py
git commit -m "feat(bridge): UtteranceStateMachine + BridgeController — VAD-driven markers"
```

---

## Task 7: CLI entry — `python -m dollos.voice.bridge`

**Files:**
- Create: `src/dollos/voice/bridge/__main__.py`

- [ ] **Step 1: Implement `__main__.py`**

`src/dollos/voice/bridge/__main__.py`:

```python
"""Local audio bridge CLI.

Usage:
    python -m dollos.voice.bridge --daemon ws://localhost:9876

Captures mic, runs silero VAD, streams to daemon over WebRTC, plays
daemon's TTS output. Press Ctrl-C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import websockets

from dollos.voice.bridge.controller import BridgeController
from dollos.voice.bridge.mic import MicrophoneTrack
from dollos.voice.bridge.signaling import BridgeSignaling
from dollos.voice.bridge.speaker import SpeakerPlayer
from dollos.voice.bridge.vad import SileroVAD


logger = logging.getLogger("dollos.voice.bridge")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.voice.bridge",
        description="Local audio bridge — mic + speaker WebRTC client for DollOS",
    )
    p.add_argument(
        "--daemon", type=str, default="ws://127.0.0.1:9876",
        help="Daemon WS URL (default ws://127.0.0.1:9876)",
    )
    p.add_argument(
        "--data-root", type=Path, default=Path("data"),
        help="data root for VAD model cache (default ./data)",
    )
    p.add_argument(
        "--mic-rate", type=int, default=16000,
        help="Microphone sample rate (default 16000 — matches VAD + ASR)",
    )
    p.add_argument(
        "--speaker-rate", type=int, default=48000,
        help="Speaker output sample rate (default 48000 — luxtts native)",
    )
    p.add_argument(
        "--mic-device", type=int, default=None,
        help="Optional sounddevice input device index",
    )
    p.add_argument("--verbose", action="store_true")
    return p


async def run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    logger.info("connecting to daemon: %s", args.daemon)
    async with websockets.connect(args.daemon) as ws:
        vad = SileroVAD(data_root=args.data_root)
        mic = MicrophoneTrack(
            sample_rate=args.mic_rate, device=args.mic_device,
        )
        speaker = SpeakerPlayer(sample_rate=args.speaker_rate)
        signaling = BridgeSignaling(ws=ws)
        controller = BridgeController(
            signaling=signaling, vad=vad, sample_rate=args.mic_rate,
        )

        async def _on_remote_track(track) -> None:
            await speaker.consume_track(track)

        await signaling.connect(
            local_audio_track=mic, on_remote_track=_on_remote_track,
        )
        logger.info("bridge connected — speak any time. Ctrl-C to quit.")

        mic_loop = asyncio.create_task(
            controller.run_mic_loop(mic), name="bridge-mic-loop",
        )

        stop_event = asyncio.Event()

        def _sigint_handler() -> None:
            logger.info("SIGINT — shutting down")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            loop.add_signal_handler(getattr(signal, sig_name), _sigint_handler)

        try:
            await stop_event.wait()
        finally:
            mic_loop.cancel()
            try:
                await mic_loop
            except (asyncio.CancelledError, Exception):
                pass
            mic.stop()
            speaker.stop()
            await signaling.close()
            vad.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the entry point parses args**

```bash
uv run python -m dollos.voice.bridge --help
```

Expect: prints help, no errors.

- [ ] **Step 3: Verify imports resolve**

```bash
uv run python -c "from dollos.voice.bridge.__main__ import build_parser, run; p = build_parser(); args = p.parse_args(['--daemon', 'ws://x']); print(args.daemon)"
```

Expect: `ws://x` printed.

- [ ] **Step 4: Full suite (should be unaffected)**

```bash
uv run pytest -q -m "not voice_integration"
```

Expect: 393 passed (unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/voice/bridge/__main__.py
git commit -m "feat(bridge): CLI entry — python -m dollos.voice.bridge"
```

---

## Task 8: Live E2E smoke

**Not committed.** Manual procedure that confirms the full loop.

- [ ] **Step 1: Ensure prereqs**

```bash
# Big LLM running on 8001 + Inner Voice on 8003.
ss -tlnp 2>/dev/null | grep llama
# config.toml present + character pack has voice/engine.toml.
ls config.toml character_packs/gura/voice/engine.toml
```

If `character_packs/gura/voice/engine.toml` is missing, create it now (this is what daemon reads to build engines on first offer):

```toml
[asr]
engine = "sherpa-onnx"
model_id = "sense-voice-zh-en-ja-ko-yue"
device = "cpu"

[tts]
engine = "luxtts-onnx"
model_dir = "../../data/voice/tts/luxtts"
prompt = "voice/luxtts/prompt.npz"
device = "cpu"
num_steps = 8
t_shift = 0.9
guidance_scale = 3.0
```

Confirm `character_packs/gura/voice/luxtts/prompt.npz` exists. If not, run:
```bash
uv run python -m dollos.voice.prepare \
    --pack character_packs/gura \
    --ref <some-3-second-wav-of-a-voice-you-want-cloned> \
    --transcript "<exact transcript of that wav>" \
    --duration 3.0
```

- [ ] **Step 2: Start the daemon (background)**

```bash
uv run python -m dollos --config config.toml > /tmp/dollos_phase_c.log 2>&1 &
echo $! > /tmp/dollos_phase_c.pid
sleep 4
ss -tlnp 2>/dev/null | grep :9876
```

Expect: daemon listening on 9876.

- [ ] **Step 3: Run the bridge (foreground)**

In a separate shell (or same shell, daemon backgrounded):

```bash
uv run python -m dollos.voice.bridge --verbose
```

Expect: prints `connecting to daemon`, `bridge connected — speak any time.`

If you have GPU TTS, set `LD_LIBRARY_PATH` first (see pyproject [gpu] comment):
```bash
NV=".venv/lib/python3.13/site-packages/nvidia"
LD="$NV/cudnn/lib:$NV/cublas/lib:$NV/cuda_runtime/lib:$NV/cufft/lib:$NV/cuda_nvrtc/lib:$NV/nvjitlink/lib"
LD_LIBRARY_PATH="$LD" CUDA_VISIBLE_DEVICES=1 uv run python -m dollos.voice.bridge --verbose
```
(And ensure `character_packs/gura/voice/engine.toml` has `device = "cuda"` for the tts section.)

- [ ] **Step 4: Speak**

Say "你好" or "Hello Doll." into the mic. Watch the bridge logs for:
- `send_utterance_start` (VAD detected speech)
- `send_utterance_end` (silence detected)

In the daemon log (`tail -f /tmp/dollos_phase_c.log`), look for:
- `UserTextEvent` dispatched
- Doll's cascade running
- TextChunk + (TTS task fires)

Expect: hear Doll's voice through your speakers within a few seconds.

- [ ] **Step 5: Iterate / debug**

Common issues:
- **No mic audio**: check `sd.query_devices()` — wrong default device. Pass `--mic-device N`.
- **No VAD triggering**: too aggressive `--speech_threshold` (defaults 0.5). Could lower via env if needed; current CLI doesn't expose, edit controller defaults if persistent.
- **ICE never completes**: check daemon log for ICE candidate exchange. Localhost should be instant.
- **TTS plays but cuts off**: check sounddevice buffer; consume_track may be too slow. Verify GPU TTS if CPU is slow.

- [ ] **Step 6: Tear down**

```bash
kill "$(cat /tmp/dollos_phase_c.pid)"
rm /tmp/dollos_phase_c.pid /tmp/dollos_phase_c.log
```

- [ ] **Step 7: No commit (smoke is manual).**

---

## Task 9: Docs

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: roadmap.md — add step 28**

Insert above step 27:

```markdown
### 28. Voice pipeline Phase C — local-audio-bridge + live E2E  ✅ Merged

**範圍**：
- 新 `src/dollos/voice/bridge/` 子套件：
  - `vad.py` — SileroVAD wrapper（onnxruntime，無 torch；HF auto-download silero_vad.onnx 2.3MB 進 `data/voice/vad/`）
  - `mic.py` — MicrophoneTrack（sounddevice InputStream → aiortc audio track）
  - `speaker.py` — SpeakerPlayer（aiortc 遠端 track → sounddevice OutputStream）
  - `signaling.py` — BridgeSignaling（WS + aiortc client peer，bridge 當 offerer）
  - `controller.py` — UtteranceStateMachine（VAD speech_prob 序列 → utterance_start/end 標記）+ BridgeController
  - `__main__.py` — CLI: `python -m dollos.voice.bridge --daemon ws://...`
- 加 `sounddevice>=0.5` dep
- Tests：unit-level mocked aiortc + mocked sounddevice；VAD 整合測試標 voice_integration
- Live E2E smoke：daemon + llama-server + bridge 全跑，使用者講「你好」聽到 Doll 回應

**設計選擇**：
- VAD 走 ONNX 不走 torch（silero PyPI 強制 torch；直接用 silero ONNX export + onnxruntime）
- VAD chunk 32ms（silero v5 預設 512 samples @ 16kHz）；silence_chunks_to_end 預設 25 = 800ms 沉默才結束 utterance
- Bridge 是 WebRTC offerer，daemon 是 answerer（matching Phase B 預期）
- Mic 跟 speaker 用獨立 sample rate（16k mic 配 ASR、48k speaker 配 luxtts），sounddevice 各自獨立 stream
- Bridge tests 全 mock 真實裝置；真實裝置驗證走 manual smoke

**Phase 後續（未來 plan）**：
- Zero-shot wake word + speaker ID
- Phone app / UI client（同個 WS+WebRTC protocol）
- 多 client 同時 voice
```

- [ ] **Step 2: CLAUDE.md — update tables**

Append to "已完成":
```
| Roadmap step 28 — Voice pipeline Phase C (local-audio-bridge + E2E) | Merged |
```

Update "下一個":
```
- **Drone**（persistent agents — 跟 Subagent 對偶；Monitor 是無大腦版，Drone 是有大腦版）
- **Wake gating + zero-shot wake word**（研究 CLAP-like KWS）
- **Speaker ID**（zero-shot embedding 方向）
- **回應延遲壓縮**（LLM-side 工程，見 memory）
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: roadmap step 28 — voice pipeline Phase C"
```

---

## Self-Review Checklist

- [x] **Spec coverage**:
  - silero VAD via ONNX → Task 2
  - Mic + speaker via sounddevice → Tasks 3, 4
  - WebRTC client offer → Task 5
  - Utterance markers from VAD → Task 6
  - CLI entry → Task 7
  - Live E2E → Task 8
  - Docs → Task 9
- [x] **No placeholders**: all code blocks complete; all bash commands have expected output.
- [x] **Type consistency**:
  - `SileroVAD.SAMPLES_PER_CHUNK = 512`, `SAMPLE_RATE = 16000` consistent in test + impl + controller
  - `BridgeSignaling.connect(*, local_audio_track, on_remote_track)` matches test + CLI
  - `BridgeSignaling.send_utterance_start(sample_rate=...)` / `send_utterance_end()` consistent
  - `UtteranceStateMachine(*, signaling, sample_rate, speech_threshold=0.5, silence_chunks_to_end=25)` matches tests + controller wiring
  - CLI defaults: mic 16k matches VAD; speaker 48k matches luxtts
- [x] **No fallback**: missing config / unreachable daemon surfaces as exception; no silent degrade.

## Notes for Reviewer

- **System dep**: PortAudio (libportaudio2) is required for sounddevice. Document on first install. macOS / Windows ships sounddevice with bundled native.
- **Permissions**: mic access on macOS prompts the user the first time the bridge runs. Linux may need ALSA / PulseAudio permissions for the user.
- **Threading boundary**: sounddevice's callback runs on a PortAudio thread. We use `loop.call_soon_threadsafe` to bridge to asyncio. Be careful not to do heavy work in the callback.
- **VAD model auto-download** mirrors sherpa-onnx + luxtts pattern: first run pulls from HF Hub into `<data_root>/voice/vad/`, subsequent runs use the cached file.
- **No interrupt while Doll speaks**: if user starts talking during Doll's TTS playback, both will overlap. This is Phase C's intended behavior; mid-utterance interrupt is future work.
