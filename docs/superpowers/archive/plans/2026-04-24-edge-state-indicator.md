# Edge State Indicator + In-App Subtitle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 主人在其他 app 中時，螢幕邊緣以 vignette（向內漸暗的 radial gradient 邊緣）顯示 Doll 的 ops state（LISTENING / THINKING / SPEAKING），不同狀態用不同顏色 + 現代化動畫；TTS 時字幕氣泡從螢幕邊緣滑入短暫顯示。Launcher 在前景時不出現（Haru 直接表現）；鎖屏不出現（Plan 3 處理）；Doll 視覺擷取時隱藏（避免汙染截圖）。

**Architecture:** DollOSLauncher 內新增 `DollOSEdgeOverlayService`（foreground service，常駐），透過 `WindowManager.addView()` 建立兩個 system overlay window：
1. **EdgeOverlayView** — 全螢幕透明 view，Canvas drawRoundRect 配合 RadialGradient / LinearGradient 從邊緣向內漸暗，依目前狀態切色 + 平滑漸入漸出動畫
2. **SubtitleBubbleView** — 從螢幕底部邊緣滑入 / 滑出的 floating subtitle，TTS 啟動時顯示文字、TTS 結束 / 數秒後收回

兩個 view 都 bind 到既有的 `IDollOSAIService` 並訂閱 `onOpsEvent` / `onSubtitle` callback（Plan 1 已 wired）。Service 透過 `ActivityManager` 監聽前景 app 切換以決定顯示時機；透過新增的 `onVisionCaptureStateChanged` callback 在 Doll 截圖期間隱藏自己。

**Tech Stack:** Kotlin, Android Gradle, `WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY`, AIDL（既有 IDollOSAIService + IDollOSAICallback），`ValueAnimator` + `Choreographer` for vignette breathing animation, `RadialGradient` / `LinearGradient` (Canvas), JUnit 4。

**Spec reference:** `/home/progcat/Projects/DollOS/docs/superpowers/specs/2026-04-24-avatar-redefinition-design.md` §2.2 字幕、§2.3 邊緣狀態指示

**Out of scope（其他 plan）：**
- Lock screen Live2D（Plan 3）
- DollOSCore rebind（既有 Terminal refactor plan）
- Doll AI 視覺擷取本身的實作（已有 AccessibilityService + VirtualDisplay）

---

## 視覺規格

### 邊緣 vignette

- 全螢幕 transparent overlay，Canvas drawing
- 邊緣 ~80dp 區域用 RadialGradient（中心透明、邊緣彩色）疊加
- 動畫：呼吸（1.5s 循環，opacity 0.4→0.8→0.4）
- 圓角：邊緣對齊螢幕物理圓角（~24dp）

### 狀態 → 顏色

| 狀態 | 顏色 | RGBA hex | 描述 |
|---|---|---|---|
| LISTENING | 青藍 | `#00D4FF` | 冷感、警覺 |
| THINKING | 紫色 | `#A855F7` | 思緒、運算 |
| SPEAKING | 暖橙 | `#FF8A4C` | 主動、發聲 |

組合（如 LISTENING + THINKING）：兩色 RadialGradient stop 平均混合或交錯漸層。

### 字幕氣泡

- 位置：底部，距離 nav bar 32dp
- 寬：螢幕寬度 - 64dp（左右 padding 32dp）
- 樣式：圓角 24dp，半透明黑底（80% alpha），白字 18sp，padding 16dp
- 動畫：`translationY` 從 +200 滑入到 0（300ms ease-out），收回時反向（300ms ease-in）
- 字幕收回觸發：`onSubtitle(null)` callback OR 5 秒 timeout（取較早者）

---

## 檔案結構

### 新增

**Service 與 Overlay views**
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/DollOSEdgeOverlayService.kt` — foreground service，binds AIService、管理 overlay 顯示
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/EdgeOverlayView.kt` — 全螢幕 vignette view
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/SubtitleBubbleView.kt` — 字幕氣泡 view
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/StateColorPalette.kt` — 狀態 → 顏色 + 混合邏輯
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/ForegroundAppMonitor.kt` — 偵測 launcher 是否在前景
- `DollOSLauncher/app/src/main/java/org/dollos/launcher/edge/EdgeOverlayState.kt` — Service 內部 state holder（active flags、subtitle text、visibility）

**測試**
- `DollOSLauncher/app/src/test/java/org/dollos/launcher/edge/StateColorPaletteTest.kt`
- `DollOSLauncher/app/src/test/java/org/dollos/launcher/edge/EdgeOverlayStateTest.kt`

**Resources**
- `DollOSLauncher/app/src/main/res/drawable/notif_icon_doll.xml` — foreground service 通知用 icon（簡單 vector）

### 修改

- `DollOSLauncher/app/src/main/AndroidManifest.xml` — 加 `SYSTEM_ALERT_WINDOW`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_SPECIAL_USE`、`PACKAGE_USAGE_STATS` permissions；註冊 `DollOSEdgeOverlayService`
- AIService AIDL `IDollOSAICallback.aidl` — 新增 `onVisionCaptureStateChanged(boolean active)`
- AIService 端：在 VirtualDisplay capture 開始 / 結束時呼叫 callback
- Launcher 端 AIDL 同步

