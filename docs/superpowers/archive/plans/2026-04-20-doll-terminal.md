# Doll AI Terminal — Master Plan

> **All 8 app plans reference this master for shared contracts.** Each app plan contains app-specific tasks only.

**Date:** 2026-04-20
**Status:** Draft
**Steps:** 6 sequential steps, 9 plan files total (1 master + 8 app plans)
**Not split into MVP / post-MVP.** All 6 steps are one continuous engineering effort. v1.0 is not released until everything is done.

---

## §1 六步總覽

| Step | 範圍 | 包含 plan |
|------|------|-----------|
| **1. 基底** | Core 骨架 + Observer + LLM Router 骨架 + AIService 改造 | Core §1-5, Observer §1-3 |
| **2. 對話 + 讀空氣 + 記憶基礎** | Output Orchestrator + `[SILENT]` + 對話路徑 + Memory 多層 + Launcher 精簡 + Character Pack v2 | Core §6-9, Memory §1-3, Launcher §1-2 |
| **3. 內在生活 + 主動性** | Internal Life + 四態 placement + vibrate-to-request + Mood/Attention | Core §10-11, Observer §4-5, Voice §10-11 |
| **4. 記憶成長 + 蒸餾** | Fork subagent + POLICY 寫入 + ObjectBox 蒸餾層 + session/semantic search | Memory §4-6 |
| **5. 本地 Aux + 拆完剩下 app** | Gemma 4 E4B/E2B + 抽出 AuxEngine/Memory/Skills/Voice + AIService 退役 | AuxEngine §1-3, Skills §1-2, Voice §12-13 |
| **6. 完整生活感 + 安全網** | Routines + sleep detection + overlays + Skills Library + DollOSService 安全網 | Skills §3-4, Service §1-2, Core §12-13 |

**依賴圖：**
```
Step 1 ──► Step 2 ──► Step 3 ──► Step 4 ──► Step 5 ──► Step 6
```

中間沒有可停點。每一步都是「為她能每天陪我過日子」服務。

---

## §2 App 責任表 + 依賴圖

| App | 新/既有/重構 | 主要依賴 |
|-----|-------------|---------|
| **DollOSCore** | 新 | Observer (observation events), AuxEngine (aux tier), Memory (frozen prompts), Skills (skill execution), Voice (TTS trigger) |
| **DollOSObserver** | 新 | Core (postObservation AIDL) |
| **DollOSAuxEngine** | 新 | 無（被 Core 呼叫） |
| **DollOSMemory** | 新（抽自 AIService）| 無（被 Core 透過 AIDL client 讀寫） |
| **DollOSSkills** | 新 | Core (postSkillCallback AIDL), DollOSService (system actions via uisage skill) |
| **DollOSVoice** | 新（抽自 AIService）| Core (trigger via IDollCore AIDL) |
| **DollOSLauncher** | 重構 | Core (IDollCoreStateListener callbacks) |
| **DollOSService** | 既有 | Core (emergencyStop AIDL) |

**實作順序建議：** Core + Observer 最先（Step 1），其他 app 在 Step 5 一次拆出。Launcher 在 Step 2 開始重構（依賴 Core AIDL 骨架）。Service 在 Step 6 補安全網。

---

## §3 共用 AIDL 介面

所有 AIDL 檔頂部加 `// Version: 1`，version 在 plan 階段釘住（spec §10 決定）。

### 3.1 IDollCore — DollOSCore 對外主介面

```aidl
package dollos.core;

import dollos.core.ObservationEvent;
import dollos.core.SkillCallbackResult;
import dollos.core.IDollCoreStateListener;

/**
 * Doll Core — the brain and nervous system.
 * Called by: Observer, Launcher, Voice, Skills, Service, AuxEngine.
 */
interface IDollCore {
    // === Observation input (Observer → Core) ===
    void postObservation(ObservationEvent event);

    // === Skill callback (Skills → Core) ===
    void postSkillCallback(SkillCallbackResult result);

    // === Conversation trigger (Voice / Launcher → Core) ===
    void triggerConversation(String source, in Bundle extras);
    // source values: "wake_word", "pickup", "chest_press", "gesture", "button"

    // === State queries (Launcher / Service → Core) ===
    String getContextSnapshotJson();

    // === Control (Service / Skills → Core) ===
    void setDndActive(boolean active, String reason);
    void emergencyStop(String reason);

    // === State listener registration (Launcher → Core) ===
    void registerStateListener(IDollCoreStateListener listener);
    void unregisterStateListener(IDollCoreStateListener listener);

    // === Character Pack hot-reload (Skills → Core) ===
    void applyCharacterPack(in Bundle packConfig);
}
```

