# Launcher Live2D Renderer + Character Pack v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 DollOSLauncher 的 3D avatar renderer 從 Filament 替換為 Live2D Cubism，引入 Character Pack v2 格式（含 touch interaction manifest 欄位），移除 app drawer / character picker UI，使 Launcher 預設 home 畫面顯示全螢幕 Live2D Doll 並依 Core ops event 切換 motion。

**Architecture:** Launcher 維持目前 AIService AIDL binding（Terminal 重構 plan 未來另處理）。新增 `org.dollos.launcher.live2d` package 承載 Live2D Cubism SDK for Java 整合（OpenGL ES 2.0 + TextureView）。Character Pack v2 manifest 由 DollOSAIService 端的 `CharacterManifest` 先擴充（加 `live2d` block + `touchInteractions`），Launcher 透過既有 AIDL `openCharacterAsset(path): ParcelFileDescriptor` 讀 .doll 內檔案。移除 `scene/`（Filament）、`drawer/`、`character/CharacterPickerOverlay.kt`；`DollOSLauncherActivity` 精簡為「載入當前角色 → 初始化 Live2DRenderer → 訂閱 ops event → 切 motion」。

**Tech Stack:** Kotlin, Android Gradle, Live2D Cubism SDK for Java（Live2DCubismCore.aar + CubismFramework 原始碼）, OpenGL ES 2.0, TextureView + Choreographer, AIDL, org.json, JUnit 4 + Mockito-Kotlin。

**Spec reference:** `docs/superpowers/specs/2026-04-24-avatar-redefinition-design.md`

**Out of scope（由其他 plan 處理）：**
- 邊緣狀態指示 overlay service（Plan 2）
- 鎖屏 SystemUI 改造（Plan 3）
- gura.doll Live2D 美術製作（非工程，先用 Haru placeholder）
- Terminal 架構重構（AIService → DollOSCore rebind）

---

## 檔案結構

### 新增

**Live2D 整合**
- `app/libs/Live2DCubismCore.aar` — Live2D 官方 Core binary
- `app/src/main/java/com/live2d/sdk/cubism/framework/**` — Cubism Framework Java 原始碼（複製自 SDK）
- `app/src/main/java/org/dollos/launcher/live2d/Live2DRenderer.kt` — TextureView + GL renderer bridge
- `app/src/main/java/org/dollos/launcher/live2d/Live2DModelHolder.kt` — CubismModel 生命週期 + motion / expression / physics 管理
- `app/src/main/java/org/dollos/launcher/live2d/Live2DMotionController.kt` — motion 切換 state machine（IDLE / LISTENING / THINKING / SPEAKING + 組合）
- `app/src/main/java/org/dollos/launcher/live2d/Live2DLipSync.kt` — TTS amplitude → ParamMouthOpenY
- `app/src/main/java/org/dollos/launcher/live2d/Live2DTouchInteractor.kt` — Touch event → hit area → motion + AIDL event
- `app/src/main/java/org/dollos/launcher/live2d/Live2DTextureManager.kt` — bitmap → GL texture 載入

**Character Pack v2**
- `app/src/main/java/org/dollos/launcher/character/CharacterPackV2.kt` — v2 manifest Kotlin data classes + parser
- `app/src/main/java/org/dollos/launcher/character/CharacterAssetReader.kt` — 透過 AIDL 讀 .doll 內檔案的統一介面
- DollOSAIService 端同步更新 `CharacterManifest.kt`（加 `live2d`、`touchInteractions` 欄位）

**測試**
- `app/src/test/java/org/dollos/launcher/character/CharacterPackV2Test.kt`
- `app/src/test/java/org/dollos/launcher/live2d/Live2DMotionControllerTest.kt`
- `app/src/test/java/org/dollos/launcher/live2d/Live2DLipSyncTest.kt`
- `app/src/test/java/org/dollos/launcher/live2d/Live2DTouchInteractorTest.kt`
- `app/src/androidTest/java/org/dollos/launcher/live2d/Live2DRendererInstrumentedTest.kt`

**Placeholder asset**
- `app/src/main/assets/haru_placeholder/` — Live2D 官方 Haru 範例資產直接放 Launcher assets（當無 Character Pack 可用時的 fallback placeholder；正式 Character Pack 仍透過 AIService 載入）

### 修改

- `app/build.gradle.kts` — 移除 Filament 依賴，加 `fileTree(libs)` + JUnit / Mockito testImplementation
- `app/src/main/AndroidManifest.xml` — 加 `<uses-feature android:glEsVersion="0x00020000" android:required="true" />`
- `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt` — 砍到 ~200 行：去 drawer、picker、Filament 初始化；加 Live2DRenderer 初始化 + AIDL ops event 訂閱
- `app/src/main/res/layout/activity_launcher.xml` — 移除 drawer hint、long-press 中央區；保留 TextureView + 字幕氣泡；TextureView id 維持
- DollOSAIService 端 `app/src/main/java/org/dollos/ai/character/CharacterPack.kt` — manifest 加 v2 欄位
- DollOSAIService 端 `app/src/main/java/org/dollos/ai/character/CharacterValidator.kt` — v1 拒絕、v2 驗證
- `app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl` — 確認已有 `openCharacterAsset(String path)` 或新增

### 刪除

- `app/src/main/java/org/dollos/launcher/scene/FilamentSceneManager.kt`
- `app/src/main/java/org/dollos/launcher/scene/AvatarAnimator.kt`
- `app/src/main/java/org/dollos/launcher/scene/SceneConfig.kt`
- `app/src/main/java/org/dollos/launcher/scene/`（整個目錄）
- `app/src/main/java/org/dollos/launcher/drawer/AppDrawerView.kt`
- `app/src/main/java/org/dollos/launcher/drawer/AppInfo.kt`
- `app/src/main/java/org/dollos/launcher/drawer/AppListAdapter.kt`
- `app/src/main/java/org/dollos/launcher/drawer/RecentAppsAdapter.kt`
- `app/src/main/java/org/dollos/launcher/drawer/`（整個目錄）
- `app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt`

---

## Task 0: Build 基礎與測試框架

**Files:**
- Modify: `app/build.gradle.kts`
- Modify: `app/src/main/AndroidManifest.xml`

- [ ] **Step 1: 移除 Filament、加 JUnit + Mockito testImplementation、加 `app/libs` fileTree**

編輯 `app/build.gradle.kts` 的 `dependencies` 區塊，替換為：

```kotlin
dependencies {
    // Live2D Cubism SDK（手動放入 app/libs/）
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.aar", "*.jar"))))

    // AndroidX
    implementation("androidx.core:core-ktx:1.15.0")

    // Testing
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.mockito.kotlin:mockito-kotlin:5.2.1")
    testImplementation("org.mockito:mockito-core:5.11.0")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.0")
    testImplementation("org.json:json:20231013")  // 讓 org.json 在 unit test 可用

    androidTestImplementation("androidx.test:runner:1.5.2")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
}
```

在 `android { ... }` 區塊末尾加：

```kotlin
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            isReturnDefaultValues = true
        }
    }

    packaging {
        resources {
            excludes += setOf("META-INF/LICENSE*", "META-INF/NOTICE*")
        }
    }
```

- [ ] **Step 2: AndroidManifest 加 OpenGL ES 2.0 feature**

在 `app/src/main/AndroidManifest.xml` `<manifest>` 之下、`<application>` 之前加：

```xml
<uses-feature android:glEsVersion="0x00020000" android:required="true" />
```

- [ ] **Step 3: 建立 `app/libs/` 並加 `.gitkeep`**

```bash
mkdir -p ~/Projects/DollOSLauncher/app/libs
touch ~/Projects/DollOSLauncher/app/libs/.gitkeep
```

- [ ] **Step 4: 寫入 `app/libs/README.md` 說明 SDK 下載流程**

```markdown
# Live2D Cubism SDK for Java

此目錄放置 Live2D Cubism SDK for Java 的 binary 與依賴。

## 取得方式
1. 前往 https://www.live2d.com/en/sdk/download/java/
2. 下載最新版 Cubism SDK for Java（需同意 License；DollOS 為個人用符合 Free License）
3. 從解壓縮包 `Core/android/` 複製 `Live2DCubismCore.aar` 到此目錄
4. 從解壓縮包 `Framework/` 複製 Java 原始碼到
   `app/src/main/java/com/live2d/sdk/cubism/framework/`（保留 package 結構）

## Git 處理
`.aar` 不進 repo（在 .gitignore 排除）；開發者首次 clone 後依本文件步驟自行放置。
Framework Java 原始碼**進** repo（受 SDK license 約束，需保留 Live2D 版權 header）。
```

- [ ] **Step 5: 更新 `.gitignore`**

在專案 root `.gitignore` 加：

```
# Live2D SDK binary (downloaded separately per licensing)
app/libs/*.aar
!app/libs/.gitkeep
!app/libs/README.md
```

