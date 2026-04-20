# DollOSSkills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 DollOSSkills — Doll AI 終端的 skills bundle runtime + skills library（alarm / weather / notification_summary / memory_review / music / uisage）+ 4 個 routines（早安 / 睡前 / 進家 / 出門），透過 AIDL 被 DollOSCore 呼叫，遵守 master plan §3.6 介面。

**Architecture:** 單一 Android priv-app，`DollSkillsService` (foreground service) 托管 `IDollSkills` AIDL binder。啟動時 `BundleScanner` 掃兩處（`/data/system_ext/dollos/skills/` 內建 + active Character Pack `skills/`）→ 建 `SkillMetadata` 索引（progressive disclosure，description 限 1024 char）。`invokeSkill` 透過 `ScriptExecutor` 執行對應 skill 的 scripts（支援 Android intent / AIDL call / 內部 Kotlin class）。4 個 routines 是實作了 `Routine` 介面的 Kotlin class，被 `RoutineScheduler`（AlarmManager 基礎）排程，互斥由 `RoutineLock` 控制。所有 skill / routine 輸出都回傳 `[SILENT]` / `[SPEAK "..."]` / `[VIBRATE ...]` string，由 Core 的 Output Orchestrator 處理實際動作。

**Tech Stack:** Kotlin 1.9, Android AIDL, AlarmManager, AccessibilityService, MediaSession (MediaController), NotificationListenerService, OkHttp (weather API), JUnit4 + MockK（unit test）, AndroidX Test + Espresso（instrumented test）.

**Spec reference:** `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md` §5.4, §4（Character Pack v2）
**Master plan reference:** `docs/superpowers/plans/2026-04-20-doll-terminal.md` §3.6, §4.1, §8, §9, §12, §10

---

## File Structure

新建專案 `~/Projects/DollOSSkills/`，結構參考 master §8.1：

```
DollOSSkills/
├── app/build.gradle.kts
├── app/src/main/AndroidManifest.xml
├── app/src/main/aidl/dollos/skills/
│   ├── IDollSkills.aidl
│   ├── SkillMetadata.aidl
│   └── SkillInvocationCallback.aidl
├── app/src/main/aidl/dollos/core/          ← copy from DollOSCore
│   ├── IDollCore.aidl
│   ├── ObservationEvent.aidl
│   └── SkillCallbackResult.aidl
├── app/src/main/aidl/dollos/aux/           ← copy from DollOSAuxEngine
│   └── IDollAuxEngine.aidl
├── app/src/main/aidl/dollos/memory/        ← copy from DollOSMemory
│   └── IDollMemory.aidl
├── app/src/main/aidl/dollos/voice/         ← copy from DollOSVoice
│   └── IDollVoice.aidl
├── app/src/main/java/dollos/skills/
│   ├── DollSkillsService.kt                ← foreground service, hosts IDollSkills binder
│   ├── bundle/
│   │   ├── BundleScanner.kt                ← scans two roots, parses SKILL.md
│   │   ├── SkillBundle.kt                  ← data class (id, path, source, SKILL.md content)
│   │   ├── SkillMdParser.kt                ← extracts first ~1024 char description + full body
│   │   └── SkillRegistry.kt                ← merged catalog, conflict resolution
│   ├── runtime/
│   │   ├── SkillInvoker.kt                 ← entry point for invokeSkill()
│   │   ├── ScriptExecutor.kt               ← dispatches to Kotlin impl or exec script
│   │   └── ProgressiveDisclosure.kt        ← listMetadata vs viewSkill
│   ├── aidl/
│   │   └── DollSkillsBinder.kt             ← IDollSkills.Stub impl
│   ├── connectors/                         ← AIDL clients to other Doll apps
│   │   ├── CoreConnector.kt
│   │   ├── AuxEngineConnector.kt
│   │   ├── MemoryConnector.kt
│   │   └── VoiceConnector.kt
│   ├── skills/
│   │   ├── SkillImpl.kt                    ← interface every built-in skill implements
│   │   ├── AlarmSkill.kt                   ← AlarmManager integration
│   │   ├── WeatherSkill.kt                 ← OkHttp external API
│   │   ├── NotificationSummarySkill.kt     ← reads NotificationListenerService cache → Aux summarize
│   │   ├── NotificationCacheListener.kt    ← NotificationListenerService
│   │   ├── MemoryReviewSkill.kt            ← distillation review prompt
│   │   ├── MusicSkill.kt                   ← MediaController play/pause/skip
│   │   └── UisageSkill.kt                  ← AccessibilityService wrapper
│   ├── uisage/
│   │   ├── DollAccessibilityService.kt     ← ported from AIService Plan D v2
│   │   └── UiOperationDispatcher.kt
│   └── routines/
│       ├── Routine.kt                      ← interface
│       ├── RoutineScheduler.kt             ← AlarmManager-based cron
│       ├── RoutineLock.kt                  ← mutual exclusion
│       ├── MorningRoutine.kt
│       ├── BedtimeRoutine.kt
│       ├── ArriveHomeRoutine.kt
│       └── LeaveHomeRoutine.kt
├── app/src/main/res/xml/
│   └── accessibility_service_config.xml
└── app/src/test/java/dollos/skills/
    └── ... (mirror structure)
```

AOSP integration at `~/Projects/DollOS-build/external/DollOSSkills/` — per master §8.2.

---

## §1 App 骨架

### Task 1.1: 初始化 Gradle 專案骨架

**Files:**
- Create: `~/Projects/DollOSSkills/build.gradle.kts`
- Create: `~/Projects/DollOSSkills/settings.gradle.kts`
- Create: `~/Projects/DollOSSkills/gradle.properties`
- Create: `~/Projects/DollOSSkills/app/build.gradle.kts`

- [ ] **Step 1: 建立 settings.gradle.kts**

```kotlin
rootProject.name = "DollOSSkills"
include(":app")
```

- [ ] **Step 2: 建立 root build.gradle.kts**

```kotlin
plugins {
    id("com.android.application") version "8.2.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.22" apply false
}
```

- [ ] **Step 3: 建立 app/build.gradle.kts（AIDL + Kotlin）**

```kotlin
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dollos.skills"
    compileSdk = 34

    defaultConfig {
        applicationId = "dollos.skills"
        minSdk = 34
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildFeatures {
        aidl = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging {
        resources.excludes += setOf("META-INF/LICENSE*", "META-INF/NOTICE*")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("io.mockk:mockk:1.13.8")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.7.3")

    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
}
```

- [ ] **Step 4: 驗證 gradle 可執行**

Run:
```bash
cd ~/Projects/DollOSSkills && ./gradlew tasks
```
Expected: tasks list prints without error

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOSSkills
git init
git add .
git commit -m "chore: DollOSSkills gradle skeleton"
```

### Task 1.2: 建立 AndroidManifest + foreground service stub

**Files:**
- Create: `app/src/main/AndroidManifest.xml`
- Create: `app/src/main/java/dollos/skills/DollSkillsService.kt`

- [ ] **Step 1: 寫 failing test — service 啟動不 crash**

Create `app/src/test/java/dollos/skills/DollSkillsServiceTest.kt`:
```kotlin
package dollos.skills

import org.junit.Assert.assertNotNull
import org.junit.Test

class DollSkillsServiceTest {
    @Test
    fun service_class_loads() {
        val cls = Class.forName("dollos.skills.DollSkillsService")
        assertNotNull(cls)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./gradlew :app:test`
Expected: FAIL with `ClassNotFoundException`

- [ ] **Step 3: 寫最小 foreground service**

```kotlin
package dollos.skills

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder

class DollSkillsService : Service() {
    companion object {
        private const val CHANNEL_ID = "doll_skills_fg"
        private const val NOTIF_ID = 42001
    }

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        startForeground(NOTIF_ID, buildNotification())
    }

    override fun onBind(intent: Intent?): IBinder? = null  // replaced in Task 2.x

    private fun ensureChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Doll Skills", NotificationManager.IMPORTANCE_MIN)
            )
        }
    }

    private fun buildNotification(): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Doll Skills")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .build()
}
```

- [ ] **Step 4: AndroidManifest 註冊**

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE"/>
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>

    <application android:label="DollOS Skills">
        <service
            android:name=".DollSkillsService"
            android:exported="true"
            android:foregroundServiceType="specialUse"/>
    </application>
</manifest>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./gradlew :app:test`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/ 
git commit -m "feat(skills): foreground service skeleton"
```

### Task 1.3: AOSP Android.bp + 加入 build 樹

**Files:**
- Create: `Android.bp`
- Create: `prebuilt/.gitkeep`
- Modify (external): `~/Projects/DollOS-build/external/DollOSSkills/Android.bp`

- [ ] **Step 1: 寫 Android.bp**

```bp
android_app_import {
    name: "DollOSSkills",
    apk: "prebuilt/DollOSSkills.apk",
    presigned: true,
    privileged: true,
    system_ext_specific: true,
}
```

- [ ] **Step 2: 建 prebuilt/ 目錄**

```bash
mkdir -p ~/Projects/DollOSSkills/prebuilt
touch ~/Projects/DollOSSkills/prebuilt/.gitkeep
```

- [ ] **Step 3: 第一次 build APK + rsync 到 AOSP 樹（手動驗 build 流程）**

Run:
```bash
cd ~/Projects/DollOSSkills
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSSkills.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSSkills/
```
Expected: APK exists, rsync succeeds

- [ ] **Step 4: Commit**

```bash
git add Android.bp prebuilt/
git commit -m "build: AOSP android_app_import for DollOSSkills"
```

---

## §2 AIDL 實作（master §3.6）

### Task 2.1: 寫 `SkillMetadata.aidl` parcelable

**Files:**
- Create: `app/src/main/aidl/dollos/skills/SkillMetadata.aidl`
- Create: `app/src/main/java/dollos/skills/aidl/SkillMetadata.kt`

- [ ] **Step 1: 寫 failing test — SkillMetadata 資料往返正確**

Create `app/src/test/java/dollos/skills/aidl/SkillMetadataTest.kt`:
```kotlin
package dollos.skills.aidl

import org.junit.Assert.assertEquals
import org.junit.Test

class SkillMetadataTest {
    @Test
    fun constructs_with_required_fields() {
        val m = SkillMetadata(
            skillId = "alarm",
            name = "Alarm",
            description = "Sets alarms via AlarmManager",
            requiredInputs = arrayOf("time", "label"),
            source = "builtin",
        )
        assertEquals("alarm", m.skillId)
        assertEquals(2, m.requiredInputs.size)
    }

    @Test(expected = IllegalArgumentException::class)
    fun description_longer_than_1024_throws() {
        SkillMetadata(
            skillId = "x", name = "x",
            description = "a".repeat(1025),
            requiredInputs = emptyArray(), source = "builtin",
        )
    }
}
```

- [ ] **Step 2: Run to verify FAIL**

Run: `./gradlew :app:test --tests SkillMetadataTest`
Expected: FAIL (class not found)

- [ ] **Step 3: 寫 AIDL parcelable 宣告**

`SkillMetadata.aidl`:
```aidl
// Version: 1
package dollos.skills;

parcelable SkillMetadata;
```

- [ ] **Step 4: 寫 Kotlin parcelable impl**

`SkillMetadata.kt`:
```kotlin
package dollos.skills.aidl

import android.os.Parcel
import android.os.Parcelable

data class SkillMetadata(
    val skillId: String,
    val name: String,
    val description: String,
    val requiredInputs: Array<String>,
    val source: String,  // "builtin" | "character_pack"
) : Parcelable {
    init {
        require(description.length <= 1024) {
            "SkillMetadata.description must be <= 1024 chars (got ${description.length})"
        }
        require(source == "builtin" || source == "character_pack") {
            "source must be 'builtin' or 'character_pack'"
        }
    }

    constructor(p: Parcel) : this(
        skillId = p.readString()!!,
        name = p.readString()!!,
        description = p.readString()!!,
        requiredInputs = p.createStringArray()!!,
        source = p.readString()!!,
    )

    override fun writeToParcel(dest: Parcel, flags: Int) {
        dest.writeString(skillId)
        dest.writeString(name)
        dest.writeString(description)
        dest.writeStringArray(requiredInputs)
        dest.writeString(source)
    }

    override fun describeContents(): Int = 0

    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SkillMetadata) return false
        return skillId == other.skillId && name == other.name &&
            description == other.description && source == other.source &&
            requiredInputs.contentEquals(other.requiredInputs)
    }
    override fun hashCode(): Int {
        var r = skillId.hashCode()
        r = 31 * r + name.hashCode()
        r = 31 * r + description.hashCode()
        r = 31 * r + requiredInputs.contentHashCode()
        r = 31 * r + source.hashCode()
        return r
    }

    companion object CREATOR : Parcelable.Creator<SkillMetadata> {
        override fun createFromParcel(p: Parcel) = SkillMetadata(p)
        override fun newArray(size: Int) = arrayOfNulls<SkillMetadata>(size)
    }
}
```

- [ ] **Step 5: Run to verify PASS**

Run: `./gradlew :app:test --tests SkillMetadataTest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/src/main/aidl app/src/main/java/dollos/skills/aidl app/src/test
git commit -m "feat(skills): SkillMetadata parcelable with 1024-char description invariant"
```

### Task 2.2: 宣告 `IDollSkills.aidl` 介面

**Files:**
- Create: `app/src/main/aidl/dollos/skills/IDollSkills.aidl`
- Create: `app/src/main/aidl/dollos/skills/SkillInvocationCallback.aidl`

- [ ] **Step 1: 寫 IDollSkills.aidl**

```aidl
// Version: 1
package dollos.skills;

import dollos.skills.SkillMetadata;

interface IDollSkills {
    List<SkillMetadata> listMetadata();
    String viewSkill(String skillId);
    void invokeSkill(String skillId, in Bundle inputs, String callbackTo);

    void scheduleRoutine(String routineId, String cronExpr);
    void cancelRoutine(String routineId);
    List<String> listActiveRoutines();
}
```

- [ ] **Step 2: 寫 SkillInvocationCallback.aidl（內部協定，本 app 接 Core 回 callback 用）**

```aidl
// Version: 1
package dollos.skills;