### 3.2 IDollCoreStateListener — Core → Launcher 狀態廣播

```aidl
package dollos.core;

/**
 * Ops events broadcast by Core. Launcher subscribes to drive 3D animation states.
 */
interface IDollCoreStateListener {
    void onOp(String opName, String stateJson);
    // opName values: "llm_in_flight", "llm_returned", "tts_playing",
    //   "tts_ended", "asr_started", "asr_ended", "vibrate",
    //   "emergency_stop", "emergency_stop_recovered", "flag_changed",
    //   "skill_started", "skill_completed", "overlay_added", "overlay_removed"
    // stateJson: optional JSON payload (e.g., {"tier":"main"} for llm_in_flight)
}
```

### 3.3 ObservationEvent — Observer → Core 事件

```aidl
package dollos.core;

/**
 * Observation events from DollOSObserver → DollOSCore.
 * See master §6 Event Types Catalog for all types and payload schemas.
 */
parcelable ObservationEvent {
    String type;           // §6 event type (e.g., "placement.changed")
    Map<String, String> payload;  // §6 per-type payload schema
    long timestampMs;
}
```

### 3.4 SkillCallbackResult — Skills → Core 回調

```aidl
package dollos.core;

/**
 * Result from a skill execution callback.
 */
parcelable SkillCallbackResult {
    String skillId;       // e.g., "alarm", "weather"
    String status;        // "success", "error", "progress"
    String resultJson;    // skill-specific JSON payload
    long timestampMs;
}
```

### 3.5 IDollVoice — DollOSVoice 對外介面

```aidl
package dollos.voice;

import dollos.voice.VoiceConfig;

/**
 * Voice pipeline — KWS, ASR, TTS, VAD, Speaker ID.
 * Called by: Core (trigger listening), Skills (TTS for skill results).
 */
interface IDollVoice {
    // === Start/stop listening (Core → Voice) ===
    void startListening(String source);
    // source: "wake_word", "pickup", "chest_press", "gesture", "manual"
    void stopListening();

    // === TTS trigger (Core / Skills → Voice) ===
    void speak(String text);
    void stopSpeaking();

    // === Character Pack hot-reload ===
    void applyConfig(in VoiceConfig config);

    // === Status ===
    boolean isListening();
    boolean isSpeaking();
}

parcelable VoiceConfig {
    String wakeWordModelPath;
    String ttsModelPath;
    String ttsTokensPath;
    String ttsESpeakDataPath;
    String speakerIdModelPath;
}
```

### 3.6 IDollAuxEngine — DollOSAuxEngine 對外介面

```aidl
package dollos.aux;

/**
 * Local Aux LLM engine.
 * Called by: Core (LLM Router), Memory (distillation).
 */
interface IDollAuxEngine {
    // === General LLM call ===
    String generate(String systemPrompt, String userPrompt, int maxTokens);

    // === Specialized methods ===
    String classify(String audioFeatures, in List<String> labels);
    String summarize(String text, int maxTokens);
    String silentJudgment(String context, in List<String> options);

    // === Model lifecycle ===
    boolean isModelLoaded();
    void loadModel(String modelPath);
    void unloadModel();

    // === Diagnostics ===
    EngineInfo getEngineInfo();
}

parcelable EngineInfo {
    String modelAlias;
    float tps;           // tokens per second
    long usedMemoryBytes;
    long totalMemoryBytes;
}
```

### 3.7 IDollMemory — DollOSMemory 對外介面