- [ ] **Step 6: 驗證 build 仍通過**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew assembleDebug 2>&1 | tail -20
```

Expected: BUILD SUCCESSFUL（因 Filament import 還在，可能有編譯錯誤；若有則任務 7 繼續，先確認是 Filament import 造成）

- [ ] **Step 7: Commit**

```bash
cd ~/Projects/DollOSLauncher
git add app/build.gradle.kts app/src/main/AndroidManifest.xml app/libs/.gitkeep app/libs/README.md .gitignore
git commit -m "build: replace Filament with Live2D SDK scaffold + test deps"
```

---

## Task 1: Character Pack v2 Manifest schema（DollOSAIService 端）

**Files:**
- Modify: `DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterPack.kt`
- Modify: `DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterValidator.kt`
- Create: `DollOSAIService/app/src/test/java/org/dollos/ai/character/CharacterManifestV2Test.kt`

- [ ] **Step 1: 寫失敗測試驗證 v2 manifest 解析**

Create `DollOSAIService/app/src/test/java/org/dollos/ai/character/CharacterManifestV2Test.kt`:

```kotlin
package org.dollos.ai.character

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class CharacterManifestV2Test {

    @Test
    fun `v2 manifest parses live2d block`() {
        val json = JSONObject("""
            {
              "formatVersion": 2,
              "name": "Test",
              "version": "1.0.0",
              "author": "me",
              "avatarType": "live2d",
              "live2d": {
                "modelPath": "live2d/test.model3.json",
                "animationMappings": {
                  "idle": "live2d/motions/idle.motion3.json",
                  "listening": "live2d/motions/listening.motion3.json",
                  "thinking": "live2d/motions/thinking.motion3.json",
                  "speaking": "live2d/motions/speaking.motion3.json"
                },
                "lipSync": { "enabled": true, "parameterId": "ParamMouthOpenY" },
                "background": { "type": "solid", "color": "#0D0D1A" }
              },
              "touchInteractions": [
                {
                  "hitArea": "Head",
                  "motion": "live2d/motions/headpat.motion3.json",
                  "eventName": "user_headpat",
                  "eventPayload": { "intensity": "soft" }
                }
              ]
            }
        """.trimIndent())

        val m = CharacterManifest.fromJson(json)

        assertEquals(2, m.formatVersion)
        assertEquals("live2d", m.avatarType)
        assertNotNull(m.live2d)
        assertEquals("live2d/test.model3.json", m.live2d!!.modelPath)
        assertEquals("live2d/motions/idle.motion3.json", m.live2d!!.animationMappings["idle"])
        assertTrue(m.live2d!!.lipSync.enabled)
        assertEquals("ParamMouthOpenY", m.live2d!!.lipSync.parameterId)
        assertEquals(1, m.touchInteractions.size)
        assertEquals("Head", m.touchInteractions[0].hitArea)
        assertEquals("user_headpat", m.touchInteractions[0].eventName)
        assertEquals("soft", m.touchInteractions[0].eventPayload.getString("intensity"))
    }

    @Test
    fun `v1 manifest returns null live2d block and empty touchInteractions`() {
        val json = JSONObject("""
            {
              "formatVersion": 1,
              "name": "Legacy",
              "version": "1.0.0",
              "author": "me",
              "avatarType": "3d"
            }
        """.trimIndent())

        val m = CharacterManifest.fromJson(json)

        assertEquals(1, m.formatVersion)
        assertNull(m.live2d)
        assertTrue(m.touchInteractions.isEmpty())
    }

    @Test
    fun `roundtrip toJson then fromJson preserves v2 fields`() {
        val original = CharacterManifest(
            formatVersion = 2,
            name = "Test",
            version = "1.0.0",
            author = "me",
            description = "",
            wakeWord = null,
            avatarType = "live2d",
            created = "",
            voiceReferenceFile = null,
            voiceReferenceText = null,
            live2d = Live2DConfig(
                modelPath = "m.model3.json",
                animationMappings = mapOf(
                    "idle" to "idle.motion3.json",
                    "listening" to "listen.motion3.json",
                    "thinking" to "think.motion3.json",
                    "speaking" to "speak.motion3.json"
                ),
                lipSync = LipSyncConfig(enabled = true, parameterId = "ParamMouthOpenY"),
                background = BackgroundConfig(type = "solid", color = "#000000")
            ),
            touchInteractions = listOf(
                TouchInteraction(
                    hitArea = "Head",
                    motion = "pat.motion3.json",
                    eventName = "user_headpat",
                    eventPayload = JSONObject().put("intensity", "soft")
                )
            )
        )

        val restored = CharacterManifest.fromJson(original.toJson())

        assertEquals(original.formatVersion, restored.formatVersion)
        assertEquals(original.live2d!!.modelPath, restored.live2d!!.modelPath)
        assertEquals(original.touchInteractions.size, restored.touchInteractions.size)
        assertEquals("soft", restored.touchInteractions[0].eventPayload.getString("intensity"))
    }
}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Projects/DollOSAIService && ./gradlew :app:testDebugUnitTest --tests "org.dollos.ai.character.CharacterManifestV2Test" 2>&1 | tail -30
```

Expected: FAIL — `Live2DConfig` / `TouchInteraction` 等型別不存在。

- [ ] **Step 3: 在 CharacterPack.kt 加 v2 data classes + 更新 CharacterManifest**

Edit `DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterPack.kt`. 在檔案頂端、`CharacterManifest` 之前加：

```kotlin
data class Live2DConfig(
    val modelPath: String,
    val animationMappings: Map<String, String>,
    val lipSync: LipSyncConfig,
    val background: BackgroundConfig
) {
    companion object {
        fun fromJson(json: JSONObject): Live2DConfig {
            val mapJson = json.getJSONObject("animationMappings")
            val mappings = mutableMapOf<String, String>()
            mapJson.keys().forEach { key -> mappings[key] = mapJson.getString(key) }
            return Live2DConfig(
                modelPath = json.getString("modelPath"),
                animationMappings = mappings,
                lipSync = LipSyncConfig.fromJson(json.getJSONObject("lipSync")),
                background = BackgroundConfig.fromJson(json.optJSONObject("background")
                    ?: JSONObject().put("type", "solid").put("color", "#000000"))
            )
        }
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("modelPath", modelPath)
        put("animationMappings", JSONObject(animationMappings as Map<*, *>))
        put("lipSync", lipSync.toJson())
        put("background", background.toJson())
    }
}

data class LipSyncConfig(val enabled: Boolean, val parameterId: String) {
    companion object {
        fun fromJson(json: JSONObject) = LipSyncConfig(
            enabled = json.optBoolean("enabled", false),
            parameterId = json.optString("parameterId", "ParamMouthOpenY")
        )
    }
    fun toJson(): JSONObject = JSONObject().apply {
        put("enabled", enabled); put("parameterId", parameterId)
    }
}

data class BackgroundConfig(val type: String, val color: String) {
    companion object {
        fun fromJson(json: JSONObject) = BackgroundConfig(
            type = json.optString("type", "solid"),
            color = json.optString("color", "#000000")
        )
    }
    fun toJson(): JSONObject = JSONObject().apply {
        put("type", type); put("color", color)
    }
}

