# PTT 語音輸入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 wake word pipeline 砍掉，改成「長按電源鍵 → launchAssist」當唯一 voice input 觸發路徑；同時引入 sensitive 標記 + mid-flow 指紋確認的權限模型。

**Architecture:** DollOSAIService 註冊為 Android `VoiceInteractionService` (Assist provider)；長按電源鍵透過 framework 的 launchAssist intent 進來；新加 `PTTSessionController` 持有 `IDLE / LISTENING / THINKING / SPEAKING` 狀態機協調現有 ASR / VAD / SpeakerID / LLM / TTS / EdgeOverlayState / Live2D lip sync。Sensitive 標記掛在 skill / action manifest，agent runtime 執行前檢查，sensitive 走 BiometricPrompt 路徑。

**Tech Stack:** Kotlin、Android `VoiceInteractionService` API、`BiometricPrompt`、AIDL、AOSP framework patches、現有 sherpa-onnx / Piper VITS / silero VAD / ECAPA Speaker ID。

**Spec:** `docs/superpowers/specs/2026-04-26-ptt-voice-input-design.md`

---

## Task 1: Discovery — long-press power 綁定 + GrapheneOS 預設

**Files:**
- Read: `~/Projects/DollOS-build/frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java` (search for `POWER`, `interceptPowerKey`, `launchAssist`)
- Read: `~/Projects/DollOS-build/frameworks/base/core/java/android/provider/Settings.java` (search `LONG_PRESS_POWER_BUTTON`)
- Read: `~/Projects/DollOS-build/vendor/dollos/overlay/frameworks/base/core/res/res/values/config.xml`

- [ ] **Step 1: 看 PhoneWindowManager 怎麼處理 long-press power**

```bash
grep -n "interceptPowerKeyDown\|LONG_PRESS_POWER\|launchAssist" ~/Projects/DollOS-build/frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java | head -30
```

找到 `mPowerKeyHandler` / `interceptPowerKeyDown` / `mLongPressOnPowerBehavior` 開關，記下行號。

- [ ] **Step 2: 看 GrapheneOS 預設值**

```bash
grep -rn "config_longPressOnPowerBehavior\|config_keyChordPowerVolumeUp" ~/Projects/DollOS-build/frameworks/base/core/res/res/values/ ~/Projects/DollOS-build/vendor/ 2>/dev/null
```

確認 `config_longPressOnPowerBehavior` 預設值（應是 1 = global actions / 5 = assistant 等枚舉，定義在 framework）。

- [ ] **Step 3: 文件化發現**

寫成 `docs/superpowers/discoveries/2026-04-26-power-key-binding.md`，記錄：
- 哪個 enum value = launchAssist
- 預設 GrapheneOS / AOSP 用哪個
- 改變方式：`Settings.Global.put` runtime / RRO override / framework patch
- 鎖屏行為差異（`interceptPowerKeyDown` 在鎖屏分支）

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/DollOS && git add docs/superpowers/discoveries/2026-04-26-power-key-binding.md && git commit -m "discover: long-press power binding paths in PhoneWindowManager"
```

---

## Task 2: Skill manifest 加 `sensitive` 欄位

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/agent/Skill.kt`（或實際 skill 定義所在；先找）
- Modify: 既有所有 skill 定義檔（每個 skill 顯式標 sensitive）

- [ ] **Step 1: 找 skill 定義位置**

```bash
grep -rn "data class Skill\|class Skill\b\|sealed.*Skill" ~/Projects/DollOSAIService/app/src/main/java | head
```

- [ ] **Step 2: 加 `sensitive: Boolean = true` 欄位（fail-safe 預設）**

在 Skill 資料類別 / 介面加上欄位。例如：

```kotlin
data class Skill(
    val id: String,
    val description: String,
    val parameters: List<SkillParameter>,
    val sensitive: Boolean = true,  // fail-safe: skill 沒明標就視為 sensitive
)
```

- [ ] **Step 3: 一個一個改既有 skill，明確標非敏感**

每個 skill 顯式設 `sensitive = false` 才算非敏感：

非敏感（標 false）：
- 純對話 / 查記憶 / 查時間 / 設鬧鐘（透過 `AlarmClock.ACTION_SET_ALARM`）/ 加 to-do / 切角色

敏感（不標 = 預設 true）：
- 開外部 app / 讀 SMS / 讀 email / 讀聯絡人 / 讀檔案 / 鏡頭 / 螢幕擷取 / 改系統設定 / 安裝卸載 app

逐個 skill 檢查，明確設 `sensitive = false` 給安全的；其他不動（吃預設 true）。

- [ ] **Step 4: Skill registry / dispatcher unit test 驗 sensitive flag 透出**

寫一個 test，列出所有註冊 skill，dump `id` + `sensitive`，比對預期清單，避免漏標。

```kotlin
@Test
fun `all skills have explicit sensitive classification`() {
    val skills = SkillRegistry.allSkills()
    val expectedNonSensitive = setOf("chat", "search_memory", "set_alarm", "add_todo", "switch_character", "get_time")
    for (s in skills) {
        if (s.id in expectedNonSensitive) {
            assertFalse("$s should be non-sensitive", s.sensitive)
        } else {
            assertTrue("$s should be sensitive (default)", s.sensitive)
        }
    }
}
```

