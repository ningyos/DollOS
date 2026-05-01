# DollOSLauncher Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把既有 DollOSLauncher 從「AIService-bound multi-UI launcher」重構成「純 UI 殼 bound to DollOSCore」：移除 app drawer / 角色選擇 UI / Settings 入口、改 driven by Core `IDollCoreStateListener` ops events、支援 4 UI 動畫狀態 + 組合、支援人格 overlay 指示 + character pack hot-reload + `[VIBRATE]` 震動、並精簡 DollOSSetupWizard OOBE。

**Architecture:** Launcher 只做 UI。連到 DollOSCore 透過 AIDL (`IDollCore` + `IDollCoreStateListener`)，訂閱 ops events（`asr_started` / `asr_ended` / `llm_in_flight` / `llm_returned` / `tts_playing` / `tts_ended` / `vibrate` / `flag_changed`）來驅動 4 個 UI 動畫狀態（IDLE / LISTENING / THINKING / SPEAKING）。動畫允許組合（LISTENING+THINKING 可同時）。移除 app drawer、長按叫出角色選擇、Settings 入口；角色切換 / overlay / 任何設定都改對話觸發。OOBE 精簡為 Welcome → Character Pack 選擇 → API key → Done。

**Tech Stack:** Kotlin, Android Gradle, Filament (`com.google.android.filament:*` 1.54.5), TextureView + Choreographer, AIDL client, ViewModel (lifecycle), RecyclerView (OOBE character list), androidx-appcompat, ViewPager2。

**Spec reference:**
- Master plan: `docs/superpowers/plans/2026-04-20-doll-terminal.md`（§3.1 `IDollCore`、§3.2 `IDollCoreStateListener` + `ObservationEvent`、§4 Character Pack v2、§8 build 慣例、§12 交付判準、本 plan §13 驗收）
- Spec: `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`（§3 三層分工、§4.2 UI 動畫狀態、§5.5 人格 overlay、§6 既有元件改造映射）

**Master plan 依賴：**
- §3.1 `IDollCore.aidl` — `registerStateListener` / `unregisterStateListener` / `triggerConversation` / `getContextSnapshotJson` / `emergencyStop`
- §3.2 `IDollCoreStateListener.onOp(opName, stateJson)` — Launcher 訂閱此 callback 顯示動畫
- §4 Character Pack v2 format — `manifest.json` / `model.glb` / `animations/*.glb` / `scene.json`（3D hot-reload）
- §7 AOSP build 慣例 — prebuilt APK 模式
- §13 Launcher 驗收條件 — 開機直接進入 Doll 3D、無 drawer、overlay 由對話 activate

---

## 檔案結構

**新增**：
- `app/src/main/aidl/dollos/core/IDollCore.aidl` — 從 Core plan copy 的 AIDL
- `app/src/main/aidl/dollos/core/IDollCoreStateListener.aidl` — callback AIDL
- `app/src/main/aidl/dollos/core/ObservationEvent.aidl` — parcelable
- `app/src/main/aidl/dollos/core/SkillCallbackResult.aidl` — parcelable（`IDollCore` 有引用，需湊足編譯）
- `app/src/main/java/org/dollos/launcher/core/DollCoreClient.kt` — AIDL binding wrapper，生命週期 + 重連
- `app/src/main/java/org/dollos/launcher/core/OpsEventRouter.kt` — 把 `onOp(opName, stateJson)` 解析成 UI 事件
- `app/src/main/java/org/dollos/launcher/state/LauncherUiState.kt` — 4 flags（listening / thinking / speaking / dndActive）+ 目前 overlay id + character id 的 ViewModel-friendly state
- `app/src/main/java/org/dollos/launcher/state/LauncherViewModel.kt` — 持有 state + 把 ops events 映射成 state 變化
- `app/src/main/java/org/dollos/launcher/scene/CompositeAnimator.kt` — 新動畫控制器，能同時跑 LISTENING + THINKING（取代舊 single-state `AvatarAnimator`）
- `app/src/main/java/org/dollos/launcher/overlay/PersonalityOverlayIndicator.kt` — 右上角小 icon 顯示目前 overlay
- `app/src/main/java/org/dollos/launcher/subtitle/SubtitleView.kt` — 字幕 bubble 整合 layer（從 `ResponseBubbleView` 改造）
- `app/src/main/java/org/dollos/launcher/vibrate/VibrateDispatcher.kt` — 把 `opName="vibrate"` 執行為系統震動
- `app/src/test/java/org/dollos/launcher/core/OpsEventRouterTest.kt`
- `app/src/test/java/org/dollos/launcher/state/LauncherViewModelTest.kt`
- `app/src/test/java/org/dollos/launcher/scene/CompositeAnimatorTest.kt`
- `app/src/androidTest/java/org/dollos/launcher/OOBEEndToEndTest.kt`
- `app/src/androidTest/java/org/dollos/launcher/LauncherUiIntegrationTest.kt`
- `app/src/main/res/drawable/ic_overlay_badge.xml` — overlay indicator 底圖
- `app/src/main/res/layout/view_overlay_indicator.xml`
- **OOBE（`DollOSSetupWizard` 修改）**:
  - 保留：`WelcomePage.kt` / `ApiKeyPage.kt` / `CompletePage.kt`（精簡）
  - 新增：`CharacterPackPage.kt`（列出可用 .doll）
  - 刪除：`ThemePage.kt` / `GmsPage.kt` / `WifiPage.kt` / `ModelDownloadPage.kt` / `CharacterGenPage.kt`

**修改**：
- `app/build.gradle.kts` — 新增 `implementation` lifecycle + viewmodel + 保留 filament & recyclerview、啟用 `unitTestVariants`、加入 `testImplementation` (`junit`, `mockito-kotlin`, `kotlinx-coroutines-test`)
- `app/src/main/AndroidManifest.xml` — 加 `<uses-permission android:name="android.permission.VIBRATE" />`；刪 `QUERY_ALL_PACKAGES`（無 app drawer 不再需要）
- `app/src/main/res/layout/activity_launcher.xml` — 移除 drawer hint、import_character_button、long-press 中心區域；保留 filament TextureView + subtitle bubble；新增 `overlay_indicator` 右上位置
- `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt` — 從 500+ 行砍到 ~200 行：去掉 `AppDrawerView` / `CharacterPickerOverlay` / `InputBarView` / action confirm / package receiver、改 bind to `IDollCore`、訂閱 ops events via `OpsEventRouter`
- `app/src/main/java/org/dollos/launcher/scene/FilamentSceneManager.kt` — 新增 `unloadModel()` method 供 hot-reload 時釋放舊 asset（清 entities / destroyAsset）
- `DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt` — `pageKeys` 改為 `["welcome", "character_pack", "api_key", "complete"]`；`skippablePages` 清空；刪所有其他 page 的 fragment 分支

**刪除**：
- `app/src/main/java/org/dollos/launcher/drawer/*` — 整個資料夾（AppInfo、AppDrawerView、AppListAdapter、RecentAppsAdapter）
- `app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt`
- `app/src/main/java/org/dollos/launcher/conversation/InputBarView.kt`（語音是主輸入，OOBE 後沒有 text input bar — 若將來要字輸回歸再加）
- `app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt`（被 `CompositeAnimator` 取代）
- `app/src/main/res/layout/view_app_drawer.xml` / `view_character_picker.xml` / `view_input_bar.xml` / `item_app.xml` / `item_recent_app.xml` / `item_character.xml` / `view_action_confirm.xml`
- `app/src/main/res/drawable/bg_drawer.xml` / `bg_input_bar.xml` / `bg_confirm_card.xml`
- `DollOSSetupWizard/src/org/dollos/setup/ThemePage.kt` / `GmsPage.kt` / `WifiPage.kt` / `ModelDownloadPage.kt` / `CharacterGenPage.kt`

**合計**：~15 新增、~6 修改、~14 刪除。新 Kotlin code：約 800-1000 行。

---

## 段落總覽

1. 綁定 DollOSCore AIDL（AIDL 檔、Binding Client、ViewModel 骨架）
2. 移除 app drawer
3. 移除 角色選擇 UI（長按中心）
4. 移除 設定入口（InputBar / action confirm card / CharacterPickerOverlay）
5. UI 動畫狀態系統（CompositeAnimator + 4 flags）
6. Ops events 訂閱與 routing
7. 組合動畫支援（LISTENING + THINKING 共存）
8. 人格 overlay indicator（右上小 icon）
9. 字幕 bubble 整合（subtitle 層改用 SubtitleView）
10. Vibrate 動畫（`[VIBRATE]` op 驅動）
11. Character Pack hot-reload（model + animations 換載 + scene cleanup）
12. OOBE 精簡（DollOSSetupWizard → Welcome / Character Pack / API key / Done）
13. 整合測試 + UI 驗收

---

## Task 1: Drop AIDL interface files + parcelable placeholders

**Files:**
- Create: `app/src/main/aidl/dollos/core/IDollCore.aidl`
- Create: `app/src/main/aidl/dollos/core/IDollCoreStateListener.aidl`
- Create: `app/src/main/aidl/dollos/core/ObservationEvent.aidl`
- Create: `app/src/main/aidl/dollos/core/SkillCallbackResult.aidl`

- [ ] **Step 1: Write `IDollCore.aidl`**

```aidl
// app/src/main/aidl/dollos/core/IDollCore.aidl
// Version: 1
package dollos.core;

import dollos.core.ObservationEvent;
import dollos.core.IDollCoreStateListener;
import dollos.core.SkillCallbackResult;

interface IDollCore {
    void postObservation(in ObservationEvent event);
    void triggerConversation(String source, in Bundle extras);
    void postSkillCallback(String skillId, in SkillCallbackResult result);
    String getContextSnapshotJson();
    void registerStateListener(in IDollCoreStateListener listener);
    void unregisterStateListener(in IDollCoreStateListener listener);
    void setDndActive(boolean active, String reason);
    void emergencyStop(String reason);
}
```

- [ ] **Step 2: Write `IDollCoreStateListener.aidl`**

```aidl
// app/src/main/aidl/dollos/core/IDollCoreStateListener.aidl
// Version: 1
package dollos.core;

interface IDollCoreStateListener {
    void onOp(String opName, String stateJson);
}
```

- [ ] **Step 3: Write `ObservationEvent.aidl`**