### 不改

- `DollOSLauncherActivity.kt`（Plan 1 完成的，本 plan 不動 launcher activity）

---

## Task 0: 權限與 manifest 註冊

**Files:**
- Modify: `app/src/main/AndroidManifest.xml`

- [ ] **Step 1: 加 permissions + service 註冊**

Edit `app/src/main/AndroidManifest.xml`，在 `<manifest>` 內 `<application>` 上方加：

```xml
<uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE" />
<uses-permission android:name="android.permission.PACKAGE_USAGE_STATS" tools:ignore="ProtectedPermissions" />
```

加 `xmlns:tools="http://schemas.android.com/tools"` 到 `<manifest>` 標籤如果不存在。

在 `<application>` 內加（與 Activity 平級）：

```xml
<service
    android:name=".edge.DollOSEdgeOverlayService"
    android:exported="false"
    android:foregroundServiceType="specialUse">
    <property
        android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
        android:value="ai_companion_state_indicator" />
</service>
```

- [ ] **Step 2: Build verify**

```bash
cd /home/progcat/Projects/DollOSLauncher-avatar-live2d && ./gradlew assembleDebug 2>&1 | tail -5
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit**

```bash
git add app/src/main/AndroidManifest.xml
git commit -m "build: register DollOSEdgeOverlayService + overlay/usage perms"
```

---

## Task 1: StateColorPalette（純邏輯）

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/StateColorPalette.kt`
- Create: `app/src/test/java/org/dollos/launcher/edge/StateColorPaletteTest.kt`

- [ ] **Step 1: 寫失敗測試**

Create `app/src/test/java/org/dollos/launcher/edge/StateColorPaletteTest.kt`:

```kotlin
package org.dollos.launcher.edge

import org.dollos.launcher.live2d.OpsFlag
import org.junit.Assert.*
import org.junit.Test

class StateColorPaletteTest {

    @Test
    fun `idle returns transparent (no edge)`() {
        val color = StateColorPalette.composite(setOf(OpsFlag.IDLE))
        assertEquals(0, color and 0xFF000000.toInt())  // alpha=0
    }

    @Test
    fun `listening returns cyan`() {
        val color = StateColorPalette.composite(setOf(OpsFlag.LISTENING))
        assertEquals(0xFF, (color shr 24) and 0xFF)  // full alpha
        assertEquals(0x00, (color shr 16) and 0xFF)  // R
        assertEquals(0xD4, (color shr 8) and 0xFF)   // G
        assertEquals(0xFF, color and 0xFF)           // B
    }

    @Test
    fun `thinking returns purple`() {
        val color = StateColorPalette.composite(setOf(OpsFlag.THINKING))
        assertEquals(0xA8, (color shr 16) and 0xFF)
        assertEquals(0x55, (color shr 8) and 0xFF)
        assertEquals(0xF7, color and 0xFF)
    }

    @Test
    fun `speaking returns warm orange`() {
        val color = StateColorPalette.composite(setOf(OpsFlag.SPEAKING))
        assertEquals(0xFF, (color shr 16) and 0xFF)
        assertEquals(0x8A, (color shr 8) and 0xFF)
        assertEquals(0x4C, color and 0xFF)
    }

    @Test
    fun `listening + thinking averages colors`() {
        val color = StateColorPalette.composite(setOf(OpsFlag.LISTENING, OpsFlag.THINKING))
        // R: avg(0x00, 0xA8) = 0x54
        // G: avg(0xD4, 0x55) = 0x94 (rounded down)
        // B: avg(0xFF, 0xF7) = 0xFB
        assertEquals(0x54, (color shr 16) and 0xFF)
        assertEquals(0x94, (color shr 8) and 0xFF)
        assertEquals(0xFB, color and 0xFF)
    }

    @Test
    fun `idle dropped when other flags present`() {
        val withIdle = StateColorPalette.composite(setOf(OpsFlag.IDLE, OpsFlag.LISTENING))
        val withoutIdle = StateColorPalette.composite(setOf(OpsFlag.LISTENING))
        assertEquals(withoutIdle, withIdle)
    }
}
```

- [ ] **Step 2: 確認測試失敗**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.edge.StateColorPaletteTest" 2>&1 | tail -5
```

Expected: FAIL — `StateColorPalette` 不存在。

- [ ] **Step 3: 實作**

Create `app/src/main/java/org/dollos/launcher/edge/StateColorPalette.kt`:

```kotlin
package org.dollos.launcher.edge