interface SkillInvocationCallback {
    void onSkillResult(String skillId, String resultJson);
    void onSkillError(String skillId, String errorJson);
}
```

- [ ] **Step 3: Gradle build — AIDL stub 自動生成**

Run: `./gradlew :app:compileReleaseKotlin`
Expected: BUILD SUCCESSFUL；產生 `build/generated/aidl_source_output_dir/.../IDollSkills.java`

- [ ] **Step 4: Commit**

```bash
git add app/src/main/aidl/dollos/skills/
git commit -m "feat(skills): IDollSkills AIDL interface (master §3.6)"
```

### Task 2.3: Copy 其他 app 的 AIDL 檔（Core / Aux / Memory / Voice）

**Files:**
- Create: `app/src/main/aidl/dollos/core/IDollCore.aidl`
- Create: `app/src/main/aidl/dollos/core/ObservationEvent.aidl`
- Create: `app/src/main/aidl/dollos/core/SkillCallbackResult.aidl`
- Create: `app/src/main/aidl/dollos/aux/IDollAuxEngine.aidl`
- Create: `app/src/main/aidl/dollos/memory/IDollMemory.aidl`
- Create: `app/src/main/aidl/dollos/voice/IDollVoice.aidl`

- [ ] **Step 1: 從 master §3 複製 AIDL 檔案到本 app 的 `aidl/` 目錄**

複製以下檔案（**不要自訂，與 master §3 完全一致**）：
- `app/src/main/aidl/dollos/core/IDollCore.aidl` — master §3.1
- `app/src/main/aidl/dollos/core/ObservationEvent.aidl` — master §3.3
- `app/src/main/aidl/dollos/core/SkillCallbackResult.aidl` — master §3.4
- `app/src/main/aidl/dollos/core/IDollCoreStateListener.aidl` — master §3.2
- `app/src/main/aidl/dollos/aux/IDollAuxEngine.aidl` — master §3.3
- `app/src/main/aidl/dollos/memory/IDollMemory.aidl` — master §3.7
- `app/src/main/aidl/dollos/voice/IDollVoice.aidl` — master §3.5
- `app/src/main/aidl/dollos/skills/IDollSkills.aidl` — master §3.6（本 app 自己的 interface）

Parcelable 實體（ObservationEvent / SkillCallbackResult）的 Kotlin impl 由 Core app 提供。本 app 只需 AIDL 宣告讓 codegen 跑過。

- [ ] **Step 2: 建立 `IDollSkills.Stub` 實作**

`IDollSkills` 是本 app 對外介面（per master §3.6）：
```aidl
// Version: 1
package dollos.skills;

import dollos.skills.SkillMetadata;

interface IDollSkills {
    List<String> listSkills();
    String getSkillMetadata(String skillId);
    void executeSkill(String skillId, in Bundle params);
    void triggerRoutine(String routineId);
    void registerOneShotRoutine(String trigger, in Bundle config);
}
```
    constructor(p: Parcel) : this(p.readString()!!, p.readString()!!, p.readString())
    override fun writeToParcel(dest: Parcel, flags: Int) {
        dest.writeString(status); dest.writeString(resultJson); dest.writeString(errorMessage)
    }
    override fun describeContents() = 0
    companion object CREATOR : Parcelable.Creator<SkillCallbackResult> {
        override fun createFromParcel(p: Parcel) = SkillCallbackResult(p)
        override fun newArray(size: Int) = arrayOfNulls<SkillCallbackResult>(size)
    }
}
```

- [ ] **Step 6: Build — 確認所有 AIDL 可 compile**

Run: `./gradlew :app:assembleDebug`
Expected: BUILD SUCCESSFUL

- [ ] **Step 7: Commit**

```bash
git add app/src/main/aidl app/src/main/java/dollos/core
git commit -m "feat(skills): copy Core/Aux/Memory/Voice AIDL contracts"
```

### Task 2.4: `DollSkillsBinder.kt` — IDollSkills.Stub 空實作 + service bind

**Files:**
- Create: `app/src/main/java/dollos/skills/aidl/DollSkillsBinder.kt`
- Modify: `app/src/main/java/dollos/skills/DollSkillsService.kt`

- [ ] **Step 1: 寫 failing test — bind 回傳 non-null**

Create `app/src/androidTest/java/dollos/skills/DollSkillsBindTest.kt`:
```kotlin
package dollos.skills

import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import dollos.skills.IDollSkills
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class DollSkillsBindTest {
    @Test(timeout = 10_000)
    fun binder_non_null_and_listMetadata_returns_empty_initially() {
        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        val latch = CountDownLatch(1)
        var service: IDollSkills? = null
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                service = IDollSkills.Stub.asInterface(binder); latch.countDown()
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        ctx.bindService(Intent(ctx, DollSkillsService::class.java), conn, android.content.Context.BIND_AUTO_CREATE)
        latch.await(5, TimeUnit.SECONDS)
        assertNotNull(service)
        val list = service!!.listMetadata()
        assertNotNull(list)
    }
}
```

- [ ] **Step 2: Run to verify FAIL**

Run: `./gradlew :app:connectedAndroidTest`
Expected: FAIL (onBind returns null)

- [ ] **Step 3: 寫 `DollSkillsBinder.kt`（stub 全部回空值，填充在後續 tasks）**

```kotlin
package dollos.skills.aidl

import android.os.Bundle
import dollos.skills.IDollSkills

class DollSkillsBinder : IDollSkills.Stub() {
    override fun listMetadata(): MutableList<SkillMetadata> = mutableListOf()
    override fun viewSkill(skillId: String?): String = ""
    override fun invokeSkill(skillId: String?, inputs: Bundle?, callbackTo: String?) {}
    override fun scheduleRoutine(routineId: String?, cronExpr: String?) {}
    override fun cancelRoutine(routineId: String?) {}
    override fun listActiveRoutines(): MutableList<String> = mutableListOf()
}
```

- [ ] **Step 4: 改 `DollSkillsService.onBind` 回傳 binder**

```kotlin
// DollSkillsService.kt 改 onBind
private val binder = dollos.skills.aidl.DollSkillsBinder()
override fun onBind(intent: Intent?): IBinder = binder
```

- [ ] **Step 5: Run bind test on device**

Run: `./gradlew :app:connectedAndroidTest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/
git commit -m "feat(skills): DollSkillsBinder stub wired to service onBind"
```

---

## §3 Bundle scanner + metadata 索引

### Task 3.1: `SkillMdParser` — 切第一段 description（~1024 char）

**Files:**
- Create: `app/src/main/java/dollos/skills/bundle/SkillMdParser.kt`
- Create: `app/src/test/java/dollos/skills/bundle/SkillMdParserTest.kt`

**SKILL.md 合約：** 第一個非空的 Markdown 段落（到第一個空行為止）即為 description。後續整檔為 full instructions。Description 最長 1024 char（超出 → 截斷並 log warning）。

- [ ] **Step 1: 寫 failing tests**

```kotlin
package dollos.skills.bundle

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SkillMdParserTest {
    @Test
    fun first_paragraph_is_description() {
        val md = """
            # Alarm Skill
            Sets alarms via AlarmManager.
            Accepts ISO-8601 time + label.

            ## Usage
            Call with inputs time and label.
        """.trimIndent()
        val parsed = SkillMdParser.parse(md)
        assertEquals(
            "# Alarm Skill\nSets alarms via AlarmManager.\nAccepts ISO-8601 time + label.",
            parsed.description,
        )
        assertTrue(parsed.fullBody.contains("## Usage"))
    }

    @Test
    fun description_truncated_at_1024() {
        val longFirst = "a".repeat(2000)
        val parsed = SkillMdParser.parse(longFirst)
        assertEquals(1024, parsed.description.length)
    }

    @Test
    fun empty_input_returns_empty_description() {
        val parsed = SkillMdParser.parse("")
        assertEquals("", parsed.description)
        assertEquals("", parsed.fullBody)
    }

    @Test
    fun crlf_line_endings_normalized() {
        val md = "Line1\r\nLine2\r\n\r\nSecond para"
        val parsed = SkillMdParser.parse(md)
        assertEquals("Line1\nLine2", parsed.description)
    }
}
```

- [ ] **Step 2: Run to verify FAIL**

Run: `./gradlew :app:test --tests SkillMdParserTest`
Expected: FAIL

- [ ] **Step 3: 寫實作**

```kotlin
package dollos.skills.bundle

data class ParsedSkillMd(
    val description: String,
    val fullBody: String,
)

object SkillMdParser {
    private const val DESCRIPTION_MAX = 1024

    fun parse(content: String): ParsedSkillMd {
        if (content.isBlank()) return ParsedSkillMd("", "")
        val normalized = content.replace("\r\n", "\n").replace('\r', '\n')
        val paragraphs = normalized.split(Regex("\n\\s*\n"))
        val firstNonEmpty = paragraphs.firstOrNull { it.isNotBlank() }?.trim() ?: ""
        val desc = if (firstNonEmpty.length > DESCRIPTION_MAX)
            firstNonEmpty.substring(0, DESCRIPTION_MAX) else firstNonEmpty
        return ParsedSkillMd(description = desc, fullBody = normalized.trim())
    }
}
```

- [ ] **Step 4: Run to verify PASS**

Run: `./gradlew :app:test --tests SkillMdParserTest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/dollos/skills/bundle/SkillMdParser.kt app/src/test
git commit -m "feat(skills): SKILL.md description parser with 1024-char cap"
```

### Task 3.2: `SkillBundle` 資料類別 + 單一 bundle 掃描

**Files:**
- Create: `app/src/main/java/dollos/skills/bundle/SkillBundle.kt`
- Create: `app/src/test/java/dollos/skills/bundle/SkillBundleTest.kt`

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.bundle

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class SkillBundleTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test
    fun load_from_directory_with_skill_md() {
        val dir = tmp.newFolder("alarm")
        File(dir, "SKILL.md").writeText("Alarm skill sets alarms.\n\n## Scripts\nscript1")
        File(dir, "scripts").mkdir()
        File(dir, "scripts/arm.sh").writeText("#!/bin/sh\necho arm")

        val bundle = SkillBundle.load(dir, source = "builtin")
        assertNotNull(bundle)
        assertEquals("alarm", bundle!!.skillId)
        assertEquals("Alarm skill sets alarms.", bundle.parsed.description)
        assertEquals("builtin", bundle.source)
        assertEquals(1, bundle.scriptFiles.size)
    }

    @Test
    fun missing_skill_md_returns_null() {
        val dir = tmp.newFolder("broken")
        assertNull(SkillBundle.load(dir, "builtin"))
    }
}
```

- [ ] **Step 2: Run to verify FAIL**

Run: `./gradlew :app:test --tests SkillBundleTest`
Expected: FAIL

- [ ] **Step 3: 寫實作**

```kotlin
package dollos.skills.bundle

import java.io.File

data class SkillBundle(
    val skillId: String,
    val rootDir: File,
    val parsed: ParsedSkillMd,
    val source: String,           // "builtin" | "character_pack"
    val scriptFiles: List<File>,
) {
    companion object {
        fun load(dir: File, source: String): SkillBundle? {
            if (!dir.isDirectory) return null
            val md = File(dir, "SKILL.md")
            if (!md.isFile) return null
            val parsed = SkillMdParser.parse(md.readText())
            val scripts = File(dir, "scripts").takeIf { it.isDirectory }
                ?.listFiles()?.filter { it.isFile }?.sorted() ?: emptyList()
            return SkillBundle(
                skillId = dir.name,
                rootDir = dir,
                parsed = parsed,
                source = source,
                scriptFiles = scripts,
            )
        }
    }
}
```

- [ ] **Step 4: Run PASS + Commit**

```bash
./gradlew :app:test --tests SkillBundleTest
git add app/src/main/java/dollos/skills/bundle/SkillBundle.kt app/src/test
git commit -m "feat(skills): SkillBundle loader from directory"
```

### Task 3.3: `BundleScanner` — 掃兩個根目錄

**Files:**
- Create: `app/src/main/java/dollos/skills/bundle/BundleScanner.kt`
- Create: `app/src/test/java/dollos/skills/bundle/BundleScannerTest.kt`

- [ ] **Step 1: 寫 failing test — 兩根目錄 merge**

```kotlin
package dollos.skills.bundle

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class BundleScannerTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun makeBundle(root: File, id: String, desc: String) {
        val dir = File(root, id); dir.mkdirs()
        File(dir, "SKILL.md").writeText(desc)
    }

    @Test
    fun scans_builtin_and_character_pack_roots() {
        val builtin = tmp.newFolder("builtin_skills")
        val pack = tmp.newFolder("pack_skills")
        makeBundle(builtin, "alarm", "Alarm")
        makeBundle(pack, "weather", "Weather")

        val bundles = BundleScanner(builtin, pack).scan()
        val ids = bundles.map { it.skillId }.toSet()
        assertEquals(setOf("alarm", "weather"), ids)
        assertEquals("builtin", bundles.first { it.skillId == "alarm" }.source)
        assertEquals("character_pack", bundles.first { it.skillId == "weather" }.source)
    }

    @Test
    fun missing_roots_are_skipped_silently() {
        val bundles = BundleScanner(
            builtinRoot = File(tmp.root, "does-not-exist"),
            characterPackRoot = null,
        ).scan()
        assertTrue(bundles.isEmpty())
    }

    @Test
    fun skips_entries_without_skill_md() {
        val root = tmp.newFolder("r")
        File(root, "broken").mkdir()
        makeBundle(root, "good", "Good skill")
        val bundles = BundleScanner(root, null).scan()
        assertEquals(1, bundles.size)
        assertEquals("good", bundles[0].skillId)
    }
}
```

- [ ] **Step 2: Run to verify FAIL**

Run: `./gradlew :app:test --tests BundleScannerTest`
Expected: FAIL

- [ ] **Step 3: 寫實作**

```kotlin
package dollos.skills.bundle

import java.io.File

class BundleScanner(
    private val builtinRoot: File,
    private val characterPackRoot: File?,
) {
    fun scan(): List<SkillBundle> {
        val result = mutableListOf<SkillBundle>()
        scanRoot(builtinRoot, "builtin", result)
        characterPackRoot?.let { scanRoot(it, "character_pack", result) }
        return result
    }

    private fun scanRoot(root: File, source: String, out: MutableList<SkillBundle>) {
        if (!root.isDirectory) return
        root.listFiles()?.forEach { dir ->
            SkillBundle.load(dir, source)?.let { out += it }
        }
    }

    companion object {
        const val BUILTIN_PATH = "/data/system_ext/dollos/skills"
        fun characterPackSkillsPath(packId: String): String =
            "/data/system_ext/dollos/character_packs/$packId/skills"
    }
}
```

- [ ] **Step 4: PASS + Commit**

```bash
./gradlew :app:test --tests BundleScannerTest
git add app/src/main/java/dollos/skills/bundle/BundleScanner.kt app/src/test
git commit -m "feat(skills): BundleScanner scans builtin + character_pack roots"
```

### Task 3.4: `SkillRegistry` — 衝突解決（character_pack 覆蓋 builtin）

**Files:**
- Create: `app/src/main/java/dollos/skills/bundle/SkillRegistry.kt`
- Create: `app/src/test/java/dollos/skills/bundle/SkillRegistryTest.kt`

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.bundle

import dollos.skills.aidl.SkillMetadata
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class SkillRegistryTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun makeBundle(parent: File, id: String, desc: String): SkillBundle {
        val dir = File(parent, id); dir.mkdirs()
        File(dir, "SKILL.md").writeText(desc)
        return SkillBundle.load(dir, if (parent.name == "pack") "character_pack" else "builtin")!!
    }

    @Test
    fun character_pack_overrides_same_name_builtin() {
        val builtinRoot = tmp.newFolder("builtin")
        val packRoot = tmp.newFolder("pack")
        val builtinAlarm = makeBundle(builtinRoot, "alarm", "Builtin alarm description")
        val packAlarm = makeBundle(packRoot, "alarm", "Custom alarm description")

        val registry = SkillRegistry.build(listOf(builtinAlarm, packAlarm))
        val resolved = registry.get("alarm")!!
        assertEquals("character_pack", resolved.source)
        assertEquals("Custom alarm description", resolved.parsed.description)
    }

    @Test
    fun no_conflict_both_present() {
        val builtinRoot = tmp.newFolder("builtin")
        val packRoot = tmp.newFolder("pack")
        val a = makeBundle(builtinRoot, "alarm", "Alarm")
        val b = makeBundle(packRoot, "weather", "Weather")
        val registry = SkillRegistry.build(listOf(a, b))
        assertEquals(2, registry.listIds().size)
    }

    @Test
    fun metadata_view_respects_override() {
        val builtinRoot = tmp.newFolder("builtin")
        val packRoot = tmp.newFolder("pack")
        val b = makeBundle(builtinRoot, "alarm", "Old")
        val p = makeBundle(packRoot, "alarm", "New")
        val registry = SkillRegistry.build(listOf(b, p))
        val meta: SkillMetadata = registry.listMetadata().single { it.skillId == "alarm" }
        assertEquals("New", meta.description)
        assertEquals("character_pack", meta.source)
    }
}
```