```aidl
// app/src/main/aidl/dollos/core/ObservationEvent.aidl
// Version: 1
package dollos.core;

parcelable ObservationEvent {
    String type;
    long timestampMs;
    String payloadJson;
    String source;
}
```

- [ ] **Step 4: Write `SkillCallbackResult.aidl`**

```aidl
// app/src/main/aidl/dollos/core/SkillCallbackResult.aidl
// Version: 1
package dollos.core;

parcelable SkillCallbackResult {
    String status;    // "ok" | "error"
    String resultJson;
    String errorMessage;
}
```

- [ ] **Step 5: Ensure Gradle sees AIDL srcDirs**

Check `app/build.gradle.kts` contains:

```kotlin
buildFeatures {
    aidl = true
}
sourceSets["main"].aidl.srcDirs("src/main/aidl")
```

If the old `sourceSets["main"].aidl.srcDirs("aidl")` line still exists, change to `"src/main/aidl"` (default location).

- [ ] **Step 6: Gradle sync + compile**

Run: `cd ~/Projects/DollOSLauncher && ./gradlew :app:compileDebugAidl`
Expected: BUILD SUCCESSFUL, generates `app/build/generated/aidl_source_output_dir/.../dollos/core/IDollCore.java`

- [ ] **Step 7: Commit**

```bash
git add app/src/main/aidl/dollos/core/ app/build.gradle.kts
git commit -m "launcher: add DollOSCore AIDL client stubs"
```

---

## Task 2: Add Gradle deps (lifecycle ViewModel + test libraries)

**Files:**
- Modify: `app/build.gradle.kts`

- [ ] **Step 1: Modify `app/build.gradle.kts` — dependencies block**

Replace the `dependencies { ... }` block with:

```kotlin
dependencies {
    // Filament 3D rendering
    implementation("com.google.android.filament:filament-android:1.54.5")
    implementation("com.google.android.filament:gltfio-android:1.54.5")
    implementation("com.google.android.filament:filament-utils-android:1.54.5")

    // AndroidX
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.activity:activity-ktx:1.9.3")

    // Unit tests
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.mockito.kotlin:mockito-kotlin:5.3.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")

    // Instrumented tests
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}
```

- [ ] **Step 2: Add testInstrumentationRunner to `defaultConfig`**

Add inside `android { defaultConfig { ... } }`:

```kotlin
testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
```

- [ ] **Step 3: Sync**

Run: `cd ~/Projects/DollOSLauncher && ./gradlew :app:dependencies --configuration debugRuntimeClasspath > /tmp/launcher-deps.txt 2>&1 ; head -50 /tmp/launcher-deps.txt`
Expected: lifecycle-viewmodel-ktx + lifecycle-runtime-ktx lines visible.

- [ ] **Step 4: Commit**

```bash
git add app/build.gradle.kts
git commit -m "launcher: add lifecycle+test deps for Core binding"
```

---

## Task 3: DollCoreClient — AIDL binding wrapper (failing test first)

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/core/DollCoreClient.kt`
- Test: `app/src/test/java/org/dollos/launcher/core/DollCoreClientTest.kt`

- [ ] **Step 1: Write failing test `DollCoreClientTest.kt`**

```kotlin
package org.dollos.launcher.core

import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.core.IDollCore
import org.junit.Test
import org.junit.Assert.*
import org.mockito.kotlin.*

class DollCoreClientTest {

    @Test
    fun `bind calls Context#bindService with correct intent`() {
        val ctx: Context = mock()
        whenever(ctx.bindService(any(), any<ServiceConnection>(), any())).thenReturn(true)

        val client = DollCoreClient(ctx)
        client.bind()

        val captor = argumentCaptor<Intent>()
        verify(ctx).bindService(captor.capture(), any<ServiceConnection>(), eq(Context.BIND_AUTO_CREATE))
        assertEquals("org.dollos.core.IDollCore", captor.firstValue.action)
        assertEquals("org.dollos.core", captor.firstValue.`package`)
    }

    @Test
    fun `onServiceConnected exposes IDollCore via core getter`() {
        val ctx: Context = mock()
        whenever(ctx.bindService(any(), any<ServiceConnection>(), any())).thenReturn(true)

        val client = DollCoreClient(ctx)
        client.bind()

        val connCaptor = argumentCaptor<ServiceConnection>()
        verify(ctx).bindService(any(), connCaptor.capture(), any())

        val binder: IBinder = mock()
        val stubCore: IDollCore = mock()
        whenever(binder.queryLocalInterface("dollos.core.IDollCore")).thenReturn(stubCore)

        connCaptor.firstValue.onServiceConnected(null, binder)
        assertSame(stubCore, client.core)
    }

    @Test
    fun `unbind clears core and calls unbindService when bound`() {
        val ctx: Context = mock()
        whenever(ctx.bindService(any(), any<ServiceConnection>(), any())).thenReturn(true)

        val client = DollCoreClient(ctx)
        client.bind()
        client.unbind()

        verify(ctx).unbindService(any<ServiceConnection>())
        assertNull(client.core)
    }
}
```

- [ ] **Step 2: Run — expect failure**

Run: `cd ~/Projects/DollOSLauncher && ./gradlew :app:testDebugUnitTest --tests "*DollCoreClientTest*"`
Expected: FAIL with "Unresolved reference: DollCoreClient"

- [ ] **Step 3: Implement `DollCoreClient.kt`**

```kotlin
package org.dollos.launcher.core

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import dollos.core.IDollCore
import dollos.core.IDollCoreStateListener

class DollCoreClient(private val ctx: Context) {
    companion object {
        private const val TAG = "DollCoreClient"
        private const val ACTION = "org.dollos.core.IDollCore"
        private const val PKG = "org.dollos.core"
        private const val RETRY_MS = 2000L
    }

    var core: IDollCore? = null
        private set
    private var isBound = false
    private val handler = Handler(Looper.getMainLooper())
    private val listeners = mutableListOf<IDollCoreStateListener>()
    var onConnected: (() -> Unit)? = null
    var onDisconnected: (() -> Unit)? = null

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            core = IDollCore.Stub.asInterface(service)
            Log.i(TAG, "connected to IDollCore")
            listeners.forEach { runCatching { core?.registerStateListener(it) } }
            onConnected?.invoke()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            Log.w(TAG, "IDollCore disconnected, scheduling rebind in ${RETRY_MS}ms")
            core = null
            if (isBound) {
                runCatching { ctx.unbindService(this) }
                isBound = false
            }
            onDisconnected?.invoke()
            handler.postDelayed({ bind() }, RETRY_MS)
        }
    }

    fun bind(): Boolean {
        val intent = Intent(ACTION).apply { `package` = PKG }
        isBound = ctx.bindService(intent, connection, Context.BIND_AUTO_CREATE)
        if (!isBound) {
            Log.e(TAG, "bindService returned false; retry in ${RETRY_MS}ms")
            handler.postDelayed({ bind() }, RETRY_MS)
        }
        return isBound
    }

    fun unbind() {
        if (isBound) {
            runCatching { ctx.unbindService(connection) }
            isBound = false
        }
        core = null
    }

    fun addListener(listener: IDollCoreStateListener) {
        listeners += listener
        core?.let { runCatching { it.registerStateListener(listener) } }
    }

    fun removeListener(listener: IDollCoreStateListener) {
        listeners -= listener
        core?.let { runCatching { it.unregisterStateListener(listener) } }
    }
}
```

- [ ] **Step 4: Run — expect pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*DollCoreClientTest*"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/core/DollCoreClient.kt app/src/test/java/org/dollos/launcher/core/DollCoreClientTest.kt
git commit -m "launcher: DollCoreClient wraps bindService with auto-reconnect"
```

---

## Task 4: LauncherUiState data model

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/state/LauncherUiState.kt`
- Test: `app/src/test/java/org/dollos/launcher/state/LauncherUiStateTest.kt`

- [ ] **Step 1: Write failing test**

```kotlin
package org.dollos.launcher.state

import org.junit.Test
import org.junit.Assert.*

class LauncherUiStateTest {
    @Test
    fun `default state is all off`() {
        val s = LauncherUiState()
        assertFalse(s.listening)
        assertFalse(s.thinking)
        assertFalse(s.speaking)
        assertFalse(s.dndActive)
        assertNull(s.activeOverlayId)
        assertNull(s.activeCharacterId)
        assertNull(s.subtitle)
    }

    @Test
    fun `copy-with-listening turns on listening only`() {
        val s = LauncherUiState().copy(listening = true)
        assertTrue(s.listening)
        assertFalse(s.thinking)
    }

    @Test
    fun `composite listening and thinking coexist`() {
        val s = LauncherUiState().copy(listening = true, thinking = true)
        assertTrue(s.listening)
        assertTrue(s.thinking)
        assertFalse(s.speaking)
    }
}
```

- [ ] **Step 2: Run — expect fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*LauncherUiStateTest*"`
Expected: FAIL "Unresolved reference: LauncherUiState"

- [ ] **Step 3: Implement**

```kotlin
package org.dollos.launcher.state

data class LauncherUiState(
    val listening: Boolean = false,
    val thinking: Boolean = false,
    val speaking: Boolean = false,
    val dndActive: Boolean = false,
    val activeOverlayId: String? = null,
    val activeCharacterId: String? = null,
    val subtitle: String? = null,
)
```

- [ ] **Step 4: Run — pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*LauncherUiStateTest*"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/state/LauncherUiState.kt app/src/test/java/org/dollos/launcher/state/LauncherUiStateTest.kt
git commit -m "launcher: add LauncherUiState data class"
```

---

## Task 5: OpsEventRouter — parse ops to UI state transitions

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/core/OpsEventRouter.kt`
- Test: `app/src/test/java/org/dollos/launcher/core/OpsEventRouterTest.kt`

- [ ] **Step 1: Write failing test**