```aidl
package dollos.memory;

/**
 * Multi-layer memory store.
 * Called by: Core (frozen prompts, session search), Skills (skill metadata), AuxEngine (distillation).
 */
interface IDollMemory {
    // === File-based memory (SOUL/USER/POLICY) ===
    String readFile(String fileName);  // "SOUL.md", "USER.md", "POLICY.md"
    void writeFile(String fileName, String content);
    void appendToFile(String fileName, String content);
    List<String> listFiles();

    // === Conversation log (FTS4) ===
    void appendConversation(String role, String content, long timestampMs);
    List<String> searchConversation(String query, int limit);

    // === Distillation layer (ObjectBox) ===
    void appendDistillationEntry(String summary, String embeddingJson, long timestampMs);
    List<String> semanticSearch(String query, int limit);

    // === Character Pack management ===
    void saveCharacterPack(String packId, in ParcelFileDescriptor modelFile,
                           String soulMd, String overlaysDirPath,
                           String initialPolicyMd, String skillsDirPath);
    String loadCharacterPackSoul(String packId);
    List<String> listCharacterPacks();
}
```

### 3.8 IDollSkills — DollOSSkills 對外介面

```aidl
package dollos.skills;

/**
 * Skills bundle runtime + library.
 * Called by: Core (execute skill, list skills), Service (system actions).
 */
interface IDollSkills {
    // === Discovery ===
    List<String> listSkills();  // returns skill IDs
    String getSkillMetadata(String skillId);  // returns SKILL.md content (expanded)

    // === Execution ===
    void executeSkill(String skillId, in Bundle params);

    // === Routines ===
    void triggerRoutine(String routineId);  // "morning", "bedtime", "home", "away"
    void registerOneShotRoutine(String trigger, in Bundle config);

    // === Skill callback (back to Core via IDollCore.postSkillCallback) ===
    // Note: Skills does NOT call Core directly; it returns results to caller
    // who then posts to Core. This avoids circular dependency.
}
```

---

## §4 Character Pack v2 格式

### 4.1 檔案結構

```
.doll file (zip)
├── manifest.json
├── personality/
│   ├── SOUL.md                  ← slot #1 (identity)
│   ├── overlays/
│   │   ├── formal.md           ← overlay text
│   │   ├── playful.md
│   │   └── cold.md
│   └── initial_policy.md       ← default rules
├── model.glb                    ← 3D avatar
├── animations/
│   ├── idle.fbx
│   ├── thinking.fbx
│   └── talking.fbx
├── voice/
│   └── tts-vits/
│       ├── model.onnx
│       └── tokens.txt
├── wake_word.onnx              ← openWakeWord model
├── scene.json                  ← Filament scene config
├── thumbnail.png               ← 128x128 preview
└── skills/                     ← character-specific skills
    └── <skill_dir>/
        ├── SKILL.md
        └── scripts/
```

### 4.2 manifest.json

```json
{
  "version": 2,
  "id": "rin-v1",
  "name": "凜 (Rin)",
  "description": "冷靜毒舌 + 天然呆的隨身 Doll",
  "author": "user",
  "modelFile": "model.glb",
  "soulFile": "personality/SOUL.md",
  "initialPolicyFile": "personality/initial_policy.md",
  "overlayDir": "personality/overlays/",
  "sceneFile": "scene.json",
  "wakeWordFile": "wake_word.onnx",
  "voiceDir": "voice/",
  "skillsDir": "skills/",
  "createdAt": "2026-04-20T00:00:00Z"
}
```

### 4.3 SOUL.md 格式

```markdown
# Identity

<role definition, personality traits, speech patterns>

# Voice

<how she speaks: tone, catchphrases, language preference>

# Boundaries

<what she won't do, safety limits>
```

### 4.4 Overlay 格式

```markdown
# Overlay: formal

<override text that is inserted into system prompt after base SOUL.md>
<use imperative declarations, not instructions>
```

### 4.5 遷移：v1 → v2

- 既有 `.doll` 檔案（無 SOUL.md / overlays / skills）視為 v1
- 載入時自動補空 SOUL.md（從 personality.json 轉）
- 存檔時升級為 v2 manifest
- 不破壞既有使用者資料

---

## §5 記憶檔案格式

### 5.1 USER.md

```markdown
# User Profile

## Facts
- <declarative fact about user>
- e.g., "Prefers responses in Traditional Chinese"

## Preferences
- e.g., "Likes concise summaries, not long explanations"

## History
- <significant events worth remembering>
```

**Constraints:**
- Max 2200 chars
- Declarative only ("user prefers X" not "always be X")
- Safety scan before write (no credentials, no hidden unicode)
- When full: review prompt forces consolidation