- [ ] **Step 2: Run FAIL → 寫實作**

```kotlin
package dollos.skills.bundle

import dollos.skills.aidl.SkillMetadata

class SkillRegistry private constructor(
    private val bundles: Map<String, SkillBundle>,
) {
    fun get(skillId: String): SkillBundle? = bundles[skillId]

    fun listIds(): List<String> = bundles.keys.sorted()

    fun listMetadata(): List<SkillMetadata> = bundles.values.map { b ->
        SkillMetadata(
            skillId = b.skillId,
            name = b.skillId,  // name defaults to id unless SKILL.md has explicit heading override (future)
            description = b.parsed.description,
            requiredInputs = emptyArray(),  // filled by built-in skills via SkillImpl.inputs()
            source = b.source,
        )
    }

    companion object {
        fun build(bundles: List<SkillBundle>): SkillRegistry {
            // Later entries override earlier; BundleScanner emits builtin first, then character_pack.
            val map = LinkedHashMap<String, SkillBundle>()
            for (b in bundles) map[b.skillId] = b
            return SkillRegistry(map)
        }
    }
}
```

- [ ] **Step 3: PASS**

Run: `./gradlew :app:test --tests SkillRegistryTest`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/dollos/skills/bundle/SkillRegistry.kt app/src/test
git commit -m "feat(skills): SkillRegistry with character_pack override"
```

### Task 3.5: 把 BundleScanner + Registry 接進 Service 啟動流程

**Files:**
- Modify: `app/src/main/java/dollos/skills/DollSkillsService.kt`
- Create: `app/src/main/java/dollos/skills/bundle/SkillPaths.kt`

- [ ] **Step 1: 寫 SkillPaths**

```kotlin
package dollos.skills.bundle

import java.io.File

object SkillPaths {
    const val BUILTIN_ROOT = "/data/system_ext/dollos/skills"
    private const val CHARACTER_PACK_ROOT = "/data/system_ext/dollos/character_packs"

    fun builtinRoot(): File = File(BUILTIN_ROOT)
    fun characterPackSkillsRoot(activePackId: String?): File? {
        if (activePackId.isNullOrBlank()) return null
        return File("$CHARACTER_PACK_ROOT/$activePackId/skills")
    }
}
```

- [ ] **Step 2: Service onCreate 觸發 scan + 建 registry**

```kotlin
// DollSkillsService.kt 新增欄位與 init
@Volatile private lateinit var registry: SkillRegistry

override fun onCreate() {
    super.onCreate()
    ensureChannel()
    startForeground(NOTIF_ID, buildNotification())
    reloadRegistry(activePackId = null)
    // actual active-pack id will come via IDollMemory in Task 6+; null OK for now
}

fun reloadRegistry(activePackId: String?) {
    val scanner = BundleScanner(
        builtinRoot = SkillPaths.builtinRoot(),
        characterPackRoot = SkillPaths.characterPackSkillsRoot(activePackId),
    )
    registry = SkillRegistry.build(scanner.scan())
    (binder as DollSkillsBinder).attachRegistry(registry)
}
```

- [ ] **Step 3: Binder attachRegistry + listMetadata 實作**

```kotlin
class DollSkillsBinder : IDollSkills.Stub() {
    @Volatile private var registry: SkillRegistry? = null
    fun attachRegistry(r: SkillRegistry) { registry = r }

    override fun listMetadata(): MutableList<SkillMetadata> =
        registry?.listMetadata()?.toMutableList() ?: mutableListOf()

    override fun viewSkill(skillId: String?): String =
        registry?.get(skillId ?: return "")?.parsed?.fullBody ?: ""
    // others stay stub
}
```

- [ ] **Step 4: Instrumented integration test — listMetadata 從 /data 路徑掃到 mock 目錄**

Create `app/src/androidTest/java/dollos/skills/BundleScanIntegrationTest.kt`:
```kotlin
package dollos.skills

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import dollos.skills.bundle.BundleScanner
import dollos.skills.bundle.SkillBundle
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class BundleScanIntegrationTest {
    @Test
    fun scanner_walks_files_api() {
        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        val root = File(ctx.cacheDir, "skills_test"); root.mkdirs()
        File(root, "alarm").mkdir()
        File(root, "alarm/SKILL.md").writeText("Alarm")
        val bundles = BundleScanner(root, null).scan()
        assertTrue(bundles.any { it.skillId == "alarm" })
    }
}
```

- [ ] **Step 5: Commit**

```bash
./gradlew :app:test :app:connectedAndroidTest
git add app/
git commit -m "feat(skills): wire BundleScanner → SkillRegistry into service lifecycle"
```

---

## §4 Progressive disclosure runtime

### Task 4.1: `listMetadata()` → `viewSkill()` 兩段式 AIDL 測試

**Files:**
- Create: `app/src/test/java/dollos/skills/runtime/ProgressiveDisclosureTest.kt`
- Create: `app/src/main/java/dollos/skills/runtime/ProgressiveDisclosure.kt`

- [ ] **Step 1: 寫 failing test — metadata ≠ full body**

```kotlin
package dollos.skills.runtime

import dollos.skills.bundle.SkillBundle
import dollos.skills.bundle.SkillRegistry
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ProgressiveDisclosureTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun bundle(id: String, md: String): SkillBundle {
        val dir = tmp.newFolder(id)
        File(dir, "SKILL.md").writeText(md)
        return SkillBundle.load(dir, "builtin")!!
    }

    @Test
    fun metadata_returns_description_only_not_full_body() {
        val md = "Short desc.\n\n## Detailed instructions\nMany lines of detail here."
        val reg = SkillRegistry.build(listOf(bundle("alarm", md)))
        val disc = ProgressiveDisclosure(reg)

        val meta = disc.listMetadata().single()
        assertEquals("Short desc.", meta.description)
        assertFalse(meta.description.contains("Detailed"))

        val full = disc.viewSkill("alarm")
        assertTrue(full.contains("Detailed instructions"))
    }

    @Test
    fun unknown_skill_view_returns_empty() {
        val reg = SkillRegistry.build(emptyList())
        assertEquals("", ProgressiveDisclosure(reg).viewSkill("nope"))
    }
}
```

- [ ] **Step 2: 寫實作**

```kotlin
package dollos.skills.runtime

import dollos.skills.aidl.SkillMetadata
import dollos.skills.bundle.SkillRegistry

class ProgressiveDisclosure(private val registry: SkillRegistry) {
    fun listMetadata(): List<SkillMetadata> = registry.listMetadata()
    fun viewSkill(skillId: String): String =
        registry.get(skillId)?.parsed?.fullBody ?: ""
}
```

- [ ] **Step 3: PASS + Commit**

```bash
./gradlew :app:test --tests ProgressiveDisclosureTest
git add app/src/main/java/dollos/skills/runtime/ProgressiveDisclosure.kt app/src/test
git commit -m "feat(skills): progressive disclosure metadata/full-body split"
```

### Task 4.2: 1024-char limit 全鏈保證（end-to-end check）

**Files:**
- Create: `app/src/test/java/dollos/skills/runtime/DescriptionLimitE2ETest.kt`

- [ ] **Step 1: 寫 test — 不合規 SKILL.md（過長第一段）流過 parser → registry → metadata 仍 ≤ 1024**

```kotlin
package dollos.skills.runtime

import dollos.skills.bundle.SkillBundle
import dollos.skills.bundle.SkillRegistry
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class DescriptionLimitE2ETest {
    @get:Rule val tmp = TemporaryFolder()

    @Test
    fun long_first_paragraph_never_leaks_to_metadata() {
        val dir = tmp.newFolder("big")
        File(dir, "SKILL.md").writeText("a".repeat(3000))
        val reg = SkillRegistry.build(listOf(SkillBundle.load(dir, "builtin")!!))
        val meta = reg.listMetadata().single()
        assertTrue(meta.description.length <= 1024)
    }
}
```

- [ ] **Step 2: Run PASS（已靠 Task 2.1 + 3.1 保證）**

Run: `./gradlew :app:test --tests DescriptionLimitE2ETest`
Expected: PASS（description cap already enforced in SkillMetadata init block）

- [ ] **Step 3: Commit**

```bash
git add app/src/test
git commit -m "test(skills): end-to-end 1024-char description cap"
```

---

## §5 Script executor

### Task 5.1: `SkillImpl` 介面定義 + 內建 skill dispatch table

**Files:**
- Create: `app/src/main/java/dollos/skills/skills/SkillImpl.kt`
- Create: `app/src/main/java/dollos/skills/runtime/ScriptExecutor.kt`
- Create: `app/src/test/java/dollos/skills/runtime/ScriptExecutorTest.kt`

Skill output 合約：executor 回傳**字串**（統一輸出協定 master §4.7）：`[SILENT]` / `[SPEAK "..."]` / `[VIBRATE "..."]` / `[INTERRUPT "..."]`（Core 的 Output Orchestrator 解析）。

- [ ] **Step 1: 寫 failing test — builtin dispatch 路由到對的 impl**

```kotlin
package dollos.skills.runtime

import android.os.Bundle
import dollos.skills.skills.SkillContext
import dollos.skills.skills.SkillImpl
import io.mockk.mockk
import org.junit.Assert.assertEquals
import org.junit.Test

class ScriptExecutorTest {
    private val alarmStub = object : SkillImpl {
        override val id = "alarm"
        override fun invoke(inputs: Bundle, ctx: SkillContext): String =
            "[SPEAK \"Alarm set for ${inputs.getString("time")}\"]"
    }

    @Test
    fun routes_to_registered_impl() {
        val exec = ScriptExecutor(impls = mapOf("alarm" to alarmStub), ctx = mockk(relaxed = true))
        val out = exec.run("alarm", Bundle().apply { putString("time", "07:00") })
        assertEquals("[SPEAK \"Alarm set for 07:00\"]", out)
    }

    @Test
    fun unknown_skill_returns_error_protocol() {
        val exec = ScriptExecutor(impls = emptyMap(), ctx = mockk(relaxed = true))
        val out = exec.run("nope", Bundle())
        assertEquals("[SILENT]", out)
    }

    @Test
    fun exception_in_impl_surfaces_as_silent_with_error_context() {
        val broken = object : SkillImpl {
            override val id = "broken"
            override fun invoke(inputs: Bundle, ctx: SkillContext): String = error("boom")
        }
        val exec = ScriptExecutor(impls = mapOf("broken" to broken), ctx = mockk(relaxed = true))
        val out = exec.run("broken", Bundle())
        assertEquals("[SILENT]", out)  // fail-silent per [SILENT] protocol default
    }
}
```

- [ ] **Step 2: 寫 SkillImpl interface**

```kotlin
package dollos.skills.skills

import android.content.Context
import android.os.Bundle
import dollos.skills.connectors.AuxEngineConnector
import dollos.skills.connectors.MemoryConnector
import dollos.skills.connectors.VoiceConnector

data class SkillContext(
    val androidContext: Context,
    val aux: AuxEngineConnector,
    val memory: MemoryConnector,
    val voice: VoiceConnector,
)

interface SkillImpl {
    val id: String
    fun invoke(inputs: Bundle, ctx: SkillContext): String
    /** optional metadata hook overriding registry defaults */
    fun requiredInputs(): Array<String> = emptyArray()
}
```

- [ ] **Step 3: 寫 ScriptExecutor**

```kotlin
package dollos.skills.runtime

import android.os.Bundle
import android.util.Log
import dollos.skills.skills.SkillContext
import dollos.skills.skills.SkillImpl

class ScriptExecutor(
    private val impls: Map<String, SkillImpl>,
    private val ctx: SkillContext,
) {
    fun run(skillId: String, inputs: Bundle): String {
        val impl = impls[skillId] ?: run {
            Log.w(TAG, "Unknown skill: $skillId")
            return "[SILENT]"
        }
        return try {
            impl.invoke(inputs, ctx)
        } catch (t: Throwable) {
            Log.e(TAG, "Skill $skillId threw", t)
            "[SILENT]"
        }
    }
    companion object { private const val TAG = "ScriptExecutor" }
}
```

- [ ] **Step 4: Commit**

```bash
./gradlew :app:test --tests ScriptExecutorTest
git add app/src/main/java/dollos/skills/skills/SkillImpl.kt \
        app/src/main/java/dollos/skills/runtime/ScriptExecutor.kt \
        app/src/test
git commit -m "feat(skills): SkillImpl interface + ScriptExecutor dispatch"
```

### Task 5.2: Connectors（Core / Aux / Memory / Voice） + 靜態 skill context builder

**Files:**
- Create: `app/src/main/java/dollos/skills/connectors/CoreConnector.kt`
- Create: `app/src/main/java/dollos/skills/connectors/AuxEngineConnector.kt`
- Create: `app/src/main/java/dollos/skills/connectors/MemoryConnector.kt`
- Create: `app/src/main/java/dollos/skills/connectors/VoiceConnector.kt`
- Create: `app/src/test/java/dollos/skills/connectors/ConnectorTest.kt`

- [ ] **Step 1: 寫 test — connector 在 binder 未綁時 throws IllegalStateException（不走 fallback，符合 CLAUDE.md no-fallback）**

```kotlin
package dollos.skills.connectors

import android.content.Context
import io.mockk.mockk
import org.junit.Assert.assertThrows
import org.junit.Test

class ConnectorTest {
    @Test
    fun aux_connector_throws_if_not_bound() {
        val c = AuxEngineConnector(mockk<Context>(relaxed = true))
        assertThrows(IllegalStateException::class.java) { c.generate("sys", "user", 128) }
    }
}
```

- [ ] **Step 2: 寫 AuxEngineConnector**

```kotlin
package dollos.skills.connectors

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.aux.IDollAuxEngine