- [ ] **Step 5: Run tests + commit**

```bash
cd ~/Projects/DollOSAIService && ./gradlew test --tests "*SkillRegistry*" -i
git add app/src/main/java/org/dollos/ai/agent/ app/src/test/java/org/dollos/ai/agent/
git commit -m "feat(agent): sensitive flag on Skill, fail-safe default true"
```

---

## Task 3: SensitiveActionGate — BiometricPrompt activity proxy

**Files:**
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/auth/SensitiveActionGate.kt`
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/auth/BiometricPromptActivity.kt`
- Modify: `~/Projects/DollOSAIService/app/src/main/AndroidManifest.xml`
- Test: `~/Projects/DollOSAIService/app/src/androidTest/java/org/dollos/ai/auth/SensitiveActionGateTest.kt`

- [ ] **Step 1: 寫 SensitiveActionGateTest（先 fail）**

```kotlin
@Test
fun gate_allowsImmediately_forNonSensitive() = runBlocking {
    val gate = SensitiveActionGate(InstrumentationRegistry.getInstrumentation().targetContext)
    val granted = gate.confirm(sensitive = false, reason = "test")
    assertTrue(granted)
}
```

- [ ] **Step 2: 跑 test 確認 fail（class 未定義）**

```bash
cd ~/Projects/DollOSAIService && ./gradlew connectedAndroidTest --tests "*SensitiveActionGateTest*" 2>&1 | tail -20
```

Expected: compile error / class not found.

- [ ] **Step 3: 實作 SensitiveActionGate**

```kotlin
package org.dollos.ai.auth

import android.content.Context
import android.content.Intent
import kotlinx.coroutines.CompletableDeferred

class SensitiveActionGate(private val context: Context) {

    suspend fun confirm(sensitive: Boolean, reason: String): Boolean {
        if (!sensitive) return true
        val deferred = CompletableDeferred<Boolean>()
        val token = PendingPrompts.register(deferred)
        val i = Intent(context, BiometricPromptActivity::class.java).apply {
            putExtra(BiometricPromptActivity.EXTRA_TOKEN, token)
            putExtra(BiometricPromptActivity.EXTRA_REASON, reason)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(i)
        return deferred.await()
    }
}

internal object PendingPrompts {
    private val map = java.util.concurrent.ConcurrentHashMap<String, CompletableDeferred<Boolean>>()
    fun register(d: CompletableDeferred<Boolean>): String {
        val token = java.util.UUID.randomUUID().toString()
        map[token] = d
        return token
    }
    fun resolve(token: String, granted: Boolean) {
        map.remove(token)?.complete(granted)
    }
}
```

- [ ] **Step 4: 實作 BiometricPromptActivity**

```kotlin
package org.dollos.ai.auth

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat

class BiometricPromptActivity : Activity() {
    companion object {
        const val EXTRA_TOKEN = "token"
        const val EXTRA_REASON = "reason"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: run { finish(); return }
        val reason = intent.getStringExtra(EXTRA_REASON) ?: ""
        val executor = ContextCompat.getMainExecutor(this)
        val prompt = BiometricPrompt(
            // BiometricPrompt 需要 FragmentActivity，這裡用 ComponentActivity 改裝；
            // 如果 androidx.activity.ComponentActivity 不夠，改繼承 FragmentActivity
            this as androidx.fragment.app.FragmentActivity,
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    PendingPrompts.resolve(token, true)
                    finish()
                }
                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    PendingPrompts.resolve(token, false)
                    finish()
                }
                override fun onAuthenticationFailed() {
                    // 單次失敗不立即拒絕，等使用者再試或取消
                }
            }
        )
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle("Doll 要做敏感操作")
            .setSubtitle(reason)
            .setNegativeButtonText("取消")
            .setAllowedAuthenticators(androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG)
            .build()
        Handler(Looper.getMainLooper()).post { prompt.authenticate(info) }
    }
}
```

注意：BiometricPrompt 要求 FragmentActivity。把 class 改成 `class BiometricPromptActivity : androidx.fragment.app.FragmentActivity()` 直接繼承（避免 cast）。

- [ ] **Step 5: 註冊 activity 進 manifest**

`app/src/main/AndroidManifest.xml`，在 `<application>` 加：

```xml
<activity
    android:name=".auth.BiometricPromptActivity"
    android:theme="@android:style/Theme.Translucent.NoTitleBar"
    android:exported="false"
    android:excludeFromRecents="true"
    android:showWhenLocked="true"
    android:turnScreenOn="false" />
```

- [ ] **Step 6: 加 androidx.biometric 依賴**

`app/build.gradle.kts`：

```kotlin
implementation("androidx.biometric:biometric:1.1.0")
```

- [ ] **Step 7: 補測試 — 敏感路徑會啟動 prompt activity**

寫 instrumented test 用 UiAutomator 驗 activity 出現（或至少 mock context.startActivity 抓 intent target）。如果 instrumented 太重，至少 unit test 驗「sensitive=true 時會 register pending + startActivity」用 fake context。