```kotlin
package org.dollos.launcher.core

import org.dollos.launcher.state.LauncherUiState
import org.junit.Test
import org.junit.Assert.*

class OpsEventRouterTest {

    private fun apply(ops: List<Pair<String, String>>, init: LauncherUiState = LauncherUiState()): LauncherUiState {
        var s = init
        val r = OpsEventRouter()
        for ((op, json) in ops) s = r.apply(s, op, json)
        return s
    }

    @Test fun `asr_started sets listening true`() {
        val s = apply(listOf("asr_started" to "{}"))
        assertTrue(s.listening)
    }

    @Test fun `asr_ended sets listening false`() {
        val s = apply(listOf("asr_started" to "{}", "asr_ended" to "{}"))
        assertFalse(s.listening)
    }

    @Test fun `llm_in_flight sets thinking true`() {
        val s = apply(listOf("llm_in_flight" to "{}"))
        assertTrue(s.thinking)
    }

    @Test fun `llm_returned sets thinking false`() {
        val s = apply(listOf("llm_in_flight" to "{}", "llm_returned" to "{}"))
        assertFalse(s.thinking)
    }

    @Test fun `tts_playing sets speaking true and copies text to subtitle`() {
        val s = apply(listOf("tts_playing" to """{"text":"hello"}"""))
        assertTrue(s.speaking)
        assertEquals("hello", s.subtitle)
    }

    @Test fun `tts_ended clears speaking and subtitle`() {
        val s = apply(listOf(
            "tts_playing" to """{"text":"hi"}""",
            "tts_ended" to "{}"
        ))
        assertFalse(s.speaking)
        assertNull(s.subtitle)
    }

    @Test fun `composite listening plus thinking coexist`() {
        val s = apply(listOf("asr_started" to "{}", "llm_in_flight" to "{}"))
        assertTrue(s.listening)
        assertTrue(s.thinking)
    }

    @Test fun `flag_changed dnd_active true sets dndActive`() {
        val s = apply(listOf("flag_changed" to """{"flag":"dnd_active","value":true}"""))
        assertTrue(s.dndActive)
    }

    @Test fun `flag_changed overlay_active sets activeOverlayId`() {
        val s = apply(listOf("flag_changed" to """{"flag":"overlay_active","value":"formal"}"""))
        assertEquals("formal", s.activeOverlayId)
    }

    @Test fun `flag_changed overlay_active null clears overlay`() {
        val init = LauncherUiState(activeOverlayId = "formal")
        val s = apply(listOf("flag_changed" to """{"flag":"overlay_active","value":null}"""), init)
        assertNull(s.activeOverlayId)
    }

    @Test fun `character_changed updates activeCharacterId`() {
        val s = apply(listOf("character_changed" to """{"characterId":"rin"}"""))
        assertEquals("rin", s.activeCharacterId)
    }

    @Test fun `unknown op does not mutate state`() {
        val init = LauncherUiState(listening = true)
        val s = apply(listOf("totally_new" to "{}"), init)
        assertEquals(init, s)
    }
}
```

- [ ] **Step 2: Run — expect fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*OpsEventRouterTest*"`
Expected: FAIL "Unresolved reference: OpsEventRouter"

- [ ] **Step 3: Implement**

```kotlin
package org.dollos.launcher.core

import org.dollos.launcher.state.LauncherUiState
import org.json.JSONObject

/** Pure function mapping a single (opName, stateJson) → new UI state. */
class OpsEventRouter {

    fun apply(s: LauncherUiState, opName: String, stateJson: String): LauncherUiState {
        val json = runCatching { JSONObject(stateJson) }.getOrNull() ?: JSONObject()
        return when (opName) {
            "asr_started"   -> s.copy(listening = true)
            "asr_ended"     -> s.copy(listening = false)
            "llm_in_flight" -> s.copy(thinking = true)
            "llm_returned"  -> s.copy(thinking = false)
            "tts_playing"   -> s.copy(speaking = true, subtitle = json.optString("text").ifEmpty { null })
            "tts_ended"     -> s.copy(speaking = false, subtitle = null)
            "vibrate"       -> s // state unchanged; side effect handled by VibrateDispatcher
            "flag_changed"  -> applyFlag(s, json)
            "character_changed" -> s.copy(activeCharacterId = json.optString("characterId").ifEmpty { null })
            else            -> s
        }
    }

    private fun applyFlag(s: LauncherUiState, json: JSONObject): LauncherUiState {
        val flag = json.optString("flag")
        return when (flag) {
            "dnd_active" -> s.copy(dndActive = json.optBoolean("value", false))
            "overlay_active" -> {
                val v = if (json.isNull("value")) null else json.optString("value").ifEmpty { null }
                s.copy(activeOverlayId = v)
            }
            else -> s
        }
    }
}
```

- [ ] **Step 4: Run — pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*OpsEventRouterTest*"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/core/OpsEventRouter.kt app/src/test/java/org/dollos/launcher/core/OpsEventRouterTest.kt
git commit -m "launcher: OpsEventRouter maps Core ops to UI state"
```

---

## Task 6: LauncherViewModel — state holder + event intake

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/state/LauncherViewModel.kt`
- Test: `app/src/test/java/org/dollos/launcher/state/LauncherViewModelTest.kt`

- [ ] **Step 1: Write failing test**

```kotlin
package org.dollos.launcher.state

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Before
import org.junit.Test
import org.junit.Assert.*

@OptIn(ExperimentalCoroutinesApi::class)
class LauncherViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before fun setUp() { Dispatchers.setMain(dispatcher) }
    @After fun tearDown() { Dispatchers.resetMain() }

    @Test fun `onOp asr_started updates state listening true`() = runTest {
        val vm = LauncherViewModel()
        vm.onOp("asr_started", "{}")
        dispatcher.scheduler.advanceUntilIdle()
        assertTrue(vm.state.value.listening)
    }

    @Test fun `state flow emits on each op`() = runTest {
        val vm = LauncherViewModel()
        vm.onOp("asr_started", "{}")
        vm.onOp("llm_in_flight", "{}")
        dispatcher.scheduler.advanceUntilIdle()
        val s = vm.state.value
        assertTrue(s.listening)
        assertTrue(s.thinking)
    }
}
```

- [ ] **Step 2: Run — expect fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*LauncherViewModelTest*"`
Expected: FAIL "Unresolved reference: LauncherViewModel"

- [ ] **Step 3: Implement**

```kotlin
package org.dollos.launcher.state

import androidx.lifecycle.ViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.dollos.launcher.core.OpsEventRouter

class LauncherViewModel(
    private val router: OpsEventRouter = OpsEventRouter(),
) : ViewModel() {

    private val _state = MutableStateFlow(LauncherUiState())
    val state: StateFlow<LauncherUiState> = _state.asStateFlow()

    /** Feed an op event from Core's IDollCoreStateListener.onOp(). */
    fun onOp(opName: String, stateJson: String) {
        _state.value = router.apply(_state.value, opName, stateJson)
    }

    /** Programmatic subtitle (rare — e.g. loading indicator during Core connect). */
    fun setSubtitle(text: String?) {
        _state.value = _state.value.copy(subtitle = text)
    }
}
```

- [ ] **Step 4: Run — pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*LauncherViewModelTest*"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/state/LauncherViewModel.kt app/src/test/java/org/dollos/launcher/state/LauncherViewModelTest.kt
git commit -m "launcher: add LauncherViewModel owning UI state flow"
```

---

## Task 7: Delete app drawer — source files

**Files:**
- Delete: `app/src/main/java/org/dollos/launcher/drawer/AppInfo.kt`
- Delete: `app/src/main/java/org/dollos/launcher/drawer/AppDrawerView.kt`
- Delete: `app/src/main/java/org/dollos/launcher/drawer/AppListAdapter.kt`
- Delete: `app/src/main/java/org/dollos/launcher/drawer/RecentAppsAdapter.kt`
- Delete: `app/src/main/res/layout/view_app_drawer.xml`
- Delete: `app/src/main/res/layout/item_app.xml`
- Delete: `app/src/main/res/layout/item_recent_app.xml`
- Delete: `app/src/main/res/drawable/bg_drawer.xml`
- Modify: `app/src/main/AndroidManifest.xml` — remove `QUERY_ALL_PACKAGES` permission

- [ ] **Step 1: Delete Kotlin files**

```bash
rm app/src/main/java/org/dollos/launcher/drawer/AppInfo.kt
rm app/src/main/java/org/dollos/launcher/drawer/AppDrawerView.kt
rm app/src/main/java/org/dollos/launcher/drawer/AppListAdapter.kt
rm app/src/main/java/org/dollos/launcher/drawer/RecentAppsAdapter.kt
rmdir app/src/main/java/org/dollos/launcher/drawer
```

- [ ] **Step 2: Delete layout + drawable files**

```bash
rm app/src/main/res/layout/view_app_drawer.xml
rm app/src/main/res/layout/item_app.xml
rm app/src/main/res/layout/item_recent_app.xml
rm app/src/main/res/drawable/bg_drawer.xml
```

- [ ] **Step 3: Edit `AndroidManifest.xml`** — delete the line

```xml
<uses-permission android:name="android.permission.QUERY_ALL_PACKAGES" />
```

- [ ] **Step 4: Verify nothing still references drawer classes**

Run: `grep -r "AppDrawerView\|AppListAdapter\|RecentAppsAdapter\|drawer\.Adapter" app/src/main/`
Expected: empty output.

Run: `grep -r "R.layout.view_app_drawer\|R.layout.item_app\b\|R.layout.item_recent_app\|R.drawable.bg_drawer" app/src/main/`
Expected: only `DollOSLauncherActivity.kt` may still match — we'll clean that in Task 10.

- [ ] **Step 5: Commit (delete-only — Activity still references removed classes, fixed in Task 10)**

```bash
git add -A app/src/main/java/org/dollos/launcher/drawer app/src/main/res/layout/view_app_drawer.xml app/src/main/res/layout/item_app.xml app/src/main/res/layout/item_recent_app.xml app/src/main/res/drawable/bg_drawer.xml app/src/main/AndroidManifest.xml
git commit -m "launcher: remove app drawer files (Activity cleanup in task 10)"
```

---

## Task 8: Delete character picker overlay

**Files:**
- Delete: `app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt`
- Delete: `app/src/main/res/layout/view_character_picker.xml`
- Delete: `app/src/main/res/layout/item_character.xml`

- [ ] **Step 1: Delete files**

```bash
rm app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt
rmdir app/src/main/java/org/dollos/launcher/character
rm app/src/main/res/layout/view_character_picker.xml
rm app/src/main/res/layout/item_character.xml
```

- [ ] **Step 2: Verify only Activity references remain**

Run: `grep -r "CharacterPickerOverlay\|view_character_picker\|item_character" app/src/main/`
Expected: only `DollOSLauncherActivity.kt` — cleaned in Task 10.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "launcher: remove CharacterPickerOverlay (dialog-driven character switch comes from Core ops)"
```