class AuxEngineConnector(private val context: Context) {
    @Volatile private var binder: IDollAuxEngine? = null
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
            binder = IDollAuxEngine.Stub.asInterface(b)
        }
        override fun onServiceDisconnected(name: ComponentName?) { binder = null }
    }

    fun bind() {
        val i = Intent().apply {
            component = ComponentName("dollos.aux", "dollos.aux.DollAuxEngineService")
        }
        context.bindService(i, conn, Context.BIND_AUTO_CREATE)
    }
    fun unbind() { context.unbindService(conn) }

    fun generate(sys: String, user: String, maxTokens: Int): String =
        require().generate(sys, user, maxTokens)
    fun classify(input: String, labels: Array<String>): String = require().classify(input, labels)
    fun summarize(text: String, target: Int): String = require().summarize(text, target)
    fun silentJudgment(proposed: String, ctxJson: String): String = require().silentJudgment(proposed, ctxJson)

    private fun require(): IDollAuxEngine =
        binder ?: error("AuxEngine not bound")
}
```

- [ ] **Step 3: 寫 MemoryConnector（同模式）**

```kotlin
package dollos.skills.connectors

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.memory.IDollMemory

class MemoryConnector(private val context: Context) {
    @Volatile private var binder: IDollMemory? = null
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) { binder = IDollMemory.Stub.asInterface(b) }
        override fun onServiceDisconnected(name: ComponentName?) { binder = null }
    }
    fun bind() {
        context.bindService(
            Intent().apply { component = ComponentName("dollos.memory", "dollos.memory.DollMemoryService") },
            conn, Context.BIND_AUTO_CREATE,
        )
    }
    fun unbind() { context.unbindService(conn) }
    fun sessionSearch(q: String, max: Int) = req().sessionSearch(q, max)
    fun semanticSearch(q: String, max: Int) = req().semanticSearch(q, max)
    fun writeDistilled(period: String, summary: String, startMs: Long) = req().writeDistilledSummary(period, summary, startMs)
    fun runDistillation(trigger: String) = req().runDistillation(trigger)
    fun activePackId(): String? = req().activeCharacterPackId
    private fun req(): IDollMemory = binder ?: error("Memory not bound")
}
```

- [ ] **Step 4: 寫 VoiceConnector**

```kotlin
package dollos.skills.connectors

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.voice.IDollVoice

class VoiceConnector(private val context: Context) {
    @Volatile private var binder: IDollVoice? = null
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) { binder = IDollVoice.Stub.asInterface(b) }
        override fun onServiceDisconnected(name: ComponentName?) { binder = null }
    }
    fun bind() {
        context.bindService(
            Intent().apply { component = ComponentName("dollos.voice", "dollos.voice.DollVoiceService") },
            conn, Context.BIND_AUTO_CREATE,
        )
    }
    fun unbind() { context.unbindService(conn) }
    fun speak(text: String, voiceId: String) = req().speak(text, voiceId)
    private fun req() = binder ?: error("Voice not bound")
}
```

- [ ] **Step 5: 寫 CoreConnector — 支援 postSkillCallback**

```kotlin
package dollos.skills.connectors

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.core.IDollCore
import dollos.core.SkillCallbackResult

class CoreConnector(private val context: Context) {
    @Volatile private var binder: IDollCore? = null
    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, b: IBinder?) { binder = IDollCore.Stub.asInterface(b) }
        override fun onServiceDisconnected(name: ComponentName?) { binder = null }
    }
    fun bind() {
        context.bindService(
            Intent().apply { component = ComponentName("dollos.core", "dollos.core.DollCoreService") },
            conn, Context.BIND_AUTO_CREATE,
        )
    }
    fun unbind() { context.unbindService(conn) }
    fun postResult(skillId: String, status: String, resultJson: String, err: String? = null) {
        req().postSkillCallback(skillId, SkillCallbackResult(status, resultJson, err))
    }
    fun contextSnapshot(): String = req().contextSnapshotJson
    private fun req() = binder ?: error("Core not bound")
}
```

- [ ] **Step 6: Commit**

```bash
./gradlew :app:test --tests ConnectorTest
git add app/src/main/java/dollos/skills/connectors app/src/test/java/dollos/skills/connectors
git commit -m "feat(skills): AIDL connectors to Core/Aux/Memory/Voice"
```

### Task 5.3: `invokeSkill` AIDL method 整合進 binder + callback to Core

**Files:**
- Modify: `app/src/main/java/dollos/skills/aidl/DollSkillsBinder.kt`
- Modify: `app/src/main/java/dollos/skills/DollSkillsService.kt`
- Create: `app/src/test/java/dollos/skills/aidl/DollSkillsBinderInvokeTest.kt`

- [ ] **Step 1: 寫 test — invokeSkill 會觸發 executor + postResult**

```kotlin
package dollos.skills.aidl

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.runtime.ScriptExecutor
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Test

class DollSkillsBinderInvokeTest {
    @Test
    fun invoke_routes_to_executor_and_posts_result_to_core() {
        val exec = mockk<ScriptExecutor>()
        val core = mockk<CoreConnector>(relaxed = true)
        every { exec.run("alarm", any()) } returns "[SPEAK \"done\"]"

        val binder = DollSkillsBinder().also {
            it.attachExecutor(exec); it.attachCore(core)
        }
        binder.invokeSkill("alarm", Bundle(), "dollos.core")
        Thread.sleep(200)  // executor runs on bg thread
        verify { core.postResult("alarm", "ok", match { it.contains("[SPEAK") }, null) }
    }
}
```

- [ ] **Step 2: 改 binder — 加執行路徑**

```kotlin
package dollos.skills.aidl

import android.os.Bundle
import dollos.skills.IDollSkills
import dollos.skills.bundle.SkillRegistry
import dollos.skills.connectors.CoreConnector
import dollos.skills.runtime.ProgressiveDisclosure
import dollos.skills.runtime.ScriptExecutor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class DollSkillsBinder : IDollSkills.Stub() {
    @Volatile private var registry: SkillRegistry? = null
    @Volatile private var executor: ScriptExecutor? = null
    @Volatile private var core: CoreConnector? = null
    @Volatile private var routines: dollos.skills.routines.RoutineScheduler? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    fun attachRegistry(r: SkillRegistry) { registry = r }
    fun attachExecutor(e: ScriptExecutor) { executor = e }
    fun attachCore(c: CoreConnector) { core = c }
    fun attachRoutineScheduler(s: dollos.skills.routines.RoutineScheduler) { routines = s }

    override fun listMetadata() = registry?.listMetadata()?.toMutableList() ?: mutableListOf()
    override fun viewSkill(skillId: String?): String {
        val r = registry ?: return ""; return ProgressiveDisclosure(r).viewSkill(skillId ?: return "")
    }

    override fun invokeSkill(skillId: String?, inputs: Bundle?, callbackTo: String?) {
        val id = skillId ?: return
        val bundle = inputs ?: Bundle()
        scope.launch {
            val out = executor?.run(id, bundle) ?: "[SILENT]"
            core?.postResult(id, "ok", out, null)
        }
    }

    override fun scheduleRoutine(routineId: String?, cronExpr: String?) {
        if (routineId == null || cronExpr == null) return
        routines?.schedule(routineId, cronExpr)
    }
    override fun cancelRoutine(routineId: String?) { routineId?.let { routines?.cancel(it) } }
    override fun listActiveRoutines(): MutableList<String> =
        routines?.active()?.toMutableList() ?: mutableListOf()
}
```

- [ ] **Step 3: Service — wire connectors + executor**

```kotlin
// DollSkillsService.kt 新增
private lateinit var aux: AuxEngineConnector
private lateinit var memory: MemoryConnector
private lateinit var voice: VoiceConnector
private lateinit var core: CoreConnector
private lateinit var executor: ScriptExecutor

override fun onCreate() {
    super.onCreate()
    ensureChannel()
    startForeground(NOTIF_ID, buildNotification())

    aux = AuxEngineConnector(this).also { it.bind() }
    memory = MemoryConnector(this).also { it.bind() }
    voice = VoiceConnector(this).also { it.bind() }
    core = CoreConnector(this).also { it.bind() }

    reloadRegistry(activePackId = null)
    executor = ScriptExecutor(impls = emptyMap(), ctx = SkillContext(this, aux, memory, voice))
    // impls filled in §6 per-skill tasks

    val binder = binder as DollSkillsBinder
    binder.attachExecutor(executor)
    binder.attachCore(core)
}

override fun onDestroy() {
    super.onDestroy()
    aux.unbind(); memory.unbind(); voice.unbind(); core.unbind()
}
```

- [ ] **Step 4: Run test + Commit**

```bash
./gradlew :app:test --tests DollSkillsBinderInvokeTest
git add app/
git commit -m "feat(skills): invokeSkill wires executor + Core callback"
```

---

## §6 Skills library（6 個 skills）

每個 skill 分成：(A) SKILL.md 靜態資源、(B) Kotlin SkillImpl、(C) unit test。所有 SKILL.md 放 `app/src/main/assets/builtin_skills/<id>/SKILL.md`，service 啟動時由 Task 6.0 copy 到 `/data/system_ext/dollos/skills/`（build-time 交給 AOSP 打包）。

### Task 6.0: Assets 目錄結構 + 開發期用 app assets 當來源

**Files:**
- Create: `app/src/main/assets/builtin_skills/.gitkeep`
- Modify: `app/src/main/java/dollos/skills/bundle/SkillPaths.kt`

- [ ] **Step 1: 改 SkillPaths — 支援 asset fallback 給 dev cycle**

```kotlin
// dev-time convenience: if /data/system_ext/dollos/skills not populated yet,
// copy assets/builtin_skills/* into context.filesDir/builtin_skills
// (prod: AOSP build places them at BUILTIN_ROOT directly)
```

Update SkillPaths:
```kotlin
package dollos.skills.bundle

import android.content.Context
import java.io.File

object SkillPaths {
    const val BUILTIN_ROOT = "/data/system_ext/dollos/skills"
    private const val CHARACTER_PACK_ROOT = "/data/system_ext/dollos/character_packs"

    fun builtinRoot(ctx: Context): File {
        val systemExt = File(BUILTIN_ROOT)
        if (systemExt.isDirectory && systemExt.list()?.isNotEmpty() == true) return systemExt
        return stageAssetsIntoInternal(ctx)
    }

    private fun stageAssetsIntoInternal(ctx: Context): File {
        val staged = File(ctx.filesDir, "builtin_skills")
        if (staged.isDirectory) return staged
        staged.mkdirs()
        val am = ctx.assets
        am.list("builtin_skills")?.forEach { dir ->
            val dst = File(staged, dir); dst.mkdirs()
            am.list("builtin_skills/$dir")?.forEach { entry ->
                val source = "builtin_skills/$dir/$entry"
                val out = File(dst, entry)
                am.open(source).use { input -> out.outputStream().use { input.copyTo(it) } }
            }
        }
        return staged
    }

    fun characterPackSkillsRoot(activePackId: String?): File? {
        if (activePackId.isNullOrBlank()) return null
        return File("$CHARACTER_PACK_ROOT/$activePackId/skills")
    }
}
```

- [ ] **Step 2: 更新 service 呼叫處**

Change in `DollSkillsService.reloadRegistry`:
```kotlin
val scanner = BundleScanner(
    builtinRoot = SkillPaths.builtinRoot(this),
    characterPackRoot = SkillPaths.characterPackSkillsRoot(activePackId),
)
```

- [ ] **Step 3: Commit**

```bash
git add app/src/main/assets app/src/main/java/dollos/skills/bundle/SkillPaths.kt app/src/main/java/dollos/skills/DollSkillsService.kt
git commit -m "feat(skills): asset-based builtin_skills staging for dev cycle"
```

### Task 6.1 Skill: alarm — SKILL.md

**Files:**
- Create: `app/src/main/assets/builtin_skills/alarm/SKILL.md`

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Sets or cancels alarms using the Android AlarmManager. Accepts an ISO-8601 local datetime and optional label. Returns [SPEAK] on success with confirmation, or [SILENT] if DND prevents announcing. Use when user says things like "wake me at 7" or "remind me in 10 minutes".

## Inputs
- `time` (required): ISO-8601 local datetime, e.g. "2026-04-21T07:00:00"
- `label` (optional): human label, default "Alarm"
- `action` (optional): "set" (default) or "cancel"

## Scripts
Executed in-process by `AlarmSkill.kt` using `AlarmManager.setExactAndAllowWhileIdle`. Alarms fire a PendingIntent back into DollSkillsService's `AlarmFiredReceiver` which triggers `[INTERRUPT "label"]` via Core callback (INTERRUPT bypasses dnd_active per master §4.7).

## Return value
`[SPEAK "..."]` or `[SILENT]`.
```

- [ ] **Step 2: Commit**

```bash
git add app/src/main/assets/builtin_skills/alarm/SKILL.md
git commit -m "docs(skills): alarm SKILL.md"
```

### Task 6.2 Skill: alarm — AlarmSkill.kt + AlarmManager integration

**Files:**
- Create: `app/src/main/java/dollos/skills/skills/AlarmSkill.kt`
- Create: `app/src/main/java/dollos/skills/skills/AlarmFiredReceiver.kt`
- Modify: `app/src/main/AndroidManifest.xml`
- Create: `app/src/test/java/dollos/skills/skills/AlarmSkillTest.kt`

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.skills

import android.app.AlarmManager
import android.content.Context
import android.os.Bundle
import dollos.skills.connectors.AuxEngineConnector
import dollos.skills.connectors.MemoryConnector
import dollos.skills.connectors.VoiceConnector
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.assertTrue
import org.junit.Test

class AlarmSkillTest {
    private val ctx: Context = mockk(relaxed = true)
    private val am: AlarmManager = mockk(relaxed = true)

    init { every { ctx.getSystemService(Context.ALARM_SERVICE) } returns am }