import android.graphics.Color
import org.dollos.launcher.live2d.OpsFlag

/**
 * Maps ops state flags to edge indicator colors. Combines multiple active flags
 * by averaging RGB channels (alpha kept at 0xFF unless only IDLE).
 */
object StateColorPalette {

    private val COLOR_LISTENING = Color.argb(0xFF, 0x00, 0xD4, 0xFF)  // cyan
    private val COLOR_THINKING = Color.argb(0xFF, 0xA8, 0x55, 0xF7)  // purple
    private val COLOR_SPEAKING = Color.argb(0xFF, 0xFF, 0x8A, 0x4C)  // warm orange

    /**
     * Returns ARGB int. IDLE alone → transparent (no edge).
     * Multiple flags → averaged RGB.
     */
    fun composite(flags: Set<OpsFlag>): Int {
        val active = flags.filter { it != OpsFlag.IDLE }
        if (active.isEmpty()) return 0  // fully transparent

        var r = 0
        var g = 0
        var b = 0
        for (flag in active) {
            val c = when (flag) {
                OpsFlag.LISTENING -> COLOR_LISTENING
                OpsFlag.THINKING -> COLOR_THINKING
                OpsFlag.SPEAKING -> COLOR_SPEAKING
                else -> continue
            }
            r += (c shr 16) and 0xFF
            g += (c shr 8) and 0xFF
            b += c and 0xFF
        }
        val n = active.size
        return Color.argb(0xFF, r / n, g / n, b / n)
    }
}
```

- [ ] **Step 4: 測試通過**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.edge.StateColorPaletteTest" 2>&1 | tail -5
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/StateColorPalette.kt \
        app/src/test/java/org/dollos/launcher/edge/StateColorPaletteTest.kt
git commit -m "feat(edge): add StateColorPalette for ops state → ARGB color"
```

---

## Task 2: EdgeOverlayState（純邏輯 state holder）

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/EdgeOverlayState.kt`
- Create: `app/src/test/java/org/dollos/launcher/edge/EdgeOverlayStateTest.kt`

- [ ] **Step 1: 寫失敗測試**

Create `app/src/test/java/org/dollos/launcher/edge/EdgeOverlayStateTest.kt`:

```kotlin
package org.dollos.launcher.edge

import org.dollos.launcher.live2d.OpsFlag
import org.junit.Assert.*
import org.junit.Test

class EdgeOverlayStateTest {

    @Test
    fun `default invisible idle`() {
        val s = EdgeOverlayState()
        assertFalse(s.shouldShowEdge())
        assertFalse(s.shouldShowSubtitle())
    }

    @Test
    fun `listening with launcher in background shows edge`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setActiveFlags(setOf(OpsFlag.LISTENING))
        assertTrue(s.shouldShowEdge())
    }

    @Test
    fun `launcher in foreground hides edge regardless of state`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(true)
        s.setActiveFlags(setOf(OpsFlag.LISTENING))
        assertFalse(s.shouldShowEdge())
    }

    @Test
    fun `lock screen hides edge`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setLockScreen(true)
        s.setActiveFlags(setOf(OpsFlag.SPEAKING))
        assertFalse(s.shouldShowEdge())
    }

    @Test
    fun `vision capture in progress hides everything`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setActiveFlags(setOf(OpsFlag.SPEAKING))
        s.setSubtitle("hello")
        s.setVisionCapturing(true)
        assertFalse(s.shouldShowEdge())
        assertFalse(s.shouldShowSubtitle())
    }

    @Test
    fun `subtitle shown when set non-null and launcher background and not capture`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setSubtitle("hi")
        assertTrue(s.shouldShowSubtitle())
    }

    @Test
    fun `clearing subtitle hides bubble`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setSubtitle("hi")
        s.setSubtitle(null)
        assertFalse(s.shouldShowSubtitle())
    }

    @Test
    fun `idle alone hides edge but subtitle independent`() {
        val s = EdgeOverlayState()
        s.setLauncherForeground(false)
        s.setActiveFlags(setOf(OpsFlag.IDLE))
        s.setSubtitle("background message")
        assertFalse(s.shouldShowEdge())
        assertTrue(s.shouldShowSubtitle())
    }
}
```

- [ ] **Step 2: 確認失敗**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.edge.EdgeOverlayStateTest" 2>&1 | tail -5
```

- [ ] **Step 3: 實作**

Create `app/src/main/java/org/dollos/launcher/edge/EdgeOverlayState.kt`:

