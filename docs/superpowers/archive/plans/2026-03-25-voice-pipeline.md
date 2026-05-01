# Voice Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Sherpa-ONNX into DollOSAIService to provide complete on-device voice: streaming ASR, TTS (Kokoro 103 speakers), wake word, VAD, and speaker identification. All models bundled in system image.

**Architecture:** A `VoicePipeline` module in DollOSAIService orchestrates 5 Sherpa-ONNX engines (ASR, TTS, VAD, KWS, Speaker ID). Audio recording feeds into VAD+KWS continuously. On trigger, ASR streams recognition, then TTS synthesizes response with streaming playback. Launcher connects via AIDL callbacks.

**Tech Stack:** Kotlin, Sherpa-ONNX (com.github.k2-fsa:sherpa-onnx), AudioRecord, AudioTrack, AIDL

---

## File Structure

### DollOSAIService (new files)

```
app/src/main/java/org/dollos/ai/voice/
  AudioRecorder.kt          — AudioRecord wrapper (16kHz mono PCM float)
  VadEngine.kt              — Sherpa-ONNX Vad wrapper
  AsrEngine.kt              — Sherpa-ONNX OnlineRecognizer wrapper (streaming)
  TtsEngine.kt              — Sherpa-ONNX OfflineTts wrapper (Kokoro, streaming playback)
  WakeWordEngine.kt         — Sherpa-ONNX KeywordSpotter wrapper
  SpeakerIdEngine.kt        — Sherpa-ONNX SpeakerEmbeddingExtractor wrapper
  VoicePipeline.kt          — orchestrator: state machine, ties all engines together
```

### DollOSAIService (modify existing)

```
  DollOSAIServiceImpl.kt    — add VoicePipeline init + AIDL voice methods
  DollOSAIApp.kt            — no change (VoicePipeline init in ServiceImpl)
  aidl/IDollOSAIService.aidl — add voice control methods
  aidl/IDollOSAICallback.aidl — add voice event callbacks
  app/build.gradle.kts       — add sherpa-onnx dependency
  app/src/main/AndroidManifest.xml — add RECORD_AUDIO permission
```

---

## Task 1: Add Sherpa-ONNX Dependency + Permissions

**Goal:** Add the Sherpa-ONNX library and audio permission.

**Files:**
- Modify: `app/build.gradle.kts`
- Modify: `app/src/main/AndroidManifest.xml`

- [ ] **Step 1: Add Sherpa-ONNX to build.gradle.kts**

Add to repositories in `settings.gradle.kts` (if not already there):
```kotlin
maven { url = uri("https://jitpack.io") }
```

Add to `app/build.gradle.kts` dependencies:
```kotlin
    // Sherpa-ONNX (on-device ASR, TTS, VAD, KWS, Speaker ID)
    implementation("com.github.k2-fsa:sherpa-onnx:v1.12.34")
```

- [ ] **Step 2: Add RECORD_AUDIO permission to AndroidManifest.xml**

```xml
<uses-permission android:name="android.permission.RECORD_AUDIO" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE" />
```

- [ ] **Step 3: Build to verify dependency resolves**

```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add app/build.gradle.kts settings.gradle.kts app/src/main/AndroidManifest.xml
git commit -m "feat: add Sherpa-ONNX dependency and RECORD_AUDIO permission"
```

---

## Task 2: AudioRecorder

**Goal:** Wrap Android AudioRecord for 16kHz mono PCM float output.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/AudioRecorder.kt`

- [ ] **Step 1: Create AudioRecorder.kt**

```kotlin
package org.dollos.ai.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log

class AudioRecorder(
    private val sampleRate: Int = 16000,
    private val onAudioData: (FloatArray) -> Unit
) {
    companion object {
        private const val TAG = "AudioRecorder"
        private const val CHUNK_DURATION_MS = 100 // 100ms chunks
    }

    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    @Volatile
    private var isRecording = false

    private val bufferSize: Int
        get() = sampleRate * CHUNK_DURATION_MS / 1000  // 1600 samples per 100ms at 16kHz

    fun start() {
        if (isRecording) return

        val minBufferSize = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )

        audioRecord = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBufferSize, bufferSize * 2)
        )

        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord initialization failed")
            return
        }

        isRecording = true
        audioRecord?.startRecording()

        recordingThread = Thread({
            val buffer = ShortArray(bufferSize)
            while (isRecording) {
                val read = audioRecord?.read(buffer, 0, buffer.size) ?: 0
                if (read > 0) {
                    val floatSamples = FloatArray(read) { buffer[it] / 32768.0f }
                    onAudioData(floatSamples)
                }
            }
        }, "AudioRecorder").apply { start() }

        Log.i(TAG, "Recording started: ${sampleRate}Hz, bufferSize=$bufferSize")
    }

    fun stop() {
        isRecording = false
        recordingThread?.join(1000)
        recordingThread = null
        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null
        Log.i(TAG, "Recording stopped")
    }

    fun isRecording(): Boolean = isRecording
}
```

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/voice/
git commit -m "feat: add AudioRecorder wrapper for 16kHz mono PCM"
```

---

## Task 3: VadEngine

**Goal:** Wrap Sherpa-ONNX VAD for continuous voice activity detection.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/VadEngine.kt`

- [ ] **Step 1: Create VadEngine.kt**

```kotlin
package org.dollos.ai.voice

import android.util.Log
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig

class VadEngine(modelDir: String) {

    companion object {
        private const val TAG = "VadEngine"
        private const val SAMPLE_RATE = 16000
    }

    private val vad: Vad

    init {
        val config = VadModelConfig(
            sileroVadModelConfig = SileroVadModelConfig(
                model = "$modelDir/silero_vad.onnx",
                threshold = 0.5f,
                minSilenceDuration = 0.5f,
                minSpeechDuration = 0.25f,
                windowSize = 512
            ),
            sampleRate = SAMPLE_RATE,
            numThreads = 1,
            provider = "cpu",
            debug = false
        )
        vad = Vad(config = config)
        Log.i(TAG, "VAD initialized")
    }