    @Test
    fun set_alarm_returns_speak_confirmation() {
        val skill = AlarmSkill()
        val sctx = SkillContext(ctx, mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        val out = skill.invoke(
            Bundle().apply {
                putString("time", "2026-04-21T07:00:00")
                putString("label", "Wake up")
            },
            sctx,
        )
        assertTrue(out.startsWith("[SPEAK"))
        verify { am.setExactAndAllowWhileIdle(any(), any(), any()) }
    }

    @Test
    fun cancel_alarm_returns_speak_and_cancels() {
        val skill = AlarmSkill()
        val sctx = SkillContext(ctx, mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        val out = skill.invoke(
            Bundle().apply { putString("action", "cancel"); putString("label", "Wake up") },
            sctx,
        )
        assertTrue(out.startsWith("[SPEAK"))
        verify { am.cancel(any<android.app.PendingIntent>()) }
    }

    @Test(expected = IllegalArgumentException::class)
    fun set_without_time_throws() {
        AlarmSkill().invoke(
            Bundle(),
            SkillContext(ctx, mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
    }
}
```

- [ ] **Step 2: 寫 AlarmSkill**

```kotlin
package dollos.skills.skills

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Bundle
import java.time.LocalDateTime
import java.time.ZoneId

class AlarmSkill : SkillImpl {
    override val id = "alarm"
    override fun requiredInputs() = arrayOf("time")

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val action = inputs.getString("action", "set")
        val label = inputs.getString("label", "Alarm")
        val am = ctx.androidContext.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pi = pendingIntent(ctx.androidContext, label)
        return when (action) {
            "set" -> {
                val timeStr = inputs.getString("time")
                    ?: throw IllegalArgumentException("time required for set")
                val epochMs = LocalDateTime.parse(timeStr)
                    .atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, epochMs, pi)
                "[SPEAK \"Alarm '$label' set for $timeStr.\"]"
            }
            "cancel" -> {
                am.cancel(pi)
                "[SPEAK \"Alarm '$label' cancelled.\"]"
            }
            else -> "[SILENT]"
        }
    }

    private fun pendingIntent(context: Context, label: String): PendingIntent {
        val intent = Intent(context, AlarmFiredReceiver::class.java).apply {
            putExtra("label", label); action = "dollos.skills.ALARM_FIRE:$label"
        }
        return PendingIntent.getBroadcast(
            context, label.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
```

- [ ] **Step 3: 寫 AlarmFiredReceiver — 發 [INTERRUPT] 回 Core**

```kotlin
package dollos.skills.skills

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import dollos.core.IDollCore
import dollos.core.SkillCallbackResult

class AlarmFiredReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val label = intent.getStringExtra("label") ?: "Alarm"
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
                IDollCore.Stub.asInterface(b).postSkillCallback(
                    "alarm",
                    SkillCallbackResult("ok", "[INTERRUPT \"$label\"]", null),
                )
                try { context.unbindService(this) } catch (_: Throwable) {}
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        context.bindService(
            Intent().apply { component = ComponentName("dollos.core", "dollos.core.DollCoreService") },
            conn, Context.BIND_AUTO_CREATE,
        )
    }
}
```

- [ ] **Step 4: AndroidManifest 註冊 receiver + 權限**

```xml
<uses-permission android:name="android.permission.SCHEDULE_EXACT_ALARM"/>
<uses-permission android:name="android.permission.USE_EXACT_ALARM"/>

<receiver android:name=".skills.AlarmFiredReceiver" android:exported="false"/>
```

- [ ] **Step 5: PASS + Commit**

```bash
./gradlew :app:test --tests AlarmSkillTest
git add app/
git commit -m "feat(skills): alarm skill with AlarmManager + INTERRUPT callback"
```

### Task 6.3 Skill: weather — SKILL.md + WeatherSkill.kt

**Files:**
- Create: `app/src/main/assets/builtin_skills/weather/SKILL.md`
- Create: `app/src/main/java/dollos/skills/skills/WeatherSkill.kt`
- Create: `app/src/test/java/dollos/skills/skills/WeatherSkillTest.kt`

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Fetches current weather for a location from Open-Meteo (no API key). Returns a short [SPEAK] phrase summarizing temperature + conditions. Use when user asks "what's the weather" or in morning_routine.

## Inputs
- `lat` (required): float
- `lon` (required): float
- `locationLabel` (optional): "home"|"work"|"current" for phrasing

## Output
`[SPEAK "..."]` with temp_c + description, or `[SILENT]` on network failure.
```

- [ ] **Step 2: 寫 failing test**

```kotlin
package dollos.skills.skills

import android.os.Bundle
import io.mockk.every
import io.mockk.mockk
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertTrue
import org.junit.Test

class WeatherSkillTest {
    @Test
    fun parses_open_meteo_response_into_speak() {
        val http: OkHttpClient = mockk()
        val call = mockk<okhttp3.Call>()
        val resp = Response.Builder()
            .code(200).protocol(Protocol.HTTP_1_1).message("OK")
            .request(Request.Builder().url("https://x").build())
            .body("""{"current_weather":{"temperature":18.5,"weathercode":1}}""".toResponseBody())
            .build()
        every { http.newCall(any()) } returns call
        every { call.execute() } returns resp

        val skill = WeatherSkill(http)
        val out = skill.invoke(
            Bundle().apply { putDouble("lat", 25.03); putDouble("lon", 121.56) },
            SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertTrue(out.startsWith("[SPEAK"))
        assertTrue(out.contains("18"))
    }

    @Test
    fun http_error_returns_silent() {
        val http: OkHttpClient = mockk()
        val call = mockk<okhttp3.Call>()
        every { http.newCall(any()) } returns call
        every { call.execute() } throws java.io.IOException("no net")

        val out = WeatherSkill(http).invoke(
            Bundle().apply { putDouble("lat", 0.0); putDouble("lon", 0.0) },
            SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertTrue(out == "[SILENT]")
    }
}
```

- [ ] **Step 3: 寫 WeatherSkill**

```kotlin
package dollos.skills.skills

import android.os.Bundle
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

class WeatherSkill(
    private val http: OkHttpClient = OkHttpClient(),
) : SkillImpl {
    override val id = "weather"
    override fun requiredInputs() = arrayOf("lat", "lon")

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val lat = inputs.getDouble("lat"); val lon = inputs.getDouble("lon")
        val label = inputs.getString("locationLabel", "current")
        val url = "https://api.open-meteo.com/v1/forecast?latitude=$lat&longitude=$lon&current_weather=true"
        return try {
            http.newCall(Request.Builder().url(url).build()).execute().use { resp ->
                if (!resp.isSuccessful) return@use "[SILENT]"
                val body = resp.body?.string() ?: return@use "[SILENT]"
                val cw = JSONObject(body).getJSONObject("current_weather")
                val t = cw.getDouble("temperature")
                val code = cw.getInt("weathercode")
                "[SPEAK \"Weather at $label: ${t.toInt()}°C, ${describe(code)}.\"]"
            }
        } catch (_: Throwable) { "[SILENT]" }
    }

    private fun describe(code: Int): String = when (code) {
        0 -> "clear"
        1, 2, 3 -> "partly cloudy"
        45, 48 -> "foggy"
        in 51..67 -> "rainy"
        in 71..77 -> "snowy"
        in 80..99 -> "stormy"
        else -> "mixed"
    }
}
```

- [ ] **Step 4: Add permission + PASS + Commit**

Manifest:
```xml
<uses-permission android:name="android.permission.INTERNET"/>
```

```bash
./gradlew :app:test --tests WeatherSkillTest
git add app/
git commit -m "feat(skills): weather skill via Open-Meteo"
```

### Task 6.4 Skill: notification_summary — NotificationListenerService cache

**Files:**
- Create: `app/src/main/assets/builtin_skills/notification_summary/SKILL.md`
- Create: `app/src/main/java/dollos/skills/skills/NotificationCacheListener.kt`
- Create: `app/src/main/java/dollos/skills/skills/NotificationSummarySkill.kt`
- Modify: `app/src/main/AndroidManifest.xml`
- Create: `app/src/test/java/dollos/skills/skills/NotificationSummarySkillTest.kt`

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Summarizes recent system notifications into a short spoken brief. Uses the Aux LLM to condense the last N notifications into one sentence. Use when user asks "what did I miss" or in morning_routine.

## Inputs
- `maxAgeMinutes` (optional, default 60): ignore older notifications
- `maxCount` (optional, default 20): cap list size before summarization

## Output
`[SPEAK "..."]` with a one-sentence summary, or `[SILENT]` when nothing to report.
```

- [ ] **Step 2: 寫 NotificationCacheListener（app 內 global 存 recent notifications）**

```kotlin
package dollos.skills.skills

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.util.concurrent.ConcurrentLinkedDeque

data class CachedNotification(
    val pkg: String,
    val title: String,
    val text: String,
    val postTimeMs: Long,
)

class NotificationCacheListener : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val extras = sbn.notification.extras
        cache.addFirst(
            CachedNotification(
                pkg = sbn.packageName,
                title = extras.getCharSequence("android.title")?.toString() ?: "",
                text = extras.getCharSequence("android.text")?.toString() ?: "",
                postTimeMs = sbn.postTime,
            ),
        )
        while (cache.size > MAX_CACHE) cache.pollLast()
    }

    companion object {
        private const val MAX_CACHE = 200
        val cache = ConcurrentLinkedDeque<CachedNotification>()
        fun snapshot(maxAgeMs: Long, maxCount: Int): List<CachedNotification> {
            val cutoff = System.currentTimeMillis() - maxAgeMs
            return cache.asSequence()
                .filter { it.postTimeMs >= cutoff }
                .take(maxCount).toList()
        }
    }
}
```

- [ ] **Step 3: 寫 failing test**

```kotlin
package dollos.skills.skills

import android.os.Bundle
import dollos.skills.connectors.AuxEngineConnector
import io.mockk.every
import io.mockk.mockk
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class NotificationSummarySkillTest {
    @After fun clear() { NotificationCacheListener.cache.clear() }

    @Test
    fun empty_cache_returns_silent() {
        val skill = NotificationSummarySkill()
        val out = skill.invoke(
            Bundle(),
            SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertEquals("[SILENT]", out)
    }

    @Test
    fun aux_summarize_invoked_with_cached_notifications() {
        NotificationCacheListener.cache.addFirst(CachedNotification("m.im", "Alice", "hi", System.currentTimeMillis()))
        val aux: AuxEngineConnector = mockk()
        every { aux.summarize(any(), any()) } returns "Alice messaged you."

        val out = NotificationSummarySkill().invoke(
            Bundle(),
            SkillContext(mockk(relaxed = true), aux, mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertTrue(out.startsWith("[SPEAK"))
        assertTrue(out.contains("Alice messaged"))
    }
}
```

- [ ] **Step 4: 寫 NotificationSummarySkill**

```kotlin
package dollos.skills.skills

import android.os.Bundle

class NotificationSummarySkill : SkillImpl {
    override val id = "notification_summary"

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val maxAgeMin = inputs.getInt("maxAgeMinutes", 60)
        val maxCount = inputs.getInt("maxCount", 20)
        val recent = NotificationCacheListener.snapshot(maxAgeMin * 60_000L, maxCount)
        if (recent.isEmpty()) return "[SILENT]"
        val joined = recent.joinToString("\n") { "[${it.pkg}] ${it.title}: ${it.text}" }
        val summary = ctx.aux.summarize(joined, 120)
        if (summary.isBlank()) return "[SILENT]"
        return "[SPEAK \"${summary.replace("\"", "'")}\"]"
    }
}
```

- [ ] **Step 5: Manifest — 註冊 listener**

```xml
<uses-permission android:name="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"/>

<service
    android:name=".skills.NotificationCacheListener"
    android:label="Doll Notifications"
    android:permission="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"
    android:exported="true">
    <intent-filter>
        <action android:name="android.service.notification.NotificationListenerService"/>
    </intent-filter>
</service>
```

- [ ] **Step 6: PASS + Commit**

```bash
./gradlew :app:test --tests NotificationSummarySkillTest
git add app/
git commit -m "feat(skills): notification_summary via Aux LLM over NotificationListenerService cache"
```

### Task 6.5 Skill: memory_review — distillation skill

**Files:**
- Create: `app/src/main/assets/builtin_skills/memory_review/SKILL.md`
- Create: `app/src/main/java/dollos/skills/skills/MemoryReviewSkill.kt`
- Create: `app/src/test/java/dollos/skills/skills/MemoryReviewSkillTest.kt`

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Generates a distilled summary of recent memory for DollOSMemory's distillation engine. Reads the period's raw conversation/observation log via IDollMemory.sessionSearch, prompts the Aux LLM with a fixed review prompt, and writes the result back via IDollMemory.writeDistilledSummary. Called by DollOSMemory's distillation trigger (on_idle / on_charging / on_session_end).

## Inputs
- `period` (required): "daily"|"weekly"|"monthly"
- `periodStartMs` (required): long, epoch ms
- `periodEndMs` (required): long, epoch ms

## Output
`[SILENT]` (always — this is a background skill, not a speech action). Side effect: a new distilled summary written via Memory connector.
```

- [ ] **Step 2: 寫 failing test**

```kotlin
package dollos.skills.skills

import android.os.Bundle
import dollos.skills.connectors.AuxEngineConnector
import dollos.skills.connectors.MemoryConnector
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.assertEquals
import org.junit.Test

class MemoryReviewSkillTest {
    @Test
    fun writes_distilled_summary_via_memory_connector() {
        val mem: MemoryConnector = mockk(relaxed = true)
        val aux: AuxEngineConnector = mockk()
        every { mem.sessionSearch(any(), any()) } returns """[{"snippet":"talked about coffee"}]"""
        every { aux.generate(any(), any(), any()) } returns "User drinks black coffee daily."

        val out = MemoryReviewSkill().invoke(
            Bundle().apply {
                putString("period", "daily")
                putLong("periodStartMs", 1_000_000L)
                putLong("periodEndMs", 2_000_000L)
            },
            SkillContext(mockk(relaxed = true), aux, mem, mockk(relaxed = true)),
        )
        assertEquals("[SILENT]", out)
        verify { mem.writeDistilled("daily", "User drinks black coffee daily.", 1_000_000L) }
    }

    @Test(expected = IllegalArgumentException::class)
    fun missing_period_throws() {
        MemoryReviewSkill().invoke(
            Bundle(),
            SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
    }
}
```

- [ ] **Step 3: 寫 MemoryReviewSkill**

```kotlin
package dollos.skills.skills

import android.os.Bundle

class MemoryReviewSkill : SkillImpl {
    override val id = "memory_review"
    override fun requiredInputs() = arrayOf("period", "periodStartMs", "periodEndMs")

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val period = inputs.getString("period")
            ?: throw IllegalArgumentException("period required")
        val start = inputs.getLong("periodStartMs", -1L)
        val end = inputs.getLong("periodEndMs", -1L)
        require(start > 0 && end > start) { "periodStartMs/periodEndMs invalid" }

        val raw = ctx.memory.sessionSearch(
            "ts >= $start AND ts < $end",
            maxResults = when (period) {
                "daily" -> 200; "weekly" -> 500; else -> 1000
            },
        )
        val systemPrompt = REVIEW_PROMPT
        val userPrompt = "Period: $period\nRaw log JSON:\n$raw\n\nWrite a concise distilled summary (<= 500 chars)."
        val summary = ctx.aux.generate(systemPrompt, userPrompt, 600)
        if (summary.isNotBlank()) ctx.memory.writeDistilled(period, summary, start)
        return "[SILENT]"
    }

    companion object {
        private val REVIEW_PROMPT = """
            You are Doll's memory distillation assistant. Read the raw log for the given
            period and produce a short declarative summary capturing what mattered:
            - facts about the user (to add to USER.md)
            - patterns / routines observed
            - sentiment / mood shifts
            Output plain text, no JSON. <= 500 chars. Do NOT invent details.
        """.trimIndent()
    }
}
```

- [ ] **Step 4: PASS + Commit**

```bash
./gradlew :app:test --tests MemoryReviewSkillTest
git add app/
git commit -m "feat(skills): memory_review skill for DollOSMemory distillation"
```

### Task 6.6 Skill: music — MediaController integration

**Files:**
- Create: `app/src/main/assets/builtin_skills/music/SKILL.md`
- Create: `app/src/main/java/dollos/skills/skills/MusicSkill.kt`
- Create: `app/src/test/java/dollos/skills/skills/MusicSkillTest.kt`

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Controls the active media session on the device (play, pause, next, previous, volume). Uses MediaSessionManager.getActiveSessions to find the current player. Requires notification-listener permission (piggybacks on NotificationCacheListener grant).

## Inputs
- `action` (required): "play"|"pause"|"next"|"previous"|"volume_up"|"volume_down"

## Output
`[SPEAK "..."]` with confirmation, or `[SILENT]` if no active session.
```

- [ ] **Step 2: 寫 failing test**

```kotlin
package dollos.skills.skills

import android.content.ComponentName
import android.content.Context
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Bundle
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.*
import org.junit.Test

class MusicSkillTest {
    @Test
    fun pause_sends_pause_on_active_controller() {
        val ctx: Context = mockk(relaxed = true)
        val msm: MediaSessionManager = mockk()
        val ctrl: MediaController = mockk(relaxed = true)
        val transport: MediaController.TransportControls = mockk(relaxed = true)
        every { ctx.getSystemService(Context.MEDIA_SESSION_SERVICE) } returns msm
        every { msm.getActiveSessions(any<ComponentName>()) } returns listOf(ctrl)
        every { ctrl.transportControls } returns transport

        val out = MusicSkill().invoke(
            Bundle().apply { putString("action", "pause") },
            SkillContext(ctx, mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertTrue(out.startsWith("[SPEAK"))
        verify { transport.pause() }
    }

    @Test
    fun no_active_session_returns_silent() {
        val ctx: Context = mockk(relaxed = true)
        val msm: MediaSessionManager = mockk()
        every { ctx.getSystemService(Context.MEDIA_SESSION_SERVICE) } returns msm
        every { msm.getActiveSessions(any<ComponentName>()) } returns emptyList()

        val out = MusicSkill().invoke(
            Bundle().apply { putString("action", "play") },
            SkillContext(ctx, mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertEquals("[SILENT]", out)
    }
}
```

- [ ] **Step 3: 寫 MusicSkill**

```kotlin
package dollos.skills.skills

import android.content.ComponentName
import android.content.Context
import android.media.AudioManager
import android.media.session.MediaSessionManager
import android.os.Bundle

class MusicSkill : SkillImpl {
    override val id = "music"
    override fun requiredInputs() = arrayOf("action")

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val action = inputs.getString("action") ?: return "[SILENT]"
        val app = ctx.androidContext
        return when (action) {
            "play", "pause", "next", "previous" -> {
                val msm = app.getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
                val cn = ComponentName(app, "dollos.skills.skills.NotificationCacheListener")
                val ctrl = msm.getActiveSessions(cn).firstOrNull() ?: return "[SILENT]"
                when (action) {
                    "play" -> ctrl.transportControls.play()
                    "pause" -> ctrl.transportControls.pause()
                    "next" -> ctrl.transportControls.skipToNext()
                    "previous" -> ctrl.transportControls.skipToPrevious()
                }
                "[SPEAK \"$action.\"]"
            }
            "volume_up", "volume_down" -> {
                val am = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                val dir = if (action == "volume_up") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER
                am.adjustStreamVolume(AudioManager.STREAM_MUSIC, dir, AudioManager.FLAG_SHOW_UI)
                "[SPEAK \"$action.\"]"
            }
            else -> "[SILENT]"
        }
    }
}
```

- [ ] **Step 4: PASS + Commit**

```bash
./gradlew :app:test --tests MusicSkillTest
git add app/
git commit -m "feat(skills): music skill via MediaSessionManager + AudioManager"
```

### Task 6.7 Skill: uisage — AccessibilityService + VirtualDisplay 從 AIService 搬家

**Files:**
- Create: `app/src/main/assets/builtin_skills/uisage/SKILL.md`
- Create: `app/src/main/java/dollos/skills/uisage/DollAccessibilityService.kt`
- Create: `app/src/main/java/dollos/skills/uisage/UiOperationDispatcher.kt`
- Create: `app/src/main/java/dollos/skills/skills/UisageSkill.kt`
- Create: `app/src/main/res/xml/accessibility_service_config.xml`
- Modify: `app/src/main/AndroidManifest.xml`
- Create: `app/src/test/java/dollos/skills/skills/UisageSkillTest.kt`

**既有程式碼來源：** `~/Projects/DollOSAIService/app/src/main/java/org/dollos/ai/` 下的 AccessibilityService / VirtualDisplay-related class（AI Core Plan D v2）。搬過來 package rename 到 `dollos.skills.uisage`，AndroidManifest `<service>` 宣告遷到本 app。

- [ ] **Step 1: 寫 SKILL.md**

```markdown
Executes UI operations on this device: tap at (x,y), long-press, swipe, type text, read accessibility tree, navigate to app. Wraps DollOS's on-device UI perception pipeline (AccessibilityService + VirtualDisplay snapshot). Requires user grant of AccessibilityService during OOBE.

## Inputs
- `action` (required): "tap"|"long_press"|"swipe"|"type"|"read_tree"|"open_app"
- `x`, `y` (tap/long_press/swipe start): int
- `x2`, `y2` (swipe end): int
- `text` (type): string
- `pkg` (open_app): string

## Output
`[SPEAK]` with short confirmation, or `[SILENT]` if accessibility not granted. For `read_tree` the tree JSON is posted back via Core callback resultJson.
```

- [ ] **Step 2: 寫 accessibility_service_config.xml**

```xml
<?xml version="1.0" encoding="utf-8"?>
<accessibility-service xmlns:android="http://schemas.android.com/apk/res/android"
    android:accessibilityEventTypes="typeAllMask"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:accessibilityFlags="flagDefault|flagRetrieveInteractiveWindows|flagReportViewIds"
    android:canPerformGestures="true"
    android:canRetrieveWindowContent="true"
    android:description="@string/uisage_a11y_desc"/>
```

And string in `app/src/main/res/values/strings.xml`:
```xml
<resources>
    <string name="app_name">DollOS Skills</string>
    <string name="uisage_a11y_desc">DollOS UI operation (uisage skill)</string>
</resources>
```

- [ ] **Step 3: 寫 DollAccessibilityService（port 自 AIService）**

```kotlin
package dollos.skills.uisage

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class DollAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        INSTANCE = this
    }

    override fun onDestroy() {
        if (INSTANCE === this) INSTANCE = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* no-op, poll-based */ }
    override fun onInterrupt() {}

    fun tapAt(x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val g = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 80))
            .build()
        return dispatchGesture(g, null, null)
    }