```kotlin
package org.dollos.launcher.edge

import org.dollos.launcher.live2d.OpsFlag

/**
 * Mutable state holder for the edge overlay service. Pure logic, no Android deps.
 * Visibility decisions combine ops flags + foreground state + lock state + vision capture.
 */
class EdgeOverlayState {
    private var activeFlags: Set<OpsFlag> = setOf(OpsFlag.IDLE)
    private var launcherForeground: Boolean = true   // assume launcher in front initially
    private var lockScreen: Boolean = false
    private var visionCapturing: Boolean = false
    private var subtitle: String? = null

    fun setActiveFlags(flags: Set<OpsFlag>) { activeFlags = flags }
    fun setLauncherForeground(fg: Boolean) { launcherForeground = fg }
    fun setLockScreen(locked: Boolean) { lockScreen = locked }
    fun setVisionCapturing(capturing: Boolean) { visionCapturing = capturing }
    fun setSubtitle(text: String?) { subtitle = if (text.isNullOrEmpty()) null else text }

    fun activeFlags(): Set<OpsFlag> = activeFlags
    fun subtitleText(): String? = subtitle

    /** Edge vignette visible when: not in launcher, not on lock, not capturing, has non-IDLE flag. */
    fun shouldShowEdge(): Boolean {
        if (launcherForeground) return false
        if (lockScreen) return false
        if (visionCapturing) return false
        return activeFlags.any { it != OpsFlag.IDLE }
    }

    /** Subtitle visible when: not in launcher, not on lock, not capturing, has subtitle text. */
    fun shouldShowSubtitle(): Boolean {
        if (launcherForeground) return false
        if (lockScreen) return false
        if (visionCapturing) return false
        return subtitle != null
    }
}
```

- [ ] **Step 4: 測試通過**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.edge.EdgeOverlayStateTest" 2>&1 | tail -5
```

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/EdgeOverlayState.kt \
        app/src/test/java/org/dollos/launcher/edge/EdgeOverlayStateTest.kt
git commit -m "feat(edge): add EdgeOverlayState pure-logic visibility holder"
```

---

## Task 3: ForegroundAppMonitor

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/ForegroundAppMonitor.kt`

- [ ] **Step 1: 實作**

```kotlin
package org.dollos.launcher.edge

import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Handler
import android.os.Looper

/**
 * Polls UsageStatsManager (every 1s) to determine if the Launcher is in the foreground.
 * Calls back onChange(launcherForeground: Boolean) only when state changes.
 *
 * Requires PACKAGE_USAGE_STATS permission (granted to system app via priv-app whitelist).
 */
class ForegroundAppMonitor(
    private val context: Context,
    private val launcherPackage: String,
    private val onChange: (Boolean) -> Unit
) {
    private val handler = Handler(Looper.getMainLooper())
    private val poll = object : Runnable {
        override fun run() {
            val current = isLauncherInForeground()
            if (current != lastValue) {
                lastValue = current
                onChange(current)
            }
            handler.postDelayed(this, POLL_INTERVAL_MS)
        }
    }
    private var lastValue: Boolean = true
    private var running: Boolean = false

    fun start() {
        if (running) return
        running = true
        handler.post(poll)
    }

    fun stop() {
        running = false
        handler.removeCallbacks(poll)
    }

    private fun isLauncherInForeground(): Boolean {
        val usm = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val now = System.currentTimeMillis()
        val events = usm.queryEvents(now - 5_000, now)
        val event = android.app.usage.UsageEvents.Event()
        var lastForegroundPkg: String? = null
        while (events.hasNextEvent()) {
            events.getNextEvent(event)
            if (event.eventType == android.app.usage.UsageEvents.Event.ACTIVITY_RESUMED) {
                lastForegroundPkg = event.packageName
            }
        }
        return lastForegroundPkg == launcherPackage
    }

    companion object {
        private const val POLL_INTERVAL_MS = 1_000L
    }
}
```

- [ ] **Step 2: Build**

```bash
./gradlew assembleDebug 2>&1 | tail -3
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/ForegroundAppMonitor.kt
git commit -m "feat(edge): add ForegroundAppMonitor polling UsageStatsManager"
```

---

## Task 4: EdgeOverlayView（vignette drawing + breathing animation）

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/EdgeOverlayView.kt`

- [ ] **Step 1: 實作**