### 5.2 POLICY.md

```markdown
# Rules

## Conversation Rules
- e.g., "Respond in Traditional Chinese unless asked otherwise"

## Behavior Rules
- e.g., "Don't speak during quiet hours (22:00-07:00)"

## Preferences
- e.g., "Vibrate instead of speak when in pocket"
```

**Constraints:**
- Max 2200 chars
- Same declarative rule as USER.md
- Editable via conversation ("remember that I prefer X")
- Visible in minimal Settings entry

### 5.3 SOUL.md

See §4.3. Max chars: no hard limit (part of system prompt, bounded by context window).

### 5.4 檔案實體位置

```
/data/system_ext/dollos/memory/
├── SOUL.md          ← current character's soul
├── USER.md
├── POLICY.md
├── packs/           ← per-character pack data
│   └── <packId>/
│       ├── manifest.json
│       └── model.glb (symlink to /data/system_ext/dollos/characters/)
└── distillation/    ← distilled summaries + embedding index
    └── entries.db   ← Room database (FTS4 + ObjectBox)
```

---

## §6 Event Types Catalog

所有 `ObservationEvent.type` 值 + `payload` schema。Context Engine 的 `ObservationReducer` 必須覆蓋所有 type。

### 6.1 Mic + VAD

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `mic.vad_start` | VAD | 否 | `{}` |
| `mic.vad_end` | VAD | 否 | `{durationMs: "<int>"}` |
| `mic.classified` | VAD + Aux | 是 | `{label: "<quiet|conversation|noisy|calling_me>", confidence: "<float>"}` |
| `mic.transcript` | ASR | 否 | `{text: "<string>", source: "<wake_word|pickup|chest_press|gesture>"}` |

### 6.2 Accelerometer + Gyro

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `motion.state` | Accelerometer | 否 | `{state: "<still|walking|moving_fast>"}` |

### 6.3 Proximity + Light

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `placement.changed` | Proximity + Light | 否 | `{from: "<pocket|hand|stand|chest|unknown>", to: "<pocket|hand|stand|chest|unknown>"}` |
| `placement.stable` | Proximity + Light | 否 | `{placement: "<pocket|hand|stand|chest>", durationSec: "<int>"}` |

### 6.4 WiFi + Location

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `location.changed` | WiFi SSID + GPS | 否 | `{from: "<home|work|out|unknown>", to: "<home|work|out|unknown>", ssid: "<string?>"}` |

### 6.5 System State

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `system.dnd.changed` | Settings API | 否 | `{active: "<true|false>"}` |
| `system.sleep.inferred` | Sleep detection | 否 | `{state: "<awake|sleeping>", confidence: "<float>"}` |
| `system.battery.changed` | BatteryManager | 否 | `{level: "<int>", charging: "<true|false>"}` |
| `system.screen.changed` | WindowManager | 否 | `{state: "<on|off>", trigger: "<user|auto|charging>"}` |
| `system.notification` | NotificationListener | 否 | `{tag: "<string>", summary: "<string>", app: "<string>"}` |

### 6.6 Time

| Type | Producer | Aux? | Payload |
|------|----------|------|---------|
| `time.hour_changed` | Clock | 否 | `{hour: "<int>", minute: "<int>"}` |

---

## §7 測試策略

### 7.1 測試分層

| 層級 | 工具 | 覆蓋 | 執行時機 |
|------|------|------|---------|
| Unit | JUnit4 + MockK + Robolectric | 所有純 Kotlin 邏輯 | `./gradlew test` |
| Instrumented | AndroidX Test | AIDL binding + service lifecycle + real device behavior | `./gradlew connectedAndroidTest` |
| E2E | 自寫 instrumentation | 跨 app AIDL flow（Observer → Core → Output） | 裝機後手動 |
| Smoke | 腳本 | APK 裝機 + service running + AIDL bind + basic op | 每次 build |

### 7.2 關鍵測試矩陣