---

## Task 9: Delete InputBarView + action confirm card

**Files:**
- Delete: `app/src/main/java/org/dollos/launcher/conversation/InputBarView.kt`
- Delete: `app/src/main/res/layout/view_input_bar.xml`
- Delete: `app/src/main/res/layout/view_action_confirm.xml`
- Delete: `app/src/main/res/drawable/bg_input_bar.xml`
- Delete: `app/src/main/res/drawable/bg_confirm_card.xml`

- [ ] **Step 1: Delete files**

```bash
rm app/src/main/java/org/dollos/launcher/conversation/InputBarView.kt
rm app/src/main/res/layout/view_input_bar.xml
rm app/src/main/res/layout/view_action_confirm.xml
rm app/src/main/res/drawable/bg_input_bar.xml
rm app/src/main/res/drawable/bg_confirm_card.xml
```

- [ ] **Step 2: Verify**

Run: `grep -r "InputBarView\|view_input_bar\|view_action_confirm\|bg_input_bar\|bg_confirm_card" app/src/main/`
Expected: only `DollOSLauncherActivity.kt`.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "launcher: remove text InputBar + action confirm card (voice-only input, confirms flow through DollOSService safety UI)"
```

---

## Task 10: Rewrite DollOSLauncherActivity — minimal shell bound to Core

**Files:**
- Modify: `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt` — rewrite whole file
- Modify: `app/src/main/res/layout/activity_launcher.xml` — strip drawer hint / import button

- [ ] **Step 1: Replace `activity_launcher.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:id="@+id/root"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="@color/scene_background">

    <!-- Filament 3D scene -->
    <TextureView
        android:id="@+id/filament_texture_view"
        android:layout_width="match_parent"
        android:layout_height="match_parent" />

    <!-- Loading spinner while DollOSCore is binding -->
    <LinearLayout
        android:id="@+id/loading_container"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_gravity="center"
        android:orientation="vertical"
        android:gravity="center">

        <ProgressBar
            android:layout_width="32dp"
            android:layout_height="32dp"
            style="?android:attr/progressBarStyle" />

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_marginTop="12dp"
            android:text="Waking Doll..."
            android:textColor="#88ffffff"
            android:textSize="14sp" />
    </LinearLayout>

    <!-- UI overlay -->
    <FrameLayout
        android:id="@+id/ui_overlay"
        android:layout_width="match_parent"
        android:layout_height="match_parent"
        android:visibility="gone">

        <!-- Subtitle bubble (centered upper region) -->
        <include
            layout="@layout/view_response_bubble"
            android:id="@+id/subtitle_bubble"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_gravity="center_horizontal"
            android:layout_marginTop="120dp"
            android:visibility="gone" />

        <!-- Battery / thermal pause indicator -->
        <View
            android:id="@+id/battery_pause_indicator"
            android:layout_width="24dp"
            android:layout_height="24dp"
            android:layout_gravity="top|end"
            android:layout_marginTop="48dp"
            android:layout_marginEnd="16dp"
            android:background="@drawable/ic_pause_indicator"
            android:alpha="0.4"
            android:visibility="gone" />

        <!-- Personality overlay indicator (top-right badge) -->
        <include
            layout="@layout/view_overlay_indicator"
            android:id="@+id/overlay_indicator"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_gravity="top|end"
            android:layout_marginTop="48dp"
            android:layout_marginEnd="56dp"
            android:visibility="gone" />
    </FrameLayout>

</FrameLayout>
```

- [ ] **Step 2: Replace `DollOSLauncherActivity.kt`**

```kotlin
package org.dollos.launcher

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.BroadcastReceiver
import android.os.Bundle
import android.os.PowerManager
import android.util.Log
import android.view.TextureView
import android.view.View
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import dollos.core.IDollCoreStateListener
import kotlinx.coroutines.launch
import org.dollos.launcher.core.DollCoreClient
import org.dollos.launcher.overlay.PersonalityOverlayIndicator
import org.dollos.launcher.scene.CompositeAnimator
import org.dollos.launcher.scene.FilamentSceneManager
import org.dollos.launcher.state.LauncherViewModel
import org.dollos.launcher.subtitle.SubtitleView
import org.dollos.launcher.vibrate.VibrateDispatcher

class DollOSLauncherActivity : AppCompatActivity() {

    companion object { private const val TAG = "DollOSLauncher" }

    private val vm: LauncherViewModel by viewModels()
    private lateinit var filamentScene: FilamentSceneManager
    private lateinit var animator: CompositeAnimator
    private lateinit var subtitle: SubtitleView
    private lateinit var overlayIndicator: PersonalityOverlayIndicator
    private lateinit var vibrateDispatcher: VibrateDispatcher
    private lateinit var coreClient: DollCoreClient
    private var lastCharacterId: String? = null

    private val stateListener = object : IDollCoreStateListener.Stub() {
        override fun onOp(opName: String?, stateJson: String?) {
            opName ?: return
            runOnUiThread {
                vm.onOp(opName, stateJson ?: "{}")
                if (opName == "vibrate") vibrateDispatcher.dispatch(stateJson ?: "{}")
            }
        }
    }

    private val powerReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) = updatePowerState()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_launcher)

        val textureView = findViewById<TextureView>(R.id.filament_texture_view)
        filamentScene = FilamentSceneManager(textureView)
        animator = CompositeAnimator { flags -> filamentScene.setAnimationFlags(flags) }
        subtitle = SubtitleView(findViewById(R.id.subtitle_bubble))
        overlayIndicator = PersonalityOverlayIndicator(findViewById(R.id.overlay_indicator))
        vibrateDispatcher = VibrateDispatcher(this)

        textureView.isSoundEffectsEnabled = false

        coreClient = DollCoreClient(this).apply {
            onConnected = {
                runOnUiThread {
                    findViewById<View>(R.id.loading_container).visibility = View.GONE
                    findViewById<View>(R.id.ui_overlay).visibility = View.VISIBLE
                }
            }
            onDisconnected = {
                runOnUiThread {
                    findViewById<View>(R.id.loading_container).visibility = View.VISIBLE
                    findViewById<View>(R.id.ui_overlay).visibility = View.GONE
                }
            }
            addListener(stateListener)
        }
        coreClient.bind()

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                vm.state.collect { s ->
                    animator.applyFlags(s.listening, s.thinking, s.speaking)
                    subtitle.render(s.subtitle)
                    overlayIndicator.render(s.activeOverlayId)
                    maybeReloadCharacter(s.activeCharacterId)
                }
            }
        }

        registerReceiver(
            powerReceiver,
            IntentFilter().apply {
                addAction(Intent.ACTION_BATTERY_LOW)
                addAction(Intent.ACTION_BATTERY_OKAY)
                addAction(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED)
            },
            Context.RECEIVER_EXPORTED,
        )
    }

    private fun maybeReloadCharacter(characterId: String?) {
        if (characterId == null || characterId == lastCharacterId) return
        Log.i(TAG, "character_changed → hot-reload $characterId")
        lastCharacterId = characterId
        val assets = CharacterAssetFetcher(this).fetch(characterId) ?: return
        filamentScene.unloadModel()
        filamentScene.applySceneConfig(assets.scene)
        filamentScene.loadModel(assets.modelFd)
    }

    override fun onResume() {
        super.onResume()
        updatePowerState()
    }

    override fun onPause() {
        super.onPause()
        filamentScene.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        runCatching { unregisterReceiver(powerReceiver) }
        coreClient.removeListener(stateListener)
        coreClient.unbind()
        filamentScene.destroy()
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        // No back navigation: launcher is home.
    }

    private fun updatePowerState() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val bm = getSystemService(Context.BATTERY_SERVICE) as android.os.BatteryManager
        val level = bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val pauseForThermal = pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_MODERATE
        val pause = pm.isPowerSaveMode || pauseForThermal || level in 1..15
        val indicator = findViewById<View>(R.id.battery_pause_indicator)
        if (pause) {
            filamentScene.pause()
            indicator.visibility = View.VISIBLE
        } else {
            indicator.visibility = View.GONE
            filamentScene.resume()
        }
    }
}
```

- [ ] **Step 3: Compile — `CharacterAssetFetcher`, `CompositeAnimator`, `PersonalityOverlayIndicator`, `SubtitleView`, `VibrateDispatcher` don't exist yet**

Run: `./gradlew :app:compileDebugKotlin`
Expected: FAIL with "Unresolved reference: CharacterAssetFetcher / CompositeAnimator / PersonalityOverlayIndicator / SubtitleView / VibrateDispatcher / view_overlay_indicator"

This is fine — those are tasks 11-16. Keep activity code as-is and move on.

- [ ] **Step 4: Commit (known compile break — fixed by tasks 11-16)**

```bash
git add app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt app/src/main/res/layout/activity_launcher.xml
git commit -m "launcher: rewrite Activity as Core-bound shell (depends on upcoming components)"
```

---

## Task 11: CompositeAnimator — 支援同時多個 UI 狀態

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/scene/CompositeAnimator.kt`
- Test: `app/src/test/java/org/dollos/launcher/scene/CompositeAnimatorTest.kt`
- Delete: `app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt`

- [ ] **Step 1: Write failing test**

```kotlin
package org.dollos.launcher.scene

import org.junit.Test
import org.junit.Assert.*

class CompositeAnimatorTest {

    private val emitted = mutableListOf<Set<String>>()
    private val anim = CompositeAnimator { emitted += it.toSet() }

    @Test fun `all off emits idle`() {
        anim.applyFlags(listening = false, thinking = false, speaking = false)
        assertEquals(setOf("idle"), emitted.last())
    }

    @Test fun `only listening on emits listening`() {
        anim.applyFlags(listening = true, thinking = false, speaking = false)
        assertEquals(setOf("listening"), emitted.last())
    }

    @Test fun `listening plus thinking emits both flags`() {
        anim.applyFlags(listening = true, thinking = true, speaking = false)
        assertEquals(setOf("listening", "thinking"), emitted.last())
    }

    @Test fun `speaking only emits speaking (listening+speaking does not happen per spec, but if it did speaking wins)`() {
        anim.applyFlags(listening = false, thinking = false, speaking = true)
        assertEquals(setOf("speaking"), emitted.last())
    }

    @Test fun `no emit when flags unchanged`() {
        anim.applyFlags(listening = true, thinking = false, speaking = false)
        anim.applyFlags(listening = true, thinking = false, speaking = false)
        assertEquals(1, emitted.size)
    }
}
```

