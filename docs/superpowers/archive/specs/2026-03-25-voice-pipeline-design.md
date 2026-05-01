# Voice Pipeline Design

## Overview

Integrate Sherpa-ONNX into DollOSAIService to provide a complete on-device voice pipeline: streaming ASR, TTS, wake word detection, VAD, and speaker identification. All models run locally on the phone — no server required.

## Models

All models bundled in system image at `/system_ext/dollos/models/voice/`.

| Function | Model | Size | Expected RTF (Pixel 6a) |
|----------|-------|------|------------------------|
| Streaming ASR | sherpa-onnx-streaming-paraformer-bilingual-zh-en (int8) | ~226MB | <0.1 |
| TTS | Kokoro multi-lang v1.1 | ~310MB | ~1.0 |
| Wake Word | kws-zipformer-zh-en-3M | ~38MB | <0.05 |
| VAD | Silero VAD | ~2MB | <0.01 |
| Speaker ID | Speaker embedding extractor | ~small | <0.01 |

Total: ~580MB. All offline-capable.

## Voice Conversation Flow

```
1. VAD continuously monitors microphone (low power)
2. Trigger: wake word detected / power button double-tap / Launcher mic button
3. ASR streams recognition (text appears as user speaks)
4. VAD detects end of utterance → finalize text
5. Text sent to AI via sendMessage() as VOICE_MESSAGE event
6. AI response → TTS synthesis → streaming playback
7. Avatar animation syncs: IDLE → THINKING → TALKING → IDLE
8. Return to step 1 (VAD monitoring)
```

## Trigger Mechanisms

| Trigger | How | When |
|---------|-----|------|
| Wake word | KWS runs continuously alongside VAD | Always (configurable) |
| Power button double-tap | AOSP config_doublePressOnPowerBehavior | Screen on |
| Launcher mic button | UI button tap | Launcher visible |

### Power Button Double-Tap Change

Currently double-tap launches TaskManagerActivity. Change to:
- **Double-tap** → start voice input (new behavior)
- **TaskManagerActivity** → accessed only from power menu AI Activity button (already exists)

Update `vendor/dollos/overlay/frameworks/base/core/res/res/values/config.xml`:
- `config_doublePressOnPowerBehavior` → launch a voice input activity/broadcast instead of TaskManagerActivity

## Architecture

All voice components live in DollOSAIService as a `VoicePipeline` module.

```
DollOSAIService
└── voice/
    ├── VoicePipeline.kt        — orchestrator: manages ASR, TTS, VAD, KWS, speaker ID lifecycle
    ├── AsrEngine.kt            — Sherpa-ONNX OnlineRecognizer wrapper (streaming ASR)
    ├── TtsEngine.kt            — Sherpa-ONNX OfflineTts wrapper (Kokoro, streaming playback)
    ├── VadEngine.kt            — Sherpa-ONNX VoiceActivityDetector wrapper
    ├── WakeWordEngine.kt       — Sherpa-ONNX KeywordSpotter wrapper
    ├── SpeakerIdEngine.kt      — Sherpa-ONNX SpeakerEmbeddingExtractor wrapper
    └── AudioRecorder.kt        — AudioRecord wrapper (16kHz mono PCM)
```

### VoicePipeline States

```
IDLE          — VAD + KWS monitoring (low power)
LISTENING     — ASR active, streaming recognition
PROCESSING    — AI thinking (sendMessage sent, waiting for response)
SPEAKING      — TTS playing audio
```

### Component Lifecycle

- VoicePipeline initialized in DollOSAIServiceImpl init block
- Models loaded lazily on first use (avoid slow boot)
- AudioRecorder starts when wake word enabled or listening starts
- VAD runs on audio stream continuously
- KWS runs on audio stream continuously (when enabled)
- ASR starts on trigger, stops on VAD end-of-utterance
- TTS starts on AI response, stops on completion or interruption
- Speaker ID runs once per utterance start (identify who's speaking)

### Voice Interruption

When user speaks during TTS playback:
1. VAD detects speech
2. TTS playback stops immediately
3. ASR starts on the new utterance
4. Previous AI response discarded

## TTS Integration with Character Pack

`voice.json` in character pack:
```json
{
  "speed": 1.0,
  "pitch": 1.0,
  "ttsModel": "default",
  "speakerId": 0,
  "language": "zh-TW"
}
```

- `speakerId`: index into Kokoro's 103 speakers (0-102)
- Character switch → TTS engine updates speaker ID
- Future: voice cloning field reserved

## AIDL Changes

### IDollOSAIService additions

```aidl
// Voice pipeline control
void startListening();
void stopListening();
boolean isListening();

// Wake word
void setWakeWordEnabled(boolean enabled);
boolean isWakeWordEnabled();
void setWakeWord(String keyword);

// Speaker ID
void setSpeakerIdEnabled(boolean enabled);
String getRegisteredSpeakers();
void registerSpeaker(String name);
void deleteSpeaker(String name);

// TTS control
void speak(String text);
void stopSpeaking();
void setTtsSpeakerId(int speakerId);
```

### IDollOSAICallback additions

```aidl
void onSpeechRecognized(String text, boolean isFinal);
void onTtsStarted();
void onTtsCompleted();
void onWakeWordDetected();
void onSpeakerIdentified(String speakerName, float confidence);
void onVoicePipelineStateChanged(String state);
```

## Launcher Integration

### Mic Button
- Tap → `startListening()`
- ASR results displayed in real-time above input bar (or in bubble)
- Final result → sent as message

### Avatar Animation
- `onSpeechRecognized(isFinal=false)` → show partial text
- `onSpeechRecognized(isFinal=true)` → avatar → THINKING
- `onTtsStarted()` → avatar → TALKING
- `onTtsCompleted()` → avatar → IDLE

### Wake Word
- `onWakeWordDetected()` → Launcher shows listening indicator (visual feedback)
- If screen is off: wake screen, show Launcher, start listening

## Permissions

Add to DollOSAIService AndroidManifest:
```xml
<uses-permission android:name="android.permission.RECORD_AUDIO" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE" />
```

Since DollOSAIService is a system app with platform certificate, RECORD_AUDIO is auto-granted.

## Dependencies

Add to DollOSAIService `app/build.gradle.kts`:
```kotlin
implementation("com.github.k2-fsa:sherpa-onnx:v1.12.34")
```

## Model File Layout

```
/system_ext/dollos/models/voice/
├── asr/
│   ├── encoder.onnx
│   ├── decoder.onnx
│   └── tokens.txt
├── tts/
│   ├── model.onnx
│   ├── voices.bin
│   └── tokens.txt
├── vad/
│   └── silero_vad.onnx
├── kws/
│   ├── encoder.onnx
│   ├── decoder.onnx
│   ├── joiner.onnx
│   └── tokens.txt
└── speaker-id/
    └── model.onnx
```

Exact file names depend on Sherpa-ONNX model format. VoicePipeline reads from this directory at init.

## Settings UI

Add to Memory Settings or create new Voice Settings sub-page:

```
Settings → AI → Voice Settings
├── Wake Word (toggle + keyword text)
├── Speaker ID (toggle + registered speakers list + register button)
├── TTS Speed (slider)
├── TTS Speaker (dropdown, from Kokoro speakers)
└── Voice Input Language (auto / zh / en)
```

## Out of Scope

- Voice cloning (future, reserved in voice.json)
- Server-side TTS/ASR fallback (DollOS Protocol, separate spec)
- Noise cancellation / echo cancellation
- Multi-turn voice conversation without re-trigger (future enhancement)
- Custom wake word training (use pre-trained KWS model with configurable keywords)