```kotlin
package org.dollos.launcher.edge

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.*
import android.view.View
import android.view.animation.LinearInterpolator

/**
 * Full-screen transparent overlay drawing a 4-sided vignette (radial gradient towards each
 * edge) tinted with the current state color. Breathing animation cycles opacity 0.4 → 0.8 → 0.4.
 */
class EdgeOverlayView(context: Context) : View(context) {

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val edgePath = Path()
    private var stateColor: Int = 0  // ARGB
    private var breathPhase: Float = 0.4f  // 0..1

    private val breathAnimator = ValueAnimator.ofFloat(0.4f, 0.8f).apply {
        duration = BREATH_DURATION_MS
        repeatMode = ValueAnimator.REVERSE
        repeatCount = ValueAnimator.INFINITE
        interpolator = LinearInterpolator()
        addUpdateListener { anim ->
            breathPhase = anim.animatedValue as Float
            invalidate()
        }
    }

    init {
        setLayerType(LAYER_TYPE_HARDWARE, null)
    }

    fun setStateColor(argb: Int) {
        if (argb == stateColor) return
        stateColor = argb
        if (argb == 0) {
            // hide
            breathAnimator.cancel()
            visibility = INVISIBLE
        } else {
            visibility = VISIBLE
            if (!breathAnimator.isStarted) breathAnimator.start()
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        if (stateColor == 0) return
        val w = width.toFloat()
        val h = height.toFloat()

        val baseAlpha = ((stateColor ushr 24) and 0xFF) / 255f
        val r = (stateColor ushr 16) and 0xFF
        val g = (stateColor ushr 8) and 0xFF
        val b = stateColor and 0xFF
        val effectiveAlpha = (baseAlpha * breathPhase * 255f).toInt().coerceIn(0, 255)
        val tinted = Color.argb(effectiveAlpha, r, g, b)

        // Use a LinearGradient on each side blending into transparent center.
        // For top edge:
        paint.shader = LinearGradient(
            0f, 0f, 0f, EDGE_DP * resources.displayMetrics.density,
            tinted, Color.TRANSPARENT,
            Shader.TileMode.CLAMP
        )
        canvas.drawRect(0f, 0f, w, EDGE_DP * resources.displayMetrics.density, paint)

        // Bottom
        paint.shader = LinearGradient(
            0f, h, 0f, h - EDGE_DP * resources.displayMetrics.density,
            tinted, Color.TRANSPARENT,
            Shader.TileMode.CLAMP
        )
        canvas.drawRect(0f, h - EDGE_DP * resources.displayMetrics.density, w, h, paint)

        // Left
        paint.shader = LinearGradient(
            0f, 0f, EDGE_DP * resources.displayMetrics.density, 0f,
            tinted, Color.TRANSPARENT,
            Shader.TileMode.CLAMP
        )
        canvas.drawRect(0f, 0f, EDGE_DP * resources.displayMetrics.density, h, paint)

        // Right
        paint.shader = LinearGradient(
            w, 0f, w - EDGE_DP * resources.displayMetrics.density, 0f,
            tinted, Color.TRANSPARENT,
            Shader.TileMode.CLAMP
        )
        canvas.drawRect(w - EDGE_DP * resources.displayMetrics.density, 0f, w, h, paint)
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        breathAnimator.cancel()
    }

    companion object {
        private const val EDGE_DP = 80f
        private const val BREATH_DURATION_MS = 1500L
    }
}
```

- [ ] **Step 2: Build**

```bash
./gradlew assembleDebug 2>&1 | tail -3
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/EdgeOverlayView.kt
git commit -m "feat(edge): add EdgeOverlayView with 4-sided gradient + breathing animation"
```

---

## Task 5: SubtitleBubbleView

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/SubtitleBubbleView.kt`

- [ ] **Step 1: 實作**

```kotlin
package org.dollos.launcher.edge

import android.animation.AnimatorListenerAdapter
import android.animation.Animator
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView

/**
 * Bottom-edge subtitle bubble with slide-in / slide-out animation.
 * Container is a FrameLayout that holds a styled TextView; we animate translationY.
 */
class SubtitleBubbleView(context: Context) : FrameLayout(context) {

    private val textView: TextView = TextView(context).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        setTextColor(Color.WHITE)
        gravity = Gravity.CENTER
        setPadding(48, 32, 48, 32)
        background = GradientDrawable().apply {
            cornerRadius = 24f * resources.displayMetrics.density
            setColor(Color.argb(0xCC, 0, 0, 0))
        }
    }

    init {
        addView(textView, LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER
        ))
        translationY = HIDDEN_TRANSLATION_PX
        visibility = GONE
    }

    private val hiddenY: Float
        get() = HIDDEN_TRANSLATION_PX * resources.displayMetrics.density

    fun showWithText(text: String) {
        textView.text = text
        if (visibility == VISIBLE) return
        visibility = VISIBLE
        translationY = hiddenY
        animate()
            .translationY(0f)
            .setDuration(SHOW_DURATION_MS)
            .setListener(null)
            .start()
    }

    fun hide() {
        if (visibility != VISIBLE) return
        animate()
            .translationY(hiddenY)
            .setDuration(HIDE_DURATION_MS)
            .setListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) {
                    visibility = GONE
                }
            })
            .start()
    }

    companion object {
        private const val SHOW_DURATION_MS = 300L
        private const val HIDE_DURATION_MS = 300L
        private const val HIDDEN_TRANSLATION_PX = 200f  // dp
    }
}
```

- [ ] **Step 2: Build**

```bash
./gradlew assembleDebug 2>&1 | tail -3
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/SubtitleBubbleView.kt
git commit -m "feat(edge): add SubtitleBubbleView with slide-in/out animation"
```

---

## Task 6: AIDL onVisionCaptureStateChanged callback

**Files:**
- Modify: `DollOSAIService/aidl/org/dollos/ai/IDollOSAICallback.aidl`
- Modify: `DollOSAIService` capture impl
- Modify: `DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAICallback.aidl`

- [ ] **Step 1: 加 AIDL method**

Edit `/home/progcat/Projects/DollOSAIService-avatar-live2d/aidl/org/dollos/ai/IDollOSAICallback.aidl`，加：

```aidl
/** Doll's vision capture state. When true, overlays should hide to avoid polluting screenshots. */
void onVisionCaptureStateChanged(boolean active);
```

- [ ] **Step 2: 同步到 Launcher**

```bash
cp /home/progcat/Projects/DollOSAIService-avatar-live2d/aidl/org/dollos/ai/IDollOSAICallback.aidl \
   /home/progcat/Projects/DollOSLauncher-avatar-live2d/app/aidl/org/dollos/ai/IDollOSAICallback.aidl