- [ ] **Step 8: Run + commit**

```bash
cd ~/Projects/DollOSAIService && ./gradlew test connectedAndroidTest --tests "*SensitiveActionGate*" 2>&1 | tail -20
git add app/src/main/java/org/dollos/ai/auth/ app/src/main/AndroidManifest.xml app/build.gradle.kts app/src/androidTest/java/org/dollos/ai/auth/
git commit -m "feat(auth): SensitiveActionGate + BiometricPromptActivity"
```

---

## Task 4: PTTState 列舉 + PTTSessionController 狀態機（離線單元測試）

**Files:**
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/PTTState.kt`
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/PTTSessionController.kt`
- Test: `~/Projects/DollOSAIService/app/src/test/java/org/dollos/ai/voice/PTTSessionControllerTest.kt`

- [ ] **Step 1: 寫 PTTState**

```kotlin
package org.dollos.ai.voice

enum class PTTState { IDLE, LISTENING, THINKING, SPEAKING }
```

- [ ] **Step 2: 寫狀態機測試先（TDD）**

```kotlin
@Test
fun `idle on long-press transitions to listening`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress()
    assertEquals(PTTState.LISTENING, ctrl.state.value)
}

@Test
fun `vad silence ends listening transitions to thinking`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress()
    ctrl.onVadSilence()
    assertEquals(PTTState.THINKING, ctrl.state.value)
}

@Test
fun `llm complete transitions thinking to speaking`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress(); ctrl.onVadSilence()
    ctrl.onLLMResponse("hi")
    assertEquals(PTTState.SPEAKING, ctrl.state.value)
}

@Test
fun `tts complete transitions to idle`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress(); ctrl.onVadSilence(); ctrl.onLLMResponse("hi"); ctrl.onTTSComplete()
    assertEquals(PTTState.IDLE, ctrl.state.value)
}

@Test
fun `long-press during speaking interrupts and starts new listening`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress(); ctrl.onVadSilence(); ctrl.onLLMResponse("hi")
    assertEquals(PTTState.SPEAKING, ctrl.state.value)
    ctrl.onPTTPress()  // 中斷
    assertEquals(PTTState.LISTENING, ctrl.state.value)
}

@Test
fun `long-press during listening cancels back to idle`() = runTest {
    val ctrl = makeController()
    ctrl.onPTTPress()
    ctrl.onPTTPress()
    assertEquals(PTTState.IDLE, ctrl.state.value)
}

@Test
fun `30s recording timeout forces VAD silence`() = runTest {
    val ctrl = makeController(maxRecordMs = 30_000)
    ctrl.onPTTPress()
    advanceTimeBy(30_001)
    assertEquals(PTTState.THINKING, ctrl.state.value)
}

@Test
fun `20s thinking timeout aborts to idle`() = runTest {
    val ctrl = makeController(maxThinkingMs = 20_000)
    ctrl.onPTTPress(); ctrl.onVadSilence()
    advanceTimeBy(20_001)
    assertEquals(PTTState.IDLE, ctrl.state.value)
}
```

- [ ] **Step 3: 跑 test 確認全 fail**

```bash
cd ~/Projects/DollOSAIService && ./gradlew test --tests "*PTTSessionControllerTest*" 2>&1 | tail -20
```

Expected: compile error。

- [ ] **Step 4: 實作 PTTSessionController（最小、純狀態機 + 接口）**

```kotlin
package org.dollos.ai.voice

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

interface PTTHardware {
    fun startRecording()
    fun stopRecording()
    fun startTTS(text: String, onComplete: () -> Unit)
    fun stopTTS()
    fun chime(kind: ChimeKind)
    fun setEdgeOverlay(state: PTTState)
}
enum class ChimeKind { ENTER_LISTEN, CANCEL }

interface LLMRunner { suspend fun respond(transcript: String): String }
interface ASRRunner {
    fun start()
    fun stop(): String
}

class PTTSessionController(
    private val hw: PTTHardware,
    private val asr: ASRRunner,
    private val llm: LLMRunner,
    private val scope: CoroutineScope,
    private val maxRecordMs: Long = 30_000,
    private val vadSilenceMs: Long = 1_500,
    private val maxThinkingMs: Long = 20_000,
) {
    private val _state = MutableStateFlow(PTTState.IDLE)
    val state: StateFlow<PTTState> = _state

    private var recordJob: Job? = null
    private var thinkJob: Job? = null

    fun onPTTPress() {
        when (_state.value) {
            PTTState.IDLE -> enterListening()
            PTTState.LISTENING -> cancel()
            PTTState.THINKING -> abortThinking()
            PTTState.SPEAKING -> { hw.stopTTS(); enterListening() }
        }
    }

    fun onVadSilence() {
        if (_state.value == PTTState.LISTENING) {
            recordJob?.cancel()
            val transcript = asr.stop()
            hw.stopRecording()
            enterThinking(transcript)
        }
    }

    fun onLLMResponse(text: String) {
        if (_state.value == PTTState.THINKING) {
            thinkJob?.cancel()
            _state.value = PTTState.SPEAKING
            hw.setEdgeOverlay(PTTState.SPEAKING)
            hw.startTTS(text) { onTTSComplete() }
        }
    }

    fun onTTSComplete() {
        if (_state.value == PTTState.SPEAKING) {
            _state.value = PTTState.IDLE
            hw.setEdgeOverlay(PTTState.IDLE)
        }
    }

    private fun enterListening() {
        _state.value = PTTState.LISTENING
        hw.setEdgeOverlay(PTTState.LISTENING)
        hw.chime(ChimeKind.ENTER_LISTEN)
        hw.startRecording()
        asr.start()
        recordJob = scope.launch {
            delay(maxRecordMs)
            onVadSilence()  // timeout = 強制結束
        }
    }

    private fun cancel() {
        recordJob?.cancel()
        asr.stop()
        hw.stopRecording()
        hw.chime(ChimeKind.CANCEL)
        _state.value = PTTState.IDLE
        hw.setEdgeOverlay(PTTState.IDLE)
    }

    private fun enterThinking(transcript: String) {
        _state.value = PTTState.THINKING
        hw.setEdgeOverlay(PTTState.THINKING)
        thinkJob = scope.launch {
            try {
                withTimeout(maxThinkingMs) {
                    val reply = llm.respond(transcript)
                    onLLMResponse(reply)
                }
            } catch (_: TimeoutCancellationException) {
                abortThinking()
            }
        }
    }

    private fun abortThinking() {
        thinkJob?.cancel()
        _state.value = PTTState.IDLE
        hw.setEdgeOverlay(PTTState.IDLE)
    }
}
```