| 測試場景 | App | 層級 |
|----------|-----|------|
| `[SILENT]` / `[SPEAK]` / `[VIBRATE]` / `[INTERRUPT]` 解析 | Core | Unit |
| Read-the-Air Gate: DND + Speak → Silent | Core | Unit |
| Silent pending re-evaluation | Core | Unit |
| Frozen system prompt: freeze → forKey → overlay → endSession | Core | Unit |
| EventBus: post → receive order + buffer behavior | Core | Unit |
| ContextSnapshot: serialize/deserialize + reducer all types | Core | Unit |
| Mood/Attention: noteIgnored → patience decay | Core | Unit |
| Emergency stop: full loop | Core | Instrumented |
| triggerConversation E2E | Core | Instrumented |
| ObservationEvent → ContextEngine apply all types | Observer | Unit |
| Placement classifier logic | Observer | Unit |
| Sleep detection heuristic | Observer | Unit |
| Aux LLM generate + model lifecycle | AuxEngine | Unit + Instrumented |
| Prompt templates generation | AuxEngine | Unit |
| SOUL/USER/POLICY read/write + chars limit | Memory | Unit |
| FTS4 conversation search | Memory | Instrumented |
| Skill metadata scan + progressive disclosure | Skills | Unit |
| Routine trigger + execution | Skills | Instrumented |
| KWS → VAD → ASR chain | Voice | Instrumented |
| TTS speak + stop | Voice | Instrumented |
| Character Pack hot-reload (wake word + TTS model swap) | Voice | Instrumented |
| Launcher 3D animation state transitions | Launcher | Instrumented |
| Emergency stop via power menu | Service | Instrumented |

### 7.3 Smoke Test 腳本

每個 app 提供 `scripts/smoke_<app>.sh`：
1. `./gradlew :app:assembleRelease`
2. `adb push` APK + ODex
3. `adb reboot`
4. 驗證：`dumpsys activity services` / `dumpsys package` 確認 service 註冊
5. AIDL bind test（`service call` 或 instrumentation）

---

## §8 建構與部署

### 8.1 Gradle 專案慣例

所有 app 遵循 DollOSAIService 的 Gradle 結構：
- `settings.gradle.kts` + `build.gradle.kts` (root) + `app/build.gradle.kts`
- `namespace = "dollos.<app>"`
- `compileSdk = 34, minSdk = 34, targetSdk = 34`
- `buildFeatures { aidl = true }`

### 8.2 AOSP 整合路徑

| App | AOSP 路徑 | 類型 |
|-----|----------|------|
| DollOSCore | `external/DollOSCore/` | priv-app |
| DollOSObserver | `external/DollOSObserver/` | priv-app |
| DollOSAuxEngine | `external/DollOSAuxEngine/` | priv-app |
| DollOSMemory | `external/DollOSMemory/` | priv-app |
| DollOSSkills | `external/DollOSSkills/` | priv-app |
| DollOSVoice | `external/DollOSVoice/` | priv-app |
| DollOSLauncher | `packages/apps/DollOSLauncher/` | priv-app (重構) |
| DollOSService | `frameworks/base/services/` / priv-app | system priv-app (既有) |

### 8.3 Android.bp 慣例

```bp
prebuilt_android_arm64 {
    name: "DollOSCore",
    src: "prebuilt/DollOSCore.apk",
    optimizations: {
        release_pgo_instr: { enabled: false },
        release_jni_compile_options: { enabled: false },
    },
    cert: "platform",
    module_in_install_namespace: true,
}
```

### 8.4 Build 命令

```bash
# Per-app build
cd ~/Projects/<App>/
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/<App>.apk

# Sync to AOSP build tree
rsync -av --delete . ~/Projects/DollOS-build/external/<App>/

# AOSP build
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m <App> -j$(nproc)

# Deploy
adb root && adb remount
adb push /system_ext/priv-app/<App>/<App>.apk /system_ext/priv-app/<App>/
adb reboot
```

---

## §9 依賴清單（跨 plan）

每個 plan 的「不實作但引用」介面：

| DollOSCore 引用 | 由 plan 負責 |
|----------------|-------------|
| `dollos.memory.IDollMemory` | Memory plan |
| `dollos.aux.IDollAuxEngine` | AuxEngine plan |
| `dollos.voice.IDollVoice` | Voice plan |
| `dollos.skills.IDollSkills` | Skills plan |

| DollOSObserver 引用 | 由 plan 負責 |
|-------------------|-------------|
| `dollos.core.IDollCore` | Core plan (AIDL 宣告) |