- [ ] **Step 2: Run — fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*CompositeAnimatorTest*"`
Expected: FAIL "Unresolved reference: CompositeAnimator"

- [ ] **Step 3: Implement**

```kotlin
package org.dollos.launcher.scene

/**
 * Collects three UI flags and emits a composite set of animation names.
 * The FilamentSceneManager receives the set and blends on bones.
 *
 * Composition rules:
 * - Nothing on → {"idle"}
 * - listening only → {"listening"}
 * - thinking only → {"thinking"}
 * - speaking on → speaking wins alone (mouth sync dominates)
 * - listening + thinking → {"listening", "thinking"} (user still talking while Doll starts reasoning)
 */
class CompositeAnimator(private val onFlags: (Set<String>) -> Unit) {

    private var last: Set<String>? = null

    fun applyFlags(listening: Boolean, thinking: Boolean, speaking: Boolean) {
        val flags = compute(listening, thinking, speaking)
        if (flags == last) return
        last = flags
        onFlags(flags)
    }

    private fun compute(listening: Boolean, thinking: Boolean, speaking: Boolean): Set<String> {
        if (speaking) return setOf("speaking")
        val s = mutableSetOf<String>()
        if (listening) s += "listening"
        if (thinking)  s += "thinking"
        if (s.isEmpty()) s += "idle"
        return s
    }
}
```

- [ ] **Step 4: Run — pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*CompositeAnimatorTest*"`
Expected: PASS

- [ ] **Step 5: Add `setAnimationFlags(Set<String>)` to `FilamentSceneManager.kt`**

Replace the existing `fun setAnimationState(state: String)` block (around line 293-303) with:

```kotlin
private var animationStartTime = 0L
private var activeFlags: Set<String> = setOf("idle")
private var currentAnimIdx = -1

/** Legacy single-state API kept for transitional calls. */
fun setAnimationState(state: String) {
    setAnimationFlags(setOf(state))
}

fun setAnimationFlags(flags: Set<String>) {
    activeFlags = flags
    currentAnimIdx = -1
    animationStartTime = 0L
    targetFrameIntervalMs = if (flags.singleOrNull() == "idle") IDLE_FRAME_INTERVAL_MS else ACTIVE_FRAME_INTERVAL_MS
}
```

Then update the `private fun render(frameTimeNanos: Long)` animation-picking block (around line 305-326) so it picks one "primary" animation from the set (priority: speaking > thinking > listening > idle) — the current 3D rigs ship only single animations per state; true blending is a future optimization:

```kotlin
private fun pickPrimary(): String {
    return when {
        "speaking"  in activeFlags -> "speaking"
        "thinking"  in activeFlags -> "thinking"
        "listening" in activeFlags -> "listening"
        else -> "idle"
    }
}
```

And replace the existing `if (currentAnimIdx < 0)` block inside `render()` with:

```kotlin
if (currentAnimIdx < 0) {
    val animName = sceneConfig.animationMap[pickPrimary()]
    currentAnimIdx = if (animName != null) {
        (0 until anim.animationCount).firstOrNull { anim.getAnimationName(it) == animName } ?: 0
    } else 0
}
```

- [ ] **Step 6: Delete legacy `AvatarAnimator.kt`**

```bash
rm app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt
```

- [ ] **Step 7: Compile**

Run: `./gradlew :app:compileDebugKotlin 2>&1 | tail -20`
Expected: still fails on `CharacterAssetFetcher / PersonalityOverlayIndicator / SubtitleView / VibrateDispatcher / view_overlay_indicator` but `CompositeAnimator` resolved.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "launcher: CompositeAnimator supports simultaneous listening+thinking states"
```

---

## Task 12: SubtitleView (subtitle bubble integration)

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/subtitle/SubtitleView.kt`
- Modify: `app/src/main/java/org/dollos/launcher/conversation/ResponseBubbleView.kt` — left as-is (still used internally)
- Test: `app/src/test/java/org/dollos/launcher/subtitle/SubtitleViewTest.kt` (use Robolectric? — skip, test via AndroidTest below)

- [ ] **Step 1: Implement `SubtitleView.kt` wrapping existing `ResponseBubbleView`**

```kotlin
package org.dollos.launcher.subtitle

import android.view.View
import org.dollos.launcher.conversation.ResponseBubbleView

/**
 * Stateless adapter driven by LauncherUiState.subtitle:
 *   - null   → hide
 *   - empty  → hide (treat as null)
 *   - any    → show as complete text
 */
class SubtitleView(bubbleRoot: View) {

    private val bubble = ResponseBubbleView(bubbleRoot)

    fun render(text: String?) {
        if (text.isNullOrEmpty()) {
            if (bubble.isVisible()) bubble.dismiss()
        } else {
            bubble.setComplete(text)
        }
    }
}
```

- [ ] **Step 2: Verify ResponseBubbleView keeps its existing API**

Run: `grep -n "fun setComplete\|fun dismiss\|fun isVisible" app/src/main/java/org/dollos/launcher/conversation/ResponseBubbleView.kt`
Expected: all three methods present.

- [ ] **Step 3: Compile**

Run: `./gradlew :app:compileDebugKotlin 2>&1 | tail -20`
Expected: SubtitleView resolved; remaining errors limited to other new components.

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/subtitle/SubtitleView.kt
git commit -m "launcher: SubtitleView renders LauncherUiState.subtitle"
```

---

## Task 13: PersonalityOverlayIndicator (右上 overlay 小 icon)

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/overlay/PersonalityOverlayIndicator.kt`
- Create: `app/src/main/res/layout/view_overlay_indicator.xml`
- Create: `app/src/main/res/drawable/ic_overlay_badge.xml`

- [ ] **Step 1: Write layout `view_overlay_indicator.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="wrap_content"
    android:layout_height="wrap_content"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:background="@drawable/ic_overlay_badge"
    android:paddingStart="10dp"
    android:paddingEnd="12dp"
    android:paddingTop="4dp"
    android:paddingBottom="4dp">

    <View
        android:layout_width="6dp"
        android:layout_height="6dp"
        android:layout_marginEnd="6dp"
        android:background="#99ffffff" />

    <TextView
        android:id="@+id/overlay_label"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:textColor="#e0ffffff"
        android:textSize="11sp"
        android:fontFamily="sans-serif-medium" />

</LinearLayout>
```

- [ ] **Step 2: Write drawable `ic_overlay_badge.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android"
    android:shape="rectangle">
    <solid android:color="#66000000" />
    <corners android:radius="10dp" />
    <stroke android:width="0.5dp" android:color="#44ffffff" />
</shape>
```

- [ ] **Step 3: Implement `PersonalityOverlayIndicator.kt`**

```kotlin
package org.dollos.launcher.overlay

import android.view.View
import android.widget.TextView
import org.dollos.launcher.R

/**
 * Small top-right badge that shows the active personality overlay (e.g. "FORMAL MODE").
 * null → hidden. Driven by LauncherUiState.activeOverlayId.
 *
 * Label is derived from the overlayId via a fixed map (keeps code local — ids come
 * from manifest.json.personality.overlays).
 */
class PersonalityOverlayIndicator(private val root: View) {

    private val label: TextView = root.findViewById(R.id.overlay_label)

    fun render(overlayId: String?) {
        if (overlayId.isNullOrEmpty()) {
            root.visibility = View.GONE
        } else {
            label.text = humanLabel(overlayId).uppercase()
            root.visibility = View.VISIBLE
        }
    }

    private fun humanLabel(id: String): String = when (id) {
        "formal"  -> "認真模式"
        "playful" -> "頑皮模式"
        "cold"    -> "冷淡模式"
        else      -> id
    }
}
```

- [ ] **Step 4: Compile**

Run: `./gradlew :app:compileDebugKotlin 2>&1 | tail -20`
Expected: overlay indicator resolved. Remaining: `VibrateDispatcher`, `CharacterAssetFetcher`.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/overlay/ app/src/main/res/layout/view_overlay_indicator.xml app/src/main/res/drawable/ic_overlay_badge.xml
git commit -m "launcher: PersonalityOverlayIndicator shows active overlay as top-right badge"
```

---

## Task 14: VibrateDispatcher — 執行 `vibrate` op

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/vibrate/VibrateDispatcher.kt`
- Test: `app/src/test/java/org/dollos/launcher/vibrate/VibrateDispatcherTest.kt`
- Modify: `app/src/main/AndroidManifest.xml` — add VIBRATE permission

- [ ] **Step 1: Add permission to manifest**

Insert into `<manifest>`:

```xml
<uses-permission android:name="android.permission.VIBRATE" />
```

- [ ] **Step 2: Write failing test**

```kotlin
package org.dollos.launcher.vibrate

import android.content.Context
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import org.junit.Test
import org.mockito.kotlin.*

class VibrateDispatcherTest {

    @Test fun `dispatch with empty json vibrates with default pattern`() {
        val vib: Vibrator = mock()
        val mgr: VibratorManager = mock { on { defaultVibrator } doReturn vib }
        val ctx: Context = mock { on { getSystemService(Context.VIBRATOR_MANAGER_SERVICE) } doReturn mgr }

        VibrateDispatcher(ctx).dispatch("{}")

        verify(vib).vibrate(any<VibrationEffect>())
    }

    @Test fun `dispatch with custom pattern passes VibrationEffect to vibrator`() {
        val vib: Vibrator = mock()
        val mgr: VibratorManager = mock { on { defaultVibrator } doReturn vib }
        val ctx: Context = mock { on { getSystemService(Context.VIBRATOR_MANAGER_SERVICE) } doReturn mgr }

        VibrateDispatcher(ctx).dispatch("""{"pattern":[0,100,80,100]}""")

        verify(vib).vibrate(any<VibrationEffect>())
    }
}
```

- [ ] **Step 3: Run — fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*VibrateDispatcherTest*"`
Expected: FAIL "Unresolved reference: VibrateDispatcher"

- [ ] **Step 4: Implement**

