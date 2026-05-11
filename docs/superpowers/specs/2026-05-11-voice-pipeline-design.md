# Voice Pipeline Design

**Date:** 2026-05-11
**Status:** Spec — pending plan
**Supersedes:** Voice Pipeline target table in `CLAUDE.md` (parts of it — kept as direction for KWS/Speaker ID which remain phone-only)

## Goal

Build a bidirectional voice pipeline so DollOS can listen and speak. End-to-end MVP with one real ASR engine (sherpa-onnx) and one real TTS engine (luxtts-onnx), wired into the existing event dispatcher without changing Doll's cascade semantics.

## Why WebRTC + WS (not pure WS binary)

Audio is real-time streaming media. WebRTC gives us Opus, jitter buffer, ICE, DTLS, and adaptive packet loss recovery for free, with native support on browsers (UI) and Android (phone). Raw audio over WS would force us to reinvent these. The WS connection stays as the **control plane** (text I/O + WebRTC signaling); WebRTC is the **media plane** (Opus tracks).

## Architecture

### Component map

```
┌─────────────────────────────────────────────────────────────┐
│ Daemon                                                       │
│                                                              │
│   IPCServer (WS @ 9876)                                      │
│     ├ TextChannel (existing — text_input/text_chunk/...)     │
│     └ Signaling  (new — webrtc_offer/answer/ice_candidate,   │
│                          utterance_start/end)                │
│                                                              │
│   VoiceSession (per WS client)                               │
│     ├ aiortc.RTCPeerConnection                               │
│     ├ inbound audio track → PCM frames                       │
│     │     → utterance buffer (bounded by client markers)     │
│     │     → ASREngine.transcribe(pcm, rate) → str            │
│     │     → UserTextEvent → dispatcher                       │
│     └ outbound audio track ← PCM frames                      │
│           ← TTSEngine.synthesize(text) → AsyncIterator[bytes]│
│           ← Doll.Say sink interceptor                        │
│                                                              │
│   EventDispatcher (existing)                                 │
│   Doll cascade (existing)                                    │
│                                                              │
│   ASREngine ABC + SherpaOnnxASR                              │
│   TTSEngine ABC + LuxTTSEngine                               │
└─────────────────────────────────────────────────────────────┘
              ↕  WS text + WebRTC media (Opus)
   ┌──────────┴──────────┬────────────┐
   │                     │            │
   local-audio-bridge   Tauri UI    Android app
   (this step)          (future)    (future)
```

### Signal flow

**Voice in** (user speaks → Doll sees text):

1. Client-side VAD (silero) detects utterance start → WS text frame `{"type":"utterance_start","sample_rate":16000}`
2. Client pushes utterance audio into WebRTC outbound track (Opus encoded)
3. Daemon `VoiceSession` receives inbound track frames, decodes via aiortc (Opus → PCM), appends to utterance buffer
4. Client VAD detects endpoint → WS text frame `{"type":"utterance_end"}`
5. `VoiceSession` invokes `ASREngine.transcribe(pcm, sample_rate)` → transcript string
6. `VoiceSession` builds `UserTextEvent(text=transcript, response_sink=this_client_sink)` and calls `dispatcher.dispatch(...)`
7. Dispatcher runs Doll cascade per existing flow. Doll.Say emits `TextChunk(text)` to sink.

**Voice out** (Doll Says → user hears):