- [ ] **Step 5: 寫 test fixture (`makeController`)**

用 fake `PTTHardware` / `ASRRunner` / `LLMRunner` 實作。Coroutine 測試用 `runTest` + `TestScope`。

- [ ] **Step 6: 跑 test 確認全 pass**

```bash
./gradlew test --tests "*PTTSessionControllerTest*" 2>&1 | tail -20
```

- [ ] **Step 7: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/PTT*.kt app/src/test/java/org/dollos/ai/voice/PTTSessionControllerTest.kt
git commit -m "feat(voice): PTTSessionController state machine + tests"
```

---

## Task 5: DollOSVoiceInteractionService + Session

**Files:**
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/DollOSVoiceInteractionService.kt`
- Create: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/DollOSVoiceInteractionSession.kt`
- Create: `~/Projects/DollOSAIService/app/src/main/res/xml/voice_interaction_service.xml`

- [ ] **Step 1: 建 voice_interaction_service.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<voice-interaction-service xmlns:android="http://schemas.android.com/apk/res/android"
    android:sessionService="org.dollos.ai.voice.DollOSVoiceInteractionSession"
    android:recognitionService="android.speech.SpeechRecognitionService"
    android:supportsAssist="true"
    android:supportsLaunchVoiceAssistFromKeyguard="true"
    android:supportsLocalInteraction="true" />
```

注意 `recognitionService` 屬性必填但我們不用 system speech recognition；指向預設 dummy 即可（或實作一個 stub）。

- [ ] **Step 2: 建 DollOSVoiceInteractionService**

```kotlin
package org.dollos.ai.voice

import android.service.voice.VoiceInteractionService
import android.util.Log

class DollOSVoiceInteractionService : VoiceInteractionService() {
    companion object { private const val TAG = "DollOSVIS" }
    override fun onReady() {
        super.onReady()
        Log.i(TAG, "VoiceInteractionService ready")
    }
}
```

`showSession()` 由 framework 在 launchAssist 時自動呼叫。

- [ ] **Step 3: 建 DollOSVoiceInteractionSession**

```kotlin
package org.dollos.ai.voice

import android.content.Context
import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.util.Log
import org.dollos.ai.DollOSAIApp

class DollOSVoiceInteractionSession(context: Context) : VoiceInteractionSession(context) {
    companion object { private const val TAG = "DollOSVISession" }

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        Log.i(TAG, "session show, flags=$showFlags")
        // 不顯示任何 session UI（VoiceInteractionSession 預設可選擇用自己的 UI；我們交給 PTTSessionController + EdgeOverlay）
        DollOSAIApp.instance.pttSessionController.onPTTPress()
        // 立即收掉 session window，讓 PTT UX 接手
        hide()
    }
}
```

- [ ] **Step 4: DollOSAIApp 持有 PTTSessionController instance**

`DollOSAIApp.kt` 加：

```kotlin
lateinit var pttSessionController: PTTSessionController
    private set
```

並在 `onCreate` 後（user unlocked 後也算）構造它。具體 wiring 在 Task 7。先放 lateinit 讓 Session 編得過。

- [ ] **Step 5: Commit（編譯能過即可，integration 在後續 task）**

```bash
git add app/src/main/java/org/dollos/ai/voice/DollOSVoiceInteractionService.kt app/src/main/java/org/dollos/ai/voice/DollOSVoiceInteractionSession.kt app/src/main/res/xml/voice_interaction_service.xml app/src/main/java/org/dollos/ai/DollOSAIApp.kt
git commit -m "feat(voice): VoiceInteractionService + Session skeleton"
```

---