```kotlin
package org.dollos.launcher.vibrate

import android.content.Context
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import org.json.JSONArray
import org.json.JSONObject

class VibrateDispatcher(ctx: Context) {

    private val vibrator: Vibrator? = run {
        val mgr = ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
        mgr?.defaultVibrator
    }

    fun dispatch(stateJson: String) {
        val v = vibrator ?: return
        val pattern = parsePattern(stateJson) ?: DEFAULT_PATTERN
        v.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }

    private fun parsePattern(json: String): LongArray? {
        val arr: JSONArray = runCatching { JSONObject(json).getJSONArray("pattern") }.getOrNull() ?: return null
        val out = LongArray(arr.length())
        for (i in 0 until arr.length()) out[i] = arr.optLong(i)
        return out.takeIf { it.isNotEmpty() }
    }

    companion object {
        private val DEFAULT_PATTERN = longArrayOf(0, 80, 60, 80)
    }
}
```

- [ ] **Step 5: Run — pass**

Run: `./gradlew :app:testDebugUnitTest --tests "*VibrateDispatcherTest*"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/vibrate/ app/src/main/AndroidManifest.xml app/src/test/java/org/dollos/launcher/vibrate/
git commit -m "launcher: VibrateDispatcher executes Core 'vibrate' ops"
```

---

## Task 15: CharacterAssetFetcher + FilamentSceneManager.unloadModel (hot-reload support)

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/CharacterAssetFetcher.kt`
- Modify: `app/src/main/java/org/dollos/launcher/scene/FilamentSceneManager.kt` — add `unloadModel()`
- Test: `app/src/test/java/org/dollos/launcher/CharacterAssetFetcherTest.kt`

- [ ] **Step 1: Design memory cleanup in `FilamentSceneManager.unloadModel()`**

The existing `loadModel()` already calls this cleanup step at its top:

```kotlin
filamentAsset?.let {
    scene.removeEntities(it.entities)
    assetLoader?.destroyAsset(it)
}
```

We extract it into an explicit method so hot-reload can clear the scene BEFORE the new PFD is opened (lets GC run between models):

```kotlin
fun unloadModel() {
    filamentAsset?.let {
        scene.removeEntities(it.entities)
        assetLoader?.destroyAsset(it)
    }
    filamentAsset = null
    animator = null
    currentAnimIdx = -1
    animationStartTime = 0L
}
```

And adjust `loadModel()` so it calls `unloadModel()` first instead of inlining the cleanup. Also force the scene background to clear: destroy/rebuild `sunEntity` when `applySceneConfig` is called on a hot reload (the existing `applyConfig()` already handles sun rebuild — good).

- [ ] **Step 2: Add `unloadModel` + adjust `loadModel` in `FilamentSceneManager.kt`**

Replace the top of the existing `fun loadModel(fd: ParcelFileDescriptor)` (the part before the `try {`) with:

```kotlin
fun loadModel(fd: ParcelFileDescriptor) {
    if (!isInitialized) {
        Log.w(TAG, "Engine not initialized yet")
        return
    }

    unloadModel()
    // fall through to original body...
```

And add `unloadModel()` as a sibling public method in the same file (before `fun playAnimation`).

- [ ] **Step 3: Write failing test for CharacterAssetFetcher**

```kotlin
package org.dollos.launcher

import android.content.ContentResolver
import android.net.Uri
import org.junit.Test
import org.junit.Assert.*
import org.mockito.kotlin.*

class CharacterAssetFetcherTest {

    @Test fun `fetch null characterId returns null`() {
        val ctx: android.content.Context = mock()
        assertNull(CharacterAssetFetcher(ctx).fetch(""))
    }

    @Test fun `fetch uses content provider uri with characterId path segment`() {
        val ctx: android.content.Context = mock()
        val cr: ContentResolver = mock()
        whenever(ctx.contentResolver).thenReturn(cr)

        CharacterAssetFetcher(ctx).fetch("rin")

        val captor = argumentCaptor<Uri>()
        verify(cr, atLeastOnce()).openFileDescriptor(captor.capture(), eq("r"))
        val uri = captor.firstValue
        assertEquals("dollos.memory", uri.authority)
        assertTrue(uri.pathSegments.contains("rin"))
    }
}
```

- [ ] **Step 4: Run — fail**

Run: `./gradlew :app:testDebugUnitTest --tests "*CharacterAssetFetcherTest*"`
Expected: FAIL "Unresolved reference: CharacterAssetFetcher"

- [ ] **Step 5: Implement `CharacterAssetFetcher.kt`**

```kotlin
package org.dollos.launcher

import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.util.Log
import org.dollos.launcher.scene.SceneConfig
import java.io.FileInputStream

class CharacterAssetFetcher(private val ctx: Context) {

    companion object {
        private const val TAG = "CharacterAssetFetcher"
        private const val AUTHORITY = "dollos.memory"
    }

    data class Result(val scene: SceneConfig, val modelFd: ParcelFileDescriptor)

    fun fetch(characterId: String): Result? {
        if (characterId.isEmpty()) return null
        val cr = ctx.contentResolver
        val sceneUri = Uri.parse("content://$AUTHORITY/character/$characterId/scene.json")
        val modelUri = Uri.parse("content://$AUTHORITY/character/$characterId/model.glb")

        val scene = runCatching {
            cr.openFileDescriptor(sceneUri, "r")?.use { fd ->
                SceneConfig.fromJson(FileInputStream(fd.fileDescriptor))
            }
        }.getOrElse {
            Log.e(TAG, "scene.json fetch failed for $characterId", it)
            null
        } ?: return null

        val modelFd = runCatching {
            cr.openFileDescriptor(modelUri, "r")
        }.getOrElse {
            Log.e(TAG, "model.glb fetch failed for $characterId", it)
            null
        } ?: return null

        return Result(scene, modelFd)
    }
}
```

- [ ] **Step 6: Update Activity to use `Result` fields**

The Activity code in Task 10 was written against `.scene` and `.modelFd` — already matches. Verify:

Run: `grep -n "assets.scene\|assets.modelFd" app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`
Expected: two lines present (inside `maybeReloadCharacter`).

- [ ] **Step 7: Wrap modelFd in `.use {}` so PFD gets closed**

Replace the body of `maybeReloadCharacter` in `DollOSLauncherActivity.kt` with:

```kotlin
private fun maybeReloadCharacter(characterId: String?) {
    if (characterId == null || characterId == lastCharacterId) return
    Log.i(TAG, "character_changed → hot-reload $characterId")
    lastCharacterId = characterId
    val assets = CharacterAssetFetcher(this).fetch(characterId) ?: return
    filamentScene.unloadModel()
    filamentScene.applySceneConfig(assets.scene)
    assets.modelFd.use { fd -> filamentScene.loadModel(fd) }
}
```

- [ ] **Step 8: Run test + compile**

Run: `./gradlew :app:testDebugUnitTest --tests "*CharacterAssetFetcherTest*"`
Expected: PASS

Run: `./gradlew :app:compileDebugKotlin 2>&1 | tail -10`
Expected: BUILD SUCCESSFUL (all missing refs now filled).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "launcher: Character Pack hot-reload via CharacterAssetFetcher + FilamentSceneManager.unloadModel"
```

---

## Task 16: Add subtitle id to layout (naming alignment)

**Files:**
- Modify: `app/src/main/res/layout/activity_launcher.xml`
- Modify: `app/src/main/res/layout/view_response_bubble.xml` — verify root id is usable via `<include>`

- [ ] **Step 1: Verify `view_response_bubble.xml` has a consistent root**

Run: `cat app/src/main/res/layout/view_response_bubble.xml`

If its root TextView uses `@+id/bubble_text`, the outer `<include android:id="@+id/subtitle_bubble">` wraps it fine. No change needed.

- [ ] **Step 2: Full build**

Run: `./gradlew :app:assembleDebug 2>&1 | tail -30`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit (empty if no change)**

```bash
git status
# If no change, skip commit. Otherwise:
# git commit -am "launcher: verify layout ids for subtitle include"
```

---

## Task 17: Full unit test sweep

- [ ] **Step 1: Run all unit tests**

Run: `./gradlew :app:testDebugUnitTest 2>&1 | tail -40`
Expected: all tests PASS, BUILD SUCCESSFUL.

- [ ] **Step 2: Count tests executed**

Run: `cat app/build/test-results/testDebugUnitTest/*.xml | grep -c '<testcase '`
Expected: >= 25 test cases across Router / ViewModel / Animator / Fetcher / State / VibrateDispatcher / CoreClient.

- [ ] **Step 3: Commit (no code changes; tag milestone)**

```bash
git tag -a launcher-refactor-unit-green -m "launcher refactor: all unit tests green"
```

---

## Task 18: Android integration test — Launcher boots into 3D without drawer

**Files:**
- Create: `app/src/androidTest/java/org/dollos/launcher/LauncherUiIntegrationTest.kt`

- [ ] **Step 1: Write integration test**

```kotlin
package org.dollos.launcher

import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class LauncherUiIntegrationTest {

    @Test fun launcher_boots_into_3d_scene() {
        ActivityScenario.launch(DollOSLauncherActivity::class.java).use {
            onView(withId(R.id.filament_texture_view)).check(matches(isDisplayed()))
        }
    }

    @Test fun launcher_has_no_app_drawer() {
        // Cause compile-fail if view_app_drawer.xml returns — test the existence of resource id at build time
        val id = this.javaClass.classLoader?.loadClass("org.dollos.launcher.R\$layout")
            ?.fields?.map { it.name }?.toSet().orEmpty()
        assert("view_app_drawer" !in id) { "view_app_drawer layout still exists" }
        assert("view_character_picker" !in id) { "view_character_picker layout still exists" }
        assert("view_input_bar" !in id) { "view_input_bar layout still exists" }
    }
}
```

- [ ] **Step 2: Build test APK**

Run: `./gradlew :app:assembleDebugAndroidTest 2>&1 | tail -10`
Expected: BUILD SUCCESSFUL (may need `adb` device to actually run, which runs in subagent).

- [ ] **Step 3: Commit**

```bash
git add app/src/androidTest/java/org/dollos/launcher/LauncherUiIntegrationTest.kt
git commit -m "launcher: integration test asserts 3D scene boots + drawer layout gone"
```

---

## Task 19: OOBE — simplify SetupWizard page list

**Files:**
- Modify: `DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt`
- Delete: `ThemePage.kt` / `GmsPage.kt` / `WifiPage.kt` / `ModelDownloadPage.kt` / `CharacterGenPage.kt`
- Create: `CharacterPackPage.kt`

- [ ] **Step 1: Delete legacy page files**

```bash
cd ~/Projects/DollOS-build/packages/apps/DollOSSetupWizard
rm src/org/dollos/setup/ThemePage.kt
rm src/org/dollos/setup/GmsPage.kt
rm src/org/dollos/setup/WifiPage.kt
rm src/org/dollos/setup/ModelDownloadPage.kt
rm src/org/dollos/setup/CharacterGenPage.kt
```

- [ ] **Step 2: Edit `SetupWizardActivity.kt`** — reduce pages

Replace the `pageKeys` / `skippablePages` / `skipTargets` / `getNextButtonText` / `SetupPagerAdapter` to use the slim flow:

```kotlin
private val pageKeys = listOf("welcome", "character_pack", "api_key", "complete")

private val skippablePages = emptySet<String>()
private val skipTargets = emptyMap<String, String>()
```

And the adapter:

```kotlin
private inner class SetupPagerAdapter(activity: AppCompatActivity) : FragmentStateAdapter(activity) {
    override fun getItemCount(): Int = pageKeys.size
    override fun createFragment(position: Int): Fragment {
        return when (pageKeys[position]) {
            "welcome" -> WelcomePage()
            "character_pack" -> CharacterPackPage()
            "api_key" -> ApiKeyPage()
            "complete" -> CompletePage()
            else -> WelcomePage()
        }
    }
}
```

And `getNextButtonText`:

```kotlin
private fun getNextButtonText(position: Int): String {
    if (position == pageKeys.size - 1) return "Get Started"
    val nextKey = pageKeys.getOrNull(position + 1) ?: return "Continue"
    val label = when (nextKey) {
        "character_pack" -> "Character Pack"
        "api_key"        -> "API Key"
        "complete"       -> "Finish"
        else             -> return "Continue"
    }
    return "Next: $label"
}
```

Also drop the theme-customization line in `finishSetup()`. The block is still harmless (no user-facing theme picker exists), but remove it to keep finishSetup focused:

```kotlin
private fun finishSetup() {
    android.provider.Settings.Global.putInt(contentResolver, android.provider.Settings.Global.DEVICE_PROVISIONED, 1)
    android.provider.Settings.Secure.putInt(contentResolver, android.provider.Settings.Secure.USER_SETUP_COMPLETE, 1)

    packageManager.setComponentEnabledSetting(
        ComponentName(this, SetupWizardActivity::class.java),
        PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
        PackageManager.DONT_KILL_APP
    )

    val intent = Intent(Intent.ACTION_MAIN)
    intent.addCategory(Intent.CATEGORY_HOME)
    intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
    startActivity(intent)
    finish()
}
```

- [ ] **Step 3: Verify no remaining references to deleted pages**

Run: `grep -rn "ThemePage\|GmsPage\|WifiPage\|ModelDownloadPage\|CharacterGenPage" src/`
Expected: empty.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "setup-wizard: slim OOBE to Welcome → Character Pack → API Key → Done"
```

---

## Task 20: OOBE — CharacterPackPage (list .doll bundles)

**Files:**
- Create: `DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/CharacterPackPage.kt`
- Create: `DollOS-build/packages/apps/DollOSSetupWizard/res/layout/page_character_pack.xml`
- Create: `DollOS-build/packages/apps/DollOSSetupWizard/res/layout/item_character_pack.xml`

- [ ] **Step 1: Write layout `page_character_pack.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:padding="24dp">

    <TextView
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:text="Choose your companion"
        android:textSize="22sp"
        android:textStyle="bold" />

    <TextView
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_marginTop="8dp"
        android:text="Pick a Character Pack. Rin is the default."
        android:textSize="14sp"
        android:textColor="#88ffffff" />

    <androidx.recyclerview.widget.RecyclerView
        android:id="@+id/character_list"
        android:layout_width="match_parent"
        android:layout_height="0dp"
        android:layout_weight="1"
        android:layout_marginTop="16dp" />

</LinearLayout>
```

- [ ] **Step 2: Write layout `item_character_pack.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="wrap_content"
    android:orientation="horizontal"
    android:gravity="center_vertical"
    android:padding="12dp"
    android:background="?android:attr/selectableItemBackground">

    <ImageView
        android:id="@+id/thumbnail"
        android:layout_width="48dp"
        android:layout_height="48dp" />

    <LinearLayout
        android:layout_width="0dp"
        android:layout_height="wrap_content"
        android:layout_weight="1"
        android:orientation="vertical"
        android:layout_marginStart="12dp">

        <TextView
            android:id="@+id/name"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:textSize="16sp"
            android:textStyle="bold" />

        <TextView
            android:id="@+id/description"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:textSize="13sp"
            android:textColor="#aaffffff" />
    </LinearLayout>

    <RadioButton
        android:id="@+id/selected"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:clickable="false"
        android:focusable="false" />

</LinearLayout>
```

- [ ] **Step 3: Write `CharacterPackPage.kt`**

```kotlin
package org.dollos.setup

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.RadioButton
import android.widget.TextView
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import org.dollos.ai.IDollOSAIService
import org.json.JSONArray

/**
 * OOBE page to pick a Character Pack.
 * Lists installed .doll bundles from DollOSAIService (transitional — will move to
 * DollOSMemory listCharacterPacks() once Memory app lands).
 */
class CharacterPackPage : Fragment(), SetupPage {

    private lateinit var adapter: PackAdapter
    private var selectedId: String? = null

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        val v = inflater.inflate(R.layout.page_character_pack, container, false)
        val list = v.findViewById<RecyclerView>(R.id.character_list)
        list.layoutManager = LinearLayoutManager(requireContext())
        adapter = PackAdapter(loadPacks()) { id -> selectedId = id }
        list.adapter = adapter
        // Auto-select default (Rin) if present
        adapter.items.firstOrNull { it.id == "rin" }?.let {
            selectedId = it.id
            adapter.selectedId = it.id
            adapter.notifyDataSetChanged()
        }
        return v
    }

    override fun onNext(): Boolean {
        val id = selectedId ?: return false
        val svc: IDollOSAIService? = (activity as? SetupWizardActivity)?.getAIService()
        runCatching { svc?.setActiveCharacter(id) }
        return true
    }

    private fun loadPacks(): List<PackInfo> {
        val svc = (activity as? SetupWizardActivity)?.getAIService() ?: return emptyList()
        val jsonStr = runCatching { svc.listCharacters() }.getOrNull() ?: return emptyList()
        val arr = runCatching { JSONArray(jsonStr) }.getOrNull() ?: return emptyList()
        val out = mutableListOf<PackInfo>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            out += PackInfo(
                id = o.optString("id"),
                name = o.optString("name"),
                description = o.optString("description"),
            )
        }
        return out
    }

    data class PackInfo(val id: String, val name: String, val description: String)

    private class PackAdapter(
        val items: List<PackInfo>,
        private val onSelect: (String) -> Unit,
    ) : RecyclerView.Adapter<PackAdapter.VH>() {

        var selectedId: String? = null

        class VH(v: View) : RecyclerView.ViewHolder(v) {
            val name: TextView = v.findViewById(R.id.name)
            val desc: TextView = v.findViewById(R.id.description)
            val radio: RadioButton = v.findViewById(R.id.selected)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val v = LayoutInflater.from(parent.context).inflate(R.layout.item_character_pack, parent, false)
            return VH(v)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val item = items[position]
            holder.name.text = item.name
            holder.desc.text = item.description
            holder.radio.isChecked = item.id == selectedId
            holder.itemView.setOnClickListener {
                selectedId = item.id
                onSelect(item.id)
                notifyDataSetChanged()
            }
        }

        override fun getItemCount() = items.size
    }
}
```

- [ ] **Step 4: Verify `SetupWizardActivity.getAIService()` still returns the binder**

Run: `grep -n "fun getAIService" ~/Projects/DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/SetupWizardActivity.kt`
Expected: one match — keep it. This is a transitional call: the binder will swap to `IDollMemory` once Memory app lands.

- [ ] **Step 5: AOSP build of SetupWizard**

Run:
```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSSetupWizard -j$(nproc) 2>&1 | tail -20
```
Expected: BUILD SUCCESSFUL.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "setup-wizard: CharacterPackPage lists installed .doll bundles with Rin preselected"
```