```

- [ ] **Step 3: AIService 實作 — 在 vision capture start/stop 時 fire callback**

找到 AIService 內 VirtualDisplay capture 啟動 / 結束的位置（`grep -rn "VirtualDisplay\|MediaProjection\|takeScreenshot" /home/progcat/Projects/DollOSAIService-avatar-live2d/app/src/main/`），在啟動時 fire `callback.onVisionCaptureStateChanged(true)`，結束時 `false`。

若有 `CallbackBroadcaster` 集中處理（Plan 1 Task 11 加的），加新 method `broadcastVisionCaptureStateChanged(active: Boolean)` 並在相應位置呼叫。

- [ ] **Step 4: TestActivity callback stub 補新 method（no-op）**

```kotlin
override fun onVisionCaptureStateChanged(active: Boolean) {}
```

- [ ] **Step 5: Build both repos**

```bash
cd /home/progcat/Projects/DollOSAIService-avatar-live2d && ./gradlew assembleDebug 2>&1 | tail -3
cd /home/progcat/Projects/DollOSLauncher-avatar-live2d && ./gradlew assembleDebug 2>&1 | tail -3
```

- [ ] **Step 6: Commit (兩個 repo)**

```bash
cd /home/progcat/Projects/DollOSAIService-avatar-live2d
git add aidl/ app/src/main/java/
git commit -m "aidl: add onVisionCaptureStateChanged for overlay coordination"

cd /home/progcat/Projects/DollOSLauncher-avatar-live2d
git add app/aidl/
git commit -m "aidl: sync onVisionCaptureStateChanged from AIService"
```

---

## Task 7: DollOSEdgeOverlayService — bind, manage views, react to state

**Files:**
- Create: `app/src/main/java/org/dollos/launcher/edge/DollOSEdgeOverlayService.kt`
- Create: `app/src/main/res/drawable/notif_icon_doll.xml`

- [ ] **Step 1: 通知 icon vector**

Create `app/src/main/res/drawable/notif_icon_doll.xml`:

```xml
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp"
    android:height="24dp"
    android:viewportWidth="24"
    android:viewportHeight="24">
    <path android:fillColor="#FFFFFF"
          android:pathData="M12,2C9.79,2 8,3.79 8,6 8,8.21 9.79,10 12,10 14.21,10 16,8.21 16,6 16,3.79 14.21,2 12,2zM12,12C9.33,12 4,13.34 4,16L4,18 20,18 20,16C20,13.34 14.67,12 12,12z"/>
</vector>
```

- [ ] **Step 2: Service 實作**

Create `app/src/main/java/org/dollos/launcher/edge/DollOSEdgeOverlayService.kt`:

```kotlin
package org.dollos.launcher.edge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.ServiceInfo
import android.graphics.PixelFormat
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import org.dollos.ai.IDollOSAICallback
import org.dollos.ai.IDollOSAIService
import org.dollos.launcher.R
import org.dollos.launcher.live2d.OpsFlag
import org.json.JSONObject

class DollOSEdgeOverlayService : Service() {

    companion object {
        private const val TAG = "EdgeOverlayService"
        private const val CHANNEL_ID = "dollos_edge_overlay"
        private const val NOTIF_ID = 1
        private const val LAUNCHER_PKG = "org.dollos.launcher"
    }

    private val state = EdgeOverlayState()
    private var aiService: IDollOSAIService? = null
    private var edgeView: EdgeOverlayView? = null
    private var subtitleView: SubtitleBubbleView? = null
    private lateinit var wm: WindowManager
    private var monitor: ForegroundAppMonitor? = null

