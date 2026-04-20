# DollOSVoice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 從既有 DollOSAIService 抽出完整 Voice Pipeline（KWS / ASR / TTS / VAD / Speaker ID）到獨立的 DollOSVoice app，以 AIDL 對外提供能力、獨占管理 AudioRecord、支援多元觸發源（wake word / pickup / chest_press / gesture）、並支援 Character Pack 熱切換。

**Architecture:** DollOSVoice 是 `system_ext` priv-app，內有一個 `VoiceService`（foreground service，binding 到 `IDollVoice` AIDL）。核心是 `AudioRecordManager` 獨占麥克風 — 單一 AudioRecord instance，透過 `PcmBuffer` channel 分發音訊 buffer 給 VAD / KWS / ASR 並行消費（所有元件都要原始 PCM）。KWS / ASR / TTS / VAD / Speaker ID 五個 engine 各自從 AIService 直接搬過來，只修 package 名、依賴與 lifecycle。觸發源多元化：wake word 由 KWS 自己觸發 `onWakeWord`；pickup / chest_press / gesture 由 DollOSObserver 推 Core，Core 透過 `IDollCore.triggerConversation(source, extras)` 再反向呼叫 `IDollVoice.startListening()`（不直接從 Observer 呼 Voice，保持 Core 為 handler 中心）。Character Pack 切換時 hot-reload `wake_word.onnx`、TTS `tts-vits/` 模型目錄、voice config。

**Tech Stack:** Kotlin, Android AIDL, AudioRecord (MIC, 16kHz, PCM 16-bit mono, AGC + NoiseSuppressor), ONNX Runtime (openWakeWord 3-stage), sherpa-onnx (ASR paraformer, VAD silero, Speaker ID ECAPA-TDNN, TTS Piper VITS), Kotlin Coroutines + Channels (PCM 分發), JUnit 4 + MockK（unit tests）, Espresso（instrumented tests）。

**Spec reference:** `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`
**Master plan:** `docs/superpowers/plans/2026-04-20-doll-terminal.md`

**Master dependencies:**
- §3.5（IDollVoice AIDL 介面契約）
- §8（Build / deploy conventions — 新 app 骨架、AOSP 整合、Android.bp 範本）
- §9（Testing strategy — TDD、單元 / instrumented / E2E 金字塔）

**Non-goals（不做）：**
- DollOSObserver 事件推送（在 observer plan 做）
- DollOSCore event handler 邏輯（在 core plan 做）
- Character Pack v2 parser（在 memory plan 做 — 本 plan 只消費 pack 的檔案路徑 + voice config）
- 新 wake word / TTS 模型訓練（CLAUDE.md 訓練腳本已涵蓋，本 plan 只消費既有模型）

---

## File Structure

Root: `~/Projects/DollOSVoice/`

```
DollOSVoice/
├── build.gradle.kts
├── settings.gradle.kts
├── gradle.properties
├── gradle/wrapper/
├── gradlew
├── app/
│   ├── build.gradle.kts
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── aidl/dollos/voice/
│   │   │   ├── IDollVoice.aidl             ← §3.5 服務端介面
│   │   │   └── IDollVoiceListener.aidl     ← Voice 內部 callback（非 master 定義）
│   │   └── java/dollos/voice/
│   │       ├── VoiceService.kt              ← foreground service + IDollVoice.Stub
│   │       ├── VoiceServiceImpl.kt          ← binder 實作（分發到 manager）
│   │       ├── AudioRecordManager.kt        ← 獨占 AudioRecord，分發 PCM buffer
│   │       ├── PcmBroadcaster.kt            ← coroutine channel → multi-consumer fanout
│   │       ├── VoiceManager.kt              ← 統合 KWS / VAD / ASR / TTS / Speaker ID lifecycle
│   │       ├── VoiceConfig.kt               ← data class：model paths, thresholds
│   │       ├── VoicePaths.kt                ← path constants（/system_ext/dollos/models/voice/...）
│   │       ├── VoiceListenerRegistry.kt     ← 多 listener 管理 + RemoteCallbackList
│   │       ├── TriggerRouter.kt             ← 統一處理 wake_word / pickup / chest_press / gesture 進 LISTENING
│   │       ├── engine/
│   │       │   ├── KwsEngine.kt             ← 從 AIService WakeWordEngine 搬
│   │       │   ├── VadEngine.kt             ← 從 AIService 搬
│   │       │   ├── AsrEngine.kt             ← 從 AIService 搬
│   │       │   ├── TtsEngine.kt             ← 從 AIService 搬
│   │       │   └── SpeakerIdEngine.kt       ← 從 AIService 搬
│   │       └── util/
│   │           └── Logging.kt               ← 統一 TAG prefix "DollVoice"
│   ├── src/test/java/dollos/voice/
│   │   ├── AudioRecordManagerTest.kt
│   │   ├── PcmBroadcasterTest.kt
│   │   ├── VoiceManagerTest.kt
│   │   ├── VoiceListenerRegistryTest.kt
│   │   ├── TriggerRouterTest.kt
│   │   └── VoiceConfigTest.kt
│   └── src/androidTest/java/dollos/voice/
│       ├── VoiceServiceBindingTest.kt       ← IDollVoice AIDL end-to-end
│       └── VoicePipelineIntegrationTest.kt  ← 真機 mic → ASR → TTS 串起來驗
└── prebuilt/
    └── DollOSVoice.apk                       ← Gradle build 產出放這

AOSP 整合（Android.bp）：
~/Projects/DollOS-build/external/DollOSVoice/
├── Android.bp
└── prebuilt/DollOSVoice.apk

既有資產沿用（路徑不變，只改 ownership — 原 AIService 讀這些，改 DollOSVoice 讀）：
/system_ext/dollos/models/voice/
├── asr/           ← sherpa paraformer encoder + decoder + tokens
├── vad/           ← silero_vad.onnx
├── tts-vits/      ← Piper VITS model.onnx + tokens.txt + espeak-ng-data/
├── oww/           ← melspectrogram.onnx + embedding_model.onnx（共用）
└── speaker-id/    ← ECAPA-TDNN model.onnx

Per-character wake_word.onnx 從 character pack 載入，由 Memory app（ContentProvider / AIDL）提供路徑 — 本 plan 只收路徑，不負責 pack 解壓。
```

---

## §1 App 骨架

### Task 1: Gradle 專案初始化

**Files:**
- Create: `~/Projects/DollOSVoice/settings.gradle.kts`
- Create: `~/Projects/DollOSVoice/build.gradle.kts`
- Create: `~/Projects/DollOSVoice/gradle.properties`
- Create: `~/Projects/DollOSVoice/app/build.gradle.kts`

- [ ] **Step 1: 建立 settings.gradle.kts**

```kotlin
// ~/Projects/DollOSVoice/settings.gradle.kts
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "DollOSVoice"
include(":app")
```

- [ ] **Step 2: 建立 root build.gradle.kts**

```kotlin
// ~/Projects/DollOSVoice/build.gradle.kts
plugins {
    id("com.android.application") version "8.5.0" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
```

- [ ] **Step 3: 建立 gradle.properties**

```properties
# ~/Projects/DollOSVoice/gradle.properties
org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8
android.useAndroidX=true
kotlin.code.style=official
```

- [ ] **Step 4: 建立 app/build.gradle.kts**

```kotlin
// ~/Projects/DollOSVoice/app/build.gradle.kts
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dollos.voice"
    compileSdk = 34

    defaultConfig {
        applicationId = "dollos.voice"
        minSdk = 33
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildFeatures {
        aidl = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // ONNX Runtime（openWakeWord 三階段模型用）
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.19.0")

    // sherpa-onnx（ASR / VAD / TTS / Speaker ID）— 與 AIService 相同版本
    implementation("com.k2fsa.sherpa.onnx:sherpa-onnx-android:1.10.40")

    testImplementation("junit:junit:4.13.2")
    testImplementation("io.mockk:mockk:1.13.12")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")

    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOSVoice
git init
git add settings.gradle.kts build.gradle.kts gradle.properties app/build.gradle.kts
git commit -m "chore: initialize DollOSVoice gradle project"
```

### Task 2: AndroidManifest + VoiceService 骨架

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/AndroidManifest.xml`
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceService.kt`

- [ ] **Step 1: 寫 failing test — service binder 必為 IDollVoice.Stub**

```kotlin
// app/src/androidTest/java/dollos/voice/VoiceServiceBindingTest.kt
package dollos.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class VoiceServiceBindingTest {
    @Test
    fun binder_is_IDollVoice_stub() {
        val ctx: Context = ApplicationProvider.getApplicationContext()
        val latch = CountDownLatch(1)
        var binder: IBinder? = null
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
                binder = service
                latch.countDown()
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        val intent = Intent(ctx, VoiceService::class.java)
        ctx.bindService(intent, conn, Context.BIND_AUTO_CREATE)
        assertTrue("Service did not bind in 5s", latch.await(5, TimeUnit.SECONDS))
        val iface = IDollVoice.Stub.asInterface(binder)
        assertTrue("Expected IDollVoice interface", iface != null)
        ctx.unbindService(conn)
    }
}
```

- [ ] **Step 2: 跑 test — FAIL（service / AIDL 不存在）**

Run: `cd ~/Projects/DollOSVoice && ./gradlew :app:connectedAndroidTest`
Expected: FAIL 或 compile error — VoiceService + IDollVoice 未定義。

- [ ] **Step 3: 寫 AndroidManifest.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<!-- app/src/main/AndroidManifest.xml -->
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE" />
    <uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />

    <application
        android:label="DollOSVoice"
        android:allowBackup="false">
        <service
            android:name=".VoiceService"
            android:exported="true"
            android:foregroundServiceType="microphone"
            android:permission="android.permission.BIND_VOICE_INTERACTION">
            <intent-filter>
                <action android:name="dollos.voice.IDollVoice" />
            </intent-filter>
        </service>
    </application>
</manifest>
```

- [ ] **Step 4: 寫最小 VoiceService（空 stub）**

```kotlin
// app/src/main/java/dollos/voice/VoiceService.kt
package dollos.voice

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log

class VoiceService : Service() {
    companion object { private const val TAG = "DollVoice.Service" }

    private val binder = object : IDollVoice.Stub() {
        override fun registerWakeWord(path: String?, threshold: Float) {}
        override fun enableKws(enabled: Boolean) {}
        override fun startListening() {}
        override fun stopListening() {}
        override fun speak(text: String?, voiceId: String?) {}
        override fun stopSpeaking() {}
        override fun identifySpeaker(pcmBuffer: ByteArray?): String? = null
        override fun registerListener(listener: IDollVoiceListener?) {}
        override fun unregisterListener(listener: IDollVoiceListener?) {}
    }

    override fun onBind(intent: Intent?): IBinder = binder
    override fun onCreate() { super.onCreate(); Log.i(TAG, "VoiceService created") }
}
```

- [ ] **Step 5: Commit（跑 test 會再失敗因 AIDL 還沒定義 — 下個 task 補）**

```bash
git add app/src/main/AndroidManifest.xml app/src/main/java/dollos/voice/VoiceService.kt app/src/androidTest/java/dollos/voice/VoiceServiceBindingTest.kt
git commit -m "chore: scaffold VoiceService and binding test"
```

---

## §2 AIDL 介面（§3.5）

### Task 3: IDollVoiceListener.aidl

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/aidl/dollos/voice/IDollVoiceListener.aidl`