data class TouchInteraction(
    val hitArea: String,
    val motion: String,
    val eventName: String,
    val eventPayload: JSONObject
) {
    companion object {
        fun fromJson(json: JSONObject) = TouchInteraction(
            hitArea = json.getString("hitArea"),
            motion = json.getString("motion"),
            eventName = json.getString("eventName"),
            eventPayload = json.optJSONObject("eventPayload") ?: JSONObject()
        )
    }
    fun toJson(): JSONObject = JSONObject().apply {
        put("hitArea", hitArea)
        put("motion", motion)
        put("eventName", eventName)
        put("eventPayload", eventPayload)
    }
}
```

修改 `CharacterManifest` data class 加 v2 欄位：

```kotlin
data class CharacterManifest(
    val formatVersion: Int,
    val name: String,
    val version: String,
    val author: String,
    val description: String,
    val wakeWord: String?,
    val avatarType: String,
    val created: String,
    val voiceReferenceFile: String?,
    val voiceReferenceText: String?,
    val live2d: Live2DConfig? = null,              // v2 新增
    val touchInteractions: List<TouchInteraction> = emptyList()  // v2 新增
) {
    companion object {
        fun fromJson(json: JSONObject): CharacterManifest {
            val live2d = json.optJSONObject("live2d")?.let { Live2DConfig.fromJson(it) }
            val interactions = json.optJSONArray("touchInteractions")?.let { arr ->
                List(arr.length()) { i -> TouchInteraction.fromJson(arr.getJSONObject(i)) }
            } ?: emptyList()

            return CharacterManifest(
                formatVersion = json.getInt("formatVersion"),
                name = json.getString("name"),
                version = json.getString("version"),
                author = json.getString("author"),
                description = json.optString("description", ""),
                wakeWord = if (json.isNull("wakeWord")) null
                           else json.optString("wakeWord", "").ifEmpty { null },
                avatarType = json.optString("avatarType", "3d"),
                created = json.optString("created", ""),
                voiceReferenceFile = json.optString("voiceReferenceFile", "").ifEmpty { null },
                voiceReferenceText = json.optString("voiceReferenceText", "").ifEmpty { null },
                live2d = live2d,
                touchInteractions = interactions
            )
        }
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("formatVersion", formatVersion)
        put("name", name)
        put("version", version)
        put("author", author)
        put("description", description)
        put("wakeWord", wakeWord)
        put("avatarType", avatarType)
        put("created", created)
        put("voiceReferenceFile", voiceReferenceFile)
        put("voiceReferenceText", voiceReferenceText)
        if (live2d != null) put("live2d", live2d.toJson())
        if (touchInteractions.isNotEmpty()) {
            val arr = JSONArray()
            touchInteractions.forEach { arr.put(it.toJson()) }
            put("touchInteractions", arr)
        }
    }
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Projects/DollOSAIService && ./gradlew :app:testDebugUnitTest --tests "org.dollos.ai.character.CharacterManifestV2Test" 2>&1 | tail -30
```

Expected: All 3 tests pass.

- [ ] **Step 5: CharacterValidator 拒絕 v1（avatarType != live2d 或 live2d block 缺）**

Edit `DollOSAIService/app/src/main/java/org/dollos/ai/character/CharacterValidator.kt`，在 `validate` function 開頭（manifest 已解析後）加：

```kotlin
// v2 only: 必須 formatVersion=2 且 avatarType="live2d" 且有 live2d block
if (manifest.formatVersion < 2) {
    return ValidationResult.Error("Character Pack format v${manifest.formatVersion} not supported. Please use v2 (Live2D).")
}
if (manifest.avatarType != "live2d" || manifest.live2d == null) {
    return ValidationResult.Error("Character Pack must use Live2D (avatarType=\"live2d\" with live2d block).")
}
```

（若 `ValidationResult.Error` 名稱不同，照現有類別調整；保留原驗證其他邏輯）

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/java/org/dollos/ai/character/ app/src/test/java/org/dollos/ai/character/
git commit -m "feat(character): add v2 manifest with live2d + touchInteractions"
```

---

## Task 2: AIDL `openCharacterAsset` 確認

**Files:**
- Modify（如缺）: `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify（如缺）: `DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAIService.aidl`

- [ ] **Step 1: 檢查 AIDL 是否已有 `openCharacterAsset`**

```bash
grep -n "openCharacterAsset\|ParcelFileDescriptor" ~/Projects/DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl
```

Expected: 若已存在 → 跳到 Step 4。若無 → 繼續 Step 2。

- [ ] **Step 2: AIDL 加 method（AIService 端）**

Edit `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl`，加 method：

```aidl
/**
 * 開啟目前 active character pack 內指定相對路徑的檔案。
 * path 範例："live2d/gura.model3.json" 或 "live2d/motions/idle.motion3.json"
 * 找不到或超出 pack 範圍回傳 null。
 */
ParcelFileDescriptor openCharacterAsset(String path);
```

- [ ] **Step 3: 同步到 Launcher 端 AIDL**

```bash
cp ~/Projects/DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl \
   ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAIService.aidl
```

- [ ] **Step 4: AIService 實作（若 Step 2 新增）**

Edit `DollOSAIService/app/src/main/java/org/dollos/ai/DollOSAIService.kt`（或 binder stub 實作處），加：

```kotlin
override fun openCharacterAsset(path: String?): ParcelFileDescriptor? {
    if (path.isNullOrEmpty()) return null
    val activePackDir = characterManager.getActiveCharacterDir() ?: return null
    val file = File(activePackDir, path)
    // 防止 path traversal
    if (!file.canonicalPath.startsWith(activePackDir.canonicalPath)) {
        Log.w(TAG, "openCharacterAsset rejected out-of-pack path: $path")
        return null
    }
    if (!file.exists()) return null
    return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
}
```

（若 `characterManager.getActiveCharacterDir()` API 不存在，先在 `CharacterManager` 加：`fun getActiveCharacterDir(): File? = activePack?.rootDir`）

- [ ] **Step 5: 驗證 build**

```bash
cd ~/Projects/DollOSAIService && ./gradlew assembleDebug 2>&1 | tail -10
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/aidl/ app/src/main/java/org/dollos/ai/
git commit -m "aidl: add openCharacterAsset for Live2D asset streaming"

cd ~/Projects/DollOSLauncher
git add app/aidl/
git commit -m "aidl: sync openCharacterAsset from AIService"
```

---

## Task 3: Live2D SDK 本地放置（手動步驟文件化）

**Files:**
- Create: `DollOSLauncher/app/libs/Live2DCubismCore.aar`（手動放）
- Create: `DollOSLauncher/app/src/main/java/com/live2d/sdk/cubism/framework/**`（手動複製）

- [ ] **Step 1: 下載並放置 Live2D Cubism SDK for Java**

執行以下手動步驟（非 agentic，由人類開發者操作；agent 可透過 `curl` 若 URL 可取得，但 Live2D 需登入同意條款，通常仍需人手操作）：

1. 前往 https://www.live2d.com/en/sdk/download/java/
2. 勾選同意「Live2D Open Software License Agreement」+「Live2D Proprietary Software License Agreement」
3. 下載 `CubismSdkForJava-<version>.zip`
4. 解壓縮後：
   ```bash
   cp /path/to/CubismSdkForJava/Core/android/Live2DCubismCore.aar \
      ~/Projects/DollOSLauncher/app/libs/
   cp -r /path/to/CubismSdkForJava/Framework/src/main/java/com/live2d \
      ~/Projects/DollOSLauncher/app/src/main/java/com/
   ```
5. 同樣複製 SDK Sample `Sample/Demo/src/main/java/com/live2d/demo/LApp*.java` → 暫留作 Step 2 參考（不進 repo）

- [ ] **Step 2: 複製 Haru 範例資產當 placeholder**

```bash
mkdir -p ~/Projects/DollOSLauncher/app/src/main/assets/haru_placeholder
cp -r /path/to/CubismSdkForJava/Sample/Demo/src/main/assets/Resources/Haru/* \
   ~/Projects/DollOSLauncher/app/src/main/assets/haru_placeholder/
```

驗證目錄內有：`Haru.moc3`、`Haru.model3.json`、`Haru.pose3.json`（選用）、`Haru.physics3.json`（選用）、`textures/`、`motions/`、`expressions/`。

- [ ] **Step 3: 驗證 build 含 SDK 仍通過**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew assembleDebug 2>&1 | tail -30
```

Expected: BUILD SUCCESSFUL。若 Framework Java 編譯錯誤（例如缺少 annotation），檢查是否漏複製資源或 SDK 版本不相容。

- [ ] **Step 4: Commit（Framework 原始碼 + Haru assets，不含 .aar）**

```bash
cd ~/Projects/DollOSLauncher
git add app/src/main/java/com/live2d/ app/src/main/assets/haru_placeholder/
git commit -m "vendor: add Live2D Cubism Framework sources + Haru placeholder"
```

---

## Task 4: CharacterAssetReader（Launcher 端）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/character/CharacterAssetReader.kt`
- Create: `DollOSLauncher/app/src/test/java/org/dollos/launcher/character/CharacterAssetReaderTest.kt`

- [ ] **Step 1: 寫失敗測試**

Create `app/src/test/java/org/dollos/launcher/character/CharacterAssetReaderTest.kt`:

```kotlin
package org.dollos.launcher.character

import android.os.ParcelFileDescriptor
import org.dollos.ai.IDollOSAIService
import org.junit.Assert.*
import org.junit.Test
import org.mockito.kotlin.*
import java.io.File
import java.io.FileWriter

class CharacterAssetReaderTest {

    @Test
    fun `readText returns content via AIDL openCharacterAsset`() {
        val tmp = File.createTempFile("asset", ".json")
        FileWriter(tmp).use { it.write("""{"hello":"world"}""") }
        val pfd = ParcelFileDescriptor.open(tmp, ParcelFileDescriptor.MODE_READ_ONLY)

        val service = mock<IDollOSAIService> {
            on { openCharacterAsset("live2d/test.model3.json") } doReturn pfd
        }
        val reader = CharacterAssetReader(service)

        val content = reader.readText("live2d/test.model3.json")

        assertEquals("""{"hello":"world"}""", content)
        tmp.delete()
    }

    @Test(expected = CharacterAssetNotFoundException::class)
    fun `readText throws when AIDL returns null`() {
        val service = mock<IDollOSAIService> {
            on { openCharacterAsset(any()) } doReturn null
        }
        val reader = CharacterAssetReader(service)
        reader.readText("missing.json")
    }

    @Test
    fun `readBytes returns binary content`() {
        val tmp = File.createTempFile("model", ".moc3")
        tmp.writeBytes(byteArrayOf(0x01, 0x02, 0x03, 0x04))
        val pfd = ParcelFileDescriptor.open(tmp, ParcelFileDescriptor.MODE_READ_ONLY)

        val service = mock<IDollOSAIService> {
            on { openCharacterAsset("live2d/test.moc3") } doReturn pfd
        }
        val reader = CharacterAssetReader(service)

        val bytes = reader.readBytes("live2d/test.moc3")

        assertArrayEquals(byteArrayOf(0x01, 0x02, 0x03, 0x04), bytes)
        tmp.delete()
    }
}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.character.CharacterAssetReaderTest" 2>&1 | tail -20
```

Expected: FAIL — `CharacterAssetReader` / `CharacterAssetNotFoundException` 不存在。

- [ ] **Step 3: 實作 CharacterAssetReader**

Create `app/src/main/java/org/dollos/launcher/character/CharacterAssetReader.kt`:

```kotlin
package org.dollos.launcher.character

import android.os.ParcelFileDescriptor
import org.dollos.ai.IDollOSAIService
import java.io.FileInputStream

class CharacterAssetNotFoundException(path: String) : RuntimeException("Asset not found: $path")

/**
 * 統一透過 AIDL 讀當前 active character pack 內的檔案。
 * 由 `IDollOSAIService.openCharacterAsset(path)` 取得 ParcelFileDescriptor 後讀成 bytes / text。
 */
class CharacterAssetReader(private val aiService: IDollOSAIService) {

    fun readBytes(path: String): ByteArray {
        val pfd: ParcelFileDescriptor = aiService.openCharacterAsset(path)
            ?: throw CharacterAssetNotFoundException(path)
        return FileInputStream(pfd.fileDescriptor).use { it.readBytes() }.also { pfd.close() }
    }

    fun readText(path: String): String = String(readBytes(path), Charsets.UTF_8)
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.character.CharacterAssetReaderTest" 2>&1 | tail -20
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/DollOSLauncher
git add app/src/main/java/org/dollos/launcher/character/CharacterAssetReader.kt \
        app/src/test/java/org/dollos/launcher/character/CharacterAssetReaderTest.kt
git commit -m "feat(character): add CharacterAssetReader for AIDL-based asset IO"
```

---

## Task 5: Live2DMotionController（state machine + 組合）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/live2d/Live2DMotionController.kt`
- Create: `DollOSLauncher/app/src/test/java/org/dollos/launcher/live2d/Live2DMotionControllerTest.kt`

- [ ] **Step 1: 寫失敗測試**

```kotlin
package org.dollos.launcher.live2d

import org.junit.Assert.*
import org.junit.Test

class Live2DMotionControllerTest {

    private fun mappings() = mapOf(
        "idle" to "idle.motion3.json",
        "listening" to "listen.motion3.json",
        "thinking" to "think.motion3.json",
        "speaking" to "speak.motion3.json"
    )

    @Test
    fun `initial state is idle, idle motion active`() {
        val c = Live2DMotionController(mappings())
        assertEquals(setOf(OpsFlag.IDLE), c.activeFlags())
        assertEquals(listOf("idle.motion3.json"), c.desiredMotions())
    }

    @Test
    fun `listening sets listening motion, removes idle`() {
        val c = Live2DMotionController(mappings())
        c.onOp("asr_started")
        assertEquals(setOf(OpsFlag.LISTENING), c.activeFlags())
        assertEquals(listOf("listen.motion3.json"), c.desiredMotions())
    }

    @Test
    fun `listening plus thinking combined plays both motions`() {
        val c = Live2DMotionController(mappings())
        c.onOp("asr_started")
        c.onOp("llm_in_flight")
        assertEquals(setOf(OpsFlag.LISTENING, OpsFlag.THINKING), c.activeFlags())
        assertEquals(listOf("listen.motion3.json", "think.motion3.json"), c.desiredMotions())
    }

    @Test
    fun `asr_ended clears listening, thinking remains`() {
        val c = Live2DMotionController(mappings())
        c.onOp("asr_started")
        c.onOp("llm_in_flight")
        c.onOp("asr_ended")
        assertEquals(setOf(OpsFlag.THINKING), c.activeFlags())
        assertEquals(listOf("think.motion3.json"), c.desiredMotions())
    }

    @Test
    fun `all ops ended returns to idle`() {
        val c = Live2DMotionController(mappings())
        c.onOp("tts_playing")
        c.onOp("tts_ended")
        assertEquals(setOf(OpsFlag.IDLE), c.activeFlags())
        assertEquals(listOf("idle.motion3.json"), c.desiredMotions())
    }

    @Test
    fun `unknown op is ignored without state change`() {
        val c = Live2DMotionController(mappings())
        val before = c.activeFlags()
        c.onOp("weird_op")
        assertEquals(before, c.activeFlags())
    }

    @Test
    fun `speaking and listening combined`() {
        // 使用者打斷 Doll 講話的情境：TTS 還沒停、使用者又開始講
        val c = Live2DMotionController(mappings())
        c.onOp("tts_playing")
        c.onOp("asr_started")
        assertEquals(setOf(OpsFlag.SPEAKING, OpsFlag.LISTENING), c.activeFlags())
    }
}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DMotionControllerTest" 2>&1 | tail -20
```

Expected: FAIL — class 不存在。

- [ ] **Step 3: 實作 Live2DMotionController**

```kotlin
package org.dollos.launcher.live2d

enum class OpsFlag { IDLE, LISTENING, THINKING, SPEAKING }

/**
 * 依 DollOSCore ops event 名稱管理 active motion flags。
 * 允許組合：LISTENING + THINKING 可同時 active（打斷場景）。
 * 不含 GL 邏輯，純狀態機便於測試。
 */
class Live2DMotionController(
    private val animationMappings: Map<String, String>
) {
    private val flags = mutableSetOf(OpsFlag.IDLE)

    fun activeFlags(): Set<OpsFlag> = flags.toSet()

    fun desiredMotions(): List<String> = flags.mapNotNull { flag ->
        when (flag) {
            OpsFlag.IDLE -> animationMappings["idle"]
            OpsFlag.LISTENING -> animationMappings["listening"]
            OpsFlag.THINKING -> animationMappings["thinking"]
            OpsFlag.SPEAKING -> animationMappings["speaking"]
        }
    }

    fun onOp(opName: String) {
        when (opName) {
            "asr_started" -> setFlag(OpsFlag.LISTENING, on = true)
            "asr_ended" -> setFlag(OpsFlag.LISTENING, on = false)
            "llm_in_flight" -> setFlag(OpsFlag.THINKING, on = true)
            "llm_returned" -> setFlag(OpsFlag.THINKING, on = false)
            "tts_playing" -> setFlag(OpsFlag.SPEAKING, on = true)
            "tts_ended" -> setFlag(OpsFlag.SPEAKING, on = false)
            else -> { /* unknown ops are no-op */ }
        }
    }

    private fun setFlag(flag: OpsFlag, on: Boolean) {
        if (on) {
            flags.remove(OpsFlag.IDLE)
            flags.add(flag)
        } else {
            flags.remove(flag)
            if (flags.isEmpty()) flags.add(OpsFlag.IDLE)
        }
    }
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DMotionControllerTest" 2>&1 | tail -20
```

Expected: All 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/live2d/Live2DMotionController.kt \
        app/src/test/java/org/dollos/launcher/live2d/Live2DMotionControllerTest.kt
git commit -m "feat(live2d): add MotionController state machine for ops events"
```

---

## Task 6: Live2DLipSync（amplitude → parameter）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/live2d/Live2DLipSync.kt`
- Create: `DollOSLauncher/app/src/test/java/org/dollos/launcher/live2d/Live2DLipSyncTest.kt`

- [ ] **Step 1: 寫失敗測試**

```kotlin
package org.dollos.launcher.live2d

import org.junit.Assert.*
import org.junit.Test

class Live2DLipSyncTest {

    @Test
    fun `amplitude zero maps to mouth closed`() {
        val ls = Live2DLipSync(enabled = true, smoothing = 0.0f)
        ls.feedAmplitude(0.0f)
        assertEquals(0.0f, ls.currentOpenness(), 0.001f)
    }

    @Test
    fun `amplitude full maps to mouth full open`() {
        val ls = Live2DLipSync(enabled = true, smoothing = 0.0f)
        ls.feedAmplitude(1.0f)
        assertEquals(1.0f, ls.currentOpenness(), 0.001f)
    }

    @Test
    fun `amplitude clamps above one`() {
        val ls = Live2DLipSync(enabled = true, smoothing = 0.0f)
        ls.feedAmplitude(2.5f)
        assertEquals(1.0f, ls.currentOpenness(), 0.001f)
    }

    @Test
    fun `disabled always returns zero`() {
        val ls = Live2DLipSync(enabled = false, smoothing = 0.0f)
        ls.feedAmplitude(0.9f)
        assertEquals(0.0f, ls.currentOpenness(), 0.001f)
    }

    @Test
    fun `smoothing lerps toward target`() {
        val ls = Live2DLipSync(enabled = true, smoothing = 0.5f)
        ls.feedAmplitude(1.0f)  // first call: 0 + 0.5*(1-0) = 0.5
        assertEquals(0.5f, ls.currentOpenness(), 0.001f)
        ls.feedAmplitude(1.0f)  // second: 0.5 + 0.5*(1-0.5) = 0.75
        assertEquals(0.75f, ls.currentOpenness(), 0.001f)
    }
}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DLipSyncTest" 2>&1 | tail -20
```

- [ ] **Step 3: 實作**

```kotlin
package org.dollos.launcher.live2d

/**
 * TTS amplitude → Live2D mouth parameter openness (0.0-1.0)。
 * `smoothing` 是 lerp 係數，越大越跟得緊（0 = 不平滑、1 = 完全不動）。
 * 建議 0.3-0.5 之間可得自然張合。
 */
class Live2DLipSync(
    private val enabled: Boolean,
    private val smoothing: Float
) {
    private var openness: Float = 0.0f

    fun feedAmplitude(raw: Float) {
        if (!enabled) { openness = 0.0f; return }
        val target = raw.coerceIn(0.0f, 1.0f)
        openness += smoothing * (target - openness)
    }

    fun currentOpenness(): Float = openness

    fun reset() { openness = 0.0f }
}
```

- [ ] **Step 4: 測試通過**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DLipSyncTest" 2>&1 | tail -20
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/live2d/Live2DLipSync.kt \
        app/src/test/java/org/dollos/launcher/live2d/Live2DLipSyncTest.kt
git commit -m "feat(live2d): add LipSync amplitude mapper with smoothing"
```

---

## Task 7: Live2DTouchInteractor（hit area dispatcher）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/live2d/Live2DTouchInteractor.kt`
- Create: `DollOSLauncher/app/src/test/java/org/dollos/launcher/live2d/Live2DTouchInteractorTest.kt`

- [ ] **Step 1: 寫失敗測試**

```kotlin
package org.dollos.launcher.live2d

import org.dollos.ai.character.TouchInteraction
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.mockito.kotlin.*

class Live2DTouchInteractorTest {

    private fun interactions() = listOf(
        TouchInteraction(
            hitArea = "Head",
            motion = "pat.motion3.json",
            eventName = "user_headpat",
            eventPayload = JSONObject().put("intensity", "soft")
        ),
        TouchInteraction(
            hitArea = "Body",
            motion = "poke.motion3.json",
            eventName = "user_poke",
            eventPayload = JSONObject()
        )
    )

    @Test
    fun `hit Head triggers headpat motion and event`() {
        val motionPlayer = mock<MotionPlayer>()
        val eventSink = mock<TouchEventSink>()
        val hitTest = mock<HitTester> { on { hit(0.5f, 0.2f) } doReturn "Head" }

        val interactor = Live2DTouchInteractor(interactions(), hitTest, motionPlayer, eventSink)
        interactor.onTap(0.5f, 0.2f)

        verify(motionPlayer).playOneShot("pat.motion3.json")
        argumentCaptor<String>().apply {
            argumentCaptor<JSONObject>().apply {
                verify(eventSink).send(eq("user_headpat"), check { payload ->
                    assertEquals("soft", payload.getString("intensity"))
                })
            }
        }
    }

    @Test
    fun `hit unknown area does nothing`() {
        val motionPlayer = mock<MotionPlayer>()
        val eventSink = mock<TouchEventSink>()
        val hitTest = mock<HitTester> { on { hit(any(), any()) } doReturn null }

        val interactor = Live2DTouchInteractor(interactions(), hitTest, motionPlayer, eventSink)
        interactor.onTap(0.1f, 0.1f)

        verifyNoInteractions(motionPlayer)
        verifyNoInteractions(eventSink)
    }

    @Test
    fun `hit area with no matching interaction does nothing`() {
        val motionPlayer = mock<MotionPlayer>()
        val eventSink = mock<TouchEventSink>()
        val hitTest = mock<HitTester> { on { hit(any(), any()) } doReturn "Tail" }  // 無對應

        val interactor = Live2DTouchInteractor(interactions(), hitTest, motionPlayer, eventSink)
        interactor.onTap(0.9f, 0.9f)

        verifyNoInteractions(motionPlayer)
        verifyNoInteractions(eventSink)
    }
}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DTouchInteractorTest" 2>&1 | tail -20
```

- [ ] **Step 3: 實作**

```kotlin
package org.dollos.launcher.live2d

import org.dollos.ai.character.TouchInteraction
import org.json.JSONObject

/** Hit test 介面，由 Live2DModelHolder 提供實作。輸入標準化座標 (0-1)。 */
interface HitTester {
    fun hit(normalizedX: Float, normalizedY: Float): String?
}

/** 播放一次性 motion。由 Live2DMotionController 的底層 motion queue 提供實作。 */
interface MotionPlayer {
    fun playOneShot(motionPath: String)
}

/** 觸發事件到 AIService / Core。由 DollOSLauncherActivity 提供 AIDL 封裝實作。 */
interface TouchEventSink {
    fun send(eventName: String, payload: JSONObject)
}

/**
 * 依 Character Pack manifest 定義 route Touch event 到對應 motion + 外部事件。
 * 不觸發系統功能，純情感互動（見 spec §3）。
 */
class Live2DTouchInteractor(
    private val interactions: List<TouchInteraction>,
    private val hitTester: HitTester,
    private val motionPlayer: MotionPlayer,
    private val eventSink: TouchEventSink
) {
    fun onTap(normalizedX: Float, normalizedY: Float) {
        val area = hitTester.hit(normalizedX, normalizedY) ?: return
        val interaction = interactions.firstOrNull { it.hitArea == area } ?: return
        motionPlayer.playOneShot(interaction.motion)
        eventSink.send(interaction.eventName, interaction.eventPayload)
    }
}
```

- [ ] **Step 4: 測試通過**

```bash
./gradlew :app:testDebugUnitTest --tests "org.dollos.launcher.live2d.Live2DTouchInteractorTest" 2>&1 | tail -20
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/live2d/Live2DTouchInteractor.kt \
        app/src/test/java/org/dollos/launcher/live2d/Live2DTouchInteractorTest.kt
git commit -m "feat(live2d): add TouchInteractor dispatching hit areas to motion + events"
```

---

## Task 8: Live2DModelHolder（Cubism SDK 整合）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/live2d/Live2DModelHolder.kt`

> **注意：** 此 task 主要是把 Live2D SDK Sample 的 `LAppModel.java` 邏輯 Kotlin 化 + 改用 `CharacterAssetReader` 讀資料。測試以後續 Task 11 的 instrumented test 驗證（GL context 需要）。

- [ ] **Step 1: 實作 Live2DModelHolder**

Create `app/src/main/java/org/dollos/launcher/live2d/Live2DModelHolder.kt`:

```kotlin
package org.dollos.launcher.live2d

import android.util.Log
import com.live2d.sdk.cubism.framework.CubismFramework
import com.live2d.sdk.cubism.framework.ICubismModelSetting
import com.live2d.sdk.cubism.framework.CubismModelSettingJson
import com.live2d.sdk.cubism.framework.math.CubismMatrix44
import com.live2d.sdk.cubism.framework.model.CubismUserModel
import com.live2d.sdk.cubism.framework.motion.ACubismMotion
import com.live2d.sdk.cubism.framework.motion.CubismMotion
import com.live2d.sdk.cubism.framework.rendering.android.CubismRendererAndroid
import org.dollos.ai.character.Live2DConfig
import org.dollos.launcher.character.CharacterAssetReader

/**
 * 封裝單一 Live2D 模型的生命週期：載入 moc3、model3.json、motion3.json、textures。
 * 以 CharacterAssetReader 透過 AIDL 讀所有資產。
 *
 * 公開 API：
 * - loadModel(): 讀 model3.json + moc3 + textures + motions，建立 CubismUserModel
 * - update(deltaSeconds): 推進 motion 時間
 * - draw(matrix): 交給 CubismRendererAndroid 繪製
 * - startMotion(path, priority): 切換 motion
 * - setLipSyncValue(openness): 設定 mouth 參數
 * - testHit(x, y): 回傳命中的 hit area 名稱或 null
 */
class Live2DModelHolder(
    private val reader: CharacterAssetReader,
    private val config: Live2DConfig
) : CubismUserModel() {

    companion object { private const val TAG = "Live2DModelHolder" }

    private lateinit var modelSetting: ICubismModelSetting
    private var currentMotion: ACubismMotion? = null

    fun loadModel() {
        val model3Json = reader.readBytes(config.modelPath)
        modelSetting = CubismModelSettingJson(model3Json)

        // 載入 moc3
        val mocPath = resolveRelative(config.modelPath, modelSetting.modelFileName)
        val mocBytes = reader.readBytes(mocPath)
        loadModel(mocBytes, modelSetting.mocConsistencyValidationFlag)

        // Physics（選用）
        modelSetting.physicsFileName?.takeIf { it.isNotEmpty() }?.let { physics ->
            val physicsBytes = reader.readBytes(resolveRelative(config.modelPath, physics))
            loadPhysics(physicsBytes)
        }

        // Pose（選用）
        modelSetting.poseFileName?.takeIf { it.isNotEmpty() }?.let { pose ->
            val poseBytes = reader.readBytes(resolveRelative(config.modelPath, pose))
            loadPose(poseBytes)
        }

        // Renderer
        setupRenderer(CubismRendererAndroid.create())

        // Textures
        for (i in 0 until modelSetting.textureCount) {
            val texPath = resolveRelative(config.modelPath, modelSetting.getTextureFileName(i))
            val texBytes = reader.readBytes(texPath)
            val textureId = Live2DTextureManager.loadFromBytes(texBytes)
            (renderer as CubismRendererAndroid).bindTexture(i, textureId)
        }

        // Lip sync parameter 註冊
        if (config.lipSync.enabled) {
            addLipSyncParameterId(config.lipSync.parameterId)
        }

        Log.i(TAG, "Live2D model loaded from ${config.modelPath}")
    }

    fun update(deltaSeconds: Float) {
        model.loadParameters()
        currentMotion?.updateParameters(model, deltaSeconds)
        model.saveParameters()
        physics?.evaluate(model, deltaSeconds)
        pose?.updateParameters(model, deltaSeconds)
        model.update()
    }

    fun draw(projection: CubismMatrix44) {
        (renderer as CubismRendererAndroid).also {
            it.setMvpMatrix(projection)
            it.drawModel()
        }
    }

    fun startMotion(motionPath: String, priority: Int = 2) {
        val bytes = reader.readBytes(motionPath)
        val motion = CubismMotion.create(bytes, null)
        currentMotion = motion
    }

    fun setLipSyncValue(openness: Float) {
        if (!config.lipSync.enabled) return
        model.setParameterValue(config.lipSync.parameterId, openness.toDouble())
    }

    fun testHit(normalizedX: Float, normalizedY: Float): String? {
        for (i in 0 until modelSetting.hitAreasCount) {
            val areaName = modelSetting.getHitAreaName(i)
            val paramId = modelSetting.getHitAreaId(i)
            if (isHit(paramId, normalizedX, normalizedY)) return areaName
        }
        return null
    }

    private fun resolveRelative(base: String, child: String): String {
        val parent = base.substringBeforeLast('/', "")
        return if (parent.isEmpty()) child else "$parent/$child"
    }
}
```

> **注意：** 實際 API 名稱依 Live2D Cubism SDK for Java 最新版本調整（SDK 有時更新 API）。本 task 若遇到編譯錯誤，對照 SDK `Sample/Demo/src/main/java/com/live2d/demo/LAppModel.java` 相對應段落修正。

- [ ] **Step 2: 加 Live2DTextureManager（bitmap → GL texture）**

Create `app/src/main/java/org/dollos/launcher/live2d/Live2DTextureManager.kt`:

```kotlin
package org.dollos.launcher.live2d

import android.graphics.BitmapFactory
import android.opengl.GLES20
import android.opengl.GLUtils

object Live2DTextureManager {

    fun loadFromBytes(bytes: ByteArray): Int {
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: throw IllegalArgumentException("Could not decode texture bytes")

        val ids = IntArray(1)
        GLES20.glGenTextures(1, ids, 0)
        val textureId = ids[0]

        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, textureId)
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR_MIPMAP_LINEAR)
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
        GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, bitmap, 0)
        GLES20.glGenerateMipmap(GLES20.GL_TEXTURE_2D)

        bitmap.recycle()
        return textureId
    }
}
```

- [ ] **Step 3: 驗證 build 通過**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew assembleDebug 2>&1 | tail -30
```

若 Framework API 錯配，比照 SDK Sample 的 `LAppModel.java` / `LAppTextureManager.java` 修正欄位名稱 / method 簽名。修完再跑。

Expected: BUILD SUCCESSFUL。

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/live2d/Live2DModelHolder.kt \
        app/src/main/java/org/dollos/launcher/live2d/Live2DTextureManager.kt
git commit -m "feat(live2d): add ModelHolder + TextureManager over Cubism Framework"
```

---

## Task 9: Live2DRenderer（TextureView + GL context）

**Files:**
- Create: `DollOSLauncher/app/src/main/java/org/dollos/launcher/live2d/Live2DRenderer.kt`

- [ ] **Step 1: 實作 Renderer**

Create `app/src/main/java/org/dollos/launcher/live2d/Live2DRenderer.kt`:

```kotlin
package org.dollos.launcher.live2d

import android.graphics.SurfaceTexture
import android.opengl.*
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Choreographer
import android.view.Surface
import android.view.TextureView
import com.live2d.sdk.cubism.framework.math.CubismMatrix44

/**
 * TextureView 承載的 Live2D renderer。獨立 GL thread，Choreographer 驅動。
 *
 * 使用流程：
 *   val renderer = Live2DRenderer(textureView)
 *   renderer.setModel(modelHolder)
 *   renderer.start()
 *   // … 運作中 …
 *   renderer.stop()
 */
class Live2DRenderer(private val textureView: TextureView) {

    companion object { private const val TAG = "Live2DRenderer" }

    private var glThread: HandlerThread? = null
    private var glHandler: Handler? = null
    private var eglDisplay: EGLDisplay = EGL14.EGL_NO_DISPLAY
    private var eglContext: EGLContext = EGL14.EGL_NO_CONTEXT
    private var eglSurface: EGLSurface = EGL14.EGL_NO_SURFACE
    private var modelHolder: Live2DModelHolder? = null
    private var lastFrameNs: Long = 0L
    private var running: Boolean = false
    private var viewportW: Int = 0
    private var viewportH: Int = 0

    fun setModel(holder: Live2DModelHolder) { this.modelHolder = holder }

    fun start() {
        if (running) return
        running = true
        val thread = HandlerThread("Live2DGL").apply { start() }
        glThread = thread
        glHandler = Handler(thread.looper)

        textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(s: SurfaceTexture, w: Int, h: Int) {
                glHandler?.post { initGL(s); viewportW = w; viewportH = h; scheduleFrame() }
            }
            override fun onSurfaceTextureSizeChanged(s: SurfaceTexture, w: Int, h: Int) {
                glHandler?.post { viewportW = w; viewportH = h }
            }
            override fun onSurfaceTextureDestroyed(s: SurfaceTexture): Boolean {
                glHandler?.post { releaseGL() }
                return true
            }
            override fun onSurfaceTextureUpdated(s: SurfaceTexture) {}
        }
    }

    fun stop() {
        running = false
        glHandler?.post { releaseGL() }
        glThread?.quitSafely()
        glThread = null
        glHandler = null
    }

    private fun scheduleFrame() {
        if (!running) return
        Choreographer.getInstance().postFrameCallback {
            glHandler?.post { renderFrame() }
            scheduleFrame()
        }
    }

    private fun initGL(surfaceTexture: SurfaceTexture) {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        val version = IntArray(2)
        EGL14.eglInitialize(eglDisplay, version, 0, version, 1)

        val configAttrs = intArrayOf(
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8,
            EGL14.EGL_BLUE_SIZE, 8, EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_NONE
        )
        val configs = arrayOfNulls<EGLConfig>(1)
        val numConfig = IntArray(1)
        EGL14.eglChooseConfig(eglDisplay, configAttrs, 0, configs, 0, 1, numConfig, 0)

        val ctxAttrs = intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE)
        eglContext = EGL14.eglCreateContext(eglDisplay, configs[0], EGL14.EGL_NO_CONTEXT, ctxAttrs, 0)

        eglSurface = EGL14.eglCreateWindowSurface(
            eglDisplay, configs[0], Surface(surfaceTexture), intArrayOf(EGL14.EGL_NONE), 0
        )
        EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext)

        GLES20.glClearColor(0.0f, 0.0f, 0.0f, 1.0f)
        GLES20.glEnable(GLES20.GL_BLEND)
        GLES20.glBlendFunc(GLES20.GL_ONE, GLES20.GL_ONE_MINUS_SRC_ALPHA)

        modelHolder?.loadModel()
    }

    private fun renderFrame() {
        val holder = modelHolder ?: return
        val now = System.nanoTime()
        val dt = if (lastFrameNs == 0L) 1.0f / 60f else (now - lastFrameNs) / 1_000_000_000f
        lastFrameNs = now

        GLES20.glViewport(0, 0, viewportW, viewportH)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)

        holder.update(dt)
        val projection = CubismMatrix44.create()
        val aspect = viewportW.toFloat() / viewportH.toFloat()
        if (viewportW > viewportH) projection.scale(1.0f / aspect, 1.0f)
        else projection.scale(1.0f, aspect)
        holder.draw(projection)

        EGL14.eglSwapBuffers(eglDisplay, eglSurface)
    }

    private fun releaseGL() {
        if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
            EGL14.eglMakeCurrent(eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
            if (eglSurface != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(eglDisplay, eglSurface)
            if (eglContext != EGL14.EGL_NO_CONTEXT) EGL14.eglDestroyContext(eglDisplay, eglContext)
            EGL14.eglTerminate(eglDisplay)
        }
        eglDisplay = EGL14.EGL_NO_DISPLAY
        eglContext = EGL14.EGL_NO_CONTEXT
        eglSurface = EGL14.EGL_NO_SURFACE
    }
}
```

- [ ] **Step 2: 驗證 build**

```bash
./gradlew assembleDebug 2>&1 | tail -15
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit**

```bash
git add app/src/main/java/org/dollos/launcher/live2d/Live2DRenderer.kt
git commit -m "feat(live2d): add TextureView-based GL renderer with Choreographer loop"
```

---

## Task 10: DollOSLauncherActivity 精簡 + Live2D 接入

**Files:**
- Modify: `DollOSLauncher/app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`
- Modify: `DollOSLauncher/app/src/main/res/layout/activity_launcher.xml`

- [ ] **Step 1: 修改 Layout — 留 TextureView + 字幕，移除 drawer hint / 輸入框（若有）**

Edit `app/src/main/res/layout/activity_launcher.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<FrameLayout
    xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="@android:color/black">

    <TextureView
        android:id="@+id/live2d_surface"
        android:layout_width="match_parent"
        android:layout_height="match_parent" />

    <TextView
        android:id="@+id/subtitle_bubble"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:layout_gravity="bottom"
        android:padding="24dp"
        android:gravity="center"
        android:textSize="20sp"
        android:textColor="#FFFFFF"
        android:background="#80000000"
        android:visibility="gone" />
</FrameLayout>
```

- [ ] **Step 2: 重寫 DollOSLauncherActivity（精簡版）**

Edit `app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt`:

```kotlin
package org.dollos.launcher

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.view.MotionEvent
import android.view.TextureView
import android.widget.TextView
import org.dollos.ai.IDollOSAICallback
import org.dollos.ai.IDollOSAIService
import org.dollos.ai.character.CharacterManifest
import org.dollos.launcher.character.CharacterAssetReader
import org.dollos.launcher.live2d.*
import org.json.JSONObject

class DollOSLauncherActivity : Activity() {

    companion object { private const val TAG = "DollOSLauncher" }

    private lateinit var textureView: TextureView
    private lateinit var subtitleBubble: TextView
    private var aiService: IDollOSAIService? = null
    private var renderer: Live2DRenderer? = null
    private var modelHolder: Live2DModelHolder? = null
    private var motionController: Live2DMotionController? = null
    private var lipSync: Live2DLipSync? = null
    private var touchInteractor: Live2DTouchInteractor? = null

    private val serviceConn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            aiService = IDollOSAIService.Stub.asInterface(binder)
            aiService?.registerCallback(aiCallback)
            initCharacter()
        }
        override fun onServiceDisconnected(name: ComponentName) {
            aiService = null
        }
    }

    private val aiCallback = object : IDollOSAICallback.Stub() {
        override fun onOpsEvent(opName: String, stateJson: String?) {
            motionController?.onOp(opName)
            // 依 desiredMotions 切 motion（取頭一個作為主，其餘組合交 MotionHolder 排佇列）
            motionController?.desiredMotions()?.firstOrNull()?.let { path ->
                modelHolder?.startMotion(path)
            }
        }
        override fun onTtsAmplitude(amplitude: Float) {
            lipSync?.feedAmplitude(amplitude)
            lipSync?.currentOpenness()?.let { modelHolder?.setLipSyncValue(it) }
        }
        override fun onSubtitle(text: String?) {
            runOnUiThread {
                if (text.isNullOrEmpty()) {
                    subtitleBubble.visibility = android.view.View.GONE
                } else {
                    subtitleBubble.text = text
                    subtitleBubble.visibility = android.view.View.VISIBLE
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_launcher)
        textureView = findViewById(R.id.live2d_surface)
        subtitleBubble = findViewById(R.id.subtitle_bubble)

        textureView.setOnTouchListener { _, ev ->
            if (ev.action == MotionEvent.ACTION_UP) {
                val nx = ev.x / textureView.width
                val ny = ev.y / textureView.height
                touchInteractor?.onTap(nx, ny)
            }
            true
        }

        val intent = Intent().setClassName("org.dollos.ai", "org.dollos.ai.DollOSAIService")
        bindService(intent, serviceConn, BIND_AUTO_CREATE)
    }

    override fun onDestroy() {
        super.onDestroy()
        aiService?.unregisterCallback(aiCallback)
        unbindService(serviceConn)
        renderer?.stop()
    }

    private fun initCharacter() {
        val service = aiService ?: return
        val manifestJson = service.getActiveCharacterManifestJson() ?: run {
            Log.w(TAG, "No active character; cannot init Live2D")
            return
        }
        val manifest = CharacterManifest.fromJson(JSONObject(manifestJson))
        val live2d = manifest.live2d ?: run {
            Log.e(TAG, "Active character has no live2d block")
            return
        }

        val reader = CharacterAssetReader(service)
        modelHolder = Live2DModelHolder(reader, live2d)
        motionController = Live2DMotionController(live2d.animationMappings)
        lipSync = Live2DLipSync(live2d.lipSync.enabled, smoothing = 0.4f)
        touchInteractor = Live2DTouchInteractor(
            interactions = manifest.touchInteractions,
            hitTester = object : HitTester {
                override fun hit(x: Float, y: Float) = modelHolder?.testHit(x, y)
            },
            motionPlayer = object : MotionPlayer {
                override fun playOneShot(p: String) { modelHolder?.startMotion(p) }
            },
            eventSink = object : TouchEventSink {
                override fun send(eventName: String, payload: JSONObject) {
                    service.triggerTouchEvent(eventName, payload.toString())
                }
            }
        )

        renderer = Live2DRenderer(textureView).apply {
            setModel(modelHolder!!)
            start()
        }
    }
}
```

> **注意：** `getActiveCharacterManifestJson()` 與 `triggerTouchEvent(name, payloadJson)` 這兩個 AIDL method 若目前沒有需在 Task 11 加入（AIService 端實作為讀 active pack manifest JSON、送 touch event 到 EventQueue）。`onTtsAmplitude` 與 `onSubtitle` 若 `IDollOSAICallback` 沒有對應 method 也在 Task 11 加入。

- [ ] **Step 3: 驗證 build（可能缺 AIDL method，暫不跑，紀錄缺口）**

```bash
./gradlew assembleDebug 2>&1 | tail -20
```

若 AIDL method 缺，編譯錯誤；進 Task 11 補齊。

- [ ] **Step 4: Commit（即使有缺口先 commit scaffold）**

```bash
git add app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt \
        app/src/main/res/layout/activity_launcher.xml
git commit -m "refactor(launcher): rewrite Activity around Live2D renderer + AIService callback"
```

---

## Task 11: AIDL method 補齊（AIService 端 + Launcher 同步）

**Files:**
- Modify: `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAICallback.aidl`
- Modify: `DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAIService.aidl`
- Modify: `DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAICallback.aidl`
- Modify: AIService binder 實作

- [ ] **Step 1: 確認缺哪些 method**

```bash
grep -E "getActiveCharacterManifestJson|triggerTouchEvent|onOpsEvent|onTtsAmplitude|onSubtitle" \
  ~/Projects/DollOSAIService/app/src/main/aidl/org/dollos/ai/*.aidl
```

記下缺少的 method。

- [ ] **Step 2: AIDL 加 method（AIService 端）**

`IDollOSAIService.aidl` 加（如缺）:

```aidl
/** 取得目前 active character pack 的 manifest.json 內容（UTF-8 字串）。 */
String getActiveCharacterManifestJson();

/** Launcher 傳來觸碰 hit area 觸發事件，進 EventQueue。 */
void triggerTouchEvent(String eventName, String payloadJson);
```

`IDollOSAICallback.aidl` 加（如缺）:

```aidl
/** Core ops event（參見 spec §4.2）。stateJson 目前未使用，保留擴充。 */
void onOpsEvent(String opName, String stateJson);

/** TTS 播放中的瞬時音量，0.0-1.0，用於 lip sync。 */
void onTtsAmplitude(float amplitude);

/** 目前字幕文字。null/空字串代表收起。 */
void onSubtitle(String text);
```

- [ ] **Step 3: 同步 AIDL 到 Launcher**

```bash
cp ~/Projects/DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAIService.aidl \
   ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAIService.aidl
cp ~/Projects/DollOSAIService/app/src/main/aidl/org/dollos/ai/IDollOSAICallback.aidl \
   ~/Projects/DollOSLauncher/app/aidl/org/dollos/ai/IDollOSAICallback.aidl
```

- [ ] **Step 4: AIService binder 實作 getActiveCharacterManifestJson**

Edit AIService binder impl（處 `IDollOSAIService.Stub()`）加：

```kotlin
override fun getActiveCharacterManifestJson(): String? {
    val pack = characterManager.getActivePack() ?: return null
    return pack.manifest.toJson().toString()
}

override fun triggerTouchEvent(eventName: String?, payloadJson: String?) {
    if (eventName.isNullOrEmpty()) return
    val payload = try { JSONObject(payloadJson ?: "{}") } catch (_: Exception) { JSONObject() }
    eventQueue.enqueue(Event.TouchEvent(eventName, payload))
}
```

若 `Event.TouchEvent` 不存在，在 EventQueue 相關 sealed class 加：

```kotlin
data class TouchEvent(val name: String, val payload: JSONObject) : Event()
```

- [ ] **Step 5: AIService callback 發送 ops event / amplitude / subtitle**

Edit 現有 conversation engine / TTS player 發送路徑：

- 原本廣播 ops event 的地方（ASR / LLM / TTS 生命週期）改呼叫 `callback.onOpsEvent(opName, null)`。
- TTS player 在播放時每幀回報音量（實作：AudioTrack 寫入 buffer 時 sample 最大振幅 / RMS）→ `callback.onTtsAmplitude(value)`。
- Conversation engine 有字幕輸出時 → `callback.onSubtitle(text)`，結束時 `callback.onSubtitle(null)`。

> **注意：** 具體實作位置視既有 AIService 結構決定，此 task 僅定義行為。若 AIService 內部沒有統一的「發 ops event」位置，另起 `OpsEventBroadcaster` 類集中處理。

- [ ] **Step 6: AIService build + install**

```bash
cd ~/Projects/DollOSAIService && ./gradlew assembleDebug 2>&1 | tail -10
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 7: Launcher build**

```bash
cd ~/Projects/DollOSLauncher && ./gradlew assembleDebug 2>&1 | tail -10
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 8: Commit（AIService + Launcher）**

```bash
cd ~/Projects/DollOSAIService
git add app/src/main/aidl/ app/src/main/java/
git commit -m "aidl: add manifest query, touch event, ops callback, lip sync + subtitle callbacks"

cd ~/Projects/DollOSLauncher
git add app/aidl/
git commit -m "aidl: sync new methods from AIService"
```

---

## Task 12: 移除 Filament scene + drawer + character picker

**Files:**
- Delete: `DollOSLauncher/app/src/main/java/org/dollos/launcher/scene/`（整個目錄）
- Delete: `DollOSLauncher/app/src/main/java/org/dollos/launcher/drawer/`（整個目錄）
- Delete: `DollOSLauncher/app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt`

- [ ] **Step 1: 驗證 DollOSLauncherActivity 已無 import 這些類別**

```bash
grep -E "FilamentSceneManager|AvatarAnimator|SceneConfig|AppDrawerView|AppInfo|AppListAdapter|RecentAppsAdapter|CharacterPickerOverlay" \
  ~/Projects/DollOSLauncher/app/src/main/java/org/dollos/launcher/DollOSLauncherActivity.kt
```

Expected: 無輸出。若有輸出表示 Task 10 精簡不完全，先回去清。

- [ ] **Step 2: 刪除檔案**

```bash
cd ~/Projects/DollOSLauncher
rm -rf app/src/main/java/org/dollos/launcher/scene/
rm -rf app/src/main/java/org/dollos/launcher/drawer/
rm app/src/main/java/org/dollos/launcher/character/CharacterPickerOverlay.kt
```

- [ ] **Step 3: 驗證無殘留引用**

```bash
grep -rE "org\.dollos\.launcher\.scene|org\.dollos\.launcher\.drawer|CharacterPickerOverlay" \
  ~/Projects/DollOSLauncher/app/src/
```

Expected: 無輸出。

- [ ] **Step 4: Build**

```bash
./gradlew assembleDebug 2>&1 | tail -10
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 5: Commit**

```bash
git add -A app/src/main/java/org/dollos/launcher/
git commit -m "refactor(launcher): remove Filament scene + app drawer + character picker"
```

---

## Task 13: haru.doll 打包 + 安裝 + 手動 end-to-end 驗證

**Files:**
- Create: `DollOSLauncher/tools/pack_haru.sh`（打包 script）
- Create: `haru.doll`（打包產物，臨時）

- [ ] **Step 1: 寫打包 script**

Create `DollOSLauncher/tools/pack_haru.sh`:

```bash
#!/usr/bin/env bash
# 把 app/src/main/assets/haru_placeholder/ 打包成 haru.doll (v2 格式) 供 AIService import
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$SCRIPT_DIR/../app/src/main/assets/haru_placeholder"
OUT="$SCRIPT_DIR/../build/haru.doll"
STAGE=$(mktemp -d)

mkdir -p "$STAGE/live2d"
cp -r "$APP_ROOT"/* "$STAGE/live2d/"

cat > "$STAGE/manifest.json" << 'EOF'
{
  "formatVersion": 2,
  "name": "Haru (Placeholder)",
  "version": "1.0.0",
  "author": "Live2D Cubism SDK Sample",
  "description": "Placeholder Live2D model for technical validation.",
  "avatarType": "live2d",
  "created": "2026-04-24",
  "wakeWord": null,
  "voiceReferenceFile": null,
  "voiceReferenceText": null,
  "live2d": {
    "modelPath": "live2d/Haru.model3.json",
    "animationMappings": {
      "idle": "live2d/motions/idle_00.motion3.json",
      "listening": "live2d/motions/idle_01.motion3.json",
      "thinking": "live2d/motions/idle_02.motion3.json",
      "speaking": "live2d/motions/tap_00.motion3.json"
    },
    "lipSync": { "enabled": true, "parameterId": "ParamMouthOpenY" },
    "background": { "type": "solid", "color": "#0D0D1A" }
  },
  "touchInteractions": [
    {
      "hitArea": "Head",
      "motion": "live2d/motions/tap_01.motion3.json",
      "eventName": "user_headpat",
      "eventPayload": { "intensity": "soft" }
    },
    {
      "hitArea": "Body",
      "motion": "live2d/motions/tap_02.motion3.json",
      "eventName": "user_poke",
      "eventPayload": {}
    }
  ]
}
EOF

mkdir -p "$(dirname "$OUT")"
(cd "$STAGE" && zip -r "$OUT" .)
echo "Packed: $OUT"
rm -rf "$STAGE"
```

```bash
chmod +x ~/Projects/DollOSLauncher/tools/pack_haru.sh
```

> **注意：** 若實際 Haru SDK sample 的 motion 檔名不同，依實際內容調整 `animationMappings`（範例中假設 motion 集合為 `idle_00/01/02/tap_00/01/02`）。

- [ ] **Step 2: 執行打包**

```bash
cd ~/Projects/DollOSLauncher && ./tools/pack_haru.sh
ls -la build/haru.doll
```

Expected: `haru.doll` 檔案產生。

- [ ] **Step 3: Build + deploy Launcher 與 AIService**

參考 CLAUDE.md 的 build commands 章節，Gradle build → prebuilt 複製 → AOSP `m` → adb push 或完整 rebuild。

```bash
cd ~/Projects/DollOSLauncher && ./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk \
   ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk

cd ~/Projects/DollOSAIService && ./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/

cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSLauncher DollOSAIService -j$(nproc)
```

- [ ] **Step 4: Flash 並在裝置匯入 haru.doll**

```bash
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push ~/Projects/DollOS-build/out/target/product/bluejay/system_ext/priv-app/DollOSLauncher/DollOSLauncher.apk \
  /system_ext/priv-app/DollOSLauncher/
adb push ~/Projects/DollOS-build/out/target/product/bluejay/system_ext/priv-app/DollOSAIService/DollOSAIService.apk \
  /system_ext/priv-app/DollOSAIService/
adb push ~/Projects/DollOSLauncher/build/haru.doll /sdcard/Download/
adb reboot
```

開機後透過 AIService 提供的 Character import UI / Settings 匯入 `/sdcard/Download/haru.doll`。若既有 import 流程尚可用則走其 UI；若壞了在 Task 11 / Task 1 檢視。

- [ ] **Step 5: 手動驗證 checklist**

對照 spec §11 驗收條件逐項確認：

```
[ ] Launcher home 能載入 Haru 並顯示全螢幕（無黑屏、無 crash）
[ ] 說「嗨 Doll」→ Haru 切到 listening motion（eyes / face 變化可見）
[ ] Doll 回應時 → Haru 嘴巴同步開合
[ ] 點 Haru 頭部 → 播放 tap 動作 + logcat 可看到 user_headpat 事件進 EventQueue
[ ] 點 Haru 身體 → 播放不同 tap 動作 + user_poke 事件
[ ] 試匯入一個 v1 manifest pack → CharacterValidator 回錯誤訊息
[ ] grep -r filament ~/Projects/DollOSLauncher/app/src → 無結果
[ ] 螢幕開啟 home 5 分鐘：logcat 無持續錯誤、fps ≥ 30（可用 Layout Inspector 看）
```

- [ ] **Step 6: 紀錄結果**

將手動驗證結果（通過 / 失敗 / 觀察）寫入 `docs/superpowers/plans/2026-04-24-avatar-live2d-launcher-result.md`（執行時再建）。

- [ ] **Step 7: 最終 commit（工具與結果）**

```bash
cd ~/Projects/DollOSLauncher
git add tools/pack_haru.sh
git commit -m "tools: add pack_haru.sh for Live2D placeholder .doll generation"

cd ~/Projects/DollOS
git add docs/superpowers/plans/2026-04-24-avatar-live2d-launcher-result.md
git commit -m "plan-result: avatar live2d launcher end-to-end verification"
```

---

## Task 14: Terminal 重構 plan 標註對齊

**Files:**
- Modify: `docs/superpowers/plans/2026-04-20-doll-terminal-launcher.md`

- [ ] **Step 1: 在 plan 頂端加注記**

Edit `docs/superpowers/plans/2026-04-20-doll-terminal-launcher.md`，在 `**Goal:**` 上方加：

```markdown
> **2026-04-24 更新：** 本 plan 的「Filament 3D」工作已被 `2026-04-24-avatar-live2d-launcher.md` 完全替換為 Live2D。執行本 plan 時：
> - `FilamentSceneManager` / `AvatarAnimator` / `SceneConfig` / Filament 依賴：**已在 2026-04-24 plan 中移除，略過此 plan 中所有對應 task**
> - `CompositeAnimator.kt` 等新增 3D 控制器：**替換為 `Live2DMotionController` / `Live2DModelHolder`**（已在 2026-04-24 plan 建立）
> - 本 plan 剩餘重點：**DollOSCore AIDL rebind**（從 AIService 改到 Core）+ **移除最後殘餘的 Launcher 舊功能**
> 執行前先核對目前 Launcher 狀態。
```

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/DollOS
git add docs/superpowers/plans/2026-04-20-doll-terminal-launcher.md
git commit -m "plan: annotate Terminal Launcher plan with 2026-04-24 Live2D overlay"
```

---

## 自我檢查

### 規格覆蓋

| Spec 條目 | 對應 task |
|---|---|
| §2 畫面狀態矩陣 - home 全螢幕 Live2D | Task 9, 10, 13 |
| §2 in-app 邊緣指示 | **Out of scope**（Plan 2）|
| §2.1 鎖屏 | **Out of scope**（Plan 3）|
| §2.2 字幕 | Task 10（subtitle_bubble View + onSubtitle callback）|
| §3 觸碰互動（.doll 定義）| Task 1, 7, 10, 13 |
| §4 Renderer 替換 | Task 0, 3, 8, 9, 12 |
| §5 Character Pack v2 格式 | Task 1, 2 |
| §6 gura.doll 遷移（Haru placeholder）| Task 3, 13 |
| §7.1 Launcher plan diff 修訂 | Task 14 |
| §11 驗收 | Task 13 |

### Placeholder / 模糊掃描

- Task 8 Step 1 有「具體 API 名稱依 SDK 最新版本調整」— 這不是 placeholder，是現實（SDK 會演進），engineer 對照 SDK sample 即可
- Task 11 Step 5 有「若 AIService 內部沒有統一的『發 ops event』位置，另起 OpsEventBroadcaster 類集中處理」— 動作明確，位置靈活，可接受
- Task 13 Step 1 motion 檔名假設 — 會依實際 Haru SDK 版本調整，script 中直接改
- 其餘 step 皆有具體 code / command，無 TBD / TODO 字樣

### 型別一致性

- `CharacterManifest.live2d: Live2DConfig?` — Task 1 定義，Task 10 使用
- `Live2DConfig.animationMappings: Map<String, String>` — Task 1 定義，Task 5 / Task 10 使用
- `TouchInteraction` fields — Task 1 定義，Task 7 / Task 10 使用
- `OpsFlag` enum values — Task 5 定義與使用
- `HitTester` / `MotionPlayer` / `TouchEventSink` 介面 — Task 7 定義，Task 10 用 inline anonymous object 實作

---

## 執行選項

**Plan complete and saved to `docs/superpowers/plans/2026-04-24-avatar-live2d-launcher.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 每個 task 派獨立 subagent、task 間 review、快速迭代

**2. Inline Execution** - 在此 session 執行，checkpoints review

**Which approach?**