## Task 6: AndroidManifest + privapp-permissions

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/AndroidManifest.xml`
- Modify: `~/Projects/DollOS-build/packages/apps/DollOSAIService/privapp-permissions-dollos-ai.xml`（先用 `find` 確認實際路徑；若不存在則建）

- [ ] **Step 1: 註冊 VoiceInteractionService 進 manifest**

`<application>` 內加：

```xml
<service
    android:name=".voice.DollOSVoiceInteractionService"
    android:label="DollOS"
    android:permission="android.permission.BIND_VOICE_INTERACTION"
    android:exported="true">
    <meta-data android:name="android.voice_interaction" android:resource="@xml/voice_interaction_service" />
    <intent-filter>
        <action android:name="android.service.voice.VoiceInteractionService" />
    </intent-filter>
</service>
```

- [ ] **Step 2: 找 / 確認 privapp-permissions XML 路徑**

```bash
find ~/Projects/DollOS-build -name "privapp-permissions*dollos*" 2>/dev/null
```

- [ ] **Step 3: 加 BIND_VOICE_INTERACTION 進 privapp-permissions**

對應 XML 的 `<privapp-permissions package="org.dollos.ai">` 區塊內加：

```xml
<permission name="android.permission.BIND_VOICE_INTERACTION" />
```

- [ ] **Step 4: 確認其他 voice 相關 permission（RECORD_AUDIO 之類）已存在**

如果 `android.permission.RECORD_AUDIO` / `MODIFY_AUDIO_SETTINGS` / `CAPTURE_AUDIO_HOTWORD` 之前已加（既有 voice pipeline 必有），不重加。

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOSAIService && git add app/src/main/AndroidManifest.xml
git commit -m "feat(voice): register VoiceInteractionService + BIND_VOICE_INTERACTION"
cd ~/Projects/DollOS-build && git add packages/apps/DollOSAIService/privapp-permissions-dollos-ai.xml
# 注意 DollOS-build root 可能不是 git；如果不是就 commit 在子模組或對應 vendor repo
```

---

## Task 7: AgentRuntime 整合 SensitiveActionGate

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/agent/AgentRuntime.kt`（或實際 agent dispatcher 所在）
- Test: `~/Projects/DollOSAIService/app/src/test/java/org/dollos/ai/agent/AgentRuntimeSensitiveTest.kt`

- [ ] **Step 1: 找 agent runtime 入口**

```bash
grep -rn "fun execute\|fun invoke\|fun runSkill\|class.*Agent.*Runtime\|class.*Agent.*Dispatcher" ~/Projects/DollOSAIService/app/src/main/java | head
```

- [ ] **Step 2: 寫測試（先 fail）**

```kotlin
@Test
fun `non-sensitive skill executes without gate`() = runTest {
    val gate = FakeGate(grantSensitive = false)
    val runtime = AgentRuntime(skills = listOf(fakeSkill("chat", sensitive = false)), gate = gate)
    val ok = runtime.execute("chat", emptyMap())
    assertTrue(ok)
    assertFalse(gate.wasCalled)
}

@Test
fun `sensitive skill calls gate, executes only if granted`() = runTest {
    val gate = FakeGate(grantSensitive = true)
    val runtime = AgentRuntime(skills = listOf(fakeSkill("open_bank", sensitive = true)), gate = gate)
    val ok = runtime.execute("open_bank", emptyMap())
    assertTrue(ok)
    assertTrue(gate.wasCalled)
}

@Test
fun `sensitive skill denied when gate refuses`() = runTest {
    val gate = FakeGate(grantSensitive = false)
    val runtime = AgentRuntime(skills = listOf(fakeSkill("open_bank", sensitive = true)), gate = gate)
    val ok = runtime.execute("open_bank", emptyMap())
    assertFalse(ok)
    assertTrue(gate.wasCalled)
}
```

- [ ] **Step 3: 跑確認 fail（FakeGate 還沒、AgentRuntime 簽名不對）**

- [ ] **Step 4: 實作改動**

`AgentRuntime` 接受 `gate: SensitiveActionGate` 注入；在 dispatch skill 前呼叫：

```kotlin
suspend fun execute(skillId: String, args: Map<String, Any?>): Boolean {
    val skill = skills.find { it.id == skillId } ?: return false
    val granted = gate.confirm(sensitive = skill.sensitive, reason = skill.description)
    if (!granted) return false
    skill.run(args)
    return true
}
```

- [ ] **Step 5: 跑 test**

```bash
./gradlew test --tests "*AgentRuntimeSensitiveTest*" 2>&1 | tail -10
```

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/org/dollos/ai/agent/AgentRuntime.kt app/src/test/java/org/dollos/ai/agent/AgentRuntimeSensitiveTest.kt
git commit -m "feat(agent): gate sensitive skills via SensitiveActionGate"
```

---

## Task 8: 設 DollOS 為預設 Assist app

**Files:**
- Modify: `~/Projects/DollOS-build/vendor/dollos/overlay/frameworks/base/core/res/res/values/config.xml`

- [ ] **Step 1: 找預設 assistant config key**

```bash
grep -rn "config_defaultAssistant\|DefaultAssist\|setting_assist" ~/Projects/DollOS-build/frameworks/base/core/res/res/values/config.xml
```