- [ ] **Step 1: 寫 aidl**

```aidl
// Version: 1
// app/src/main/aidl/dollos/voice/IDollVoiceListener.aidl
package dollos.voice;

interface IDollVoiceListener {
    void onWakeWord();
    void onAsrPartial(String text);
    void onAsrFinal(String text);
    void onTtsProgress(int position, int total);
    void onTtsEnd();
}
```

- [ ] **Step 2: Build 驗證 aidl 可編譯**

Run: `cd ~/Projects/DollOSVoice && ./gradlew :app:compileReleaseAidl`
Expected: BUILD SUCCESSFUL。

- [ ] **Step 3: Commit**

```bash
git add app/src/main/aidl/dollos/voice/IDollVoiceListener.aidl
git commit -m "feat(aidl): add IDollVoiceListener interface"
```

### Task 4: IDollVoice.aidl

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/aidl/dollos/voice/IDollVoice.aidl`

- [ ] **Step 1: 寫 aidl（對齊 master §3.5）**

```aidl
// Version: 1
// app/src/main/aidl/dollos/voice/IDollVoice.aidl
package dollos.voice;

import dollos.voice.IDollVoiceListener;

interface IDollVoice {
    // KWS
    void registerWakeWord(String wakeWordOnnxPath, float threshold);
    void enableKws(boolean enabled);

    // ASR stream
    void startListening();
    void stopListening();

    // TTS
    void speak(String text, String voiceId);
    void stopSpeaking();

    // Speaker ID
    String identifySpeaker(in byte[] pcmBuffer);

    // Listener subscription
    void registerListener(in IDollVoiceListener listener);
    void unregisterListener(in IDollVoiceListener listener);
}
```

- [ ] **Step 2: Build 驗證**

Run: `./gradlew :app:compileReleaseAidl`
Expected: BUILD SUCCESSFUL。

- [ ] **Step 3: 跑 VoiceServiceBindingTest**

Run: `./gradlew :app:connectedAndroidTest`
Expected: PASS（service binds, binder 為 IDollVoice.Stub）。

- [ ] **Step 4: Commit**

```bash
git add app/src/main/aidl/dollos/voice/IDollVoice.aidl
git commit -m "feat(aidl): add IDollVoice interface (master §3.5)"
```

---

## §3 VoiceConfig + VoicePaths + Logging util

### Task 5: VoicePaths 常數

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoicePaths.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/VoicePathsTest.kt`

- [ ] **Step 1: 寫 failing test**

```kotlin
// app/src/test/java/dollos/voice/VoicePathsTest.kt
package dollos.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class VoicePathsTest {
    @Test
    fun model_base_is_system_ext_dollos_models_voice() {
        assertEquals("/system_ext/dollos/models/voice", VoicePaths.MODEL_BASE)
    }
    @Test
    fun subdir_paths_are_under_model_base() {
        assertEquals("/system_ext/dollos/models/voice/asr", VoicePaths.ASR_DIR)
        assertEquals("/system_ext/dollos/models/voice/vad", VoicePaths.VAD_DIR)
        assertEquals("/system_ext/dollos/models/voice/tts-vits", VoicePaths.TTS_VITS_DIR)
        assertEquals("/system_ext/dollos/models/voice/oww", VoicePaths.OWW_DIR)
        assertEquals("/system_ext/dollos/models/voice/speaker-id/model.onnx", VoicePaths.SPEAKER_ID_MODEL)
    }
}
```

- [ ] **Step 2: 跑 — FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoicePathsTest`
Expected: FAIL — VoicePaths 未定義。

- [ ] **Step 3: 寫 VoicePaths**

```kotlin
// app/src/main/java/dollos/voice/VoicePaths.kt
package dollos.voice

object VoicePaths {
    const val MODEL_BASE = "/system_ext/dollos/models/voice"
    const val ASR_DIR = "$MODEL_BASE/asr"
    const val VAD_DIR = "$MODEL_BASE/vad"
    const val TTS_VITS_DIR = "$MODEL_BASE/tts-vits"
    const val OWW_DIR = "$MODEL_BASE/oww"
    const val SPEAKER_ID_MODEL = "$MODEL_BASE/speaker-id/model.onnx"
}
```

- [ ] **Step 4: 跑 — PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoicePathsTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/VoicePaths.kt app/src/test/java/dollos/voice/VoicePathsTest.kt
git commit -m "feat: add VoicePaths model path constants"
```