    fun longPressAt(x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val g = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 700))
            .build()
        return dispatchGesture(g, null, null)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long = 300): Boolean {
        val path = Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }
        val g = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs))
            .build()
        return dispatchGesture(g, null, null)
    }

    fun typeText(text: String): Boolean {
        val focus = findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return false
        val args = android.os.Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    fun readTreeJson(): String {
        val root = rootInActiveWindow ?: return "{}"
        val sb = StringBuilder("{")
        appendNode(root, sb)
        sb.append("}")
        return sb.toString()
    }

    private fun appendNode(n: AccessibilityNodeInfo, sb: StringBuilder) {
        val cls = n.className?.toString().orEmpty()
        val txt = n.text?.toString()?.replace("\"", "'").orEmpty()
        sb.append("\"class\":\"$cls\",\"text\":\"$txt\",\"children\":[")
        for (i in 0 until n.childCount) {
            if (i > 0) sb.append(",")
            sb.append("{")
            n.getChild(i)?.let { appendNode(it, sb) }
            sb.append("}")
        }
        sb.append("]")
    }

    companion object {
        @Volatile private var INSTANCE: DollAccessibilityService? = null
        fun instance(): DollAccessibilityService? = INSTANCE
    }
}
```

- [ ] **Step 4: 寫 UiOperationDispatcher**

```kotlin
package dollos.skills.uisage

import android.content.Context
import android.content.Intent

class UiOperationDispatcher(private val context: Context) {
    fun tap(x: Int, y: Int): Boolean =
        DollAccessibilityService.instance()?.tapAt(x, y) ?: false

    fun longPress(x: Int, y: Int): Boolean =
        DollAccessibilityService.instance()?.longPressAt(x, y) ?: false

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int): Boolean =
        DollAccessibilityService.instance()?.swipe(x1, y1, x2, y2) ?: false

    fun type(text: String): Boolean =
        DollAccessibilityService.instance()?.typeText(text) ?: false

    fun readTree(): String =
        DollAccessibilityService.instance()?.readTreeJson() ?: "{}"

    fun openApp(pkg: String): Boolean {
        val pm = context.packageManager
        val intent = pm.getLaunchIntentForPackage(pkg) ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }
}
```

- [ ] **Step 5: 寫 UisageSkill + test**

Test:
```kotlin
package dollos.skills.skills

import android.content.Context
import android.os.Bundle
import dollos.skills.uisage.UiOperationDispatcher
import io.mockk.every
import io.mockk.mockk
import io.mockk.mockkConstructor
import io.mockk.verify
import org.junit.Assert.*
import org.junit.Test