| DollOSLauncher 引用 | 由 plan 負責 |
|-------------------|-------------|
| `dollos.core.IDollCore` | Core plan (AIDL 宣告) |
| `dollos.core.IDollCoreStateListener` | Core plan |

| DollOSService 引用 | 由 plan 負責 |
|-------------------|-------------|
| `dollos.core.IDollCore` | Core plan (AIDL 宣告) |

| DollOSVoice 引用 | 由 plan 負責 |
|-----------------|-------------|
| `dollos.core.IDollCore` | Core plan |

| DollOSSkills 引用 | 由 plan 負責 |
|------------------|-------------|
| `dollos.core.IDollCore` | Core plan |

| DollOSAuxEngine 引用 | 由 plan 負責 |
|---------------------|-------------|
| 無 | — |

| DollOSMemory 引用 | 由 plan 負責 |
|------------------|-------------|
| 無（被其他 app 呼叫） | — |

---

## §10 Plan 階段決定（待確認）

| 決定 | 選項 | 建議 | 影響 plan |
|------|------|------|----------|
| **Gemma 4 本地推論 runtime** | ONNX Runtime / MLC / llama.cpp / MediaPipe | ONNX Runtime（與現有 wakeword/TTS 工具鏈一致） | AuxEngine |
| **雲端 Main LLM provider** | Anthropic / OpenAI / Gemini | Anthropic（既有 DollOSAIService 已支援） | Core |
| **Memory 檔案位置** | `/data/system_ext/dollos/memory/` | 此路徑（與其他 system_ext 元件一致） | Memory |
| **Skills 檔案位置** | 內建: `/data/system_ext/dollos/skills/`，角色包: `/data/system_ext/dollos/characters/<id>/skills/` | 此路徑 | Skills |
| **Foreground service notification** | 最小化文字 + icon | 最小化（使用者可自訂關閉？不，系統強制要有） | Core |
| **Character Pack v1 → v2 遷移** | 自動升級（見 §4.5） | 自動升級 | Memory |
| **鬧鐘 skill** | AlarmManager + 自己排程 | AlarmManager（AOSP 既有 API） | Skills |
| **AIDL 版本化** | 手動 version 欄位 + backward compat | 手動 `// Version: N` + 不破壞性新增 method | 所有 plan |
| **Core OOM_adj** | `foreground` / `persistent` | `foreground`（已有 foreground service） | Core |
| **記憶檔案備份/還原** | adb pull/push + 未來雲端同步 | adb pull/push（v1.0 不碰雲端） | Memory |

---

## §11 執行順序與並行建議

### Step 1 並行軌道
```
軌道 A: Core §1-5 (骨架 + EventBus + Context + Handler framework)
軌道 B: Observer §1-3 (sensors + VAD + placement + system state)
```
A 和 B 可並行——它們只透過 AIDL 契約耦合，契約定義在 master §3。

### Step 2 並行軌道
```
軌道 A: Core §6-9 (Output Orchestrator + Frozen Prompt + Internal Life)
軌道 B: Memory §1-3 (MD files + FTS4 + ContentProvider)
軌道 C: Launcher §1-2 (remove app drawer, pure 3D shell)
```
三個軌道可並行——依賴都在 master §3 的 AIDL。

### Step 3-6
逐步推進，每個 step 內可小範圍並行。

---

## §12 交付判準

v1.0 完成 = **所有 8 份 plan 的交付判準都滿足**：

1. 所有 task 的 commit 都在 main
2. `./gradlew test` 全綠（所有 8 個 app）
3. `./gradlew connectedAndroidTest` 全綠（需要 device）
4. 所有 8 個 APK 都能在 AOSP build 編過
5. 裝機 smoke：8 個 service 都在跑、AIDL bind 全通
6. **完整路徑走通**：wake word → ASR → Core → LLM → `[SPEAK "hi"]` → TTS → 聽到聲音
7. **完整路徑走通**：idle 5 分鐘 → inner thought → Aux LLM → `[SILENT]`
8. **完整路徑走通**：DND on → `[SPEAK]` → 降 Silent + silent_pending
9. **完整路徑走通**：emergency stop → service 停止 + DND on
10. **完整路徑走通**：Character Pack 切換 → SOUL.md 更換 + 3D model 更換

---

**Plan complete.** 9 份 plan 全部寫完後進入 self-review。