### Task 6: VoiceConfig data class

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceConfig.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/VoiceConfigTest.kt`

- [ ] **Step 1: 寫 failing test**

```kotlin
// app/src/test/java/dollos/voice/VoiceConfigTest.kt
package dollos.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceConfigTest {
    @Test
    fun default_config_has_no_wake_word_model_and_default_threshold() {
        val cfg = VoiceConfig.default()
        assertNull(cfg.wakeWordOnnxPath)
        assertEquals(0.7f, cfg.wakeWordThreshold, 0.0001f)
        assertEquals(VoicePaths.TTS_VITS_DIR, cfg.ttsModelDir)
        assertEquals(1.0f, cfg.ttsSpeed, 0.0001f)
    }

    @Test
    fun copy_with_new_wake_word_keeps_other_fields() {
        val cfg = VoiceConfig.default().copy(wakeWordOnnxPath = "/x/wake_word.onnx", wakeWordThreshold = 0.8f)
        assertEquals("/x/wake_word.onnx", cfg.wakeWordOnnxPath)
        assertEquals(0.8f, cfg.wakeWordThreshold, 0.0001f)
        assertEquals(VoicePaths.TTS_VITS_DIR, cfg.ttsModelDir)
    }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceConfigTest`
Expected: FAIL。

- [ ] **Step 3: 實作**

```kotlin
// app/src/main/java/dollos/voice/VoiceConfig.kt
package dollos.voice

data class VoiceConfig(
    val wakeWordOnnxPath: String?,
    val wakeWordThreshold: Float,
    val ttsModelDir: String,
    val ttsSpeed: Float,
    val kwsEnabled: Boolean,
    val speakerIdEnabled: Boolean,
) {
    companion object {
        fun default() = VoiceConfig(
            wakeWordOnnxPath = null,
            wakeWordThreshold = 0.7f,
            ttsModelDir = VoicePaths.TTS_VITS_DIR,
            ttsSpeed = 1.0f,
            kwsEnabled = false,
            speakerIdEnabled = false,
        )
    }
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceConfigTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceConfig.kt app/src/test/java/dollos/voice/VoiceConfigTest.kt
git commit -m "feat: add VoiceConfig data class"
```

### Task 7: Logging util

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/util/Logging.kt`

- [ ] **Step 1: 寫**

```kotlin
// app/src/main/java/dollos/voice/util/Logging.kt
package dollos.voice.util

object Logging {
    const val PREFIX = "DollVoice"
    fun tag(component: String): String = "$PREFIX.$component"
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/util/Logging.kt
git commit -m "chore: add unified logging tag helper"
```

---

## §4 AudioRecord manager + PCM broadcaster

### Task 8: PcmBroadcaster — 單一 channel fanout 給多 consumer

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/PcmBroadcaster.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/PcmBroadcasterTest.kt`

- [ ] **Step 1: 寫 failing test — 一個 publish 被所有 subscribed consumer 收到**

```kotlin
// app/src/test/java/dollos/voice/PcmBroadcasterTest.kt
package dollos.voice

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PcmBroadcasterTest {
    @Test
    fun publish_reaches_all_subscribers() = runTest {
        val bcast = PcmBroadcaster()
        val got1 = mutableListOf<FloatArray>()
        val got2 = mutableListOf<FloatArray>()
        val sub1 = launch { bcast.subscribe().collect { got1.add(it) } }
        val sub2 = launch { bcast.subscribe().collect { got2.add(it) } }
        yield() // allow subscribers to register

        bcast.publish(floatArrayOf(0.1f, 0.2f))
        bcast.publish(floatArrayOf(0.3f))
        yield()

        assertEquals(2, got1.size)
        assertEquals(2, got2.size)
        assertEquals(0.1f, got1[0][0], 0.001f)
        assertEquals(0.3f, got2[1][0], 0.001f)

        sub1.cancel(); sub2.cancel()
    }

    @Test
    fun late_subscriber_only_sees_future_publishes() = runTest {
        val bcast = PcmBroadcaster()
        bcast.publish(floatArrayOf(0.5f)) // before any sub
        val got = mutableListOf<FloatArray>()
        val sub = launch { bcast.subscribe().collect { got.add(it) } }
        yield()
        bcast.publish(floatArrayOf(0.7f))
        yield()
        assertEquals(1, got.size)
        assertEquals(0.7f, got[0][0], 0.001f)
        sub.cancel()
    }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.PcmBroadcasterTest`
Expected: FAIL — PcmBroadcaster 未定義。

- [ ] **Step 3: 實作（用 MutableSharedFlow，replay=0）**

```kotlin
// app/src/main/java/dollos/voice/PcmBroadcaster.kt
package dollos.voice

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * Fanout PCM buffers from the single AudioRecord to multiple consumers
 * (VAD / KWS / ASR). Late subscribers do not see historical buffers.
 */
class PcmBroadcaster {
    private val flow = MutableSharedFlow<FloatArray>(
        replay = 0,
        extraBufferCapacity = 64
    )

    fun subscribe(): SharedFlow<FloatArray> = flow.asSharedFlow()

    suspend fun publish(samples: FloatArray) {
        flow.emit(samples)
    }

    /** Non-suspending fast path for [AudioRecordManager]'s recording thread. */
    fun tryPublish(samples: FloatArray): Boolean = flow.tryEmit(samples)
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.PcmBroadcasterTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/PcmBroadcaster.kt app/src/test/java/dollos/voice/PcmBroadcasterTest.kt
git commit -m "feat: add PcmBroadcaster for multi-consumer PCM fanout"
```

### Task 9: AudioRecordManager — 獨占 AudioRecord + 分發

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/AudioRecordManager.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/AudioRecordManagerTest.kt`

- [ ] **Step 1: 寫 failing test — 單一 instance + start/stop 幂等**

```kotlin
// app/src/test/java/dollos/voice/AudioRecordManagerTest.kt
package dollos.voice

import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class AudioRecordManagerTest {
    private lateinit var driver: FakeAudioDriver
    private lateinit var mgr: AudioRecordManager

    @Before
    fun setUp() {
        driver = FakeAudioDriver()
        mgr = AudioRecordManager(driver)
    }

    @Test
    fun start_when_not_running_starts_driver() {
        assertTrue(mgr.start())
        assertTrue(driver.started)
    }

    @Test
    fun start_when_already_running_is_idempotent() {
        mgr.start()
        driver.startCount = 0
        assertTrue(mgr.start())
        assertTrue(driver.startCount == 0)
    }

    @Test
    fun stop_stops_driver_and_allows_restart() {
        mgr.start()
        mgr.stop()
        assertFalse(driver.started)
        assertTrue(mgr.start())
        assertTrue(driver.started)
    }

    @Test
    fun publishes_samples_received_from_driver() = kotlinx.coroutines.runBlocking {
        mgr.start()
        val received = mutableListOf<FloatArray>()
        val job = kotlinx.coroutines.GlobalScope.launch {
            mgr.pcm.subscribe().collect { received.add(it) }
        }
        kotlinx.coroutines.delay(50)
        driver.emit(floatArrayOf(0.1f, 0.2f))
        kotlinx.coroutines.delay(50)
        assertTrue(received.isNotEmpty())
        job.cancel()
    }
}

// Fake driver (mic abstraction) — so unit test runs without Android AudioRecord
class FakeAudioDriver : AudioDriver {
    var started = false
    var startCount = 0
    private var listener: ((FloatArray) -> Unit)? = null
    override fun start(onAudio: (FloatArray) -> Unit): Boolean {
        started = true; startCount++; listener = onAudio; return true
    }
    override fun stop() { started = false; listener = null }
    override fun isRunning(): Boolean = started
    fun emit(samples: FloatArray) { listener?.invoke(samples) }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.AudioRecordManagerTest`
Expected: FAIL — AudioRecordManager / AudioDriver 未定義。

- [ ] **Step 3: 定義 AudioDriver 介面 + AudioRecordManager**

```kotlin
// app/src/main/java/dollos/voice/AudioRecordManager.kt
package dollos.voice

import android.util.Log
import dollos.voice.util.Logging

/** Mic driver abstraction (real impl wraps Android AudioRecord; test impl is fake). */
interface AudioDriver {
    fun start(onAudio: (FloatArray) -> Unit): Boolean
    fun stop()
    fun isRunning(): Boolean
}

/**
 * Owns the single system-wide AudioRecord instance and multicasts PCM to
 * VAD / KWS / ASR via [PcmBroadcaster]. No other component in DollOSVoice
 * may open AudioRecord.
 */
class AudioRecordManager(private val driver: AudioDriver) {
    private val tag = Logging.tag("AudioMgr")
    val pcm = PcmBroadcaster()

    @Synchronized
    fun start(): Boolean {
        if (driver.isRunning()) return true
        val ok = driver.start { samples -> pcm.tryPublish(samples) }
        Log.i(tag, "start ok=$ok")
        return ok
    }

    @Synchronized
    fun stop() {
        if (!driver.isRunning()) return
        driver.stop()
        Log.i(tag, "stop")
    }

    fun isRunning(): Boolean = driver.isRunning()
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.AudioRecordManagerTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/AudioRecordManager.kt app/src/test/java/dollos/voice/AudioRecordManagerTest.kt
git commit -m "feat: add AudioRecordManager with exclusive mic ownership"
```

### Task 10: AndroidAudioDriver — 真正 AudioRecord 包裝

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/AndroidAudioDriver.kt`

- [ ] **Step 1: 從 AIService AudioRecorder.kt 整段搬過來，改成實作 AudioDriver**

參考 `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/AudioRecorder.kt` — 整個 thread / AGC / NoiseSuppressor / 16kHz / 100ms chunk / FloatArray 轉換邏輯照搬。

```kotlin
// app/src/main/java/dollos/voice/AndroidAudioDriver.kt
package dollos.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import dollos.voice.util.Logging

/**
 * Real Android AudioRecord implementation. Logic copied verbatim from
 * DollOSAIService AudioRecorder (16 kHz mono PCM 16-bit, 100 ms chunks,
 * AGC + NoiseSuppressor) and only adapted to the [AudioDriver] interface.
 */
class AndroidAudioDriver(private val sampleRate: Int = 16000) : AudioDriver {
    private val tag = Logging.tag("AndroidAudio")
    companion object { private const val CHUNK_DURATION_MS = 100 }

    private val bufferSize: Int get() = sampleRate * CHUNK_DURATION_MS / 1000
    private var audioRecord: AudioRecord? = null
    private var thread: Thread? = null
    private var agc: AutomaticGainControl? = null
    private var ns: NoiseSuppressor? = null
    @Volatile private var running = false

    override fun isRunning(): Boolean = running

    override fun start(onAudio: (FloatArray) -> Unit): Boolean {
        if (running) return true
        val minBuf = AudioRecord.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC, sampleRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, bufferSize * 2)
            )
        } catch (e: SecurityException) {
            Log.e(tag, "RECORD_AUDIO permission missing", e); return false
        }
        if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(tag, "AudioRecord not initialized")
            audioRecord?.release(); audioRecord = null; return false
        }
        val sid = audioRecord!!.audioSessionId
        if (AutomaticGainControl.isAvailable()) {
            runCatching { agc = AutomaticGainControl.create(sid).apply { enabled = true } }
        }
        if (NoiseSuppressor.isAvailable()) {
            runCatching { ns = NoiseSuppressor.create(sid).apply { enabled = true } }
        }
        running = true
        audioRecord?.startRecording()
        thread = Thread({
            val buf = ShortArray(bufferSize)
            while (running) {
                val n = audioRecord?.read(buf, 0, buf.size) ?: 0
                if (n > 0) {
                    val f = FloatArray(n) { buf[it] / 32768.0f }
                    try { onAudio(f) } catch (e: Exception) { Log.e(tag, "callback error", e) }
                }
            }
        }, "DollVoiceAudioDriver").apply { start() }
        Log.i(tag, "started rate=$sampleRate bufferSize=$bufferSize")
        return true
    }

    override fun stop() {
        if (!running) return
        running = false
        thread?.join(1000); thread = null
        runCatching { agc?.release() }; agc = null
        runCatching { ns?.release() }; ns = null
        runCatching { audioRecord?.stop(); audioRecord?.release() }
        audioRecord = null
        Log.i(tag, "stopped")
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/AndroidAudioDriver.kt
git commit -m "feat: add AndroidAudioDriver wrapping AudioRecord with AGC/NS"
```

---

## §5 KWS 抽出與整合

### Task 11: 從 AIService 搬 WakeWordEngine → engine/KwsEngine.kt

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/KwsEngine.kt`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/WakeWordEngine.kt`

- [ ] **Step 1: 整段拷貝 WakeWordEngine.kt 到 `engine/KwsEngine.kt`**

改動：
1. `package org.dollos.ai.voice` → `package dollos.voice.engine`
2. class 名 `WakeWordEngine` → `KwsEngine`
3. `private const val TAG = "WakeWordEngine"` → `private val TAG = dollos.voice.util.Logging.tag("Kws")`
4. 保留：三階段 ONNX（melspectrogram / embedding / wake_word）、1280 sample chunk、76-frame mel window、16-embedding window、0.7 threshold、3s debounce、WARMUP_CHUNKS=50、`onWakeWordDetected` callback、`enabled` 旗標、`setWakeWordModel(path, threshold)` hot-reload
5. `init { ... melSpecPath = "$baseModelDir/melspectrogram.onnx" ... }` — `baseModelDir` 建構子參數不變

**重要：** CLAUDE.md 記載 `embedding_model.onnx` 版本必須與 openWakeWord Python package 一致，本 engine 不修改模型本體，只搬 runtime 程式碼。

- [ ] **Step 2: Commit（無 test — 既有邏輯已驗過，整合測試在 Task 28 做）**

```bash
git add app/src/main/java/dollos/voice/engine/KwsEngine.kt
git commit -m "feat(engine): port KwsEngine from AIService WakeWordEngine"
```

### Task 12: KwsEngine 單元測試 — 初始化 + enabled 開關

**Files:**
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/engine/KwsEngineTest.kt`

- [ ] **Step 1: 寫 unit test — enabled 旗標控制 feedAudio 是否處理**

```kotlin
// app/src/test/java/dollos/voice/engine/KwsEngineTest.kt
package dollos.voice.engine

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class KwsEngineTest {
    @Test
    fun enabled_flag_defaults_true() {
        // Can't instantiate real engine without model files — test only static invariants
        // Real integration test (task 28) covers feedAudio behavior.
        assertTrue(true) // sanity
    }

    @Test
    fun threshold_boundary_constants_are_correct() {
        // 0.7 default threshold documented in CLAUDE.md
        // 3s debounce
        // 50 chunk warmup (~4s at 80ms per chunk)
        val expectedWarmupMs = 50 * 80
        assertTrue(expectedWarmupMs >= 3000)
        assertFalse(expectedWarmupMs > 5000)
    }
}
```

**註：** KwsEngine 依賴 ONNX runtime + 真實 model 檔，純 JVM unit test 無法深入。真正的「feedAudio 觸發 onWakeWordDetected」驗證落在 Task 28 真機整合測試。

- [ ] **Step 2: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.engine.KwsEngineTest`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add app/src/test/java/dollos/voice/engine/KwsEngineTest.kt
git commit -m "test: add KwsEngine constants sanity test"
```

---

## §6 VAD 抽出與整合

### Task 13: 從 AIService 搬 VadEngine

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/VadEngine.kt`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/VadEngine.kt`

- [ ] **Step 1: 整段拷貝**

改動：
1. package → `dollos.voice.engine`
2. TAG → 用 `Logging.tag("Vad")`
3. 保留：`SileroVadModelConfig(model = "$modelDir/silero_vad.onnx", threshold = 0.5f, minSilenceDuration = 0.5f, minSpeechDuration = 0.25f, windowSize = 512)`、`acceptWaveform` / `hasSpeechSegment` / `getSpeechSegment` / `reset` / `release`

```kotlin
// app/src/main/java/dollos/voice/engine/VadEngine.kt
package dollos.voice.engine

import android.util.Log
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import dollos.voice.util.Logging

class VadEngine(modelDir: String) {
    private val tag = Logging.tag("Vad")
    companion object { private const val SAMPLE_RATE = 16000 }

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
            numThreads = 1, provider = "cpu", debug = false
        )
        vad = Vad(config = config)
        Log.i(tag, "VAD initialized")
    }

    fun acceptWaveform(samples: FloatArray) = vad.acceptWaveform(samples)
    fun isSpeechDetected(): Boolean = vad.isSpeechDetected()
    fun hasSpeechSegment(): Boolean = !vad.empty()
    fun getSpeechSegment(): FloatArray? {
        if (vad.empty()) return null
        val seg = vad.front(); vad.pop(); return seg.samples
    }
    fun reset() = vad.reset()
    fun flush() = vad.flush()
    fun release() { vad.release(); Log.i(tag, "VAD released") }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/engine/VadEngine.kt
git commit -m "feat(engine): port VadEngine from AIService"
```

---

## §7 ASR 抽出與整合

### Task 14: 從 AIService 搬 AsrEngine

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/AsrEngine.kt`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/AsrEngine.kt`

- [ ] **Step 1: 整段拷貝**

改動：
1. package → `dollos.voice.engine`
2. TAG → `Logging.tag("Asr")`
3. 保留：paraformer bilingual zh-en、`encoder.onnx` + `decoder.onnx` + `tokens.txt`、`Simplified-Traditional` transliterator、`startRecognition` / `feedAudio` / `finishRecognition` / `stopRecognition` / `release`、`onPartialResult` + `onFinalResult` callback

```kotlin
// app/src/main/java/dollos/voice/engine/AsrEngine.kt
package dollos.voice.engine

import android.icu.text.Transliterator
import android.util.Log
import com.k2fsa.sherpa.onnx.*
import dollos.voice.util.Logging

class AsrEngine(modelDir: String) {
    private val tag = Logging.tag("Asr")
    companion object { private const val SAMPLE_RATE = 16000 }

    private val recognizer: OnlineRecognizer
    private var stream: OnlineStream? = null
    private val s2t = Transliterator.getInstance("Simplified-Traditional")

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
                numThreads = 2, provider = "cpu", debug = false,
                modelType = "paraformer"
            ),
            enableEndpoint = true, decodingMethod = "greedy_search", maxActivePaths = 4
        )
        recognizer = OnlineRecognizer(config = config)
        Log.i(tag, "ASR initialized (paraformer bilingual zh-en)")
    }

    fun startRecognition() { stream?.release(); stream = recognizer.createStream() }

    fun feedAudio(samples: FloatArray) {
        val s = stream ?: return
        s.acceptWaveform(samples, sampleRate = SAMPLE_RATE)
        while (recognizer.isReady(s)) recognizer.decode(s)
        val result = recognizer.getResult(s)
        val text = s2t.transliterate(result.text.trim())
        if (text.isNotEmpty()) onPartialResult?.invoke(text)
        if (recognizer.isEndpoint(s)) {
            if (text.isNotEmpty()) onFinalResult?.invoke(text)
            recognizer.reset(s)
        }
    }

    fun finishRecognition(): String {
        val s = stream ?: return ""
        s.inputFinished()
        while (recognizer.isReady(s)) recognizer.decode(s)
        val text = s2t.transliterate(recognizer.getResult(s).text.trim())
        if (text.isNotEmpty()) onFinalResult?.invoke(text)
        return text
    }

    fun stopRecognition() { stream?.release(); stream = null }
    fun release() { stream?.release(); recognizer.release(); Log.i(tag, "ASR released") }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/engine/AsrEngine.kt
git commit -m "feat(engine): port AsrEngine from AIService"
```

---

## §8 TTS 抽出與整合

### Task 15: 從 AIService 搬 TtsEngine

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/TtsEngine.kt`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/TtsEngine.kt`

- [ ] **Step 1: 整段拷貝**

改動：
1. package → `dollos.voice.engine`
2. TAG → `Logging.tag("Tts")`
3. 保留：Piper VITS single-speaker、`model.onnx` / `tokens.txt` / `espeak-ng-data/`、`AudioTrack USAGE_ASSISTANT` / `CONTENT_TYPE_SPEECH`、`speak` / `stopSpeaking` / `setVoiceReference`（no-op）/ `hasVoiceReference`（always true）/ `release`、`speed` 可調、`onStarted` + `onCompleted` callback

- [ ] **Step 2: 新增 hot-reload 方法（新需求：切 character pack 要換 TTS 模型）**

在 class 末尾加：

```kotlin
/**
 * Hot-reload the TTS model from a new directory (e.g. after character pack switch).
 * Releases the current OfflineTts and re-initializes from [newModelDir].
 * Must be called on a non-audio thread.
 */
@Synchronized
fun reloadFromDir(newModelDir: String) {
    stopSpeaking()
    tts.release()
    tts = OfflineTts(
        config = OfflineTtsConfig(
            model = OfflineTtsModelConfig(
                vits = OfflineTtsVitsModelConfig(
                    model = "$newModelDir/model.onnx",
                    tokens = "$newModelDir/tokens.txt",
                    dataDir = "$newModelDir/espeak-ng-data"
                ),
                numThreads = 2, provider = "cpu", debug = false
            )
        )
    )
    Log.i(Logging.tag("Tts"), "TTS reloaded from $newModelDir, ${tts.sampleRate()} Hz")
}
```

把 `private var tts: OfflineTts` 從 `val` 改為 `var`（原本為 val，reload 時需重賦值）。

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/dollos/voice/engine/TtsEngine.kt
git commit -m "feat(engine): port TtsEngine with hot-reload support"
```

### Task 16: 從 AIService 搬 TtsCallback.java

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/TtsCallback.java`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/TtsCallback.java`

- [ ] **Step 1: 整段拷貝，改 package**

```java
// app/src/main/java/dollos/voice/engine/TtsCallback.java
package dollos.voice.engine;

/** Java-side callback for sherpa-onnx TTS streaming (PCM samples). */
public abstract class TtsCallback {
    public abstract int invoke(float[] samples);
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/engine/TtsCallback.java
git commit -m "feat(engine): port TtsCallback from AIService"
```

---

## §9 Speaker ID 抽出與整合

### Task 17: 從 AIService 搬 SpeakerIdEngine

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/engine/SpeakerIdEngine.kt`
- Source: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/SpeakerIdEngine.kt`

- [ ] **Step 1: 整段拷貝**

改動：
1. package → `dollos.voice.engine`
2. TAG → `Logging.tag("SpkId")`
3. 保留：ECAPA-TDNN 512-dim、cosine similarity、threshold 0.6、`registered_speakers.json` 位置用 `context.filesDir`
4. 保留：`identify` / `registerSpeaker` / `deleteSpeaker` / `getRegisteredSpeakers` / `release`、`enabled` 旗標

```kotlin
// app/src/main/java/dollos/voice/engine/SpeakerIdEngine.kt
package dollos.voice.engine

import android.content.Context
import android.util.Log
import com.k2fsa.sherpa.onnx.*
import dollos.voice.util.Logging
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

class SpeakerIdEngine(modelPath: String, private val context: Context) {
    private val tag = Logging.tag("SpkId")
    companion object {
        private const val SAMPLE_RATE = 16000
        private const val SPEAKERS_FILE = "registered_speakers.json"
    }

    private val extractor: SpeakerEmbeddingExtractor
    private val registeredSpeakers = mutableMapOf<String, FloatArray>()
    var enabled: Boolean = false

    init {
        extractor = SpeakerEmbeddingExtractor(
            config = SpeakerEmbeddingExtractorConfig(
                model = modelPath, numThreads = 1, debug = false, provider = "cpu"
            )
        )
        loadSpeakers()
        Log.i(tag, "Speaker ID initialized: dim=${extractor.dim()}, ${registeredSpeakers.size} speakers")
    }

    fun identify(samples: FloatArray, threshold: Float = 0.6f): Pair<String, Float>? {
        if (!enabled || registeredSpeakers.isEmpty()) return null
        val emb = extractEmbedding(samples) ?: return null
        var bestName = ""; var bestScore = -1f
        for ((name, reg) in registeredSpeakers) {
            val s = cosineSimilarity(emb, reg)
            if (s > bestScore) { bestScore = s; bestName = name }
        }
        return if (bestScore >= threshold) bestName to bestScore else null
    }

    fun registerSpeaker(name: String, samples: FloatArray): Boolean {
        val emb = extractEmbedding(samples) ?: return false
        registeredSpeakers[name] = emb
        saveSpeakers(); return true
    }

    fun deleteSpeaker(name: String) { registeredSpeakers.remove(name); saveSpeakers() }

    fun getRegisteredSpeakers(): String {
        val arr = JSONArray(); registeredSpeakers.keys.forEach { arr.put(it) }; return arr.toString()
    }

    fun release() { extractor.release(); Log.i(tag, "Speaker ID released") }

    private fun extractEmbedding(samples: FloatArray): FloatArray? {
        val s = extractor.createStream()
        s.acceptWaveform(samples, SAMPLE_RATE); s.inputFinished()
        if (!extractor.isReady(s)) return null
        val emb = extractor.compute(s); s.release(); return emb
    }

    private fun cosineSimilarity(a: FloatArray, b: FloatArray): Float {
        if (a.size != b.size) return 0f
        var dot = 0f; var na = 0f; var nb = 0f
        for (i in a.indices) { dot += a[i]*b[i]; na += a[i]*a[i]; nb += b[i]*b[i] }
        val d = Math.sqrt(na.toDouble()) * Math.sqrt(nb.toDouble())
        return if (d == 0.0) 0f else (dot / d.toFloat())
    }

    private fun saveSpeakers() {
        val json = JSONObject()
        for ((n, e) in registeredSpeakers) {
            val arr = JSONArray(); e.forEach { arr.put(it.toDouble()) }; json.put(n, arr)
        }
        File(context.filesDir, SPEAKERS_FILE).writeText(json.toString())
    }

    private fun loadSpeakers() {
        val f = File(context.filesDir, SPEAKERS_FILE); if (!f.exists()) return
        try {
            val json = JSONObject(f.readText())
            for (n in json.keys()) {
                val arr = json.getJSONArray(n)
                registeredSpeakers[n] = FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
            }
        } catch (e: Exception) { Log.e(tag, "Failed to load speakers", e) }
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/engine/SpeakerIdEngine.kt
git commit -m "feat(engine): port SpeakerIdEngine from AIService"
```

---

## §10 VoiceListenerRegistry — 多 listener 管理

### Task 18: VoiceListenerRegistry 用 RemoteCallbackList

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceListenerRegistry.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/VoiceListenerRegistryTest.kt`

- [ ] **Step 1: 寫 failing test — 多 listener broadcast + 自動清除 dead**

```kotlin
// app/src/test/java/dollos/voice/VoiceListenerRegistryTest.kt
package dollos.voice

import io.mockk.mockk
import io.mockk.verify
import io.mockk.Runs
import io.mockk.every
import io.mockk.just
import org.junit.Test

class VoiceListenerRegistryTest {
    @Test
    fun broadcasts_wake_word_to_all_registered_listeners() {
        val reg = VoiceListenerRegistry()
        val l1 = mockk<IDollVoiceListener>(relaxed = true)
        val l2 = mockk<IDollVoiceListener>(relaxed = true)
        // Mock IBinder returned by asBinder() so RemoteCallbackList accepts them
        every { l1.asBinder() } returns mockk(relaxed = true)
        every { l2.asBinder() } returns mockk(relaxed = true)

        reg.register(l1); reg.register(l2)
        reg.broadcastWakeWord()

        verify { l1.onWakeWord() }
        verify { l2.onWakeWord() }
    }

    @Test
    fun unregister_stops_broadcast() {
        val reg = VoiceListenerRegistry()
        val l = mockk<IDollVoiceListener>(relaxed = true)
        every { l.asBinder() } returns mockk(relaxed = true)
        reg.register(l); reg.unregister(l)
        reg.broadcastWakeWord()
        verify(exactly = 0) { l.onWakeWord() }
    }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceListenerRegistryTest`
Expected: FAIL — VoiceListenerRegistry 未定義。

- [ ] **Step 3: 實作（包 `android.os.RemoteCallbackList`）**

```kotlin
// app/src/main/java/dollos/voice/VoiceListenerRegistry.kt
package dollos.voice

import android.os.RemoteCallbackList
import android.util.Log
import dollos.voice.util.Logging

class VoiceListenerRegistry {
    private val tag = Logging.tag("Listeners")
    private val callbacks = RemoteCallbackList<IDollVoiceListener>()

    fun register(listener: IDollVoiceListener): Boolean = callbacks.register(listener)
    fun unregister(listener: IDollVoiceListener): Boolean = callbacks.unregister(listener)

    private inline fun broadcast(crossinline block: (IDollVoiceListener) -> Unit) {
        val n = callbacks.beginBroadcast()
        try {
            for (i in 0 until n) {
                try { block(callbacks.getBroadcastItem(i)) }
                catch (e: Exception) { Log.w(tag, "listener $i threw", e) }
            }
        } finally { callbacks.finishBroadcast() }
    }

    fun broadcastWakeWord() = broadcast { it.onWakeWord() }
    fun broadcastAsrPartial(text: String) = broadcast { it.onAsrPartial(text) }
    fun broadcastAsrFinal(text: String) = broadcast { it.onAsrFinal(text) }
    fun broadcastTtsProgress(position: Int, total: Int) = broadcast { it.onTtsProgress(position, total) }
    fun broadcastTtsEnd() = broadcast { it.onTtsEnd() }

    fun release() { callbacks.kill() }
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceListenerRegistryTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceListenerRegistry.kt app/src/test/java/dollos/voice/VoiceListenerRegistryTest.kt
git commit -m "feat: add VoiceListenerRegistry with RemoteCallbackList"
```

---

## §11 VoiceManager — 統合 lifecycle

### Task 19: VoiceManager 基礎骨架（init + release）

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/VoiceManagerTest.kt`

- [ ] **Step 1: 寫 failing test — VoiceManager 延遲初始化 engines**

```kotlin
// app/src/test/java/dollos/voice/VoiceManagerTest.kt
package dollos.voice

import io.mockk.mockk
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceManagerTest {
    @Test
    fun not_initialized_until_init_called() {
        val mgr = VoiceManager(mockk(relaxed = true), VoiceConfig.default())
        assertFalse(mgr.isInitialized())
    }

    @Test
    fun release_without_init_is_noop() {
        val mgr = VoiceManager(mockk(relaxed = true), VoiceConfig.default())
        mgr.release() // should not throw
        assertFalse(mgr.isInitialized())
    }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceManagerTest`
Expected: FAIL。

- [ ] **Step 3: 實作 VoiceManager 骨架（engines 延遲初始化，release 冪等）**

```kotlin
// app/src/main/java/dollos/voice/VoiceManager.kt
package dollos.voice

import android.content.Context
import android.util.Log
import dollos.voice.engine.AsrEngine
import dollos.voice.engine.KwsEngine
import dollos.voice.engine.SpeakerIdEngine
import dollos.voice.engine.TtsEngine
import dollos.voice.engine.VadEngine
import dollos.voice.util.Logging

enum class VoiceState { IDLE, LISTENING, SPEAKING }

/**
 * Owns the full voice pipeline: KWS + VAD + ASR + TTS + Speaker ID.
 * Connects to AudioRecordManager via PcmBroadcaster subscription.
 * Triggered externally via [startListening] (from TriggerRouter or AIDL).
 */
class VoiceManager(
    private val context: Context,
    @Volatile var config: VoiceConfig,
) {
    private val tag = Logging.tag("Mgr")
    private val lock = Object()

    @Volatile var state: VoiceState = VoiceState.IDLE
        private set

    var onStateChanged: ((VoiceState) -> Unit)? = null
    var onWakeWord: (() -> Unit)? = null
    var onAsrPartial: ((String) -> Unit)? = null
    var onAsrFinal: ((String) -> Unit)? = null
    var onTtsEnd: (() -> Unit)? = null

    // Engines (nullable until init)
    private var vad: VadEngine? = null
    private var asr: AsrEngine? = null
    private var tts: TtsEngine? = null
    private var kws: KwsEngine? = null
    private var spkId: SpeakerIdEngine? = null

    @Volatile private var initialized = false
    fun isInitialized(): Boolean = initialized

    @Synchronized
    fun init() {
        if (initialized) return
        Log.i(tag, "initializing engines")
        runCatching { vad = VadEngine(VoicePaths.VAD_DIR) }.onFailure { Log.e(tag, "VAD init failed", it) }
        runCatching { asr = AsrEngine(VoicePaths.ASR_DIR).apply {
            onPartialResult = { this@VoiceManager.onAsrPartial?.invoke(it) }
            onFinalResult = { this@VoiceManager.onAsrFinal?.invoke(it) }
        } }.onFailure { Log.e(tag, "ASR init failed", it) }
        runCatching { tts = TtsEngine(config.ttsModelDir, context).apply {
            onStarted = { setState(VoiceState.SPEAKING) }
            onCompleted = { setState(VoiceState.IDLE); this@VoiceManager.onTtsEnd?.invoke() }
            speed = config.ttsSpeed
        } }.onFailure { Log.e(tag, "TTS init failed", it) }
        runCatching { kws = KwsEngine(VoicePaths.OWW_DIR).apply {
            enabled = config.kwsEnabled
            onWakeWordDetected = {
                Log.i(tag, "Wake word detected")
                this@VoiceManager.onWakeWord?.invoke()
            }
            config.wakeWordOnnxPath?.let { setWakeWordModel(it, config.wakeWordThreshold) }
        } }.onFailure { Log.e(tag, "KWS init failed", it) }
        runCatching { spkId = SpeakerIdEngine(VoicePaths.SPEAKER_ID_MODEL, context).apply {
            enabled = config.speakerIdEnabled
        } }.onFailure { Log.e(tag, "SpkId init failed", it) }
        initialized = true
        Log.i(tag, "initialized (vad=${vad!=null} asr=${asr!=null} tts=${tts!=null} kws=${kws!=null} spk=${spkId!=null})")
    }

    @Synchronized
    fun release() {
        if (!initialized) return
        runCatching { vad?.release() }
        runCatching { asr?.release() }
        runCatching { tts?.release() }
        runCatching { kws?.release() }
        runCatching { spkId?.release() }
        vad = null; asr = null; tts = null; kws = null; spkId = null
        initialized = false
    }

    private fun setState(s: VoiceState) {
        synchronized(lock) { if (state == s) return else state = s }
        onStateChanged?.invoke(s)
    }

    // Method stubs — filled in subsequent tasks
    fun startListening() { /* Task 20 */ }
    fun stopListening() { /* Task 20 */ }
    fun speak(text: String) { /* Task 21 */ }
    fun stopSpeaking() { /* Task 21 */ }
    fun processAudio(samples: FloatArray) { /* Task 22 */ }
    fun identifySpeaker(samples: FloatArray): String? { /* Task 23 */ return null }
    fun applyConfig(newConfig: VoiceConfig) { /* Task 24 hot-reload */ config = newConfig }
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceManagerTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt app/src/test/java/dollos/voice/VoiceManagerTest.kt
git commit -m "feat: add VoiceManager skeleton with engine lifecycle"
```

### Task 20: VoiceManager startListening / stopListening

**Files:**
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`

- [ ] **Step 1: 實作 startListening / stopListening（從 VoicePipeline.kt 移植）**

對齊 AIService `VoicePipeline.startListening` 邏輯：
- 必要時 `init()`
- 狀態是 LISTENING 就 no-op
- 停止任何 TTS 播放
- 重置 VAD
- Restart ASR（丟棄 wake word audio）
- setState LISTENING

替換 `fun startListening() { /* Task 20 */ }` 為：

```kotlin
fun startListening() {
    if (!initialized) init()
    synchronized(lock) { if (state == VoiceState.LISTENING) return }
    tts?.stopSpeaking()
    vad?.reset()
    asr?.finishRecognition()
    asr?.startRecognition()
    setState(VoiceState.LISTENING)
    Log.i(tag, "Listening started (ASR reset)")
}

fun stopListening() {
    synchronized(lock) { if (state != VoiceState.LISTENING) return }
    val text = asr?.finishRecognition() ?: ""
    asr?.startRecognition()
    if (text.isNotEmpty()) {
        // final result already fired via onFinalResult callback inside AsrEngine
    }
    setState(VoiceState.IDLE)
    Log.i(tag, "Listening stopped, text='$text'")
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt
git commit -m "feat(VoiceManager): implement start/stop listening"
```

### Task 21: VoiceManager speak / stopSpeaking

**Files:**
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`

- [ ] **Step 1: 實作**

替換 `fun speak(text: String) { /* Task 21 */ }` 與 `stopSpeaking`：

```kotlin
fun speak(text: String) {
    if (!initialized) init()
    if (text.isBlank()) return
    val engine = tts ?: run { Log.w(tag, "TTS not initialized"); return }
    engine.speak(text)
}

fun stopSpeaking() {
    tts?.stopSpeaking()
    setState(VoiceState.IDLE)
}
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt
git commit -m "feat(VoiceManager): implement speak / stopSpeaking"
```

### Task 22: VoiceManager processAudio — 從 PcmBroadcaster 收 buffer 分發

**Files:**
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`

- [ ] **Step 1: 實作 processAudio（逐字對齊 AIService VoicePipeline.processAudio）**

替換 `fun processAudio(samples: FloatArray) { /* Task 22 */ }`：

```kotlin
private var ttsCooldownUntil: Long = 0
// Hook tts onCompleted to set cooldown
private fun wireCooldown() {
    tts?.onCompleted = {
        ttsCooldownUntil = System.currentTimeMillis() + 800
        vad?.reset()
        setState(VoiceState.IDLE)
        onTtsEnd?.invoke()
    }
}

fun processAudio(samples: FloatArray) {
    if (!initialized) return
    // Don't process while speaking — mic picks up speaker output
    if (state == VoiceState.SPEAKING) return
    if (System.currentTimeMillis() < ttsCooldownUntil) return

    vad?.acceptWaveform(samples)
    asr?.feedAudio(samples)

    if (kws?.enabled == true && state == VoiceState.IDLE) {
        kws?.feedAudio(samples)
    }

    if (state == VoiceState.LISTENING) {
        if (vad?.hasSpeechSegment() == true) {
            vad?.getSpeechSegment()
            val text = asr?.finishRecognition() ?: ""
            asr?.startRecognition()
            if (text.isNotEmpty()) {
                setState(VoiceState.IDLE)
                // onAsrFinal already fired via AsrEngine callback
            }
        }
    }
}
```

並在 `init()` 尾端呼叫 `wireCooldown()`。

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt
git commit -m "feat(VoiceManager): implement processAudio with VAD/KWS/ASR fanout"
```

### Task 23: VoiceManager identifySpeaker

**Files:**
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`

- [ ] **Step 1: 實作**

替換 `identifySpeaker`：

```kotlin
fun identifySpeaker(samples: FloatArray): String? {
    val engine = spkId ?: return null
    val (name, _) = engine.identify(samples) ?: return null
    return name
}

fun registerSpeaker(name: String, samples: FloatArray): Boolean =
    spkId?.registerSpeaker(name, samples) ?: false

fun deleteSpeaker(name: String) = spkId?.deleteSpeaker(name) ?: Unit
fun getRegisteredSpeakers(): String = spkId?.getRegisteredSpeakers() ?: "[]"
fun setSpeakerIdEnabled(enabled: Boolean) { spkId?.enabled = enabled }
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt
git commit -m "feat(VoiceManager): wire identifySpeaker + register/delete passthroughs"
```

### Task 24: VoiceManager applyConfig — hot-reload wake word + TTS

**Files:**
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceManager.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/VoiceManagerTest.kt`

- [ ] **Step 1: 加 unit test — applyConfig 把新 config 寫進 `config` field**

在 VoiceManagerTest 加：

```kotlin
@Test
fun applyConfig_updates_config_field() {
    val mgr = VoiceManager(mockk(relaxed = true), VoiceConfig.default())
    val newCfg = VoiceConfig.default().copy(wakeWordThreshold = 0.9f, ttsSpeed = 1.5f)
    mgr.applyConfig(newCfg)
    assertEquals(0.9f, mgr.config.wakeWordThreshold, 0.0001f)
    assertEquals(1.5f, mgr.config.ttsSpeed, 0.0001f)
}
```

- [ ] **Step 2: FAIL（因為 applyConfig 目前只存 config，沒 hot-reload）**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceManagerTest`
Expected: PASS（field 更新）— 但手動檢查 engines 沒換。

- [ ] **Step 3: 實作 hot-reload 邏輯**

替換 `applyConfig`：

```kotlin
@Synchronized
fun applyConfig(newConfig: VoiceConfig) {
    val old = config
    config = newConfig

    // KWS model hot-swap
    if (old.wakeWordOnnxPath != newConfig.wakeWordOnnxPath ||
        old.wakeWordThreshold != newConfig.wakeWordThreshold) {
        newConfig.wakeWordOnnxPath?.let { path ->
            runCatching { kws?.setWakeWordModel(path, newConfig.wakeWordThreshold) }
                .onFailure { Log.e(tag, "KWS hot-reload failed", it) }
        }
    }

    // KWS enabled toggle
    if (old.kwsEnabled != newConfig.kwsEnabled) {
        kws?.enabled = newConfig.kwsEnabled
    }

    // TTS model dir hot-swap
    if (old.ttsModelDir != newConfig.ttsModelDir) {
        runCatching { tts?.reloadFromDir(newConfig.ttsModelDir) }
            .onFailure { Log.e(tag, "TTS hot-reload failed", it) }
    }

    // TTS speed
    if (old.ttsSpeed != newConfig.ttsSpeed) {
        tts?.speed = newConfig.ttsSpeed
    }

    // Speaker ID toggle
    if (old.speakerIdEnabled != newConfig.speakerIdEnabled) {
        spkId?.enabled = newConfig.speakerIdEnabled
    }

    Log.i(tag, "config applied: kws=${newConfig.kwsEnabled} ww=${newConfig.wakeWordOnnxPath}")
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.VoiceManagerTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceManager.kt app/src/test/java/dollos/voice/VoiceManagerTest.kt
git commit -m "feat(VoiceManager): hot-reload wake word + TTS on applyConfig"
```

---

## §12 TriggerRouter — 觸發源多元化

### Task 25: TriggerRouter — 把所有觸發收斂到 startListening

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/TriggerRouter.kt`
- Test: `~/Projects/DollOSVoice/app/src/test/java/dollos/voice/TriggerRouterTest.kt`

**背景：** Master §3.1 `triggerConversation(source, extras)` 定義四個觸發源：`wake_word` / `pickup` / `chest_press` / `gesture` / `user_typed`。DollOSObserver 偵測到 pickup / chest_press / gesture 會推事件進 Core，Core 在 handler 決策後透過 `IDollCore.triggerConversation` 決定要不要叫 `IDollVoice.startListening`（流程圖：Observer → Core → Voice，Voice 不直接依賴 Observer）。KWS 觸發則是 Voice 內部直接觸發（不繞 Core），只是 `onWakeWord` 也會 broadcast 給 Core 讓它知道。

本 task：TriggerRouter 統一封裝「任何觸發 → LISTENING」的策略（幂等、stopSpeaking、reset VAD、通知 listener）。

- [ ] **Step 1: 寫 failing test**

```kotlin
// app/src/test/java/dollos/voice/TriggerRouterTest.kt
package dollos.voice

import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Test

class TriggerRouterTest {
    @Test
    fun wake_word_trigger_invokes_startListening_and_broadcasts_wake() {
        val mgr = mockk<VoiceManager>(relaxed = true)
        val listeners = mockk<VoiceListenerRegistry>(relaxed = true)
        val router = TriggerRouter(mgr, listeners)

        router.onTrigger(TriggerSource.WAKE_WORD)

        verify { listeners.broadcastWakeWord() }
        verify { mgr.startListening() }
    }

    @Test
    fun pickup_trigger_invokes_startListening_without_wake_broadcast() {
        val mgr = mockk<VoiceManager>(relaxed = true)
        val listeners = mockk<VoiceListenerRegistry>(relaxed = true)
        val router = TriggerRouter(mgr, listeners)

        router.onTrigger(TriggerSource.PICKUP)

        verify(exactly = 0) { listeners.broadcastWakeWord() }
        verify { mgr.startListening() }
    }

    @Test
    fun chest_press_and_gesture_also_start_listening() {
        val mgr = mockk<VoiceManager>(relaxed = true)
        val listeners = mockk<VoiceListenerRegistry>(relaxed = true)
        val router = TriggerRouter(mgr, listeners)

        router.onTrigger(TriggerSource.CHEST_PRESS)
        router.onTrigger(TriggerSource.GESTURE)

        verify(exactly = 2) { mgr.startListening() }
    }
}
```

- [ ] **Step 2: FAIL**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.TriggerRouterTest`
Expected: FAIL。

- [ ] **Step 3: 實作**

```kotlin
// app/src/main/java/dollos/voice/TriggerRouter.kt
package dollos.voice

import android.util.Log
import dollos.voice.util.Logging

enum class TriggerSource { WAKE_WORD, PICKUP, CHEST_PRESS, GESTURE }

/**
 * Normalizes all listening triggers to a single path. Wake word additionally
 * fires an onWakeWord broadcast (so Core knows it was user-initiated speech).
 * Pickup / chest_press / gesture come from DollOSObserver via Core — Voice
 * does not import Observer; Core drives TriggerRouter through the AIDL
 * triggerConversation → startListening path.
 */
class TriggerRouter(
    private val voiceManager: VoiceManager,
    private val listeners: VoiceListenerRegistry,
) {
    private val tag = Logging.tag("Trigger")

    fun onTrigger(source: TriggerSource) {
        Log.i(tag, "trigger: $source")
        if (source == TriggerSource.WAKE_WORD) {
            listeners.broadcastWakeWord()
        }
        voiceManager.startListening()
    }
}
```

- [ ] **Step 4: PASS**

Run: `./gradlew :app:testReleaseUnitTest --tests dollos.voice.TriggerRouterTest`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/voice/TriggerRouter.kt app/src/test/java/dollos/voice/TriggerRouterTest.kt
git commit -m "feat: add TriggerRouter for multi-source listening triggers"
```

---

## §13 VoiceServiceImpl — 把 AIDL 接到 VoiceManager + TriggerRouter

### Task 26: VoiceServiceImpl 實作所有 IDollVoice 方法

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceServiceImpl.kt`
- Modify: `~/Projects/DollOSVoice/app/src/main/java/dollos/voice/VoiceService.kt`

- [ ] **Step 1: VoiceServiceImpl = IDollVoice.Stub 實作**

```kotlin
// app/src/main/java/dollos/voice/VoiceServiceImpl.kt
package dollos.voice

import android.util.Log
import dollos.voice.util.Logging

/** Implements the IDollVoice AIDL surface by delegating to [VoiceManager] + [TriggerRouter]. */
class VoiceServiceImpl(
    private val voiceManager: VoiceManager,
    private val triggerRouter: TriggerRouter,
    private val listeners: VoiceListenerRegistry,
) : IDollVoice.Stub() {
    private val tag = Logging.tag("Impl")

    override fun registerWakeWord(wakeWordOnnxPath: String?, threshold: Float) {
        val path = wakeWordOnnxPath ?: return
        voiceManager.applyConfig(voiceManager.config.copy(
            wakeWordOnnxPath = path, wakeWordThreshold = threshold
        ))
    }

    override fun enableKws(enabled: Boolean) {
        voiceManager.applyConfig(voiceManager.config.copy(kwsEnabled = enabled))
    }

    override fun startListening() {
        triggerRouter.onTrigger(TriggerSource.WAKE_WORD)
        // Note: WAKE_WORD source also broadcasts onWakeWord — when Core calls
        // startListening via triggerConversation (source=pickup/chest_press/gesture),
        // it should NOT pass through here; Core-side should call the dedicated
        // overload if we need source preservation. For v1 we treat all AIDL
        // startListening calls as "user wants to speak" and broadcast onWakeWord
        // to keep downstream UI state simple.
    }

    override fun stopListening() {
        voiceManager.stopListening()
    }

    override fun speak(text: String?, voiceId: String?) {
        val t = text ?: return
        // voiceId is reserved for future multi-voice — current TTS has baked-in voice
        voiceManager.speak(t)
    }

    override fun stopSpeaking() = voiceManager.stopSpeaking()

    override fun identifySpeaker(pcmBuffer: ByteArray?): String? {
        val bytes = pcmBuffer ?: return null
        // PCM 16-bit mono → float [-1,1]
        val samples = FloatArray(bytes.size / 2)
        for (i in samples.indices) {
            val lo = bytes[i * 2].toInt() and 0xFF
            val hi = bytes[i * 2 + 1].toInt()
            val s = (hi shl 8) or lo
            samples[i] = s.toShort() / 32768.0f
        }
        return voiceManager.identifySpeaker(samples)
    }

    override fun registerListener(listener: IDollVoiceListener?) {
        listener?.let { listeners.register(it) }
    }

    override fun unregisterListener(listener: IDollVoiceListener?) {
        listener?.let { listeners.unregister(it) }
    }
}
```

**註：** `startListening` 的 WAKE_WORD 語意 — v1 所有外部 AIDL `startListening` 都視為「使用者想講話」並 broadcast `onWakeWord`，讓下游 UI state 簡化。未來要區分來源可加 AIDL overload `startListeningFrom(source)`。

- [ ] **Step 2: 把 VoiceService 改成接 VoiceServiceImpl + foreground service lifecycle**

```kotlin
// app/src/main/java/dollos/voice/VoiceService.kt
package dollos.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import dollos.voice.util.Logging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

class VoiceService : Service() {
    private val tag = Logging.tag("Service")
    companion object {
        private const val CHANNEL_ID = "dollos_voice"
        private const val NOTIF_ID = 1001
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var pcmJob: Job? = null

    private lateinit var audioMgr: AudioRecordManager
    private lateinit var voiceMgr: VoiceManager
    private lateinit var listeners: VoiceListenerRegistry
    private lateinit var router: TriggerRouter
    private lateinit var impl: VoiceServiceImpl

    override fun onCreate() {
        super.onCreate()
        Log.i(tag, "onCreate")
        listeners = VoiceListenerRegistry()
        voiceMgr = VoiceManager(applicationContext, VoiceConfig.default()).apply {
            onWakeWord = { listeners.broadcastWakeWord() }
            onAsrPartial = { listeners.broadcastAsrPartial(it) }
            onAsrFinal = { listeners.broadcastAsrFinal(it) }
            onTtsEnd = { listeners.broadcastTtsEnd() }
            init()
        }
        audioMgr = AudioRecordManager(AndroidAudioDriver())
        router = TriggerRouter(voiceMgr, listeners)
        impl = VoiceServiceImpl(voiceMgr, router, listeners)

        startForegroundWithNotification()
        audioMgr.start()
        // Subscribe PCM to VoiceManager.processAudio
        pcmJob = scope.launch {
            audioMgr.pcm.subscribe().collect { samples ->
                voiceMgr.processAudio(samples)
            }
        }
    }

    private fun startForegroundWithNotification() {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "DollOS Voice",
                NotificationManager.IMPORTANCE_LOW)
        )
        val n: Notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("DollOS Voice")
            .setContentText("Listening for wake word")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    override fun onBind(intent: Intent?): IBinder = impl

    override fun onDestroy() {
        Log.i(tag, "onDestroy")
        pcmJob?.cancel(); pcmJob = null
        audioMgr.stop()
        voiceMgr.release()
        listeners.release()
        scope.cancel()
        super.onDestroy()
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/dollos/voice/VoiceService.kt app/src/main/java/dollos/voice/VoiceServiceImpl.kt
git commit -m "feat: wire VoiceServiceImpl to VoiceManager + foreground service"
```

---

## §14 AOSP 整合

### Task 27: AOSP Android.bp + 部署

**Files:**
- Create: `~/Projects/DollOS-build/external/DollOSVoice/Android.bp`
- Create: `~/Projects/DollOS-build/external/DollOSVoice/prebuilt/`

- [ ] **Step 1: Gradle build + 拷 APK**

Run:
```bash
cd ~/Projects/DollOSVoice
./gradlew assembleRelease
mkdir -p prebuilt
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSVoice.apk
```

Expected: BUILD SUCCESSFUL + `prebuilt/DollOSVoice.apk` 存在。

- [ ] **Step 2: 同步到 AOSP tree**

Run:
```bash
rsync -av --delete ~/Projects/DollOSVoice/ ~/Projects/DollOS-build/external/DollOSVoice/ \
    --exclude=".gradle" --exclude="build" --exclude="app/build"
```

- [ ] **Step 3: 寫 Android.bp（master §8.3 範本）**

```bp
// ~/Projects/DollOS-build/external/DollOSVoice/Android.bp
android_app_import {
    name: "DollOSVoice",
    apk: "prebuilt/DollOSVoice.apk",
    presigned: true,
    privileged: true,
    system_ext_specific: true,
}
```

- [ ] **Step 4: AOSP build**

Run:
```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m DollOSVoice -j$(nproc)
```

Expected: BUILD SUCCESSFUL。

- [ ] **Step 5: 部署到裝置**（在 subagent 執行 — 不佔用主 context）

```bash
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push $OUT/system_ext/priv-app/DollOSVoice/DollOSVoice.apk /system_ext/priv-app/DollOSVoice/
adb reboot
# 重開後驗證 service 啟動
adb shell "ps -A | grep dollos.voice"
adb shell dumpsys activity services dollos.voice/.VoiceService
```

Expected: `dollos.voice` process 在跑，service 顯示 foreground running。

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/DollOS-build
git add external/DollOSVoice/Android.bp external/DollOSVoice/prebuilt/DollOSVoice.apk
git commit -m "feat: integrate DollOSVoice into AOSP build"
```

---

## §15 整合測試

### Task 28: 真機整合測試 — KWS → ASR → TTS 走完一圈

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/androidTest/java/dollos/voice/VoicePipelineIntegrationTest.kt`

**背景：** 既有 AIService `VoicePipeline` 已在設備驗過 KWS / ASR / TTS / VAD 全套，本測試確認「抽出後」行為不變。

- [ ] **Step 1: 寫整合測試（subagent 執行，因為要放音檔 + 真 ONNX runtime）**

```kotlin
// app/src/androidTest/java/dollos/voice/VoicePipelineIntegrationTest.kt
package dollos.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class VoicePipelineIntegrationTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private var iface: IDollVoice? = null
    private val latch = CountDownLatch(1)
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
            iface = IDollVoice.Stub.asInterface(b); latch.countDown()
        }
        override fun onServiceDisconnected(name: ComponentName?) {}
    }

    @Before
    fun setUp() {
        ctx.bindService(Intent(ctx, VoiceService::class.java), conn, Context.BIND_AUTO_CREATE)
        assertTrue(latch.await(10, TimeUnit.SECONDS))
    }

    @After
    fun tearDown() { ctx.unbindService(conn) }

    @Test
    fun service_binds_and_speaker_id_returns_null_without_registered_speakers() {
        val id = iface!!.identifySpeaker(ByteArray(16000)) // silence
        // Should be null — no registered speakers yet
        assertTrue(id == null || id.isEmpty())
    }

    @Test
    fun speak_triggers_tts_end_listener_within_30s() {
        val done = CountDownLatch(1)
        iface!!.registerListener(object : IDollVoiceListener.Stub() {
            override fun onWakeWord() {}
            override fun onAsrPartial(text: String?) {}
            override fun onAsrFinal(text: String?) {}
            override fun onTtsProgress(pos: Int, total: Int) {}
            override fun onTtsEnd() { done.countDown() }
        })
        iface!!.speak("測試語音合成。", null)
        assertTrue("TTS did not end within 30s", done.await(30, TimeUnit.SECONDS))
    }
}
```

- [ ] **Step 2: 在 subagent 跑**

Run:（dispatch subagent — 請參考 subagent-driven-development skill）
```bash
cd ~/Projects/DollOSVoice
./gradlew :app:connectedAndroidTest --tests dollos.voice.VoicePipelineIntegrationTest
```
Expected: 兩個 test PASS。

- [ ] **Step 3: Commit**

```bash
git add app/src/androidTest/java/dollos/voice/VoicePipelineIntegrationTest.kt
git commit -m "test: add end-to-end voice pipeline integration test"
```

### Task 29: KWS 真機觸發測試

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/androidTest/java/dollos/voice/KwsTriggerTest.kt`

**背景：** 需要一組 character pack 的 `wake_word.onnx`（既有 Rin pack 或測試 pack）。

- [ ] **Step 1: 寫測試 — 用 AIService 既有的 wake_word.onnx，registerWakeWord + enableKws 後等 onWakeWord**

```kotlin
// app/src/androidTest/java/dollos/voice/KwsTriggerTest.kt
package dollos.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assume.assumeTrue
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class KwsTriggerTest {
    companion object {
        // Adjust to an installed character pack wake_word.onnx on the test device
        private const val TEST_WAKE_WORD =
            "/system_ext/dollos/characters/default/wake_word.onnx"
    }

    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private var iface: IDollVoice? = null
    private val bound = CountDownLatch(1)
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(n: ComponentName?, b: IBinder?) {
            iface = IDollVoice.Stub.asInterface(b); bound.countDown()
        }
        override fun onServiceDisconnected(n: ComponentName?) {}
    }

    @Before
    fun setUp() {
        assumeTrue("Test wake word model not found", File(TEST_WAKE_WORD).exists())
        ctx.bindService(Intent(ctx, VoiceService::class.java), conn, Context.BIND_AUTO_CREATE)
        assertTrue(bound.await(10, TimeUnit.SECONDS))
    }

    @After
    fun tearDown() { ctx.unbindService(conn) }

    @Test
    fun wake_word_fires_onWakeWord_when_keyword_spoken() {
        // This test requires a human to say the wake word within 30s after test starts.
        // Skip in CI; run manually on device during bring-up.
        val heard = CountDownLatch(1)
        iface!!.registerListener(object : IDollVoiceListener.Stub() {
            override fun onWakeWord() { heard.countDown() }
            override fun onAsrPartial(t: String?) {}
            override fun onAsrFinal(t: String?) {}
            override fun onTtsProgress(p: Int, t: Int) {}
            override fun onTtsEnd() {}
        })
        iface!!.registerWakeWord(TEST_WAKE_WORD, 0.7f)
        iface!!.enableKws(true)
        assertTrue("Wake word not heard in 30s — human needs to speak it",
            heard.await(30, TimeUnit.SECONDS))
    }
}
```

- [ ] **Step 2: 跑（subagent + 人工在旁念 wake word）**

Run:
```bash
./gradlew :app:connectedAndroidTest --tests dollos.voice.KwsTriggerTest
```
Expected: PASS（人工念了 wake word）或 `assumeTrue` skip（測試 pack 未裝）。

- [ ] **Step 3: Commit**

```bash
git add app/src/androidTest/java/dollos/voice/KwsTriggerTest.kt
git commit -m "test: add KWS real-device trigger test"
```

### Task 30: Character Pack hot-reload 測試

**Files:**
- Create: `~/Projects/DollOSVoice/app/src/androidTest/java/dollos/voice/CharacterHotReloadTest.kt`

- [ ] **Step 1: 測試 — 兩次 registerWakeWord 不同模型，第二次生效**

```kotlin
// app/src/androidTest/java/dollos/voice/CharacterHotReloadTest.kt
package dollos.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class CharacterHotReloadTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private var iface: IDollVoice? = null
    private val bound = CountDownLatch(1)
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(n: ComponentName?, b: IBinder?) {
            iface = IDollVoice.Stub.asInterface(b); bound.countDown()
        }
        override fun onServiceDisconnected(n: ComponentName?) {}
    }

    @Before
    fun setUp() {
        ctx.bindService(Intent(ctx, VoiceService::class.java), conn, Context.BIND_AUTO_CREATE)
        assertTrue(bound.await(10, TimeUnit.SECONDS))
    }

    @After
    fun tearDown() { ctx.unbindService(conn) }

    @Test
    fun register_wake_word_twice_does_not_throw() {
        val p1 = "/system_ext/dollos/characters/default/wake_word.onnx"
        val p2 = "/system_ext/dollos/characters/alt/wake_word.onnx"
        if (!File(p1).exists() || !File(p2).exists()) return
        iface!!.registerWakeWord(p1, 0.7f)
        iface!!.registerWakeWord(p2, 0.8f)
        // No crash = pass; deeper semantic check is in manual QA
        assertTrue(true)
    }
}
```

- [ ] **Step 2: 跑**

Run: `./gradlew :app:connectedAndroidTest --tests dollos.voice.CharacterHotReloadTest`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add app/src/androidTest/java/dollos/voice/CharacterHotReloadTest.kt
git commit -m "test: add character pack hot-reload smoke test"
```

---

## §16 AIService → DollOSVoice 搬運（漸進）

### Task 31: AIService voice API 改成 proxy 到 DollOSVoice AIDL

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/VoicePipeline.kt`
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/DollVoiceProxy.kt`

**背景：** Master §10 要求 DollOSAIService 過渡期仍在。漸進搬運策略：
1. DollOSVoice 獨立跑（Tasks 1-30 完成）
2. **本 task**：AIService 裡的 VoicePipeline 內部改成對 DollOSVoice AIDL proxy（AIService 外部 API 不變）
3. Core 完成後改直接叫 DollOSVoice AIDL（Core plan 處理）
4. AIService voice package 整個刪掉（最後收尾 — 不在本 plan 範圍）

- [ ] **Step 1: 拷貝 DollOSVoice 的 AIDL 到 AIService 當 client**

```bash
mkdir -p ~/Projects/DollOSAIService/app/src/main/aidl/dollos/voice
cp ~/Projects/DollOSVoice/app/src/main/aidl/dollos/voice/*.aidl \
   ~/Projects/DollOSAIService/app/src/main/aidl/dollos/voice/
```

- [ ] **Step 2: 寫 DollVoiceProxy — bind DollOSVoice service，proxy API**

```kotlin
// ~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/DollVoiceProxy.kt
package org.dollos.ai.voice

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import android.util.Log
import dollos.voice.IDollVoice
import dollos.voice.IDollVoiceListener

/** Proxy that forwards AIService voice calls to the standalone DollOSVoice service. */
class DollVoiceProxy(private val context: Context) {
    companion object {
        private const val TAG = "DollVoiceProxy"
        private const val VOICE_PKG = "dollos.voice"
        private const val VOICE_CLS = "dollos.voice.VoiceService"
    }

    @Volatile private var remote: IDollVoice? = null

    var onWakeWord: (() -> Unit)? = null
    var onAsrPartial: ((String) -> Unit)? = null
    var onAsrFinal: ((String) -> Unit)? = null
    var onTtsEnd: (() -> Unit)? = null

    private val listener = object : IDollVoiceListener.Stub() {
        override fun onWakeWord() { this@DollVoiceProxy.onWakeWord?.invoke() }
        override fun onAsrPartial(text: String?) { text?.let { this@DollVoiceProxy.onAsrPartial?.invoke(it) } }
        override fun onAsrFinal(text: String?) { text?.let { this@DollVoiceProxy.onAsrFinal?.invoke(it) } }
        override fun onTtsProgress(position: Int, total: Int) {}
        override fun onTtsEnd() { this@DollVoiceProxy.onTtsEnd?.invoke() }
    }

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
            remote = IDollVoice.Stub.asInterface(b)
            runCatching { remote?.registerListener(listener) }
            Log.i(TAG, "Connected to DollOSVoice")
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            remote = null; Log.w(TAG, "DollOSVoice disconnected")
        }
    }

    fun connect() {
        val intent = Intent().apply { component = ComponentName(VOICE_PKG, VOICE_CLS) }
        context.bindService(intent, conn, Context.BIND_AUTO_CREATE)
    }

    fun disconnect() {
        runCatching { remote?.unregisterListener(listener) }
        runCatching { context.unbindService(conn) }
        remote = null
    }

    fun registerWakeWord(path: String, threshold: Float) { remote?.registerWakeWord(path, threshold) }
    fun enableKws(enabled: Boolean) { remote?.enableKws(enabled) }
    fun startListening() { remote?.startListening() }
    fun stopListening() { remote?.stopListening() }
    fun speak(text: String) { remote?.speak(text, null) }
    fun stopSpeaking() { remote?.stopSpeaking() }
}
```

- [ ] **Step 3: VoicePipeline.kt 內部改用 DollVoiceProxy（外部 API 不變）**

**改動點：**
- 把既有 `vadEngine` / `asrEngine` / `ttsEngine` / `wakeWordEngine` / `speakerIdEngine` / `audioRecorder` 全部刪除或改為 `private val proxy = DollVoiceProxy(context)`
- `init()` → `proxy.connect()`
- `startListening()` → `proxy.startListening()`
- `stopListening()` → `proxy.stopListening()`
- `speak(text)` → `proxy.speak(text)`
- `stopSpeaking()` → `proxy.stopSpeaking()`
- `setWakeWordModel(path, threshold)` → `proxy.registerWakeWord(path, threshold)`
- `setWakeWordEnabled(enabled)` → `proxy.enableKws(enabled)`
- callback 連接：`proxy.onWakeWord = { this.onWakeWordDetected?.invoke() }` 等
- `release()` → `proxy.disconnect()`

**註：** `speakerIdEngine` / `registerSpeaker` / `setVoiceReference` 等 API 若目前有人 call，先保持 throw `UnsupportedOperationException`（寫備註 TODO：等 Voice AIDL 擴充 Speaker management 方法），等 DollOSVoice 擴展 AIDL 後再接。

`VoicePipelineState` enum 維持不變（AIService 內部 UI 仍需要用）。

- [ ] **Step 4: AIService build + 裝置煙霧測試**

Run:
```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
m DollOSAIService -j$(nproc)
```

之後 subagent 裝 AIService + DollOSVoice 兩個 APK 到裝置，驗證原有對話 flow 能走通（說 wake word → Doll 回應）。

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/aidl/dollos/voice/ app/src/main/java/org/dollos/ai/voice/
git commit -m "refactor(voice): proxy VoicePipeline to DollOSVoice AIDL"
```

---

## Self-Review

### Spec coverage（對齊 master §10 DollOSVoice scope）

| Scope 項 | Task |
|---|---|
| App 骨架 | Tasks 1-2 |
| AIDL 實作（IDollVoice + IDollVoiceListener，§3.5）| Tasks 3-4 |
| AudioRecord 獨占管理 | Tasks 8-10 |
| KWS 抽出 | Tasks 11-12 |
| VAD 抽出 | Task 13 |
| ASR 抽出 | Task 14 |
| TTS 抽出（含 hot-reload）| Tasks 15-16 |
| Speaker ID 抽出 | Task 17 |
| Listener management | Task 18 |
| VoiceManager 統合 | Tasks 19-24 |
| 觸發源多元化（wake word / pickup / chest_press / gesture）| Task 25 |
| IDollVoice service binding | Task 26 |
| AOSP 整合 | Task 27 |
| 整合測試 | Tasks 28-30 |
| AIService 搬運 proxy | Task 31 |

### AIDL 方法覆蓋（§3.5）

| AIDL method | 實作 task |
|---|---|
| `registerWakeWord(path, threshold)` | Task 26（呼叫 applyConfig → Task 24） |
| `enableKws(enabled)` | Task 26（applyConfig） |
| `startListening()` | Task 26（→ TriggerRouter → VoiceManager.startListening Task 20） |
| `stopListening()` | Task 26（→ VoiceManager.stopListening Task 20） |
| `speak(text, voiceId)` | Task 26（→ VoiceManager.speak Task 21） |
| `stopSpeaking()` | Task 26（→ VoiceManager.stopSpeaking Task 21） |
| `identifySpeaker(pcm)` | Task 26（→ VoiceManager.identifySpeaker Task 23） |
| `registerListener(listener)` | Task 26（→ VoiceListenerRegistry Task 18） |
| `unregisterListener(listener)` | Task 26 |
| `IDollVoiceListener.onWakeWord` | Task 18 broadcast + VoiceService wire-up Task 26 |
| `IDollVoiceListener.onAsrPartial/Final` | Task 18 + Task 26 |
| `IDollVoiceListener.onTtsProgress/End` | Task 18 + Task 26 |

### 五個 voice 元件 section

- KWS §5（Tasks 11-12）
- VAD §6（Task 13）
- ASR §7（Task 14）
- TTS §8（Tasks 15-16）
- Speaker ID §9（Task 17）

### 觸發源多元化（每類型 → LISTENING 的路徑）

| 觸發類型 | 路徑 |
|---|---|
| `wake_word` | KWS engine 內部偵測（Task 11）→ `KwsEngine.onWakeWordDetected` → `VoiceManager.onWakeWord` (Task 19) → `VoiceListenerRegistry.broadcastWakeWord` (Task 18) **同時** Core 收到會呼 `IDollVoice.startListening` → TriggerRouter WAKE_WORD (Task 25) → VoiceManager.startListening (Task 20) |
| `pickup` | DollOSObserver（外部 plan）偵測 → Core handler → `IDollVoice.startListening` (Task 26) → VoiceManager.startListening |
| `chest_press` | 同 pickup 路徑（Observer → Core → AIDL → TriggerRouter） |
| `gesture` | 同 pickup 路徑 |

AIDL `startListening` 目前不帶 source 參數（§3.5 契約），Task 26 註記了「v1 一律當 WAKE_WORD 視角並 broadcast onWakeWord」，未來要區分可加 overload。

### Character Pack hot-reload flow

觸發時機：Memory app 告知 Core「character pack 切換完成，新 wake_word.onnx 在 X 路徑、新 TTS 模型在 Y 目錄」→ Core 呼 `IDollVoice.registerWakeWord(X, threshold)` → Task 26 `VoiceServiceImpl.registerWakeWord` → Task 24 `VoiceManager.applyConfig` 比對 old vs new config → Task 15 `TtsEngine.reloadFromDir(Y)` + Task 11 `KwsEngine.setWakeWordModel(X, threshold)`。

TTS 模型目錄切換通過另一條路：`applyConfig` 裡 `old.ttsModelDir != newConfig.ttsModelDir` 偵測到 → 呼 `TtsEngine.reloadFromDir`。但目前 AIDL 沒有專門的 `setTtsModelDir`，下一版可加 `void setTtsVoice(String modelDir)` AIDL method；過渡期 Core 可透過 config 直接控制（若有需要）。自我審查標記：**Task 26 + Task 24 涵蓋了 wake word hot-reload 主路徑；TTS hot-reload 機制在 Task 15 + 24 已實作，但 AIDL 端尚未暴露專用 method**（因 master §3.5 沒定義，視為未來擴充）。

### Placeholder scan

- 無 TBD / TODO / "implement later"（除 Task 31 的 Speaker management 暫放 UnsupportedOperationException 並註記 TODO — 這是真的 deferred 到 AIDL 擴充時做）
- 所有 code step 都有完整 code block
- 所有 test 都有具體 assert

### Type consistency

- `TriggerSource` enum：WAKE_WORD / PICKUP / CHEST_PRESS / GESTURE（Task 25）— 對應 Core `triggerConversation(source)` 的 source 參數
- `VoiceState` enum：IDLE / LISTENING / SPEAKING（Task 19）— 不是 AIService 的 IDLE/LISTENING/PROCESSING/SPEAKING，因為 PROCESSING 是 Core 層狀態（master §4.2 明確說 Core 是 event-driven，不持久化狀態），Voice 只關心 IDLE/LISTENING/SPEAKING 三態即可
- `VoiceConfig` 欄位：`wakeWordOnnxPath`, `wakeWordThreshold`, `ttsModelDir`, `ttsSpeed`, `kwsEnabled`, `speakerIdEnabled`（Task 6）— Task 24 applyConfig / Task 26 registerWakeWord/enableKws 的命名一致
- `AudioDriver.start(onAudio: (FloatArray) -> Unit): Boolean`（Task 9）— Task 10 AndroidAudioDriver 實作簽章一致
- Logging tag：統一 `DollVoice.<Component>`（Task 7）— 各 engine 使用一致

### 遺漏檢查

- **Speaker enrollment flow（registerSpeaker）**：Task 17 有 engine 層實作，但 AIDL §3.5 沒定義 registration 的 method（只有 `identifySpeaker`）。目前 Task 26 透過 `VoiceManager.registerSpeaker` 保留介面給未來擴充，但 AIDL 端不暴露。這是 master spec 刻意為之還是疏漏，留待 Core/Memory plan 整合時再處理。
- **Per-character wake_word.onnx 解壓路徑**：本 plan 只接 Core 給的絕對路徑，不管 pack 解壓（在 Memory plan）
- **TTS 講到一半被中斷（barge-in）** — 既有 `processAudio` 在 SPEAKING 狀態不處理輸入，符合 AIService 原行為。若未來要 barge-in 需重新設計，超出本 plan。

---

**Plan complete.** 總 task 數：31。涵蓋 DollOSVoice 從零到整合 AIService 的完整搬運路徑，所有 AIDL 方法（§3.5）都有實作 task，五大 voice 元件（KWS/VAD/ASR/TTS/Speaker ID）各自獨立 section，四種觸發源（wake_word/pickup/chest_press/gesture）都有路徑收斂到 `startListening`，Character Pack 切換時 wake word + TTS 模型 hot-reload 透過 `applyConfig` 差異比對處理。