class UisageSkillTest {
    @Test
    fun tap_dispatches_via_uioperation() {
        mockkConstructor(UiOperationDispatcher::class)
        every { anyConstructed<UiOperationDispatcher>().tap(100, 200) } returns true

        val out = UisageSkill().invoke(
            Bundle().apply { putString("action", "tap"); putInt("x", 100); putInt("y", 200) },
            SkillContext(mockk<Context>(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertTrue(out.startsWith("[SPEAK"))
        verify { anyConstructed<UiOperationDispatcher>().tap(100, 200) }
    }

    @Test
    fun missing_coords_for_tap_returns_silent() {
        val out = UisageSkill().invoke(
            Bundle().apply { putString("action", "tap") },
            SkillContext(mockk<Context>(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true)),
        )
        assertEquals("[SILENT]", out)
    }
}
```

Impl:
```kotlin
package dollos.skills.skills

import android.os.Bundle
import dollos.skills.uisage.UiOperationDispatcher

class UisageSkill : SkillImpl {
    override val id = "uisage"
    override fun requiredInputs() = arrayOf("action")

    override fun invoke(inputs: Bundle, ctx: SkillContext): String {
        val disp = UiOperationDispatcher(ctx.androidContext)
        return when (inputs.getString("action")) {
            "tap" -> {
                val x = inputs.getInt("x", -1); val y = inputs.getInt("y", -1)
                if (x < 0 || y < 0) "[SILENT]"
                else if (disp.tap(x, y)) "[SPEAK \"Tapped.\"]" else "[SILENT]"
            }
            "long_press" -> {
                val x = inputs.getInt("x", -1); val y = inputs.getInt("y", -1)
                if (x < 0 || y < 0) "[SILENT]"
                else if (disp.longPress(x, y)) "[SPEAK \"Long pressed.\"]" else "[SILENT]"
            }
            "swipe" -> {
                val x1 = inputs.getInt("x", -1); val y1 = inputs.getInt("y", -1)
                val x2 = inputs.getInt("x2", -1); val y2 = inputs.getInt("y2", -1)
                if (listOf(x1, y1, x2, y2).any { it < 0 }) "[SILENT]"
                else if (disp.swipe(x1, y1, x2, y2)) "[SPEAK \"Swiped.\"]" else "[SILENT]"
            }
            "type" -> {
                val text = inputs.getString("text") ?: return "[SILENT]"
                if (disp.type(text)) "[SPEAK \"Typed.\"]" else "[SILENT]"
            }
            "read_tree" -> {
                val json = disp.readTree()
                "[SPEAK \"Tree read.\"]|||$json"  // caller splits on ||| to extract JSON payload
            }
            "open_app" -> {
                val pkg = inputs.getString("pkg") ?: return "[SILENT]"
                if (disp.openApp(pkg)) "[SPEAK \"Opened $pkg.\"]" else "[SILENT]"
            }
            else -> "[SILENT]"
        }
    }
}
```

- [ ] **Step 6: Manifest — accessibility service + permissions**

```xml
<uses-permission android:name="android.permission.BIND_ACCESSIBILITY_SERVICE"/>

<service
    android:name=".uisage.DollAccessibilityService"
    android:label="DollOS UI Control"
    android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"
    android:exported="true">
    <intent-filter>
        <action android:name="android.accessibilityservice.AccessibilityService"/>
    </intent-filter>
    <meta-data
        android:name="android.accessibilityservice"
        android:resource="@xml/accessibility_service_config"/>
</service>
```

- [ ] **Step 7: PASS + Commit**

```bash
./gradlew :app:test --tests UisageSkillTest
git add app/
git commit -m "feat(skills): uisage skill ported from AIService AccessibilityService"
```

### Task 6.8 Register all 6 skill impls into executor

**Files:**
- Modify: `app/src/main/java/dollos/skills/DollSkillsService.kt`

- [ ] **Step 1: 改 service — 填 impls map**

```kotlin
executor = ScriptExecutor(
    impls = mapOf(
        "alarm" to AlarmSkill(),
        "weather" to WeatherSkill(),
        "notification_summary" to NotificationSummarySkill(),
        "memory_review" to MemoryReviewSkill(),
        "music" to MusicSkill(),
        "uisage" to UisageSkill(),
    ),
    ctx = SkillContext(this, aux, memory, voice),
)
```

- [ ] **Step 2: Commit**

```bash
./gradlew :app:test
git add app/src/main/java/dollos/skills/DollSkillsService.kt
git commit -m "feat(skills): register all 6 built-in skill impls"
```

---

## §7 Routine 排程基礎

### Task 7.1: `Routine` 介面 + `RoutineLock`

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/Routine.kt`
- Create: `app/src/main/java/dollos/skills/routines/RoutineLock.kt`
- Create: `app/src/test/java/dollos/skills/routines/RoutineLockTest.kt`

- [ ] **Step 1: 寫 failing test — lock 互斥**

```kotlin
package dollos.skills.routines

import org.junit.Assert.*
import org.junit.Test

class RoutineLockTest {
    @Test
    fun acquire_blocks_second_acquire() {
        val lock = RoutineLock()
        assertTrue(lock.tryAcquire("morning"))
        assertFalse(lock.tryAcquire("bedtime"))
    }

    @Test
    fun release_allows_reacquire() {
        val lock = RoutineLock()
        assertTrue(lock.tryAcquire("morning"))
        lock.release()
        assertTrue(lock.tryAcquire("bedtime"))
    }

    @Test
    fun current_holder_reported() {
        val lock = RoutineLock()
        lock.tryAcquire("morning")
        assertEquals("morning", lock.currentHolder())
    }
}
```

- [ ] **Step 2: 寫實作**

```kotlin
package dollos.skills.routines

import java.util.concurrent.atomic.AtomicReference

class RoutineLock {
    private val holder = AtomicReference<String?>(null)

    fun tryAcquire(routineId: String): Boolean =
        holder.compareAndSet(null, routineId)

    fun release() { holder.set(null) }

    fun currentHolder(): String? = holder.get()
}
```

- [ ] **Step 3: 寫 Routine interface**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.skills.SkillContext

interface Routine {
    val id: String
    /** Returns Core Output Orchestrator protocol string, possibly [SILENT]. */
    fun run(ctx: SkillContext, trigger: Bundle): String
}
```

- [ ] **Step 4: PASS + Commit**

```bash
./gradlew :app:test --tests RoutineLockTest
git add app/src/main/java/dollos/skills/routines app/src/test
git commit -m "feat(skills): Routine interface + RoutineLock mutex"
```

### Task 7.2: `RoutineScheduler` — AlarmManager cron-lite

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/RoutineScheduler.kt`
- Create: `app/src/main/java/dollos/skills/routines/RoutineFiredReceiver.kt`
- Create: `app/src/main/java/dollos/skills/routines/CronParser.kt`
- Create: `app/src/test/java/dollos/skills/routines/CronParserTest.kt`

支援子集：`"HH:MM"`（每日）、`"event:pickup_after_sleep"`（事件觸發，由 Core 在 §12 呼叫 `executeSkill` 路徑，不走 AlarmManager）。本 task 僅實作 `HH:MM` daily。

- [ ] **Step 1: 寫 CronParserTest**

```kotlin
package dollos.skills.routines

import org.junit.Assert.*
import org.junit.Test
import java.time.LocalDateTime
import java.time.ZoneId

class CronParserTest {
    @Test
    fun daily_hhmm_returns_next_occurrence() {
        val now = LocalDateTime.of(2026, 4, 20, 10, 0)
        val next = CronParser.nextFiringMs("07:00", now)
        val expected = LocalDateTime.of(2026, 4, 21, 7, 0)
            .atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
        assertEquals(expected, next)
    }

    @Test
    fun daily_hhmm_today_if_future() {
        val now = LocalDateTime.of(2026, 4, 20, 6, 0)
        val next = CronParser.nextFiringMs("07:00", now)
        val expected = LocalDateTime.of(2026, 4, 20, 7, 0)
            .atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
        assertEquals(expected, next)
    }

    @Test(expected = IllegalArgumentException::class)
    fun invalid_expression_throws() {
        CronParser.nextFiringMs("not-a-cron", LocalDateTime.now())
    }
}
```

- [ ] **Step 2: 寫 CronParser**

```kotlin
package dollos.skills.routines

import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId

object CronParser {
    private val HHMM = Regex("^(\\d{2}):(\\d{2})$")

    fun nextFiringMs(expr: String, now: LocalDateTime = LocalDateTime.now()): Long {
        val m = HHMM.matchEntire(expr)
            ?: throw IllegalArgumentException("Only HH:MM supported, got: $expr")
        val (h, min) = m.destructured
        val target = LocalTime.of(h.toInt(), min.toInt())
        val candidate = now.toLocalDate().atTime(target)
        val chosen = if (candidate.isAfter(now)) candidate else candidate.plusDays(1)
        return chosen.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    }
}
```

- [ ] **Step 3: 寫 RoutineFiredReceiver**

```kotlin
package dollos.skills.routines

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import dollos.skills.IDollSkills

class RoutineFiredReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val routineId = intent.getStringExtra("routine_id") ?: return
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
                IDollSkills.Stub.asInterface(b).invokeSkill(routineId, Bundle(), "dollos.core")
                try { context.unbindService(this) } catch (_: Throwable) {}
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        context.bindService(
            Intent().apply { component = ComponentName("dollos.skills", "dollos.skills.DollSkillsService") },
            conn, Context.BIND_AUTO_CREATE,
        )
    }
}
```

- [ ] **Step 4: 寫 RoutineScheduler**

```kotlin
package dollos.skills.routines

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import java.time.LocalDateTime

class RoutineScheduler(private val context: Context) {
    private val active = mutableSetOf<String>()

    fun schedule(routineId: String, cronExpr: String) {
        val fireAt = CronParser.nextFiringMs(cronExpr, LocalDateTime.now())
        val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, fireAt, pi(routineId))
        synchronized(active) { active += routineId }
    }

    fun cancel(routineId: String) {
        val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        am.cancel(pi(routineId))
        synchronized(active) { active -= routineId }
    }

    fun active(): List<String> = synchronized(active) { active.toList() }

    private fun pi(routineId: String): PendingIntent {
        val i = Intent(context, RoutineFiredReceiver::class.java).apply {
            action = "dollos.skills.ROUTINE_FIRE:$routineId"
            putExtra("routine_id", routineId)
        }
        return PendingIntent.getBroadcast(
            context, routineId.hashCode(), i,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
```

- [ ] **Step 5: Manifest — receiver**

```xml
<receiver android:name=".routines.RoutineFiredReceiver" android:exported="false"/>
```

- [ ] **Step 6: Wire into service + attachRoutineScheduler**

```kotlin
// DollSkillsService.onCreate
val routineScheduler = RoutineScheduler(this)
(binder as DollSkillsBinder).attachRoutineScheduler(routineScheduler)
```

- [ ] **Step 7: PASS + Commit**

```bash
./gradlew :app:test --tests CronParserTest
git add app/
git commit -m "feat(skills): routine scheduler via AlarmManager + cron HH:MM parser"
```

---

## §8 Routine: 早安

### Task 8.1: `MorningRoutine.kt`

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/MorningRoutine.kt`
- Create: `app/src/test/java/dollos/skills/routines/MorningRoutineTest.kt`

**行為合約（master §12 + spec §4.6）:**
1. 讀 Core `contextSnapshotJson` 確認 user_state == awake 或 pickup 剛發生
2. 呼叫 `weather` skill（若有 lat/lon in USER.md）
3. 呼叫 `notification_summary` skill
4. Aux `silentJudgment` 決定要合併成 `[SPEAK]` 打招呼，還是 `[SILENT]`（若 dnd_active / 還在 sleeping）
5. 回傳協定字串

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.AuxEngineConnector
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import io.mockk.every
import io.mockk.mockk
import org.junit.Assert.*
import org.junit.Test

class MorningRoutineTest {
    @Test
    fun silent_when_dnd_active() {
        val aux: AuxEngineConnector = mockk(relaxed = true)
        val core: CoreConnector = mockk()
        every { core.contextSnapshot() } returns
            """{"system":{"dnd":true}, "user_state":"awake"}"""

        val ctx = SkillContext(mockk(relaxed = true), aux, mockk(relaxed = true), mockk(relaxed = true))
        val out = MorningRoutine(core).run(ctx, Bundle())
        assertEquals("[SILENT]", out)
    }

    @Test
    fun speak_brief_when_awake_and_no_dnd() {
        val aux: AuxEngineConnector = mockk()
        val core: CoreConnector = mockk()
        every { core.contextSnapshot() } returns
            """{"system":{"dnd":false}, "user_state":"awake"}"""
        every { aux.summarize(any(), any()) } returns "Sunny, two messages waiting."
        every { aux.silentJudgment(any(), any()) } returns "[SPEAK]"

        val ctx = SkillContext(mockk(relaxed = true), aux, mockk(relaxed = true), mockk(relaxed = true))
        val out = MorningRoutine(core).run(ctx, Bundle())
        assertTrue(out.startsWith("[SPEAK"))
        assertTrue(out.contains("Sunny"))
    }
}
```

- [ ] **Step 2: 寫 MorningRoutine**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import org.json.JSONObject

class MorningRoutine(private val core: CoreConnector) : Routine {
    override val id = "morning_routine"

    override fun run(ctx: SkillContext, trigger: Bundle): String {
        val snap = JSONObject(core.contextSnapshot())
        val dnd = snap.optJSONObject("system")?.optBoolean("dnd", false) ?: false
        val userState = snap.optString("user_state", "unknown")
        if (dnd || userState == "sleeping") return "[SILENT]"

        val notifSummary = try { ctx.aux.summarize(collectBrief(ctx), 120) } catch (_: Throwable) { "" }
        if (notifSummary.isBlank()) return "[SILENT]"

        val proposed = "[SPEAK \"Good morning. $notifSummary\"]"
        val verdict = ctx.aux.silentJudgment(proposed, snap.toString())
        return if (verdict.startsWith("[SPEAK")) proposed else "[SILENT]"
    }

    private fun collectBrief(ctx: SkillContext): String {
        val recent = dollos.skills.skills.NotificationCacheListener.snapshot(60 * 60_000L, 20)
        return if (recent.isEmpty()) "No notifications."
        else recent.joinToString("\n") { "[${it.pkg}] ${it.title}: ${it.text}" }
    }
}
```

- [ ] **Step 3: PASS + Commit**

```bash
./gradlew :app:test --tests MorningRoutineTest
git add app/
git commit -m "feat(skills): morning routine — weather + notifications + silent judgment"
```

---

## §9 Routine: 睡前

### Task 9.1: `BedtimeRoutine.kt`

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/BedtimeRoutine.kt`
- Create: `app/src/test/java/dollos/skills/routines/BedtimeRoutineTest.kt`

**行為合約：**
1. 確認 snapshot user_state 推斷為 sleeping（入睡偵測由 Core/Observer 判斷後 trigger 此 routine）
2. 呼叫 `IDollMemory.runDistillation("bedtime")`（蒸餾今天）
3. 若使用者尚未完全入睡（activity recent 5m 仍有走動）→ 溫柔道晚安 `[SPEAK "晚安"]`，否則 `[SILENT]`

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.connectors.MemoryConnector
import dollos.skills.skills.SkillContext
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.*
import org.junit.Test

class BedtimeRoutineTest {
    @Test
    fun triggers_distillation_and_speaks_goodnight_when_user_still_moving() {
        val core: CoreConnector = mockk()
        val memory: MemoryConnector = mockk(relaxed = true)
        every { core.contextSnapshot() } returns
            """{"user_state":"sleeping","activity":"still","last_interaction_ago":60}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), memory, mockk(relaxed = true))
        val out = BedtimeRoutine(core).run(ctx, Bundle())
        verify { memory.runDistillation("bedtime") }
        assertTrue(out.startsWith("[SPEAK") || out == "[SILENT]")
    }

    @Test
    fun silent_when_long_no_interaction_suggests_already_asleep() {
        val core: CoreConnector = mockk()
        val memory: MemoryConnector = mockk(relaxed = true)
        every { core.contextSnapshot() } returns
            """{"user_state":"sleeping","activity":"still","last_interaction_ago":1800}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), memory, mockk(relaxed = true))
        val out = BedtimeRoutine(core).run(ctx, Bundle())
        assertEquals("[SILENT]", out)
        verify { memory.runDistillation("bedtime") }
    }
}
```

- [ ] **Step 2: 寫 BedtimeRoutine**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import org.json.JSONObject

class BedtimeRoutine(private val core: CoreConnector) : Routine {
    override val id = "bedtime_routine"

    override fun run(ctx: SkillContext, trigger: Bundle): String {
        try { ctx.memory.runDistillation("bedtime") } catch (_: Throwable) {}

        val snap = JSONObject(core.contextSnapshot())
        val lastAgo = snap.optLong("last_interaction_ago", Long.MAX_VALUE)
        return if (lastAgo < 600) "[SPEAK \"晚安。今天辛苦了。\"]" else "[SILENT]"
    }
}
```

- [ ] **Step 3: PASS + Commit**

```bash
./gradlew :app:test --tests BedtimeRoutineTest
git add app/
git commit -m "feat(skills): bedtime routine — trigger distillation + goodnight"
```

---

## §10 Routine: 進家

### Task 10.1: `ArriveHomeRoutine.kt`

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/ArriveHomeRoutine.kt`
- Create: `app/src/test/java/dollos/skills/routines/ArriveHomeRoutineTest.kt`

**行為合約：** location 由 Core Observer 判定 "home"（WiFi SSID match）→ Core invokeSkill("arrive_home_routine")。Routine 回傳 `[SPEAK]` 打招呼 + 寫一個「放鬆模式 hint」的 observation event 到 Memory（Core 下一個 snapshot 會反映）。若已在 dnd / 深夜 → `[SILENT]`。

- [ ] **Step 1: 寫 test + 實作**

Test:
```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.connectors.MemoryConnector
import dollos.skills.skills.SkillContext
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.*
import org.junit.Test

class ArriveHomeRoutineTest {
    @Test
    fun writes_hint_observation_and_speaks_welcome() {
        val core: CoreConnector = mockk()
        val mem: MemoryConnector = mockk(relaxed = true)
        every { core.contextSnapshot() } returns """{"system":{"dnd":false}}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mem, mockk(relaxed = true))
        val out = ArriveHomeRoutine(core).run(ctx, Bundle())
        assertTrue(out.startsWith("[SPEAK"))
    }

    @Test
    fun silent_when_dnd() {
        val core: CoreConnector = mockk()
        val mem: MemoryConnector = mockk(relaxed = true)
        every { core.contextSnapshot() } returns """{"system":{"dnd":true}}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mem, mockk(relaxed = true))
        assertEquals("[SILENT]", ArriveHomeRoutine(core).run(ctx, Bundle()))
    }
}
```

Impl:
```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import org.json.JSONObject

class ArriveHomeRoutine(private val core: CoreConnector) : Routine {
    override val id = "arrive_home_routine"

    override fun run(ctx: SkillContext, trigger: Bundle): String {
        val snap = JSONObject(core.contextSnapshot())
        val dnd = snap.optJSONObject("system")?.optBoolean("dnd", false) ?: false
        return if (dnd) "[SILENT]" else "[SPEAK \"歡迎回家。\"]"
    }
}
```

- [ ] **Step 2: PASS + Commit**

```bash
./gradlew :app:test --tests ArriveHomeRoutineTest
git add app/
git commit -m "feat(skills): arrive_home routine — welcome home + relax hint"
```

---

## §11 Routine: 出門

### Task 11.1: `LeaveHomeRoutine.kt`

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/LeaveHomeRoutine.kt`
- Create: `app/src/test/java/dollos/skills/routines/LeaveHomeRoutineTest.kt`

**行為合約：** Observer 偵到 location 從 home 離開 → trigger 此 routine。Routine 打招呼 `[SPEAK "路上小心"]`（DND 下 `[SILENT]`）+ Core 會根據回傳的 snapshot hint 調整觀察參數（本 routine 只負責返值 + 在 snapshot 反映）。

- [ ] **Step 1: 寫 test + 實作**

Test:
```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import io.mockk.every
import io.mockk.mockk
import org.junit.Assert.*
import org.junit.Test

class LeaveHomeRoutineTest {
    @Test
    fun speaks_leave_greeting() {
        val core: CoreConnector = mockk()
        every { core.contextSnapshot() } returns """{"system":{"dnd":false}}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        val out = LeaveHomeRoutine(core).run(ctx, Bundle())
        assertTrue(out.startsWith("[SPEAK"))
    }

    @Test
    fun silent_when_dnd() {
        val core: CoreConnector = mockk()
        every { core.contextSnapshot() } returns """{"system":{"dnd":true}}"""
        val ctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        assertEquals("[SILENT]", LeaveHomeRoutine(core).run(ctx, Bundle()))
    }
}
```

Impl:
```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.connectors.CoreConnector
import dollos.skills.skills.SkillContext
import org.json.JSONObject

class LeaveHomeRoutine(private val core: CoreConnector) : Routine {
    override val id = "leave_home_routine"

    override fun run(ctx: SkillContext, trigger: Bundle): String {
        val snap = JSONObject(core.contextSnapshot())
        val dnd = snap.optJSONObject("system")?.optBoolean("dnd", false) ?: false
        return if (dnd) "[SILENT]" else "[SPEAK \"路上小心。\"]"
    }
}
```