    private val callback = object : IDollOSAICallback.Stub() {
        override fun onOpsEvent(opName: String?, stateJson: String?) {
            opName ?: return
            val flags = state.activeFlags().toMutableSet()
            when (opName) {
                "asr_started" -> { flags.remove(OpsFlag.IDLE); flags += OpsFlag.LISTENING }
                "asr_ended" -> flags.remove(OpsFlag.LISTENING)
                "llm_in_flight" -> { flags.remove(OpsFlag.IDLE); flags += OpsFlag.THINKING }
                "llm_returned" -> flags.remove(OpsFlag.THINKING)
                "tts_playing" -> { flags.remove(OpsFlag.IDLE); flags += OpsFlag.SPEAKING }
                "tts_ended" -> flags.remove(OpsFlag.SPEAKING)
            }
            if (flags.isEmpty()) flags += OpsFlag.IDLE
            state.setActiveFlags(flags)
            applyState()
        }
        override fun onTtsAmplitude(amplitude: Float) {}  // not used here
        override fun onSubtitle(text: String?) {
            state.setSubtitle(text)
            applyState()
        }
        override fun onVisionCaptureStateChanged(active: Boolean) {
            state.setVisionCapturing(active)
            applyState()
        }
        // Existing callbacks (from Plan 1) — stub no-ops
        override fun onToken(token: String?) {}
        override fun onResponseComplete(response: String?) {}
        override fun onResponseError(error: String?) {}
        override fun onActionConfirmRequired(actionId: String?, description: String?) {}
        override fun onActionExecuted(actionId: String?, result: String?) {}
        override fun onTaskListUpdated(json: String?) {}
        override fun onMemoryConfirmRequired(memoryId: String?, summary: String?) {}
        override fun onWorkerComplete(workerId: String?, result: String?) {}
        override fun onCharacterChanged(characterId: String?) {}
        override fun onCharacterImportFailed(error: String?) {}
        override fun onSpeechRecognized(text: String?) {}
        override fun onTtsStarted() {}
        override fun onTtsCompleted() {}
        override fun onWakeWordDetected() {}
        override fun onSpeakerIdentified(speakerId: String?, confidence: Float) {}
        override fun onVoicePipelineStateChanged(stateName: String?) {}
    }

    private val serviceConn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            aiService = IDollOSAIService.Stub.asInterface(binder)
            try { aiService?.registerCallback(callback) } catch (e: Exception) { Log.e(TAG, "register failed", e) }
        }
        override fun onServiceDisconnected(name: ComponentName) {
            aiService = null
        }
    }

    override fun onCreate() {
        super.onCreate()
        wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager

        val channel = NotificationChannel(
            CHANNEL_ID, "DollOS Edge Overlay",
            NotificationManager.IMPORTANCE_MIN
        )
        (getSystemService(NotificationManager::class.java)).createNotificationChannel(channel)

        val notif = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Doll")
            .setSmallIcon(R.drawable.notif_icon_doll)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, notif)
        }

        addOverlayViews()
        bindAIService()
        startForegroundMonitor()
    }

    override fun onDestroy() {
        super.onDestroy()
        monitor?.stop()
        try { aiService?.unregisterCallback(callback) } catch (_: Exception) {}
        try { unbindService(serviceConn) } catch (_: Exception) {}
        edgeView?.let { runCatching { wm.removeView(it) } }
        subtitleView?.let { runCatching { wm.removeView(it) } }
        edgeView = null
        subtitleView = null
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun addOverlayViews() {
        val edge = EdgeOverlayView(this)
        val edgeLp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        )
        wm.addView(edge, edgeLp)
        edgeView = edge

        val subtitle = SubtitleBubbleView(this)
        val subLp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.BOTTOM
            y = (32 * resources.displayMetrics.density).toInt()
        }
        wm.addView(subtitle, subLp)
        subtitleView = subtitle
    }

    private fun bindAIService() {
        val intent = Intent("org.dollos.ai.IDollOSAIService").apply {
            setPackage("org.dollos.ai")
        }
        bindService(intent, serviceConn, Context.BIND_AUTO_CREATE)
    }

    private fun startForegroundMonitor() {
        monitor = ForegroundAppMonitor(this, LAUNCHER_PKG) { isLauncherFg ->
            state.setLauncherForeground(isLauncherFg)
            applyState()
        }
        monitor?.start()
    }

    private fun applyState() {
        val edgeArgb = if (state.shouldShowEdge())
            StateColorPalette.composite(state.activeFlags()) else 0
        edgeView?.setStateColor(edgeArgb)

        if (state.shouldShowSubtitle()) {
            state.subtitleText()?.let { subtitleView?.showWithText(it) }
        } else {
            subtitleView?.hide()
        }
    }
}
```

- [ ] **Step 3: Build**

```bash
./gradlew assembleDebug 2>&1 | tail -5
```

Fix any compile errors (likely related to AIDL callback stubs needing exact signatures from existing `IDollOSAICallback`).

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/edge/DollOSEdgeOverlayService.kt \
        app/src/main/res/drawable/notif_icon_doll.xml
git commit -m "feat(edge): add DollOSEdgeOverlayService managing overlay views"
```