預期 key：`config_defaultAssistant`（string，component name）。

- [ ] **Step 2: 加進 vendor/dollos overlay**

```xml
<string name="config_defaultAssistant" translatable="false">org.dollos.ai/.voice.DollOSVoiceInteractionService</string>
```

- [ ] **Step 3: 也設 voice interaction service**

```xml
<string name="config_defaultVoiceInteractionService" translatable="false">org.dollos.ai/.voice.DollOSVoiceInteractionService</string>
```

- [ ] **Step 4: 確認 SettingsProvider 預設有寫**

```bash
grep -n "voice_interaction_service\|default_assistant" ~/Projects/DollOS-build/vendor/dollos/overlay/frameworks/base/packages/SettingsProvider/res/values/defaults.xml 2>/dev/null
```

如果沒有，加：

```xml
<string name="def_voice_interaction_service" translatable="false">org.dollos.ai/.voice.DollOSVoiceInteractionService</string>
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS-build/vendor/dollos && git add overlay/ && git commit -m "DollOS: default Assist + VoiceInteractionService = DollOSAIService"
```

---

## Task 9: Framework patch — 長按電源鍵 → launchAssist（依 Task 1 結果）

**Files:**
- Modify: `~/Projects/DollOS-build/frameworks/base/services/core/java/com/android/server/policy/PhoneWindowManager.java`（路徑 / 行號 by Task 1）
- 或: `~/Projects/DollOS-build/vendor/dollos/overlay/frameworks/base/core/res/res/values/config.xml`（如果 Task 1 發現只需改 config integer）

- [ ] **Step 1: 看 Task 1 的 discovery，決定用 RRO 還是 source patch**

A. 純 RRO：`config_longPressOnPowerBehavior` integer 改成「launch assist」對應的 enum 值。最簡。
B. Source patch：如果 GrapheneOS 把這個行為硬編 / 改寫成 emergency 之類，需要 patch `interceptPowerKeyDown` long-press 分支去呼叫 `launchAssistAction()`（framework 內建方法）。

- [ ] **Step 2: 改 RRO（A 路）**

```xml
<integer name="config_longPressOnPowerBehavior">5</integer>
```

(具體值依 Task 1 找到的 enum 對應；framework `LONG_PRESS_POWER_LAUNCH_ASSIST` 的 int 值為 5 in mainline AOSP，但需依 Task 1 確認。)

- [ ] **Step 3: 鎖屏分支驗證**

`interceptPowerKeyDown` 在 `mKeyguardOccluded` / `isKeyguardShowingAndNotOccluded()` 分支可能完全 bypass long-press behavior。讀 Task 1 discovery 文件，必要時加 source patch：

```java
// Around interceptPowerKeyDown long-press handling
if (isKeyguardShowingAndNotOccluded()) {
    if (mLongPressOnPowerBehavior == LONG_PRESS_POWER_LAUNCH_ASSIST) {
        launchAssistAction(null, deviceId, eventTime, INVOCATION_TYPE_POWER_BUTTON_LONG_PRESS);
        return;
    }
}
```

- [ ] **Step 4: build + push framework + reboot 驗證**