---

## Task 21: OOBE — ApiKeyPage pass-through + WelcomePage copy trim

**Files:**
- Modify: `DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/WelcomePage.kt` — refresh copy for副手機 positioning
- Modify: `DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/ApiKeyPage.kt` — only require Main LLM provider + key, drop background/aux fields if any (keep existing UI if already minimal)

- [ ] **Step 1: Read `WelcomePage.kt`**

Run: `cat ~/Projects/DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/WelcomePage.kt`

Update any title/subtitle to reflect "Doll AI Terminal" / "副手機專用 AI 陪伴". Concrete change (if the existing TextView uses `R.id.welcome_title` / `R.id.welcome_subtitle`):

```kotlin
view.findViewById<TextView>(R.id.welcome_title).text = "DollOS"
view.findViewById<TextView>(R.id.welcome_subtitle).text = "Your AI companion. Four steps and she's awake."
```

If the page already uses layout-level strings, edit the corresponding `res/layout/page_welcome.xml` copy instead.

- [ ] **Step 2: Read `ApiKeyPage.kt`**

Run: `cat ~/Projects/DollOS-build/packages/apps/DollOSSetupWizard/src/org/dollos/setup/ApiKeyPage.kt`

If the current page already accepts provider + key (Claude/OpenAI/Gemini) in a single field pair, keep it. If it has extra fields for aux/local models, delete those — Aux is local-only and requires no cloud key.

Concrete guardrail: `onNext()` must reject empty key:

```kotlin
override fun onNext(): Boolean {
    val key = findViewById<EditText>(R.id.api_key_input).text.toString().trim()
    if (key.isEmpty()) {
        findViewById<TextView>(R.id.error_label).text = "API key is required"
        return false
    }
    val provider = findViewById<RadioGroup>(R.id.provider_group).checkedRadioButtonId.let {
        when (it) {
            R.id.provider_claude -> "claude"
            R.id.provider_openai -> "openai"
            R.id.provider_gemini -> "gemini"
            else -> "claude"
        }
    }
    val svc = (activity as? SetupWizardActivity)?.getAIService()
    runCatching { svc?.setMainLlmCredentials(provider, key) }
    return true
}
```