---

## Task 8: 啟動 service — 開機自動 + Activity onCreate 觸發

**Files:**
- Modify: `app/src/main/AndroidManifest.xml`
- Create: `app/src/main/java/org/dollos/launcher/edge/EdgeOverlayBootReceiver.kt`
- Modify: `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`

- [ ] **Step 1: BootReceiver**

```kotlin
package org.dollos.launcher.edge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class EdgeOverlayBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val svcIntent = Intent(context, DollOSEdgeOverlayService::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(svcIntent)
        } else {
            context.startService(svcIntent)
        }
    }
}
```

- [ ] **Step 2: Manifest 註冊**

加 receiver + permission：

```xml
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
```

```xml
<receiver
    android:name=".edge.EdgeOverlayBootReceiver"
    android:exported="false"
    android:directBootAware="false">
    <intent-filter>
        <action android:name="android.intent.action.BOOT_COMPLETED" />
    </intent-filter>
</receiver>
```

- [ ] **Step 3: 在 Activity onCreate 也啟動（保險，覆蓋 fresh install / debug rebuild）**

Edit `DollOSLauncherActivity.kt`，在 `onCreate` 末尾加：

```kotlin
val svcIntent = Intent(this, org.dollos.launcher.edge.DollOSEdgeOverlayService::class.java)
startForegroundService(svcIntent)
```

- [ ] **Step 4: Build + commit**

```bash
./gradlew assembleDebug 2>&1 | tail -3
git add app/src/main/AndroidManifest.xml \
        app/src/main/java/org/dollos/launcher/edge/EdgeOverlayBootReceiver.kt \
        app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt
git commit -m "feat(edge): start overlay service on boot + activity create"
```

---

## Task 9: 端到端驗證 [HUMAN]

**Files:**
- 無 code 變更

**Steps:**

- [ ] **Step 1: 部署 APKs**

依 Plan 1 Task 13 流程 build + flash Launcher（AIService 若 Task 6 改了也一併）。

- [ ] **Step 2: 授予 PACKAGE_USAGE_STATS（system app 應該已經有）**

```bash
adb shell appops set --uid org.dollos.launcher GET_USAGE_STATS allow
```

- [ ] **Step 3: 重啟 + 驗證 service**

```bash
adb reboot
adb wait-for-device
sleep 30  # boot complete
adb shell dumpsys activity services org.dollos.launcher | grep -i edgeoverlay
```

Expected: service running.

- [ ] **Step 4: Checklist（手動）**

```
□ 切到其他 app（例如 Settings、Calculator）
□ 對 Doll 說「嗨 Doll」→ 螢幕邊緣出現青藍色 vignette（LISTENING）
□ Doll 思考時 → 紫色 vignette（THINKING）
□ Doll 開始講 → 暖橙 vignette + 字幕氣泡從底部滑入
□ Doll 講完 → vignette 消失 + 字幕滑出
□ 回到 Launcher → vignette 與氣泡都不顯示
□ 鎖屏 → 不顯示
□ 觸發 Doll 視覺擷取（按鈕 / 指令）→ overlay 在擷取期間消失
□ 5 分鐘穩定，無記憶體洩漏 / fps 掉
```

- [ ] **Step 5: 紀錄結果**

寫進 `docs/superpowers/plans/2026-04-24-edge-state-indicator-result.md` 並 commit。

---

## 自我檢查

### 規格覆蓋

| Spec 條目 | 對應 task |
|---|---|
| §2.3 邊緣狀態指示 vignette | Task 1, 4, 7 |
| §2.3 多狀態組合（LISTENING + THINKING）| Task 1（color compose）|
| §2.3 Launcher 在前景時隱藏 | Task 2, 3, 7（state + monitor）|
| §2.3 鎖屏不顯示 | Task 2 |
| §2.2 字幕氣泡邊緣滑出 | Task 5, 7 |
| 視覺擷取期間隱藏 | Task 6（AIDL）, Task 7（state）|
| 開機自動啟動 | Task 8 |

### Placeholder 掃描

- Task 6 Step 3 「找到 AIService 內 VirtualDisplay capture 啟動 / 結束的位置」是查找指引非 placeholder
- Task 7 Step 3「likely related to AIDL callback stubs」是合理 build error hint

### 型別一致性

- `OpsFlag` enum — Plan 1 Task 5 定義，Task 1 / 2 / 7 使用
- `EdgeOverlayState` setter / getter signatures 在 Task 2 定義 + Task 7 呼叫

---

## 執行選項

**Plan complete and saved to `docs/superpowers/plans/2026-04-24-edge-state-indicator.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - per-task subagent + two-stage review

**2. Inline Execution** - in this session, batch checkpoints

**Which approach?**