```bash
cd ~/Projects/DollOS-build && source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m services.core framework -j$(nproc)
# push services.jar / framework.jar
adb root && adb remount
adb push out/target/product/bluejay/system/framework/services.jar /system/framework/
adb push out/target/product/bluejay/system/framework/framework.jar /system/framework/
adb reboot
```

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOS-build/vendor/dollos && git add overlay/
cd ~/Projects/DollOS-build/frameworks/base && git add packages/...  # if source patch
git commit -m "DollOS: long-press power → launchAssist (incl. keyguard branch)"
```

---

## Task 10: VoiceController / VoicePipeline 改成 PTT-only

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/VoiceController.kt`
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/VoicePipeline.kt`

- [ ] **Step 1: 找 always-on ASR 啟動點**

```bash
grep -n "asr\\.start\|streamingASR\|alwaysListening" ~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/*.kt
```

- [ ] **Step 2: 移除 KWS engine 啟動 + always-on streaming**

把 `WakeWordEngine` 相關的呼叫整段刪掉。ASR 的 `start()` / `stop()` 改由外部（PTTSessionController）顯式控制。

- [ ] **Step 3: 暴露 PTTHardware / ASRRunner 對外的 thin adapter**

讓 PTTSessionController 能透過簡單介面操控：

```kotlin
class VoicePipelineAdapter(private val pipeline: VoicePipeline) : PTTHardware, ASRRunner, LLMRunner {
    override fun startRecording() = pipeline.startMic()
    override fun stopRecording() = pipeline.stopMic()
    override fun start() = pipeline.startASR()
    override fun stop(): String = pipeline.stopASRAndGetTranscript()
    override fun startTTS(text: String, onComplete: () -> Unit) = pipeline.tts(text, onComplete)
    override fun stopTTS() = pipeline.stopTTS()
    override fun chime(kind: ChimeKind) = pipeline.playChime(kind)
    override fun setEdgeOverlay(state: PTTState) = EdgeOverlayBridge.set(state)
    override suspend fun respond(transcript: String): String = pipeline.runLLM(transcript)
}
```

`EdgeOverlayBridge` 是現有 EdgeOverlayState API 的 thin wrapper。

- [ ] **Step 4: 跑 build 確認過**

```bash
./gradlew assembleRelease
```

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/
git commit -m "refactor(voice): PTT-only ASR, remove always-on streaming + KWS hooks"
```

---

## Task 11: 移除 wake word AIDL 介面

**Files:**
- Modify: `~/Projects/DollOSAIService/aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `~/Projects/DollOSAIService/aidl/org/dollos/ai/IDollOSAICallback.aidl`
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt`
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/CallbackBroadcaster.kt`
- Modify: `~/Projects/DollOSLauncher-avatar-live2d/app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`（callback stub no-ops 要刪）
- Modify: `~/Projects/DollOSLive2DWallpaper/app/src/main/java/org/dollos/wallpaper/WallpaperAIServiceClient.kt`（同上）

- [ ] **Step 1: 從 IDollOSAIService.aidl 移除**

```
void setWakeWordEnabled(boolean enabled);
boolean isWakeWordEnabled();
void setWakeWord(String keyword);
```

- [ ] **Step 2: 從 IDollOSAICallback.aidl 移除**

```
void onWakeWordDetected();
```

- [ ] **Step 3: 從 ServiceImpl 移除對應 override**

- [ ] **Step 4: 從 launcher / wallpaper 的 stub callback 移除 `onWakeWordDetected` no-op**

不刪會編不過（abstract method 不存在了）。

- [ ] **Step 5: Build 三個 repo 確認過**

```bash
cd ~/Projects/DollOSAIService && ./gradlew assembleRelease
cd ~/Projects/DollOSLauncher-avatar-live2d && ./gradlew assembleRelease
cd ~/Projects/DollOSLive2DWallpaper && ./gradlew assembleRelease
```

- [ ] **Step 6: Commit 各自 repo**

```bash
cd ~/Projects/DollOSAIService && git add aidl/ app/src/main/java/org/dollos/ai/DollOSAIServiceImpl.kt app/src/main/java/org/dollos/ai/CallbackBroadcaster.kt && git commit -m "refactor(aidl): drop wake word methods + onWakeWordDetected callback"
cd ~/Projects/DollOSLauncher-avatar-live2d && git add . && git commit -m "refactor: drop onWakeWordDetected callback stub"
cd ~/Projects/DollOSLive2DWallpaper && git add . && git commit -m "refactor: drop onWakeWordDetected callback stub"
```

---

## Task 12: 刪 WakeWordEngine + ONNX assets + character pack 欄位

**Files:**
- Delete: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/WakeWordEngine.kt`（或實際檔名）
- Delete: 相關 ONNX assets 路徑下的 model 檔
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterPack.kt` — 移除 `wakeWord` 欄位
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterValidator.kt` — 移除 `wake_word.onnx` 必檢

- [ ] **Step 1: 找 WakeWordEngine 檔案位置**

```bash
find ~/Projects/DollOSAIService -name "*WakeWord*" -o -name "*KWS*"
```

- [ ] **Step 2: 刪檔案**

```bash
git rm <files>
```

- [ ] **Step 3: 刪 character pack manifest 的 `wakeWord` 欄位**

`CharacterPack.kt`：拿掉 data class field `wakeWord: String?`。`CharacterManifest.fromJson` / `toJson` 不再讀 / 寫這個 key。

- [ ] **Step 4: 刪 validator 對 `wake_word.onnx` 的檢查**

`CharacterValidator.kt`：移除任何「找不到 wake_word.onnx 就 reject」。

- [ ] **Step 5: 既有 .doll pack 仍可 import（向後相容）**

import 流程遇到舊 pack 內含 `wake_word.onnx`：忽略不抱怨。manifest JSON 內若有 `wakeWord` key 也忽略。

- [ ] **Step 6: 跑既有 character import test 確認過**

```bash
./gradlew test --tests "*Character*"
```

- [ ] **Step 7: Commit**

```bash
git commit -m "refactor: remove WakeWordEngine + drop wakeWord from character pack manifest"
```

---

## Task 13: 移除 Settings UI 的 wake word 區

**Files:**
- Modify: 找 Settings UI 對應檔案（DollOSAIService 內或 AOSP Settings overlay）

- [ ] **Step 1: 找 wake word UI**

```bash
grep -rn "wake_word\|WakeWord\|wakeword" ~/Projects/DollOSAIService/app/src/main/res/ ~/Projects/DollOS-build/vendor/dollos/overlay/packages/apps/Settings/ 2>/dev/null
```

- [ ] **Step 2: 刪相關 preference / fragment / string**

- [ ] **Step 3: 跑 build 過**

- [ ] **Step 4: Commit**

```bash
git add . && git commit -m "refactor(settings): remove wake word UI section"
```

---

## Task 14: Audio focus 中斷序列（TTS → LISTENING）

**Files:**
- Modify: `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/voice/VoicePipeline.kt`（或 TtsEngine.kt）

- [ ] **Step 1: 確認當前 TTS engine release audio sink 的 API**

讀 `TtsEngine.kt` 看 `stop()` 是否 sync flush + release。

- [ ] **Step 2: 加保證序列**

`PTTSessionController.onPTTPress` 在 SPEAKING 路徑：

```kotlin
PTTState.SPEAKING -> {
    hw.stopTTS()
    scope.launch {
        delay(150)  // 100ms wait audio sink + 50ms quiet
        enterListening()
    }
}
```

延遲值依 Task 14 Step 1 觀察結果調整（最差不超過 200ms，免得使用者覺得卡）。

- [ ] **Step 3: 寫 instrumented test 驗 mic 沒抓到 TTS 尾巴**

如果太重，先靠手動驗（Task 15）。

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/org/dollos/ai/voice/
git commit -m "feat(voice): TTS interrupt → LISTENING audio focus sequence"
```

---

## Task 15: E2E 驗證 [HUMAN]

**Files:**
- 無 code 變更
- 寫結果到 `docs/superpowers/plans/2026-04-26-ptt-voice-input-result.md`

- [ ] **Step 1: 部署所有改動**

```bash
# DollOSAIService
cd ~/Projects/DollOSAIService && ./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/

# AOSP build
cd ~/Projects/DollOS-build && source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService Settings -j$(nproc)
m services.core framework -j$(nproc)  # 若 Task 9 動到 framework

# Push
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push out/target/product/bluejay/system_ext/priv-app/DollOSAIService/* /system_ext/priv-app/DollOSAIService/
adb push out/target/product/bluejay/system_ext/priv-app/Settings/* /system_ext/priv-app/Settings/
# framework
adb push out/target/product/bluejay/system/framework/services.jar /system/framework/
adb push out/target/product/bluejay/system/framework/framework.jar /system/framework/
adb shell "rm -rf /data/dalvik-cache/arm64/system_ext@priv-app@DollOSAIService@*"
adb reboot
```

- [ ] **Step 2: 開機後驗 PTT 在桌面**

解鎖 → 桌面 → 長按電源 → 應 chime + listening UI → 說「現在幾點」→ Doll 回答時間。

- [ ] **Step 3: 鎖屏 PTT**

睡眠 → 拿起手機 → 長按電源（不解鎖）→ chime → 「現在幾點」→ Doll 回答（語音 + 字幕浮在 wallpaper）。

- [ ] **Step 4: 鎖屏設鬧鐘（非敏感）**

鎖屏狀態 → PTT → 「設個 1 分鐘後的鬧鐘」→ Doll 直接設好（無指紋）。檢查 Clock app 真的有鬧鐘。

- [ ] **Step 5: 桌面敏感操作**

「打開銀行 app」→ 跳指紋 prompt → 過 → 開。

- [ ] **Step 6: 取消指紋**

「打開銀行 app」→ 跳指紋 prompt → 取消 → Doll 用 character 風格說「不行喔」之類，回 IDLE。

- [ ] **Step 7: 中斷 TTS 進新一輪**

PTT → 「給我講個長故事」→ Doll 開始講 → 中段再長按電源 → TTS 停 + 進 listening + chime → 講新問題。

- [ ] **Step 8: 取消 listening**

PTT → 立刻再長按電源 → 取消雙音「咚咚」 → 回 IDLE。

- [ ] **Step 9: KWS 確認移除**

```bash
adb logcat -d | grep -iE "WakeWord|openWakeWord|KWS"
```

Expected: 空。

- [ ] **Step 10: 寫結果報告**

```bash
cd ~/Projects/DollOS && cat > docs/superpowers/plans/2026-04-26-ptt-voice-input-result.md <<EOF
# PTT 語音輸入 — 驗證結果

[checkboxes from above with PASS/FAIL + notes]
EOF
git add docs/superpowers/plans/2026-04-26-ptt-voice-input-result.md
git commit -m "verify: PTT voice input E2E"
```

---

## §自我檢查（plan author）

**Spec coverage**
- ✅ Wake word pipeline 移除 → Task 11 + 12
- ✅ 註冊 Assist provider → Task 5 + 6
- ✅ 長按電源鍵 → launchAssist → Task 1 + 9
- ✅ Sensitive 標記 + 指紋確認 → Task 2 + 3 + 7
- ✅ 鎖屏 / 解鎖跟 PTT 解耦 → Task 5 (`supportsLaunchVoiceAssistFromKeyguard`) + Task 9 (keyguard branch)
- ✅ Speaker ID 退化為 hint → 既有 personalization path 不動，本 plan 不顯式處理（spec 說「passive hint」即現狀）
- ✅ 狀態機 / Audio focus interrupt → Task 4 + 14
- ✅ ASR PTT-only → Task 10
- ✅ Settings 移除 wake word → Task 13
- ✅ Verification → Task 15

**Placeholder scan**：步驟全有具體 code / command。Task 9 / 10 / 11 / 12 有「找 file path」步驟但都附 grep 指令，executor 能立刻找到。

**Type consistency**：`PTTState`、`PTTHardware`、`ASRRunner`、`LLMRunner`、`SensitiveActionGate.confirm` 在 Task 3 / 4 / 7 / 10 引用一致。

**Scope**：14 個實作 task + 1 驗證，單次 plan 可走完。