    fun acceptWaveform(samples: FloatArray) {
        vad.acceptWaveform(samples)
    }

    fun isSpeechDetected(): Boolean = vad.isSpeechDetected()

    fun hasSpeechSegment(): Boolean = !vad.empty()

    fun getSpeechSegment(): FloatArray? {
        if (vad.empty()) return null
        val segment = vad.front()
        vad.pop()
        return segment.samples
    }

    fun reset() {
        vad.reset()
    }

    fun flush() {
        vad.flush()
    }

    fun release() {
        vad.release()
        Log.i(TAG, "VAD released")
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/VadEngine.kt
git commit -m "feat: add VadEngine wrapper for Sherpa-ONNX VAD"
```

---

## Task 4: AsrEngine

**Goal:** Wrap Sherpa-ONNX OnlineRecognizer for streaming speech recognition.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/AsrEngine.kt`

- [ ] **Step 1: Create AsrEngine.kt**

```kotlin
package org.dollos.ai.voice

import android.util.Log
import com.k2fsa.sherpa.onnx.*

class AsrEngine(modelDir: String) {

    companion object {
        private const val TAG = "AsrEngine"
        private const val SAMPLE_RATE = 16000
    }

    private val recognizer: OnlineRecognizer
    private var stream: OnlineStream? = null

    var onPartialResult: ((String) -> Unit)? = null
    var onFinalResult: ((String) -> Unit)? = null

    init {
        val config = OnlineRecognizerConfig(
            featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80),
            modelConfig = OnlineModelConfig(
                paraformer = OnlineParaformerModelConfig(
                    encoder = "$modelDir/encoder.onnx",
                    decoder = "$modelDir/decoder.onnx"
                ),
                tokens = "$modelDir/tokens.txt",
                numThreads = 2,
                provider = "cpu",
                debug = false,
                modelType = "paraformer"
            ),
            enableEndpoint = true,
            decodingMethod = "greedy_search",
            maxActivePaths = 4
        )
        recognizer = OnlineRecognizer(config = config)
        Log.i(TAG, "ASR initialized (paraformer bilingual zh-en)")
    }

    fun startRecognition() {
        stream?.release()
        stream = recognizer.createStream()
        Log.d(TAG, "Recognition started")
    }

    fun feedAudio(samples: FloatArray) {
        val s = stream ?: return
        s.acceptWaveform(samples, sampleRate = SAMPLE_RATE)

        while (recognizer.isReady(s)) {
            recognizer.decode(s)
        }

        val result = recognizer.getResult(s)
        val text = result.text.trim()
        if (text.isNotEmpty()) {
            onPartialResult?.invoke(text)
        }

        if (recognizer.isEndpoint(s)) {
            if (text.isNotEmpty()) {
                onFinalResult?.invoke(text)
            }
            recognizer.reset(s)
        }
    }

    fun finishRecognition(): String {
        val s = stream ?: return ""
        s.inputFinished()
        while (recognizer.isReady(s)) {
            recognizer.decode(s)
        }
        val result = recognizer.getResult(s)
        val text = result.text.trim()
        if (text.isNotEmpty()) {
            onFinalResult?.invoke(text)
        }
        return text
    }

    fun stopRecognition() {
        stream?.release()
        stream = null
        Log.d(TAG, "Recognition stopped")
    }

    fun release() {
        stream?.release()
        recognizer.release()
        Log.i(TAG, "ASR released")
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/AsrEngine.kt
git commit -m "feat: add AsrEngine wrapper for streaming Sherpa-ONNX ASR"
```

---

## Task 5: TtsEngine

**Goal:** Wrap Sherpa-ONNX OfflineTts with Kokoro model and streaming AudioTrack playback.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/TtsEngine.kt`

- [ ] **Step 1: Create TtsEngine.kt**

```kotlin
package org.dollos.ai.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Log
import com.k2fsa.sherpa.onnx.*

class TtsEngine(modelDir: String) {

    companion object {
        private const val TAG = "TtsEngine"
    }

    private val tts: OfflineTts
    private var audioTrack: AudioTrack? = null
    @Volatile
    private var isSpeaking = false

    var speakerId: Int = 0
    var speed: Float = 1.0f
    var onStarted: (() -> Unit)? = null
    var onCompleted: (() -> Unit)? = null

    val numSpeakers: Int get() = tts.numSpeakers()
    val sampleRate: Int get() = tts.sampleRate()

    init {
        val config = OfflineTtsConfig(
            model = OfflineTtsModelConfig(
                kokoro = OfflineTtsKokoroModelConfig(
                    model = "$modelDir/model.onnx",
                    voices = "$modelDir/voices.bin",
                    tokens = "$modelDir/tokens.txt",
                    dataDir = modelDir,
                    lengthScale = 1.0f
                ),
                numThreads = 2,
                provider = "cpu",
                debug = false
            )
        )
        tts = OfflineTts(config = config)
        Log.i(TAG, "TTS initialized: Kokoro, ${tts.numSpeakers()} speakers, ${tts.sampleRate()}Hz")
    }

    fun speak(text: String) {
        if (text.isBlank()) return
        stopSpeaking()

        isSpeaking = true
        onStarted?.invoke()

        Thread({
            try {
                val audio = tts.generateWithCallback(text, speakerId, speed) { samples ->
                    if (!isSpeaking) return@generateWithCallback 0  // stop generation

                    if (audioTrack == null) {
                        initAudioTrack(tts.sampleRate())
                    }
                    val shortSamples = ShortArray(samples.size) {
                        (samples[it] * 32767).toInt().coerceIn(-32768, 32767).toShort()
                    }
                    audioTrack?.write(shortSamples, 0, shortSamples.size)
                    if (!isSpeaking) 0 else 1  // return 0 to stop, 1 to continue
                }

                if (isSpeaking) {
                    audioTrack?.stop()
                    isSpeaking = false
                    onCompleted?.invoke()
                }
            } catch (e: Exception) {
                Log.e(TAG, "TTS failed", e)
                isSpeaking = false
                onCompleted?.invoke()
            } finally {
                releaseAudioTrack()
            }
        }, "TtsPlayback").start()
    }

    fun stopSpeaking() {
        if (!isSpeaking) return
        isSpeaking = false
        releaseAudioTrack()
        Log.d(TAG, "Speaking stopped")
    }

    fun isSpeaking(): Boolean = isSpeaking

    fun release() {
        stopSpeaking()
        tts.release()
        Log.i(TAG, "TTS released")
    }

    private fun initAudioTrack(sampleRate: Int) {
        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        audioTrack = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setBufferSizeInBytes(minBuf * 2)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        audioTrack?.play()
    }

    private fun releaseAudioTrack() {
        try {
            audioTrack?.stop()
            audioTrack?.release()
        } catch (_: Exception) {}
        audioTrack = null
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/TtsEngine.kt
git commit -m "feat: add TtsEngine wrapper with Kokoro TTS and streaming AudioTrack"
```

---

## Task 6: WakeWordEngine

**Goal:** Wrap Sherpa-ONNX KeywordSpotter for wake word detection.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/WakeWordEngine.kt`

- [ ] **Step 1: Create WakeWordEngine.kt**

```kotlin
package org.dollos.ai.voice

import android.util.Log
import com.k2fsa.sherpa.onnx.*

class WakeWordEngine(modelDir: String, keyword: String = "Hey Doll") {

    companion object {
        private const val TAG = "WakeWordEngine"
        private const val SAMPLE_RATE = 16000
    }

    private val spotter: KeywordSpotter
    private var stream: OnlineStream? = null
    var onWakeWordDetected: (() -> Unit)? = null
    var enabled: Boolean = true

    init {
        val config = KeywordSpotterConfig(
            featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80),
            modelConfig = OnlineModelConfig(
                transducer = OnlineTransducerModelConfig(
                    encoder = "$modelDir/encoder.onnx",
                    decoder = "$modelDir/decoder.onnx",
                    joiner = "$modelDir/joiner.onnx"
                ),
                tokens = "$modelDir/tokens.txt",
                numThreads = 1,
                provider = "cpu",
                debug = false
            ),
            maxActivePaths = 4,
            keywordsFile = "",
            keywordsScore = 1.5f,
            keywordsThreshold = 0.25f,
            numTrailingBlanks = 2
        )
        spotter = KeywordSpotter(config = config)
        stream = spotter.createStream(keyword)
        Log.i(TAG, "Wake word engine initialized: '$keyword'")
    }

    fun feedAudio(samples: FloatArray) {
        if (!enabled) return
        val s = stream ?: return

        s.acceptWaveform(samples, sampleRate = SAMPLE_RATE)
        while (spotter.isReady(s)) {
            spotter.decode(s)
        }

        val result = spotter.getResult(s)
        val keyword = result.keyword
        if (keyword.isNotEmpty()) {
            Log.i(TAG, "Wake word detected: $keyword")
            spotter.reset(s)
            onWakeWordDetected?.invoke()
        }
    }

    fun setKeyword(keyword: String) {
        stream?.release()
        stream = spotter.createStream(keyword)
        Log.i(TAG, "Wake word changed: '$keyword'")
    }

    fun release() {
        stream?.release()
        spotter.release()
        Log.i(TAG, "Wake word engine released")
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/WakeWordEngine.kt
git commit -m "feat: add WakeWordEngine for Sherpa-ONNX keyword spotting"
```

---

## Task 7: SpeakerIdEngine

**Goal:** Wrap Sherpa-ONNX SpeakerEmbeddingExtractor for speaker identification.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/SpeakerIdEngine.kt`

- [ ] **Step 1: Create SpeakerIdEngine.kt**

```kotlin
package org.dollos.ai.voice

import android.content.Context
import android.util.Log
import com.k2fsa.sherpa.onnx.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class SpeakerIdEngine(modelPath: String, private val context: Context) {

    companion object {
        private const val TAG = "SpeakerIdEngine"
        private const val SAMPLE_RATE = 16000
        private const val SPEAKERS_FILE = "registered_speakers.json"
    }

    private val extractor: SpeakerEmbeddingExtractor
    private val registeredSpeakers = mutableMapOf<String, FloatArray>()
    var enabled: Boolean = false

    init {
        val config = SpeakerEmbeddingExtractorConfig(
            model = modelPath,
            numThreads = 1,
            debug = false,
            provider = "cpu"
        )
        extractor = SpeakerEmbeddingExtractor(config = config)
        loadSpeakers()
        Log.i(TAG, "Speaker ID initialized: dim=${extractor.dim()}, ${registeredSpeakers.size} speakers")
    }

    fun identify(audioSamples: FloatArray, threshold: Float = 0.6f): Pair<String, Float>? {
        if (!enabled || registeredSpeakers.isEmpty()) return null

        val embedding = extractEmbedding(audioSamples) ?: return null

        var bestName = ""
        var bestScore = -1f

        for ((name, registered) in registeredSpeakers) {
            val score = cosineSimilarity(embedding, registered)
            if (score > bestScore) {
                bestScore = score
                bestName = name
            }
        }

        return if (bestScore >= threshold) {
            Log.d(TAG, "Identified: $bestName (score=$bestScore)")
            bestName to bestScore
        } else {
            Log.d(TAG, "Unknown speaker (best=$bestName, score=$bestScore)")
            null
        }
    }

    fun registerSpeaker(name: String, audioSamples: FloatArray): Boolean {
        val embedding = extractEmbedding(audioSamples) ?: return false
        registeredSpeakers[name] = embedding
        saveSpeakers()
        Log.i(TAG, "Speaker registered: $name")
        return true
    }

    fun deleteSpeaker(name: String) {
        registeredSpeakers.remove(name)
        saveSpeakers()
        Log.i(TAG, "Speaker deleted: $name")
    }

    fun getRegisteredSpeakers(): String {
        val arr = JSONArray()
        registeredSpeakers.keys.forEach { arr.put(it) }
        return arr.toString()
    }

    fun release() {
        extractor.release()
        Log.i(TAG, "Speaker ID released")
    }

    private fun extractEmbedding(samples: FloatArray): FloatArray? {
        val stream = extractor.createStream()
        stream.acceptWaveform(samples, SAMPLE_RATE)
        stream.inputFinished()
        if (!extractor.isReady(stream)) return null
        val embedding = extractor.compute(stream)
        stream.release()
        return embedding
    }

    private fun cosineSimilarity(a: FloatArray, b: FloatArray): Float {
        if (a.size != b.size) return 0f
        var dot = 0f; var normA = 0f; var normB = 0f
        for (i in a.indices) {
            dot += a[i] * b[i]
            normA += a[i] * a[i]
            normB += b[i] * b[i]
        }
        val denom = Math.sqrt(normA.toDouble()) * Math.sqrt(normB.toDouble())
        return if (denom == 0.0) 0f else (dot / denom.toFloat())
    }

    private fun saveSpeakers() {
        val json = JSONObject()
        for ((name, embedding) in registeredSpeakers) {
            val arr = JSONArray()
            embedding.forEach { arr.put(it.toDouble()) }
            json.put(name, arr)
        }
        File(context.filesDir, SPEAKERS_FILE).writeText(json.toString())
    }

    private fun loadSpeakers() {
        val file = File(context.filesDir, SPEAKERS_FILE)
        if (!file.exists()) return
        try {
            val json = JSONObject(file.readText())
            for (name in json.keys()) {
                val arr = json.getJSONArray(name)
                val embedding = FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
                registeredSpeakers[name] = embedding
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load speakers", e)
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/SpeakerIdEngine.kt
git commit -m "feat: add SpeakerIdEngine for speaker identification"
```

---

## Task 8: VoicePipeline

**Goal:** Orchestrator that ties all voice engines together with a state machine.

**Files:**
- Create: `app/src/main/java/org/dollos/ai/voice/VoicePipeline.kt`

- [ ] **Step 1: Create VoicePipeline.kt**

```kotlin
package org.dollos.ai.voice

import android.content.Context
import android.util.Log

enum class VoicePipelineState {
    IDLE,       // VAD + KWS monitoring
    LISTENING,  // ASR active
    PROCESSING, // AI thinking
    SPEAKING    // TTS playing
}

class VoicePipeline(private val context: Context) {

    companion object {
        private const val TAG = "VoicePipeline"
        private const val MODEL_BASE = "/system_ext/dollos/models/voice"
    }

    @Volatile  // All state transitions should be synchronized
    var state: VoicePipelineState = VoicePipelineState.IDLE
        private set

    @Volatile
    var isVoiceMessagePending = false

    // Engines (lazy init)
    private var vadEngine: VadEngine? = null
    private var asrEngine: AsrEngine? = null
    private var ttsEngine: TtsEngine? = null
    private var wakeWordEngine: WakeWordEngine? = null
    private var speakerIdEngine: SpeakerIdEngine? = null
    private var audioRecorder: AudioRecorder? = null

    // Callbacks
    var onSpeechRecognized: ((text: String, isFinal: Boolean) -> Unit)? = null
    var onTtsStarted: (() -> Unit)? = null
    var onTtsCompleted: (() -> Unit)? = null
    var onWakeWordDetected: (() -> Unit)? = null
    var onSpeakerIdentified: ((name: String, confidence: Float) -> Unit)? = null
    var onStateChanged: ((VoicePipelineState) -> Unit)? = null
    var onFinalText: ((String) -> Unit)? = null  // called when final ASR text ready to send to AI

    private var isInitialized = false
    private var initFailed = false

    fun init() {
        if (isInitialized) return
        if (initFailed) return  // prevent retry loops after failure

        try {
            vadEngine = VadEngine("$MODEL_BASE/vad")
            asrEngine = AsrEngine("$MODEL_BASE/asr").apply {
                onPartialResult = { text -> onSpeechRecognized?.invoke(text, false) }
                onFinalResult = { text -> onSpeechRecognized?.invoke(text, true) }
            }
            ttsEngine = TtsEngine("$MODEL_BASE/tts").apply {
                onStarted = {
                    setState(VoicePipelineState.SPEAKING)
                    onTtsStarted?.invoke()
                }
                onCompleted = {
                    setState(VoicePipelineState.IDLE)
                    onTtsCompleted?.invoke()
                    startMonitoring() // resume VAD/KWS after speaking
                }
            }
            wakeWordEngine = WakeWordEngine("$MODEL_BASE/kws").apply {
                onWakeWordDetected = {
                    this@VoicePipeline.onWakeWordDetected?.invoke()
                    startListening()
                }
            }
            speakerIdEngine = SpeakerIdEngine("$MODEL_BASE/speaker-id/model.onnx", context)

            isInitialized = true
            Log.i(TAG, "Voice pipeline initialized")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize voice pipeline", e)
            initFailed = true
        }
    }

    fun startMonitoring() {
        if (!isInitialized) init()
        if (audioRecorder?.isRecording() == true) return

        audioRecorder = AudioRecorder { samples ->
            // Feed to VAD
            vadEngine?.acceptWaveform(samples)

            // Feed to KWS (if enabled)
            if (wakeWordEngine?.enabled == true && state == VoicePipelineState.IDLE) {
                wakeWordEngine?.feedAudio(samples)
            }

            // If speaking and VAD detects speech → voice interruption
            if (state == VoicePipelineState.SPEAKING && vadEngine?.isSpeechDetected() == true) {
                Log.d(TAG, "Voice interruption detected")
                ttsEngine?.stopSpeaking()
                startListening()
            }

            // If listening, feed to ASR
            if (state == VoicePipelineState.LISTENING) {
                asrEngine?.feedAudio(samples)

                // Check if utterance ended (VAD has a completed speech segment)
                if (vadEngine!!.hasSpeechSegment()) {
                    val text = asrEngine?.finishRecognition() ?: ""
                    if (text.isNotEmpty()) {
                        setState(VoicePipelineState.PROCESSING)
                        onFinalText?.invoke(text)
                    } else {
                        setState(VoicePipelineState.IDLE)
                    }
                }
            }
        }
        audioRecorder?.start()
        setState(VoicePipelineState.IDLE)
        Log.i(TAG, "Monitoring started")
    }

    fun startListening() {
        if (!isInitialized) init()
        if (state == VoicePipelineState.LISTENING) return

        ttsEngine?.stopSpeaking()  // stop if speaking
        asrEngine?.startRecognition()
        setState(VoicePipelineState.LISTENING)

        // If not already recording, start
        if (audioRecorder?.isRecording() != true) {
            startMonitoring()
        }

        // Speaker ID on first audio chunk
        speakerIdEngine?.let { sid ->
            if (sid.enabled) {
                // Will be identified from accumulated audio in ASR
            }
        }

        Log.i(TAG, "Listening started")
    }

    fun stopListening() {
        if (state != VoicePipelineState.LISTENING) return
        val text = asrEngine?.finishRecognition() ?: ""
        asrEngine?.stopRecognition()
        setState(VoicePipelineState.IDLE)
        if (text.isNotEmpty()) {
            setState(VoicePipelineState.PROCESSING)
            onFinalText?.invoke(text)
        }
        Log.i(TAG, "Listening stopped")
    }

    fun speak(text: String) {
        ttsEngine?.speak(text)
    }

    fun stopSpeaking() {
        ttsEngine?.stopSpeaking()
        setState(VoicePipelineState.IDLE)
    }

    fun onAiResponseComplete(responseText: String) {
        // Called by DollOSAIServiceImpl when AI response is ready
        speak(responseText)
    }

    fun setWakeWordEnabled(enabled: Boolean) {
        wakeWordEngine?.enabled = enabled
        Log.i(TAG, "Wake word ${if (enabled) "enabled" else "disabled"}")
    }

    fun isWakeWordEnabled(): Boolean = wakeWordEngine?.enabled ?: false

    fun setWakeWord(keyword: String) {
        wakeWordEngine?.setKeyword(keyword)
    }

    fun setSpeakerIdEnabled(enabled: Boolean) {
        speakerIdEngine?.enabled = enabled
    }

    fun registerSpeaker(name: String): Boolean {
        // TODO: need to record audio first, then register
        Log.w(TAG, "registerSpeaker: need audio recording flow")
        return false
    }

    fun deleteSpeaker(name: String) {
        speakerIdEngine?.deleteSpeaker(name)
    }

    fun getRegisteredSpeakers(): String {
        return speakerIdEngine?.getRegisteredSpeakers() ?: "[]"
    }

    fun setTtsSpeakerId(id: Int) {
        ttsEngine?.speakerId = id
    }

    fun setTtsSpeed(speed: Float) {
        ttsEngine?.speed = speed
    }

    fun isListening(): Boolean = state == VoicePipelineState.LISTENING

    fun stopAll() {
        audioRecorder?.stop()
        audioRecorder = null
        asrEngine?.stopRecognition()
        ttsEngine?.stopSpeaking()
        setState(VoicePipelineState.IDLE)
    }

    fun release() {
        stopAll()
        vadEngine?.release()
        asrEngine?.release()
        ttsEngine?.release()
        wakeWordEngine?.release()
        speakerIdEngine?.release()
        isInitialized = false
        Log.i(TAG, "Voice pipeline released")
    }

    private fun setState(newState: VoicePipelineState) {
        if (state != newState) {
            Log.d(TAG, "State: $state -> $newState")
            state = newState
            onStateChanged?.invoke(newState)
        }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/VoicePipeline.kt
git commit -m "feat: add VoicePipeline orchestrator with state machine"
```

---

## Task 9: AIDL Updates

**Goal:** Add voice control methods and callbacks to AIDL.

**Files:**
- Modify: `aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `aidl/org/dollos/ai/IDollOSAICallback.aidl`

- [ ] **Step 1: Update IDollOSAIService.aidl**

Add before closing `}`:

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
    void setTtsSpeed(float speed);
```

- [ ] **Step 2: Update IDollOSAICallback.aidl**

Add:

```aidl
    void onSpeechRecognized(String text, boolean isFinal);
    void onTtsStarted();
    void onTtsCompleted();
    void onWakeWordDetected();
    void onSpeakerIdentified(String speakerName, float confidence);
    void onVoicePipelineStateChanged(String state);
```

- [ ] **Step 3: Commit**

```bash
git add aidl/
git commit -m "feat: add voice pipeline AIDL methods and callbacks"
```

---

## Task 10: DollOSAIServiceImpl Integration

**Goal:** Wire VoicePipeline into the service, implement AIDL methods, connect TTS to AI responses.

**Files:**
- Modify: `app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`
- Modify: `app/src/main/java/org/dollos/ai/TestActivity.kt`

- [ ] **Step 1: Add VoicePipeline to DollOSAIServiceImpl**

Add import:
```kotlin
import org.dollos.ai.voice.VoicePipeline
import org.dollos.ai.voice.VoicePipelineState
```

Add field:
```kotlin
private val voicePipeline = VoicePipeline(DollOSAIApp.instance)
```

Add to init block (after existing init code):
```kotlin
// Initialize voice pipeline (lazy — models loaded on first use)
voicePipeline.onSpeechRecognized = { text, isFinal ->
    broadcastSpeechRecognized(text, isFinal)
}
voicePipeline.onTtsStarted = { broadcastTtsStarted() }
voicePipeline.onTtsCompleted = { broadcastTtsCompleted() }
voicePipeline.onWakeWordDetected = { broadcastWakeWordDetected() }
voicePipeline.onSpeakerIdentified = { name, conf -> broadcastSpeakerIdentified(name, conf) }
voicePipeline.onStateChanged = { state -> broadcastVoicePipelineStateChanged(state.name) }
voicePipeline.onFinalText = { text ->
    // Voice input → treat as VOICE_MESSAGE
    voicePipeline.isVoiceMessagePending = true
    scope.launch {
        eventQueue.push(org.dollos.ai.event.Event(
            type = org.dollos.ai.event.EventType.VOICE_MESSAGE,
            payload = text,
            source = "voice"
        ))
        sendMessage(text)  // Process as normal message
    }
}

// Wire TTS to AI response — speak the response after streaming completes
// This is done in the onComplete callback in sendMessage()
```

- [ ] **Step 2: Modify sendMessage onComplete to trigger TTS**

In `sendMessage()`, inside `onComplete` callback, after `broadcastComplete(response.content)`:

```kotlin
// If this was a voice message, speak the response
if (voicePipeline.isVoiceMessagePending) {
    voicePipeline.isVoiceMessagePending = false
    voicePipeline.onAiResponseComplete(response.content)
}
```

- [ ] **Step 3: Add AIDL implementations**

```kotlin
// --- Voice Pipeline ---

override fun startListening() {
    voicePipeline.startListening()
}

override fun stopListening() {
    voicePipeline.stopListening()
}

override fun isListening(): Boolean = voicePipeline.isListening()

override fun setWakeWordEnabled(enabled: Boolean) {
    voicePipeline.setWakeWordEnabled(enabled)
    if (enabled) voicePipeline.startMonitoring()
}

override fun isWakeWordEnabled(): Boolean = voicePipeline.isWakeWordEnabled()

override fun setWakeWord(keyword: String?) {
    if (keyword != null) voicePipeline.setWakeWord(keyword)
}

override fun setSpeakerIdEnabled(enabled: Boolean) {
    voicePipeline.setSpeakerIdEnabled(enabled)
}

override fun getRegisteredSpeakers(): String = voicePipeline.getRegisteredSpeakers()

override fun registerSpeaker(name: String?) {
    if (name != null) voicePipeline.registerSpeaker(name)
}

override fun deleteSpeaker(name: String?) {
    if (name != null) voicePipeline.deleteSpeaker(name)
}

override fun speak(text: String?) {
    if (text != null) voicePipeline.speak(text)
}

override fun stopSpeaking() {
    voicePipeline.stopSpeaking()
}

override fun setTtsSpeakerId(speakerId: Int) {
    voicePipeline.setTtsSpeakerId(speakerId)
}

override fun setTtsSpeed(speed: Float) {
    voicePipeline.setTtsSpeed(speed)
}
```

- [ ] **Step 4: Add broadcast helpers**

```kotlin
private fun broadcastSpeechRecognized(text: String, isFinal: Boolean) {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onSpeechRecognized(text, isFinal) } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}

private fun broadcastTtsStarted() {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onTtsStarted() } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}

private fun broadcastTtsCompleted() {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onTtsCompleted() } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}

private fun broadcastWakeWordDetected() {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onWakeWordDetected() } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}

private fun broadcastSpeakerIdentified(name: String, confidence: Float) {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onSpeakerIdentified(name, confidence) } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}

private fun broadcastVoicePipelineStateChanged(state: String) {
    val n = callbacks.beginBroadcast()
    try { for (i in 0 until n) {
        try { callbacks.getBroadcastItem(i).onVoicePipelineStateChanged(state) } catch (_: Exception) {}
    } } finally { callbacks.finishBroadcast() }
}
```

- [ ] **Step 5: Update pauseAll to stop voice**

In `pauseAll()`, add:
```kotlin
voicePipeline.stopAll()
```

- [ ] **Step 6: Update TestActivity callback stubs**

Add to TestActivity's callback object:
```kotlin
override fun onSpeechRecognized(text: String?, isFinal: Boolean) {
    Log.i("TestActivity", "ASR: $text (final=$isFinal)")
}
override fun onTtsStarted() { Log.i("TestActivity", "TTS started") }
override fun onTtsCompleted() { Log.i("TestActivity", "TTS completed") }
override fun onWakeWordDetected() { Log.i("TestActivity", "Wake word detected!") }
override fun onSpeakerIdentified(name: String?, confidence: Float) {
    Log.i("TestActivity", "Speaker: $name ($confidence)")
}
override fun onVoicePipelineStateChanged(state: String?) {
    Log.i("TestActivity", "Voice state: $state")
}
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: integrate VoicePipeline into DollOSAIServiceImpl"
```

---

## Task 11: Launcher Mic Button Integration

**Goal:** Wire Launcher's mic button to voice pipeline via AIDL.

**Files:**
- Modify: `~/Projects/DollOSLauncher/app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`

- [ ] **Step 1: Update Launcher callback**

Add voice callbacks to the AIDL callback object in DollOSLauncherActivity:

```kotlin
override fun onSpeechRecognized(text: String?, isFinal: Boolean) {
    text ?: return
    runOnUiThread {
        if (isFinal) {
            // Final text — will be processed by AI service automatically
            responseBubble.clear()
        } else {
            // Partial text — show in bubble as user speaks
            responseBubble.setComplete("🎤 $text")
        }
    }
}

override fun onTtsStarted() {
    avatarAnimator.onFirstToken()  // TALKING animation
}

override fun onTtsCompleted() {
    avatarAnimator.onResponseComplete()  // IDLE animation
}

override fun onWakeWordDetected() {
    Log.i(TAG, "Wake word detected")
    runOnUiThread {
        responseBubble.setComplete("🎤 Listening...")
    }
}

override fun onSpeakerIdentified(name: String?, confidence: Float) {
    Log.i(TAG, "Speaker: $name ($confidence)")
}

override fun onVoicePipelineStateChanged(state: String?) {
    Log.d(TAG, "Voice state: $state")
}
```

- [ ] **Step 2: Wire mic button**

Find the mic button click handler (currently placeholder) and change to:

```kotlin
// Mic button
val micButton = inputBarView.findViewById<View>(R.id.mic_button)
micButton?.setOnClickListener {
    try {
        if (aiService?.isListening == true) {
            aiService?.stopListening()
        } else {
            aiService?.startListening()
            responseBubble.setComplete("🎤 Listening...")
        }
    } catch (e: Exception) {
        Log.e(TAG, "Voice toggle failed", e)
    }
}
```

- [ ] **Step 3: Build Launcher**

```bash
cd ~/Projects/DollOSLauncher
./gradlew assembleRelease
```

If AIDL is out of sync, copy updated AIDL files:
```bash
cp ~/Projects/DollOSAIService/aidl/org/dollos/ai/*.aidl ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai/
```

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/DollOSLauncher
git add -A
git commit -m "feat: wire mic button and voice callbacks to Launcher"
```

---

## Task 12: Voice Settings UI

**Goal:** Add a Voice Settings sub-page to the Settings app for configuring wake word, speaker ID, TTS speed, and TTS speaker.

**Files:**
- Create: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_voice_settings.xml`
- Create: `~/Projects/DollOS-build/packages/apps/Settings/src/com/android/settings/dollos/DollOSVoiceSettingsFragment.java`
- Modify: `~/Projects/DollOS-build/packages/apps/Settings/res/xml/dollos_ai_settings.xml`

- [ ] **Step 1: Create dollos_voice_settings.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<PreferenceScreen
    xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:settings="http://schemas.android.com/apk/res-auto"
    android:title="@string/dollos_voice_settings_title">

    <!-- Wake Word -->
    <PreferenceCategory android:title="Wake Word">
        <SwitchPreference
            android:key="dollos_wake_word_enabled"
            android:title="Enable Wake Word"
            android:summary="Activate voice input by saying a keyword"
            android:defaultValue="false" />
        <EditTextPreference
            android:key="dollos_wake_word_keyword"
            android:title="Wake Word Keyword"
            android:summary="The keyword to listen for"
            android:defaultValue="Hey Doll"
            android:dependency="dollos_wake_word_enabled" />
    </PreferenceCategory>

    <!-- Speaker ID -->
    <PreferenceCategory android:title="Speaker Identification">
        <SwitchPreference
            android:key="dollos_speaker_id_enabled"
            android:title="Enable Speaker ID"
            android:summary="Identify who is speaking"
            android:defaultValue="false" />
        <Preference
            android:key="dollos_speaker_id_list"
            android:title="Registered Speakers"
            android:summary="Manage registered speaker profiles"
            android:dependency="dollos_speaker_id_enabled" />
    </PreferenceCategory>

    <!-- TTS -->
    <PreferenceCategory android:title="Text-to-Speech">
        <SeekBarPreference
            android:key="dollos_tts_speed"
            android:title="TTS Speed"
            android:defaultValue="10"
            android:max="20"
            settings:min="1" />
        <ListPreference
            android:key="dollos_tts_speaker"
            android:title="TTS Speaker"
            android:summary="Select voice for speech synthesis" />
    </PreferenceCategory>

</PreferenceScreen>
```

- [ ] **Step 2: Create DollOSVoiceSettingsFragment.java**

```java
package com.android.settings.dollos;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Bundle;
import android.os.IBinder;
import android.os.RemoteException;
import android.util.Log;

import androidx.preference.Preference;
import androidx.preference.SwitchPreference;
import androidx.preference.SeekBarPreference;
import androidx.preference.ListPreference;

import com.android.settings.R;
import com.android.settings.dashboard.DashboardFragment;

import org.dollos.ai.IDollOSAIService;

public class DollOSVoiceSettingsFragment extends DashboardFragment {
    private static final String TAG = "DollOSVoiceSettings";

    private IDollOSAIService aiService;
    private final ServiceConnection connection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            aiService = IDollOSAIService.Stub.asInterface(service);
            loadCurrentValues();
        }
        @Override
        public void onServiceDisconnected(ComponentName name) {
            aiService = null;
        }
    };

    @Override
    public int getMetricsCategory() { return 0; }

    @Override
    protected String getLogTag() { return TAG; }

    @Override
    protected int getPreferenceScreenResId() {
        return R.xml.dollos_voice_settings;
    }

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = new Intent("org.dollos.ai.IDollOSAIService");
        intent.setPackage("org.dollos.ai");
        getContext().bindService(intent, connection, Context.BIND_AUTO_CREATE);

        SwitchPreference wakeWordEnabled = findPreference("dollos_wake_word_enabled");
        if (wakeWordEnabled != null) {
            wakeWordEnabled.setOnPreferenceChangeListener((pref, newValue) -> {
                try {
                    if (aiService != null) aiService.setWakeWordEnabled((Boolean) newValue);
                } catch (RemoteException e) { Log.e(TAG, "setWakeWordEnabled failed", e); }
                return true;
            });
        }

        SwitchPreference speakerIdEnabled = findPreference("dollos_speaker_id_enabled");
        if (speakerIdEnabled != null) {
            speakerIdEnabled.setOnPreferenceChangeListener((pref, newValue) -> {
                try {
                    if (aiService != null) aiService.setSpeakerIdEnabled((Boolean) newValue);
                } catch (RemoteException e) { Log.e(TAG, "setSpeakerIdEnabled failed", e); }
                return true;
            });
        }

        SeekBarPreference ttsSpeed = findPreference("dollos_tts_speed");
        if (ttsSpeed != null) {
            ttsSpeed.setOnPreferenceChangeListener((pref, newValue) -> {
                float speed = ((Integer) newValue) / 10.0f;
                try {
                    if (aiService != null) aiService.setTtsSpeed(speed);
                } catch (RemoteException e) { Log.e(TAG, "setTtsSpeed failed", e); }
                return true;
            });
        }
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        getContext().unbindService(connection);
    }

    private void loadCurrentValues() {
        try {
            SwitchPreference wakeWordEnabled = findPreference("dollos_wake_word_enabled");
            if (wakeWordEnabled != null && aiService != null) {
                wakeWordEnabled.setChecked(aiService.isWakeWordEnabled());
            }
        } catch (RemoteException e) { Log.e(TAG, "loadCurrentValues failed", e); }
    }
}
```

- [ ] **Step 3: Add Voice Settings entry to dollos_ai_settings.xml**

Add to `dollos_ai_settings.xml` before the closing `</PreferenceScreen>`:

```xml
<Preference
    android:key="dollos_voice_settings"
    android:title="Voice Settings"
    android:summary="Wake word, speaker ID, TTS configuration"
    android:fragment="com.android.settings.dollos.DollOSVoiceSettingsFragment" />
```

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/DollOS-build
git add packages/apps/Settings/
git commit -m "feat: add Voice Settings UI page"
```

---

## Task 13: Power Button Double-Tap → Voice Input

**Goal:** Change power button double-tap from TaskManager to voice input.

**Files:**
- Modify: `~/Projects/DollOS-build/vendor/dollos/overlay/frameworks/base/core/res/res/values/config.xml`

- [ ] **Step 1: Update overlay config**

Change the double-press target from TaskManagerActivity to a voice input broadcast:

```xml
<!-- DollOS: double-press power button starts voice input -->
<integer name="config_doublePressOnPowerBehavior">3</integer>
<string name="config_doublePressOnPowerTargetActivity" translatable="false">org.dollos.ai/.voice.VoiceInputActivity</string>
```

- [ ] **Step 2: Create VoiceInputActivity**

Create a lightweight activity in DollOSAIService that sends a broadcast to start listening (avoids bind/unbind race condition):

Create `app/src/main/java/org/dollos/ai/voice/VoiceInputActivity.kt`:

```kotlin
package org.dollos.ai.voice

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.util.Log

class VoiceInputActivity : Activity() {

    companion object {
        private const val TAG = "VoiceInput"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Instead of bindService, just send a broadcast
        val intent = Intent("org.dollos.ai.ACTION_VOICE_INPUT")
        intent.setPackage("org.dollos.ai")
        sendBroadcast(intent)
        Log.i(TAG, "Voice input broadcast sent via power button")
        finish()
    }
}
```

Register a BroadcastReceiver in `DollOSAIServiceImpl` init block to handle this:

```kotlin
// Register voice input broadcast receiver (for power button double-tap)
val voiceInputReceiver = object : android.content.BroadcastReceiver() {
    override fun onReceive(context: android.content.Context?, intent: android.content.Intent?) {
        Log.i("DollOSAIServiceImpl", "Voice input broadcast received")
        voicePipeline.startListening()
    }
}
val filter = android.content.IntentFilter("org.dollos.ai.ACTION_VOICE_INPUT")
DollOSAIApp.instance.registerReceiver(voiceInputReceiver, filter, android.content.Context.RECEIVER_NOT_EXPORTED)
```

- [ ] **Step 3: Add to AndroidManifest.xml**

```xml
<activity
    android:name=".voice.VoiceInputActivity"
    android:exported="true"
    android:theme="@android:style/Theme.Translucent.NoTitleBar"
    android:excludeFromRecents="true"
    android:launchMode="singleTask" />
```

- [ ] **Step 4: Commit both repos**

```bash
cd ~/Projects/DollOSAIService
git add -A
git commit -m "feat: add VoiceInputActivity for power button double-tap"

cd ~/Projects/DollOS-build/vendor/dollos
git add -A
git commit -m "feat: change power double-tap to voice input"
```

---

## Task 14: Build, Deploy, and Verify

**Goal:** Build everything, deploy, verify voice pipeline works.

- [ ] **Step 1: Build DollOSAIService**

```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
```

Fix any compile errors (likely: missing overrides, AIDL mismatches).

- [ ] **Step 2: Deploy to AOSP + device**

```bash
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService DollOSLauncher -j$(nproc)
```

Push to device and reboot.

- [ ] **Step 3: Verify voice pipeline init**

```bash
adb logcat -d | grep -iE "VoicePipeline|AsrEngine|TtsEngine|VadEngine|WakeWord|SpeakerId" | head -20
```

Note: models may not be on device yet (they need to be placed in `/system_ext/dollos/models/voice/`). Voice pipeline will log "Failed to initialize" if models are missing — this is expected until models are bundled.

- [ ] **Step 4: Test TTS via adb**

If models are available, test TTS:
```bash
# Via TestActivity or service call
adb logcat -d | grep -iE "TTS|speak" | head -10
```

---

## Notes

### Model Bundling

Models are NOT bundled in this plan — they need to be downloaded and placed at `/system_ext/dollos/models/voice/` separately. The voice pipeline gracefully handles missing models (logs warning, voice features disabled).

To bundle in system image, add model files to `vendor/dollos/` and copy them via `dollos_bluejay.mk` PRODUCT_COPY_FILES.

### Sherpa-ONNX API Compatibility

The Sherpa-ONNX Kotlin API uses constructor-based config (not Builder pattern in all cases). The exact constructor signatures may vary between versions. If `v1.12.34` has different constructors, check the actual API source at `com.k2fsa.sherpa.onnx.*` and adjust.

### Voice Interruption

When user speaks during TTS playback, VAD detects speech → TTS stops → ASR starts. This creates a natural conversation flow without needing a "stop" button.

### TTS and AI Response

TTS only triggers for voice-initiated messages. Text-only messages (typed in Launcher) do not trigger TTS. This is controlled by checking `voicePipeline.state` when the AI response completes.