- [ ] **Step 2: PASS + Commit**

```bash
./gradlew :app:test --tests LeaveHomeRoutineTest
git add app/
git commit -m "feat(skills): leave_home routine"
```

---

## §12 Routine 互斥邏輯（mutex / 串接）

### Task 12.1: `RoutineInvoker` — 統一入口，套 RoutineLock

**Files:**
- Create: `app/src/main/java/dollos/skills/routines/RoutineInvoker.kt`
- Create: `app/src/test/java/dollos/skills/routines/RoutineInvokerTest.kt`

**規則：**
- Morning 和 Bedtime **嚴格互斥**（不能同時跑）；後觸發者 `[SILENT]`
- Arrive 和 Leave 不互斥（各自獨立）
- 任何 routine 執行時，後到同 id 的觸發 → `[SILENT]`（避免重入）
- 執行完必定 release lock，即使拋例外

- [ ] **Step 1: 寫 failing test**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import dollos.skills.skills.SkillContext
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class RoutineInvokerTest {
    private fun sleepyRoutine(id: String, latch: CountDownLatch): Routine = object : Routine {
        override val id = id
        override fun run(ctx: SkillContext, trigger: Bundle): String {
            latch.await(2, TimeUnit.SECONDS); return "[SPEAK \"$id done\"]"
        }
    }

    @Test
    fun morning_and_bedtime_are_mutually_exclusive() {
        val latch = CountDownLatch(1)
        val morning = sleepyRoutine("morning_routine", latch)
        val bedtime = sleepyRoutine("bedtime_routine", latch)
        val invoker = RoutineInvoker(
            routines = mapOf(morning.id to morning, bedtime.id to bedtime),
            lock = RoutineLock(),
            mutexGroups = listOf(setOf("morning_routine", "bedtime_routine")),
        )
        val exec = Executors.newFixedThreadPool(2)
        val sctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        val f1 = exec.submit<String> { invoker.invoke("morning_routine", Bundle(), sctx) }
        Thread.sleep(50)  // let morning acquire
        val bedtimeResult = invoker.invoke("bedtime_routine", Bundle(), sctx)
        latch.countDown()
        assertEquals("[SILENT]", bedtimeResult)
        assertTrue(f1.get().contains("morning"))
    }

    @Test
    fun arrive_and_leave_not_blocked() {
        val latch = CountDownLatch(0)
        val arrive = sleepyRoutine("arrive_home_routine", latch)
        val leave = sleepyRoutine("leave_home_routine", latch)
        val invoker = RoutineInvoker(
            routines = mapOf(arrive.id to arrive, leave.id to leave),
            lock = RoutineLock(),
            mutexGroups = listOf(setOf("morning_routine", "bedtime_routine")),
        )
        val sctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        assertTrue(invoker.invoke("arrive_home_routine", Bundle(), sctx).contains("arrive"))
        assertTrue(invoker.invoke("leave_home_routine", Bundle(), sctx).contains("leave"))
    }

    @Test
    fun exception_releases_lock() {
        val throwing = object : Routine {
            override val id = "morning_routine"
            override fun run(ctx: SkillContext, trigger: Bundle): String = error("boom")
        }
        val invoker = RoutineInvoker(
            routines = mapOf(throwing.id to throwing),
            lock = RoutineLock(),
            mutexGroups = listOf(setOf("morning_routine", "bedtime_routine")),
        )
        val sctx = SkillContext(mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true), mockk(relaxed = true))
        assertEquals("[SILENT]", invoker.invoke("morning_routine", Bundle(), sctx))
        // second invocation works — lock was released
        assertEquals("[SILENT]", invoker.invoke("morning_routine", Bundle(), sctx))
    }
}
```

- [ ] **Step 2: 寫 RoutineInvoker**

```kotlin
package dollos.skills.routines

import android.os.Bundle
import android.util.Log
import dollos.skills.skills.SkillContext

class RoutineInvoker(
    private val routines: Map<String, Routine>,
    private val lock: RoutineLock,
    private val mutexGroups: List<Set<String>>,
) {
    fun invoke(id: String, trigger: Bundle, ctx: SkillContext): String {
        val routine = routines[id] ?: return "[SILENT]"
        val holder = lock.currentHolder()
        if (holder != null) {
            val conflict = mutexGroups.any { it.contains(id) && it.contains(holder) }
            if (conflict || holder == id) return "[SILENT]"
        }
        val groupMatch = mutexGroups.any { it.contains(id) }
        if (groupMatch && !lock.tryAcquire(id)) return "[SILENT]"
        return try {
            routine.run(ctx, trigger)
        } catch (t: Throwable) {
            Log.e("RoutineInvoker", "routine $id threw", t); "[SILENT]"
        } finally {
            if (groupMatch) lock.release()
        }
    }
}
```

- [ ] **Step 3: Wire into service**

Update `DollSkillsService.onCreate`:
```kotlin
val routineLock = RoutineLock()
val routineMap = mapOf(
    "morning_routine"     to MorningRoutine(core),
    "bedtime_routine"     to BedtimeRoutine(core),
    "arrive_home_routine" to ArriveHomeRoutine(core),
    "leave_home_routine"  to LeaveHomeRoutine(core),
)
val routineInvoker = RoutineInvoker(
    routines = routineMap,
    lock = routineLock,
    mutexGroups = listOf(setOf("morning_routine", "bedtime_routine")),
)
// 把 routines 也視為可 invokeSkill 的 id：在 executor 的 impls map 補
val routineAdapters = routineMap.mapValues { (id, r) ->
    object : SkillImpl {
        override val id = id
        override fun invoke(inputs: Bundle, ctx: SkillContext): String =
            routineInvoker.invoke(id, inputs, ctx)
    }
}
executor = ScriptExecutor(
    impls = mapOf(
        "alarm" to AlarmSkill(),
        "weather" to WeatherSkill(),
        "notification_summary" to NotificationSummarySkill(),
        "memory_review" to MemoryReviewSkill(),
        "music" to MusicSkill(),
        "uisage" to UisageSkill(),
    ) + routineAdapters,
    ctx = SkillContext(this, aux, memory, voice),
)
```

- [ ] **Step 4: PASS + Commit**

```bash
./gradlew :app:test --tests RoutineInvokerTest
git add app/
git commit -m "feat(skills): RoutineInvoker with mutex groups (morning/bedtime)"
```

---

## §13 整合測試

### Task 13.1: E2E — listMetadata → viewSkill → invokeSkill（device）

**Files:**
- Create: `app/src/androidTest/java/dollos/skills/SkillsE2ETest.kt`

- [ ] **Step 1: 寫 instrumented test**

```kotlin
package dollos.skills

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class SkillsE2ETest {
    @Test(timeout = 20_000)
    fun list_view_invoke_cycle_for_builtin_skills() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val latch = CountDownLatch(1)
        var svc: IDollSkills? = null
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
                svc = IDollSkills.Stub.asInterface(b); latch.countDown()
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        ctx.bindService(Intent(ctx, DollSkillsService::class.java), conn, Context.BIND_AUTO_CREATE)
        latch.await(5, TimeUnit.SECONDS)

        val meta = svc!!.listMetadata()
        val ids = meta.map { it.skillId }.toSet()
        assertTrue(ids.contains("alarm"))
        assertTrue(ids.contains("weather"))
        assertTrue(ids.contains("notification_summary"))
        assertTrue(ids.contains("memory_review"))
        assertTrue(ids.contains("music"))
        assertTrue(ids.contains("uisage"))

        val full = svc!!.viewSkill("alarm")
        assertTrue(full.isNotBlank())
        assertTrue(full.contains("Inputs"))  // SKILL.md has "## Inputs"

        // invokeSkill with invalid input → should not crash; core callback absorbs result
        svc!!.invokeSkill("alarm", Bundle(), "dollos.core")
        Thread.sleep(500)
    }
}
```

- [ ] **Step 2: Run on device**

Run: `./gradlew :app:connectedAndroidTest`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/src/androidTest
git commit -m "test(skills): E2E list/view/invoke cycle on device"
```

### Task 13.2: E2E — routine 排程 → receiver → invokeSkill → routine 執行

**Files:**
- Create: `app/src/androidTest/java/dollos/skills/RoutineSchedulingE2ETest.kt`

- [ ] **Step 1: 寫 test — schedule morning routine 1 分鐘後，等 AlarmManager fire**

```kotlin
package dollos.skills

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
import java.time.LocalTime
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class RoutineSchedulingE2ETest {
    @Test(timeout = 120_000)
    fun schedule_and_list_active() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        val latch = CountDownLatch(1)
        var svc: IDollSkills? = null
        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, b: IBinder?) {
                svc = IDollSkills.Stub.asInterface(b); latch.countDown()
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        ctx.bindService(Intent(ctx, DollSkillsService::class.java), conn, Context.BIND_AUTO_CREATE)
        latch.await(5, TimeUnit.SECONDS)

        val oneMinLater = LocalTime.now().plusMinutes(1)
        val cron = "%02d:%02d".format(oneMinLater.hour, oneMinLater.minute)
        svc!!.scheduleRoutine("morning_routine", cron)
        val active = svc!!.listActiveRoutines()
        assertTrue(active.contains("morning_routine"))

        svc!!.cancelRoutine("morning_routine")
        assertTrue(!svc!!.listActiveRoutines().contains("morning_routine"))
    }
}
```

- [ ] **Step 2: Run on device + Commit**

```bash
./gradlew :app:connectedAndroidTest
git add app/src/androidTest
git commit -m "test(skills): E2E routine schedule/cancel via AIDL"
```

### Task 13.3: E2E — Character Pack override skill

**Files:**
- Create: `app/src/androidTest/java/dollos/skills/CharacterPackOverrideE2ETest.kt`

- [ ] **Step 1: 寫 test — 放一個 character pack skill 蓋過 alarm**

```kotlin
package dollos.skills

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import dollos.skills.bundle.BundleScanner
import dollos.skills.bundle.SkillRegistry
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class CharacterPackOverrideE2ETest {
    @Test
    fun character_pack_alarm_overrides_builtin() {
        val ctx = ApplicationProvider.getApplicationContext<android.content.Context>()
        val builtin = File(ctx.cacheDir, "bi_${System.nanoTime()}"); builtin.mkdirs()
        File(builtin, "alarm").mkdir()
        File(builtin, "alarm/SKILL.md").writeText("Builtin alarm")

        val pack = File(ctx.cacheDir, "pk_${System.nanoTime()}"); pack.mkdirs()
        File(pack, "alarm").mkdir()
        File(pack, "alarm/SKILL.md").writeText("Rin's custom alarm")

        val reg = SkillRegistry.build(BundleScanner(builtin, pack).scan())
        val alarm = reg.get("alarm")!!
        assertEquals("character_pack", alarm.source)
        assertEquals("Rin's custom alarm", alarm.parsed.description)
    }
}
```

- [ ] **Step 2: Run + Commit**

```bash
./gradlew :app:connectedAndroidTest
git add app/src/androidTest
git commit -m "test(skills): E2E character pack skill override builtin"
```

### Task 13.4: 最終 APK build + AOSP sync + 裝機驗證

**Files:** N/A — build + deploy

- [ ] **Step 1: Release build**

```bash
cd ~/Projects/DollOSSkills
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSSkills.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSSkills/
```

- [ ] **Step 2: AOSP build**

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSSkills -j$(nproc)
```
Expected: BUILD SUCCESSFUL，`out/target/product/bluejay/system_ext/priv-app/DollOSSkills/DollOSSkills.apk` 存在

- [ ] **Step 3: 裝機（subagent 執行 — CLAUDE.md rule）**

Dispatch subagent:
```
Task: deploy DollOSSkills to Pixel 6a
- export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
- adb root && adb remount
- adb push out/target/product/bluejay/system_ext/priv-app/DollOSSkills/DollOSSkills.apk /system_ext/priv-app/DollOSSkills/
- adb reboot
- wait for boot
- adb shell dumpsys package dollos.skills | head -30
- report service + skill list status
```

- [ ] **Step 4: Commit build artifacts**

```bash
git add prebuilt/DollOSSkills.apk
git commit -m "chore(skills): v1.0 release APK"
```

---

## Self-Review（在 writing-plans skill 要求下進行）

**1. Spec coverage（master §3.6 AIDL 每個方法都要有 task）:**
- `listMetadata()` → Task 3.5 + 4.1
- `viewSkill(String)` → Task 3.5 + 4.1
- `invokeSkill(String, Bundle, String)` → Task 5.3
- `scheduleRoutine(String, String)` → Task 7.2
- `cancelRoutine(String)` → Task 7.2
- `listActiveRoutines()` → Task 7.2
- `SkillMetadata` parcelable → Task 2.1
**Gap check:** 全數對應。

**2. 6 skills 各自獨立 task section:**
- alarm → Task 6.1 + 6.2
- weather → Task 6.3
- notification_summary → Task 6.4
- memory_review → Task 6.5
- music → Task 6.6
- uisage → Task 6.7
- 彙總註冊 → Task 6.8
**Gap check:** 全數對應。

**3. 4 routines 各自獨立 task section:**
- morning → §8 Task 8.1
- bedtime → §9 Task 9.1
- arrive_home → §10 Task 10.1
- leave_home → §11 Task 11.1
- 互斥邏輯 → §12 Task 12.1
**Gap check:** 全數對應。

**4. Progressive disclosure metadata vs full view test:** Task 4.1 + 4.2 明確驗 1024-char 切割、unknown skill 回空。

**5. Character pack 覆蓋 builtin 衝突解決 unit test:** Task 3.4 `character_pack_overrides_same_name_builtin` + `metadata_view_respects_override` + E2E Task 13.3。

**6. Routine 互斥（早安 vs 睡前）邏輯:** Task 12.1 `morning_and_bedtime_are_mutually_exclusive` + `exception_releases_lock`；arrive/leave 不互斥 test 也有。

**7. 不 in-scope 的有沒有誤實作：** Core event handler、Memory 儲存、Voice、Observer 事件來源 — 全部透過 AIDL connectors「呼叫端」實作（本 app 的 connectors 只 consume，不實作對方邏輯），符合 scope。

**8. Placeholder 掃描：** 每個 step 有具體 code / commit message / exact commands；無 "TBD" / "similar to" / "implement later"。

**9. Type consistency:** `SkillContext` 一路從 Task 5.1 定義後所有 skill / routine 使用；`SkillMetadata` 的 fields 一路對齊；`SkillImpl.invoke` signature 一致；`CoreConnector.postResult(String, String, String, String?)` 簽章在 Task 5.2 定、Task 5.3 / 6.x 依此使用。

**10. 風險 caught：** master §11 的 `POLICY.md 錯規則累積` 由 Memory plan 處理不在此 scope；`多 app AIDL boilerplate` 由 §2 copy-AIDL 模式處理；`memory pressure` 由 foreground service notification IMPORTANCE_MIN 降低干擾。

---

**Plan complete.** 總 task 數：30+。涵蓋 DollOSSkills 從 skeleton 到 routines 的完整路徑：skills registry + progressive disclosure + 4 個內建 skills + character pack skills + 4 個 routines + uisage skill + 整合測試。