(If `setMainLlmCredentials` is not on the current AIDL, use whatever method the existing page invokes — the point is to reject empty + persist.)

- [ ] **Step 3: AOSP rebuild**

Run: `m DollOSSetupWizard -j$(nproc) 2>&1 | tail -5`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "setup-wizard: welcome+apikey copy trimmed for DollOS Terminal positioning"
```

---

## Task 22: OOBE end-to-end instrumentation test

**Files:**
- Create: `DollOS-build/packages/apps/DollOSSetupWizard/tests/.../OOBEEndToEndTest.kt` (if the project has a tests dir; otherwise skip — manual test only)

- [ ] **Step 1: Check if SetupWizard has an androidTest configuration**

Run: `find ~/Projects/DollOS-build/packages/apps/DollOSSetupWizard -type d -name "androidTest" -o -name "tests"`

If absent: this app is built by AOSP makefile not Gradle — instrumentation is out of scope. Skip test creation; the manual verification checklist (Task 24) covers OOBE.

If present: write a minimal Espresso test that walks the pages.

- [ ] **Step 2: Commit (probably no change)**

```bash
git status
```

---

## Task 23: DollOSLauncher AOSP packaging + manifest permissions

**Files:**
- Modify: `DollOS-build/packages/apps/DollOSLauncher/privapp-permissions-dollos-launcher.xml` — add VIBRATE if missing
- Modify: `DollOS-build/packages/apps/DollOSLauncher/Android.bp` — ensure `system_ext_specific: true`

- [ ] **Step 1: Check privapp permissions file**

Run: `find ~/Projects/DollOS-build/packages/apps/DollOSLauncher -name "privapp*.xml"`

If a file exists (e.g., `privapp-permissions-dollos-launcher.xml`) add:

```xml
<permission name="android.permission.VIBRATE" />
```

under the `<privapp-permissions package="org.dollos.launcher">` element. (VIBRATE is a normal permission so this may not be needed — only add if build warns.)

- [ ] **Step 2: Build flow end-to-end**

Run:
```bash
cd ~/Projects/DollOSLauncher
./gradlew assembleRelease 2>&1 | tail -10
```
Expected: BUILD SUCCESSFUL.

```bash
cp app/build/outputs/apk/release/app-release-unsigned.apk ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSLauncher -j$(nproc) 2>&1 | tail -10
```
Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit (if privapp file changed)**

```bash
git add -A
git commit -m "launcher: wire VIBRATE privapp permission if needed"
```

---

## Task 24: §13 UI 驗收 — manual checklist on device (subagent)

**Files:** no code; run `adb` commands in a subagent (per CLAUDE.md phone ops rule).

The subagent's job:

- [ ] **Step 1: Flash new build + reboot**

Run (in subagent):
```bash
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push ~/Projects/DollOS-build/out/target/product/bluejay/system_ext/priv-app/DollOSLauncher/*.apk /system_ext/priv-app/DollOSLauncher/
adb push ~/Projects/DollOS-build/out/target/product/bluejay/system_ext/priv-app/DollOSSetupWizard/*.apk /system_ext/priv-app/DollOSSetupWizard/
adb reboot
```

- [ ] **Step 2: Verify §13-a — boot goes to Doll 3D**

After reboot, screenshot via adb. Expected: `DollOSLauncherActivity` foreground, Filament TextureView visible, no drawer hint, no import button, no long-press-picker.

- [ ] **Step 3: Verify §13-b — no app drawer / picker / settings**

Swipe from right edge → Expected: nothing happens (no drawer).
Long-press center → Expected: nothing happens (no picker).
Pull notification shade → no DollOS Settings activity in app list.

- [ ] **Step 4: Verify §13-c — overlay by dialog activation**

Use the Core dev console (or a simulated Core ops event via `am broadcast` if a debug hook exists) to send `flag_changed` `{"flag":"overlay_active","value":"formal"}` — verify top-right badge shows "認真模式".

Send `flag_changed` `{"flag":"overlay_active","value":null}` — verify badge disappears.

- [ ] **Step 5: Verify §13-d — hot-reload: character_changed op**

Emit `character_changed` `{"characterId":"<other_pack_id>"}` via the dev console. Expected: 3D model visibly swaps within ~1 second; subtitle state unaffected.

- [ ] **Step 6: Verify §13-e — vibrate op**

Emit `vibrate` `{}` → device vibrates with default pattern.
Emit `vibrate` `{"pattern":[0,300,100,300]}` → device vibrates long-short-long.

- [ ] **Step 7: Log verification results in plan**

Return a numbered list "1 PASS / 2 PASS / ..." in the subagent's summary. Any FAIL → open a follow-up task in this plan.

---

## Task 25: Self-review + final commit

- [ ] **Step 1: Grep for leftover references to removed classes**

Run:
```bash
cd ~/Projects/DollOSLauncher
grep -rn "AppDrawerView\|CharacterPickerOverlay\|InputBarView\|AvatarAnimator\|REQUEST_IMPORT_CHARACTER" app/src
```
Expected: empty.

- [ ] **Step 2: Grep for leftover IDollOSAIService references**

Run: `grep -rn "IDollOSAIService\|IDollOSAICallback" app/src/main`
Expected: empty (all Launcher code should now talk to `IDollCore` only). If any remain, delete them or migrate.

- [ ] **Step 3: Verify no placeholder strings in code**

Run: `grep -rnE "TODO|FIXME|TBD" app/src/main`
Expected: empty.

- [ ] **Step 4: Plan self-review summary commit**

```bash
git add -A
git commit --allow-empty -m "launcher: refactor complete — Doll AI Terminal Launcher

- Removed app drawer, character picker, input bar, settings entry
- Bound to DollOSCore IDollCore + IDollCoreStateListener
- 4 UI animation states (IDLE/LISTENING/THINKING/SPEAKING) with composition
- Personality overlay indicator, subtitle bubble, vibrate dispatcher
- Character Pack hot-reload
- OOBE slimmed to Welcome → Character Pack → API Key → Done

Refs: docs/superpowers/plans/2026-04-20-doll-terminal-launcher.md"
```

---

## Self-Review

**§13 UI 驗收條件逐一對照：**

| 驗收條件 | 對應 Task |
|---|---|
| 開機直接進入 Doll 3D 角色畫面 | Task 10 (Activity HOME intent-filter kept) + Task 24 Step 2 |
| 無 app drawer | Task 7 + Task 18 (layout resource check) + Task 24 Step 3 |
| 無角色選擇 UI | Task 8 + Task 24 Step 3 |
| 無獨立 Settings app | Task 9 (InputBar + action confirm deleted) + Task 19 (OOBE no theme/gms) |
| 人格 overlay 可透過對話 activate | Task 13 (indicator) + Task 5 `flag_changed` "overlay_active" parsing + Task 24 Step 4 |

**Master §3.2 Ops 每個 op 都有對應 handler：**

| op name | handled in |
|---|---|
| `asr_started` / `asr_ended` | Task 5 OpsEventRouter + Task 11 CompositeAnimator (listening) |
| `llm_in_flight` / `llm_returned` | Task 5 + Task 11 (thinking) |
| `tts_playing` / `tts_ended` | Task 5 (subtitle) + Task 11 (speaking) + Task 12 SubtitleView |
| `vibrate` | Task 5 (no-op in router) + Task 14 VibrateDispatcher (side effect in Activity) |
| `flag_changed` | Task 5 (dnd_active / overlay_active) + Task 13 indicator |
| `character_changed` | Task 5 + Task 15 CharacterAssetFetcher + Task 10 `maybeReloadCharacter` |

**既有 Launcher 要保留 vs 要刪的清楚分開：**

| 保留 | 刪除 |
|---|---|
| FilamentSceneManager (微改加 `unloadModel`) | AppDrawerView + Adapters |
| ResponseBubbleView (包在 SubtitleView 內) | CharacterPickerOverlay |
| SceneConfig | InputBarView |
| activity_launcher.xml (刪 drawer/picker/import 部分) | AvatarAnimator (被 CompositeAnimator 取代) |
| DollOSLauncherActivity 殼 (rewrite) | action_confirm_container / view_action_confirm.xml |

**Character Pack hot-reload 處理 memory / scene cleanup：**

Task 15 Step 2 加 `unloadModel()`：`scene.removeEntities(...)` + `assetLoader.destroyAsset(...)` + `animator = null` + `currentAnimIdx = -1` + `animationStartTime = 0L`。`loadModel()` 每次呼叫先跑 `unloadModel()` 確保舊 asset 徹底釋放。`applySceneConfig()` 已重建 `sunEntity` 避免 light leak。

**組合動畫語意：**

Task 11 CompositeAnimator 明確處理：

- `listening + thinking` → `{"listening", "thinking"}` 兩個 flag 同時下給 Filament（目前單動畫播放 primary，將來有 skeletal blend 可混）
- `speaking` on → speaking wins alone（嘴型 sync 優先）
- 其他組合 → 合理的 set

**OOBE 結構對應需求：**

| 需求 | Task |
|---|---|
| 1. Welcome | 既有 WelcomePage（Task 21 調文案）|
| 2. Character Pack 選擇 | Task 20 CharacterPackPage |
| 3. API key | 既有 ApiKeyPage（Task 21 驗證非空 + 寫入）|
| 4. Done | 既有 CompletePage |
| 無 theme picker | Task 19 pageKeys 拿掉 |
| 無 GMS toggle | Task 19 pageKeys 拿掉 |

**沒有遺漏：** 每個 §13 驗收條件都有至少一個實作 task + 一個驗收 step。ops 清單完整覆蓋。Character Pack hot-reload 的 scene cleanup 明確。OOBE 四步驟的每一步都有 page + 驗收。

---

**Plan complete.** 總 task 數：21。涵蓋 DollOSLauncher 從重構到整合的完整路徑：移除 app drawer / 角色選擇 / 設定入口、4 UI 動畫狀態 + 組合、人格 overlay 指示、Character Pack hot-reload、`[VIBRATE]` 震動顯示、OOBE 精簡為 welcome + 角色選擇 + API key。