1. IPC pump (existing per-client handler) processes `TextChunk` from sink
2. **New**: if connection has an active `VoiceSession` AND audio_out is enabled, pump invokes `tts_engine.synthesize(text)` in parallel with sending the WS text frame
3. `TTSEngine` yields PCM byte chunks (~20ms each at engine's native sample rate)
4. `VoiceSession` writes chunks to outbound audio track; aiortc handles Opus encoding + RTP transport
5. Client WebRTC receives the audio frames and plays them

Both directions are independent — a client can be text-only (no WebRTC offer), input-only (mic but no speaker subscription), output-only (speaker but no mic), or both.

### Per-client VoiceSession lifecycle

1. WS connection established (existing IPC behavior)
2. Client may send `{"type":"webrtc_offer","sdp":"<offer SDP>"}`. On receipt, daemon:
   - Constructs `VoiceSession` for this WS connection
   - Builds `RTCPeerConnection` with SDP answer, sends back `{"type":"webrtc_answer","sdp":"<answer SDP>"}`
   - Exchanges ICE candidates via `{"type":"ice_candidate","candidate":"..."}`  (both directions)
3. Peer connection becomes `connected`; bidirectional audio tracks live
4. Audio flows per the signal flow above
5. WS disconnect → `VoiceSession.close()` → aiortc peer closed, engine resources released

Clients that never send `webrtc_offer` continue to work as text-only (existing smoke scripts, tests).

### Engine plugin model

**Why ABC + registry:** more engines will be added over time. Adding a new engine = write a class implementing the ABC + register via decorator. No core dispatcher changes.

```python
# src/dollos/voice/engines.py

class ASREngine(ABC):
    @abstractmethod
    async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
        """Block until transcript ready. audio_pcm is mono int16 little-endian."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release model resources."""


class TTSEngine(ABC):
    sample_rate: int  # output sample rate, e.g., 48000 for LuxTTS

    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mono int16 little-endian PCM chunks at self.sample_rate."""

    @abstractmethod
    async def aclose(self) -> None: ...


ASR_REGISTRY: dict[str, type[ASREngine]] = {}
TTS_REGISTRY: dict[str, type[TTSEngine]] = {}

def register_asr(name: str):
    def decorate(cls):
        ASR_REGISTRY[name] = cls
        return cls
    return decorate

def register_tts(name: str):
    def decorate(cls):
        TTS_REGISTRY[name] = cls
        return cls
    return decorate
```

Engines load configuration from a per-character pack file (see "Character pack voice layout" below).

### MVP engine choices

**ASR — sherpa-onnx**
- Pure ONNX runtime, no torch dependency
- CPU + CUDA support (`sherpa-onnx` PyPI wheel + `sherpa-onnx-cuda` variant)
- Bundled with Paraformer / Whisper-ONNX / SenseVoice / Zipformer support — same engine handles many model families
- Single dependency: `sherpa-onnx-core` (compiled native)
- API shape used: `OfflineRecognizer.create_paraformer(...)` → `recognizer.create_stream() → stream.accept_waveform(rate, pcm) → recognizer.decode_stream(stream) → stream.result.text`
- MVP default model: **SenseVoice ZH-EN-JA-KO-YUE int8** (239 MB; multilingual, accuracy parity with Whisper-medium)
- `SherpaOnnxASR` carries a small **model registry** mapping config strings → HF repos:
  ```python
  SHERPA_MODELS = {
      "sense-voice-zh-en-ja-ko-yue": {
          "hf_repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
          "files": ["model.int8.onnx", "tokens.txt"],
          "loader": "sense_voice",
      },
      "paraformer-zh": {
          "hf_repo": "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14",
          "files": ["model.int8.onnx", "tokens.txt"],
          "loader": "paraformer",
      },
  }
  ```
- **Auto-download on first use** (mirrors luxtts-onnx behavior): if `model_dir` unset, engine uses `~/.cache/dollos/voice/asr/<model_id>/` and lazily pulls via `huggingface_hub.hf_hub_download`. SHA256 / size verification per file. If `model_dir` is set, engine reads from there and refuses to download.

**TTS — luxtts-onnx**
- User's existing repo (`~/Projects/luxtts-onnx`)
- Pure ONNX runtime + numpy + librosa, no torch
- CPU + CUDA via `[gpu]` extra
- Voice cloning: encode a reference audio + transcript once → `.npz` prompt; load instantly on subsequent runs
- Output: float32 array at **48 kHz**, whole utterance (not streaming generator)
- For our streaming-out ABC: run `tts.generate()` in `asyncio.to_thread`, then chunk the result into 20ms PCM frames for the AsyncIterator
- **Auto-downloads** model files (`text_encoder.onnx` 17 MB / `fm_decoder.onnx` 456 MB / `vocos.onnx` 69 MB / `tokens.txt`) from `YatharthS/LuxTTS` + `ProgCat/luxtts-onnx` HF repos on first use, SHA256-verified. Total ~542 MB.

### Model lifecycle summary

Both engines auto-download model files on first use:
- ASR (sherpa-onnx, SenseVoice int8): ~239 MB → `~/.cache/dollos/voice/asr/sense-voice-...`
- TTS (luxtts-onnx): ~542 MB → `~/.cache/luxtts-onnx/models/` (or our `model_dir` override)

**First-run total: ~780 MB.** Subsequent runs read from cache. First daemon start with voice enabled needs internet; document this clearly in README.

Per-character voice clone prompts (`.npz`) live inside the character pack and are tiny (~few KB-MB).

### Character pack voice layout

```
character_packs/<id>/
├── doll.toml
└── voice/
    ├── engine.toml
    └── luxtts/
        ├── prompt.npz        # voice-clone prompt (pre-encoded)
        └── ref.meta.toml     # source ref.wav path + transcript (debug only)
```

`character_packs/gura/voice/engine.toml`:
```toml
[asr]
engine = "sherpa-onnx"
model_dir = "models/paraformer-zh-en"   # relative to data root or absolute
device = "cpu"

[tts]
engine = "luxtts-onnx"
model_dir = "voice/luxtts"              # relative to pack root
prompt = "voice/luxtts/prompt.npz"      # relative to pack root
device = "cpu"
# luxtts-specific params
num_steps = 8
t_shift = 0.9
guidance_scale = 3.0
```

The voice config is **per-character** so that swapping packs swaps voice automatically. If `voice/engine.toml` is absent → voice features disabled for that character (graceful: speech-disabled mode).

### Voice prepare CLI (one-time per character)

```bash
uv run python -m dollos.voice.prepare \
    --pack character_packs/gura \
    --ref reference_recording.wav \
    --transcript "Reference transcript here." \
    --duration 15.0
```

Outcome: writes `character_packs/gura/voice/luxtts/prompt.npz` and `ref.meta.toml`. Idempotent — re-running overwrites.

### Sink interceptor: how Doll.Say reaches TTS

Currently Doll.Say does `ctx.sink.put_nowait(TextChunk(text))`. The IPC pump owns the sink → WS mapping. The minimal change:

- IPC pump keeps a `VoiceSession | None` reference for the connection
- When consuming `TextChunk` from sink, if `voice_session is not None and voice_session.tts_enabled`:
  - Schedule `voice_session.speak(text)` as an asyncio task (non-blocking)
  - Continue sending the WS text frame as before (parallel)

`VoiceSession.speak(text)`:
```python
async def speak(self, text: str) -> None:
    async for pcm_chunk in self._tts.synthesize(text):
        await self._outbound_audio_track.push_pcm(pcm_chunk, self._tts.sample_rate)
```

The track wrapper handles resampling to WebRTC's internal rate (48 kHz Opus encode is native; if engine sample rate differs from 48 kHz, codec.py provides a numpy resampler).

Doll's cascade code is **untouched** — TTS is purely presentation-layer.

### WS signaling schema additions

New `ClientMessage` variants:
- `{"type":"webrtc_offer","sdp":"..."}`
- `{"type":"webrtc_answer","sdp":"..."}` (server can send this, client receives)
- `{"type":"ice_candidate","candidate":"...","sdpMid":"...","sdpMLineIndex":0}` (both directions)
- `{"type":"utterance_start","sample_rate":16000}`
- `{"type":"utterance_end"}`

New `ServerMessage` variants:
- `{"type":"webrtc_answer","sdp":"..."}`
- `{"type":"ice_candidate", ...}`
- `{"type":"voice_state","asr_ready":bool,"tts_ready":bool}` (informational, sent after handshake)

## Module layout

```
src/dollos/voice/
├── __init__.py
├── signaling.py          # pydantic schemas for new WS messages
├── session.py            # VoiceSession (aiortc peer + audio I/O orchestration)
├── codec.py              # PCM ↔ aiortc AudioFrame helpers, resample
├── engines.py            # ASREngine + TTSEngine ABC + registries
├── asr_sherpa.py         # @register_asr("sherpa-onnx") SherpaOnnxASR
├── tts_luxtts.py         # @register_tts("luxtts-onnx") LuxTTSEngine
├── pack.py               # load voice/engine.toml from a character pack
└── prepare.py            # voice prepare CLI (luxtts prompt encoding)
```

Modifications to existing files:
- `src/dollos/ipc/messages.py` — add new ClientMessage/ServerMessage variants
- `src/dollos/ipc/server.py` (or pump module) — route signaling messages to VoiceSession, intercept TextChunk for TTS
- `src/dollos/kernel.py` — build/teardown voice subsystem per character pack; pass to IPC server
- `src/dollos/config.py` — optional `[voice]` section for global defaults (override-able by character pack)

## Error handling

- **WebRTC handshake failure**: daemon sends `{"type":"error","message":"webrtc_<reason>"}`, leaves WS connection open for text I/O
- **ASR transcribe error** (e.g., model load failure mid-utterance): daemon sends `{"type":"error","message":"asr_failed:<msg>"}` to client; UserTextEvent NOT fired for that utterance; cascade not started
- **TTS synthesis error**: daemon sends `error` message; TextChunk still flows over WS text frame so user can read (audio degraded gracefully)
- **Voice config missing for character**: daemon logs warning, voice features disabled; WS text I/O unaffected
- **No fallback / mocking** per project rule — if sherpa-onnx is not installed or model dir missing, daemon refuses to start voice and surfaces a clear error in logs + voice_state message

## Testing

**Unit tests**:
- `tests/voice/test_engines_abc.py` — registry decorators, registry lookup
- `tests/voice/test_codec.py` — PCM byte ↔ AudioFrame round-trip, resample correctness
- `tests/voice/test_session.py` — VoiceSession state machine with mocked aiortc + mocked engines
- `tests/voice/test_signaling.py` — message schema validation

**Engine integration tests** (skipped if model files absent):
- `tests/voice/test_sherpa_integration.py` — load real model, transcribe a fixture wav
- `tests/voice/test_luxtts_integration.py` — load test prompt, synthesize "hello" → verify non-silent output

**E2E**:
- Extend `tests/test_e2e.py` with a voice-flavored variant: mock LLM as before, plus a fake WebRTC peer (no real aiortc — substitute a stub that exposes the same async iter API). Verify utterance_start/end markers trigger transcribe → UserTextEvent → Say → TTS chunks → outbound track.
- Live smoke (NOT pytest): local-audio-bridge process connects, user speaks "你好", terminal shows transcript, speakers play Doll's response.

## Risks and out-of-scope

**Risks**:
1. **aiortc native deps**: needs libavcodec/libopus/libsrtp on the host. Document install for Linux/Mac. Failure mode = clean error at daemon startup.
2. **Audio clock drift**: WebRTC handles its own jitter buffer; daemon-side outbound resample at 48 kHz minimizes drift. If LuxTTS 48 kHz output → WebRTC 48 kHz: no resample, no drift.
3. **CPU load**: sherpa-onnx + luxtts both inferentially heavy. CPU-mode users may see 1-3s latency per turn. Acceptable for MVP; GPU configurable.
4. **First-utterance cold start**: ASR + TTS models load on first use → potentially 5-10s delay. Mitigation: warm-load at VoiceSession construction (after handshake).
5. **funasr-onnx not added (yet)**: ABC + registry leaves room; deferred to future plan.

**Out of scope for this plan**:
- KWS (wake word) — phone-side; project already has wake_word_training pipeline for Gura
- Phone app integration — requires the Android app (not yet built)
- Tauri UI integration — requires the UI (not yet built)
- Lip sync / viseme stream — separate concern; can layer on TTS phoneme output later
- Speaker ID — phone-side; future
- Streaming partial transcripts — engine ABC is utterance-batch; deferred to future engine variant
- Token-by-token TTS — Doll.Say emits whole utterance text; mid-cascade audio is post-cascade follow-up (existing parallel flow)

## Acceptance criteria

1. Daemon starts cleanly with sherpa-onnx + luxtts-onnx installed and a properly configured character pack
2. `dollos.voice.prepare` CLI encodes a luxtts prompt from a wav + transcript and writes it into a character pack
3. Local-audio-bridge process connects to daemon, negotiates WebRTC, opens bidirectional audio
4. User speaks an utterance, transcript reaches dispatcher, Doll cascade runs, audio response plays
5. WS text-only clients (existing smoke scripts) still work unchanged
6. Unit + integration tests pass; pytest suite remains green (current: 336)
7. ASR engine registry + TTS engine registry exposed; adding a new engine = single file with decorator, no core changes

## Open questions (parking lot)

- VoiceSession persistence: should a single VoiceSession outlive WS reconnects (per-character session pooling), or is it strictly per-WS-connection? **Decision: strictly per-WS-connection for MVP**; pooling deferred until usage patterns demand it.
- Resampling library: numpy-only crossover vs scipy `resample_poly` vs librosa? **Decision: scipy `resample_poly` (already a transitive dep via librosa, which luxtts-onnx pulls). No new dependency.**
- Voice config override at runtime via tool: should Doll have a `SwitchVoice` tool? **Decision: no, YAGNI. Voice tied to character pack.**
