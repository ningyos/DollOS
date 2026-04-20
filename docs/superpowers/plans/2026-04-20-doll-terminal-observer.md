# DollOSObserver Implementation Plan — Doll AI Terminal v1.0

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 `DollOSObserver` — always-on foreground service Android app，負責感測器、mic VAD、物理情境分類、系統狀態監聽、sleep detection。產生 master plan §6 定義的 `ObservationEvent` 事件流，透過 `IDollCore.postObservation()` 推送給 `DollOSCore`。

**Tech Stack:** Kotlin、AndroidX、Android SensorManager、silero_vad.onnx（既有模型檔）、ONNX Runtime for Android、Kotlin Coroutines + Flow、AIDL Binder client、WorkManager（idle/timer）、WifiManager、LocationManager、FusedLocationProviderClient。

**Spec reference:** `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md` §4.4（Observation Pipeline）

**Master reference:** `docs/superpowers/plans/2026-04-20-doll-terminal.md`
- §3.1 `IDollCore.aidl` — `postObservation(ObservationEvent)` 介面契約
- §3.3 `ObservationEvent.aidl` — parcelable schema（type / payload: Map<String, String> / timestampMs）
- §3.4 `IDollAuxEngine.aidl` — `classify(input, labels)` 用於 mic 聲音分類
- §6 — Event Types Catalog（本 plan 產生的所有 type 必須對應 §6 列表，不可自創）
- §7 — App 依賴清單（本 plan 引用 IDollCore AIDL）
- §8 — Build / Deploy 慣例（Gradle → prebuilt APK → AOSP `system_ext` priv-app）
- §9 — 測試策略（TDD、unit → integration，真機測試在 subagent 執行）

**Scope 邊界：**
- 本 app **只做 producer** — 產生事件、推給 Core。
- **不做** Core 事件消費邏輯、LLM routing、Output Orchestrator。
- **不做** Aux LLM runtime（只 call `IDollAuxEngine` 已暴露的 `classify()`）。
- **不做** 對話決策、不寫 Memory。
- VAD 使用既有 `silero_vad.onnx`（從 DollOSAIService 複製過來），不重新訓練。
- Mic 分類 label 集：`"quiet" | "conversation" | "calling_me" | "noisy"`（對齊 master §6.1 `mic.classified` payload label values）。

**心態：** 先把 producers 獨立做好（每個都能在 adb logcat 看到事件），最後串一次整合測試。每個 producer 都可獨立 enable/disable 便於除錯。真機測試都 dispatch subagent，避免截圖 / 感測器 dump 吃主 context。

---

## 段落索引

1. App 骨架 + foreground service（Task 1-6）
2. AIDL client binder（Task 7-10）
3. Accelerometer + Gyro producer（Task 11-15）
4. Proximity + Light producer（Task 16-19）
5. Mic + VAD producer（Task 20-24）
6. Aux classify 整合（Task 25-28）
7. System state listener（Task 29-33）
8. Location listener（Task 34-37）
9. Timer / idle（Task 38-41）
10. Placement 四態 classifier（Task 42-45）
11. Sleep detection（Task 46-48）
12. 整合測試（Task 49-52）

**Total tasks：** 52

---

## §1 App 骨架 + foreground service

### Task 1 — Gradle 專案初始化
- [ ] 建立 `~/Projects/DollOSObserver/` 目錄
- [ ] `settings.gradle.kts`：`rootProject.name = "DollOSObserver"`、`include(":app")`
- [ ] `build.gradle.kts`（root）：AGP 8.x、Kotlin 1.9+、相依 Google + Maven Central
- [ ] `app/build.gradle.kts`：
  - `applicationId = "dollos.observer"`
  - `minSdk = 34`、`targetSdk = 35`、`compileSdk = 35`
  - Kotlin、AndroidX、coroutines、ONNX Runtime（`com.microsoft.onnxruntime:onnxruntime-android:1.17.1`）
  - Unit test：JUnit 4、MockK、Robolectric
  - AndroidTest：AndroidX test、Espresso 可省
- [ ] `gradle/wrapper/`、`gradlew`：從 `DollOSAIService` 複製 wrapper 同步版本
- [ ] 驗證：`./gradlew tasks` 成功
- [ ] Commit: `observer: bootstrap Gradle project skeleton`

### Task 2 — AndroidManifest + permissions
- [ ] `app/src/main/AndroidManifest.xml`：
  - `<uses-permission>` 列：`RECORD_AUDIO`、`ACCESS_FINE_LOCATION`、`ACCESS_COARSE_LOCATION`、`ACCESS_WIFI_STATE`、`CHANGE_WIFI_STATE`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MICROPHONE`、`FOREGROUND_SERVICE_LOCATION`、`FOREGROUND_SERVICE_SPECIAL_USE`、`POST_NOTIFICATIONS`、`HIGH_SAMPLING_RATE_SENSORS`、`ACTIVITY_RECOGNITION`
  - `<uses-feature>`：`android.hardware.sensor.accelerometer`、`gyroscope`、`proximity`、`light`、`microphone`
  - `<application android:name=".ObserverApp">`，註冊 service（下一 task）
- [ ] Commit: `observer: declare permissions and features in manifest`

### Task 3 — 建立 ObserverApp + ObserverService 骨架（TDD：先寫 test）
- [ ] **Test first：** `app/src/test/kotlin/dollos/observer/ObserverServiceTest.kt`：
  - Robolectric 啟動 service，驗證 `onCreate` 建立 foreground notification channel
  - 驗證 `onStartCommand` return `START_STICKY`
- [ ] Run test → FAIL
- [ ] 實作 `app/src/main/kotlin/dollos/observer/ObserverApp.kt`（Application subclass）
- [ ] 實作 `app/src/main/kotlin/dollos/observer/ObserverService.kt`：
  - extends `Service`
  - `onCreate()`：建立 notification channel `"observer_foreground"`（LOW importance）
  - `onStartCommand()`：`startForeground(NOTIFICATION_ID, buildNotification())`、return `START_STICKY`
  - `onBind()` return `null`（本 service 不被綁，是它綁 Core）
  - `buildNotification()`：status "Doll 正在觀察"，ongoing、no-clear
- [ ] Manifest 註冊 service：`android:foregroundServiceType="microphone|location|specialUse"`、`android:exported="false"`
- [ ] Run test → PASS
- [ ] Commit: `observer: add foreground service skeleton with notification channel`

### Task 4 — ProducerRegistry（管理所有 producer 生命週期）
- [ ] **Test first：** `ProducerRegistryTest.kt`：
  - `register()` 加入後，`startAll()` 呼叫各 producer 的 `start()`
  - `stopAll()` 反向停止
  - duplicate id 拋 `IllegalStateException`
- [ ] 定義 `Producer` interface：`val id: String`、`suspend fun start()`、`suspend fun stop()`、`val isRunning: StateFlow<Boolean>`
- [ ] 實作 `ProducerRegistry`：
  - `register(producer: Producer)`
  - `startAll()`、`stopAll()`
  - `producerById(id: String): Producer?`
  - `enabled` map 持久化到 SharedPreferences（key `observer_producers`）
- [ ] Run test → PASS
- [ ] Commit: `observer: add ProducerRegistry with lifecycle management`

### Task 5 — Observer 啟動流程整合
- [ ] **Test first：** `ObserverServiceIntegrationTest.kt`（Robolectric）：
  - service `onCreate` 後，`ProducerRegistry.startAll()` 被呼叫
  - service `onDestroy` 後，`stopAll()` 被呼叫
- [ ] 在 `ObserverService.onCreate()` 建立 `ProducerRegistry` 實例（暫時空 registry）
- [ ] `onStartCommand` → launch coroutine scope (`SupervisorJob + Dispatchers.Default`) → `registry.startAll()`
- [ ] `onDestroy` → cancel scope → `registry.stopAll()`
- [ ] Run test → PASS
- [ ] Commit: `observer: wire ProducerRegistry into service lifecycle`

### Task 6 — BOOT_COMPLETED receiver 自動啟動
- [ ] **Test first：** `BootReceiverTest.kt`：收到 `ACTION_BOOT_COMPLETED` 後發出 `startForegroundService` intent
- [ ] 實作 `BootReceiver.kt`
- [ ] Manifest 註冊 receiver + permission `RECEIVE_BOOT_COMPLETED`
- [ ] Run test → PASS
- [ ] Commit: `observer: auto-start service on boot`

---

## §2 AIDL client binder（bind IDollCore）

### Task 7 — 引入 Core AIDL 檔
- [ ] 從 `~/Projects/DollOSCore/app/src/main/aidl/dollos/core/` 複製（若尚無 Core 實體，先依 master §3.1/§3.2 手建）：
  - `IDollCore.aidl`
  - `IDollCoreStateListener.aidl`
  - `ObservationEvent.aidl`
  - `SkillCallbackResult.aidl`（Parcelable stub — 本 app 不用但必須在 path 內）
- [ ] 放在 `app/src/main/aidl/dollos/core/`
- [ ] `ObservationEvent.kt` 實作 Parcelable（sibling `.kt`，implements Parcelable by `@Parcelize`）
- [ ] Commit: `observer: import Core AIDL interfaces`

### Task 8 — CoreClient binder wrapper（TDD）
- [ ] **Test first：** `CoreClientTest.kt`：
  - `bind()` 呼叫 `bindService(Intent(ACTION_BIND_DOLL_CORE))` — 用 MockK 驗證
  - 連線成功後 `isConnected.value = true`
  - `postEvent()` 在未連線時放入 queue，連線後 flush
  - binder dead 時自動 `rebind()`（指數退避 1s → 2s → 4s → max 30s）
- [ ] 實作 `app/src/main/kotlin/dollos/observer/core/CoreClient.kt`：
  - `ServiceConnection` 實作
  - `bind(context)` / `unbind()`
  - `val isConnected: StateFlow<Boolean>`
  - `postEvent(event: ObservationEvent)` — queue pattern，未連線時 buffer（上限 500，滿則丟最舊）
  - `DeathRecipient` → mark disconnected → schedule rebind
  - Intent action：`"dollos.core.IDollCore"`、target package `"dollos.core"`
- [ ] Run test → PASS
- [ ] Commit: `observer: add CoreClient AIDL binder wrapper with reconnection`

### Task 9 — EventSink（producer 對外窗口）
- [ ] **Test first：** `EventSinkTest.kt`：驗證 `emit()` 呼叫 `CoreClient.postEvent()`，且自動填 `timestampMs = System.currentTimeMillis()` 若 caller 未提供
- [ ] 實作 `EventSink.kt`：
  - 建構接受 `CoreClient` + source 名稱
  - `suspend fun emit(type: String, payload: JSONObject, overrideTimestampMs: Long? = null, source: String? = null)`
  - 構 `ObservationEvent(type, ts, payload.toString(), source ?: defaultSource)`
  - call `coreClient.postEvent(event)`
- [ ] Run test → PASS
- [ ] Commit: `observer: add EventSink helper for producers`

### Task 10 — CoreClient 整合進 ObserverService
- [ ] `ObserverService.onCreate`：實例化 `CoreClient` + `bind()`
- [ ] `onDestroy`：`unbind()`
- [ ] 將 `CoreClient` 傳給 `ProducerRegistry`，每個 producer 用 `EventSink(coreClient, source=...)`
- [ ] 手動驗證：`adb shell am start-foreground-service ...` + logcat 看 `CoreClient connected=true`
- [ ] Commit: `observer: wire CoreClient into service and expose EventSink to producers`

---

## §3 Accelerometer + Gyro producer

### Task 11 — SensorSample 資料結構
- [ ] 定義 `data class SensorSample(val x: Float, val y: Float, val z: Float, val timestampMs: Long)`
- [ ] 定義 `enum class Activity { STILL, WALKING, MOVING_FAST }`
- [ ] Commit: `observer: add sensor data types`

### Task 12 — ActivityClassifier（規則式，不用 LLM）
- [ ] **Test first：** `ActivityClassifierTest.kt`：
  - 餵靜止樣本（std dev < 0.15 m/s²）→ `STILL`
  - 餵走路樣本（std dev 0.8-3.5）→ `WALKING`
  - 餵高頻樣本（std dev > 3.5）→ `MOVING_FAST`
  - 滑動視窗 3s、debounce 1s（連續 3 次同結果才轉移）
- [ ] 實作 `ActivityClassifier.kt`：
  - ring buffer 最近 3 秒加速度 magnitude
  - `classify(): Activity`：用 `sqrt(x²+y²+z²)` 扣重力 9.81 後算 std dev，對照門檻
  - state transition debounce
- [ ] Run test → PASS
- [ ] Commit: `observer: add ActivityClassifier with debounce`

### Task 13 — AccelGyroProducer 骨架（TDD）
- [ ] **Test first：** `AccelGyroProducerTest.kt`：
  - `start()` 註冊 `SensorManager` 的 accelerometer + gyroscope listener（SENSOR_DELAY_UI = ~60ms）
  - `stop()` unregister
  - 模擬感測事件 → classifier 輸出變化 → `EventSink.emit("motion.state", ...)` 被呼叫
  - payload JSON：`{"from": "still", "to": "walking"}`
- [ ] 實作 `AccelGyroProducer.kt`：
  - implements `Producer`、`id = "accel_gyro"`
  - 建構接收 `SensorManager`、`EventSink`、`ActivityClassifier`
  - 實作 `SensorEventListener`
  - Gyro 資料暫存給 PlacementClassifier（§10）使用 — 這裡先 expose via `latestGyro: StateFlow<SensorSample>`
  - Rate limit：同一 activity 每 30s 最多 re-emit 一次（防抖已在 classifier，但加保險）
- [ ] Run test → PASS
- [ ] Commit: `observer: implement AccelGyroProducer with activity events`

### Task 14 — 註冊到 ProducerRegistry
- [ ] 在 `ObserverService.onCreate` 建立並 register `AccelGyroProducer`
- [ ] Commit: `observer: register accel/gyro producer`

### Task 15 — 真機驗證 (subagent)
- [ ] Subagent：adb 安裝、`logcat -s DollObserver:D`、走路 30s / 靜止 30s / 手機甩動 5s
- [ ] 驗證三種事件都出現且 from/to 合理
- [ ] Commit（若需調參）: `observer: tune activity classifier thresholds`

---

## §4 Proximity + Light producer

### Task 16 — LightSample + ProximitySample
- [ ] `data class LightSample(val lux: Float, val timestampMs: Long)`
- [ ] `data class ProximitySample(val distanceCm: Float, val near: Boolean, val timestampMs: Long)`
- [ ] Commit: `observer: add light/proximity sample types`

### Task 17 — ProximityLightProducer（TDD）
- [ ] **Test first：** `ProximityLightProducerTest.kt`：
  - Light sensor reading 變化超過 ±30% 或 lux 跨 10 / 50 / 200 閾值 → emit `light.changed`
  - Proximity near ↔ far 轉變 → emit `proximity.changed`
  - Debounce 500ms
  - payload：`light.changed` → `{"lux": 123.4, "covered": false}`；`proximity.changed` → `{"distance_cm": 0.0, "near": true}`
- [ ] 實作 `ProximityLightProducer.kt`：
  - `TYPE_LIGHT` + `TYPE_PROXIMITY` listener
  - 最近值緩存 expose via `latestLight` / `latestProximity` StateFlow（供 PlacementClassifier 用）
  - `covered: lux < 3`
- [ ] Run test → PASS
- [ ] Commit: `observer: implement ProximityLightProducer`

### Task 18 — 註冊到 ProducerRegistry
- [ ] `ObserverService` register 該 producer
- [ ] Commit: `observer: register proximity/light producer`

### Task 19 — 真機驗證 (subagent)
- [ ] Subagent：手遮感測 / 開蓋蓋蓋 / 從亮到暗；logcat 驗證事件
- [ ] Commit（若需）: `observer: tune light sensor thresholds`

---

## §5 Mic + VAD producer

### Task 20 — 複製 silero_vad.onnx 與初始化 ONNX Runtime
- [ ] 從 DollOSAIService 複製 `silero_vad.onnx` 到 `app/src/main/assets/models/silero_vad.onnx`
- [ ] `VadModel.kt`：`OrtEnvironment` + `OrtSession` 包裝：
  - 16kHz 單聲道 float32 輸入
  - 每 chunk 30ms（480 samples）
  - 內部維護 h、c state tensor（silero 規格 2×1×64 float32）
  - `isSpeech(chunk: FloatArray): Float`（回 speech probability 0-1）
- [ ] **Test：** `VadModelTest.kt`（instrumented test, androidTest）— 載入 model、餵零輸入應回接近 0 的機率
- [ ] Commit: `observer: add silero VAD model wrapper`

### Task 21 — AudioRecord 獨占協定定義
- [ ] **注意：** 依 spec，Voice app 管 AudioRecord 獨占。但 Observer 也要 mic。
- [ ] 決策（寫進 plan）：Observer 直接開自己的 `AudioRecord(source=MediaRecorder.AudioSource.VOICE_RECOGNITION)`，Voice app 講話 / 監聽時 Core 透過 AIDL 通知 Observer pause mic（本 plan 暫時先讓 Observer 一直占用；未來 Voice plan 加 `pauseMic()` AIDL on Observer）
- [ ] 新增 AIDL `IDollObserverControl.aidl`：`void pauseMic(String reason); void resumeMic();`
- [ ] `ObserverService.onBind` 回傳此 binder
- [ ] **Test：** `ObserverControlBinderTest.kt` — pause/resume flag 切換
- [ ] Commit: `observer: add IDollObserverControl AIDL for mic coordination`

### Task 22 — MicVadProducer（TDD）
- [ ] **Test first：** `MicVadProducerTest.kt`（Robolectric + shadow AudioRecord）：
  - `start()` 開 AudioRecord（16kHz mono PCM16）
  - 模擬 speech probability > 0.6 持續 150ms → emit `mic.vad_start` 含 `{"energy_db": -20.3}`
  - 沉默 > 600ms → emit `mic.vad_end` 含 `{"energy_db": ..., "duration_ms": 1234}`
  - pause 時不 emit
- [ ] 實作 `MicVadProducer.kt`：
  - 開 `AudioRecord` → 讀 chunks → Resample 到 16kHz 若需要（多數 phone 原生支援）
  - convert int16 → float32 normalize /32768
  - 送進 `VadModel`
  - State machine: `IDLE → SPEECH_STARTING → SPEECH → SPEECH_ENDING → IDLE`
  - thresholds：start >0.6 連續 5 chunks、end <0.35 連續 20 chunks
  - energy_db = `20*log10(rms)` 最近 1 秒
  - 暫存錄音段（最多 15 秒 ring buffer）給 Task 25 的 classify 用 — expose via `latestSpeechSegment: SharedFlow<FloatArray>`
- [ ] Run test → PASS
- [ ] Commit: `observer: implement MicVadProducer with silero state machine`

### Task 23 — 註冊到 ProducerRegistry
- [ ] register 後 service `onDestroy` 必須確保 AudioRecord 關閉
- [ ] Commit: `observer: register mic VAD producer`

### Task 24 — 真機驗證 (subagent)
- [ ] Subagent：對手機說話 3 秒 / 安靜 5 秒 / 播放音樂；logcat 驗證 start/end 對稱且 duration 合理
- [ ] Commit（若需）: `observer: tune VAD thresholds for pixel 6a mic`

---

## §6 Aux classify 整合（VAD 結果 → Aux → mic.classified）

### Task 25 — 引入 AuxEngine AIDL
- [ ] 複製 `IDollAuxEngine.aidl` 到 `app/src/main/aidl/dollos/aux/`（依 master §3.3）
- [ ] Commit: `observer: import AuxEngine AIDL`

### Task 26 — AuxClient binder wrapper（TDD）
- [ ] **Test first：** `AuxClientTest.kt`：
  - `bind()` 綁 `"dollos.aux.IDollAuxEngine"` service
  - `classify(text, labels)` 走 binder、timeout 5s 未回應則 return `null`
  - 未連線時 `classify` return `null`（不 block producer）
- [ ] 實作 `AuxClient.kt`，結構仿 `CoreClient`
- [ ] Run test → PASS
- [ ] Commit: `observer: add AuxClient binder wrapper`

### Task 27 — MicClassifier（TDD）
- [ ] **Test first：** `MicClassifierTest.kt`：
  - 輸入 15s PCM → 內部轉成描述（目前階段先用簡易 feature：duration / avg energy / speech ratio）→ call `AuxClient.classify(desc, labels=["ambient","conversation","calling_me","noise"])`
  - Aux 回 `"conversation"` → `EventSink.emit("mic.classified", {"label":"conversation","confidence":0.8})`
  - Aux 回 null（未連線）→ fallback 用規則：duration<1s 且 energy<-30 → `"noise"`；>2s 中等 energy → `"conversation"`；其他 → `"ambient"`
  - **不實作 fallback 的品質保證**，只是避免事件全沒（依 CLAUDE.md "No fallback mechanisms" — 這裡改為：Aux 不在則**不發 mic.classified 事件**，不做替代）
  - ← 修正：**移除 fallback**。Aux 不在就靜默，不發事件。
- [ ] 實作 `MicClassifier.kt`：
  - 接 `MicVadProducer.latestSpeechSegment` flow
  - 轉成 description string（例："speech segment 3.2s, avg rms -22dB, 78% voice"）
  - call `AuxClient.classify(desc, LABELS)`
  - 有結果才 emit `mic.classified`
- [ ] Run test → PASS
- [ ] Commit: `observer: add MicClassifier that routes speech segments through Aux`

### Task 28 — 整合與真機驗證 (subagent)
- [ ] `ObserverService` 註冊 `MicClassifier` 監聽 `MicVadProducer`
- [ ] Subagent 真機：先 mock / stub AuxEngine service（寫個最小 fake service APK 或直接跳過等真 Aux），測 VAD end 後能觸發 classify call path（logcat 看 `AuxClient.classify called`）
- [ ] Commit: `observer: wire MicClassifier after VAD end`

---

## §7 System state listener

### Task 29 — SystemStateProducer 骨架（TDD）
- [ ] **Test first：** `SystemStateProducerTest.kt`（Robolectric）：
  - DND toggle `NotificationManager.INTERRUPTION_FILTER_ALL → PRIORITY` → emit `system.dnd.changed {"active":true}`
  - Battery broadcast 電量 50% → 49% → emit `system.battery.changed`（rate-limit：level 變化 ≥1% 或 charging 狀態變）
  - Charging on/off → emit
  - Screen on/off → `system.screen.changed {"on":true,"brightness":128}`
- [ ] 實作 `SystemStateProducer.kt`：
  - Listener：`Intent.ACTION_BATTERY_CHANGED`、`ACTION_SCREEN_ON` / `OFF`、`ACTION_INTERRUPTION_FILTER_CHANGED`
  - BroadcastReceiver 註冊在 service scope
  - Brightness：`Settings.System.SCREEN_BRIGHTNESS`
- [ ] Run test → PASS
- [ ] Commit: `observer: implement SystemStateProducer for battery/screen/dnd`

### Task 30 — NotificationListenerService 加 DND 精細監聽
- [ ] `INTERRUPTION_FILTER_ALL` vs `PRIORITY` vs `NONE` 都要正確反映 `active: bool`
- [ ] `active = filter != INTERRUPTION_FILTER_ALL`
- [ ] Commit: `observer: refine DND active detection`

### Task 31 — PowerManager + idle detection hook
- [ ] expose `isScreenOn`、`isInteractive`、`isPowerSaveMode` 供其他 producer（placement / sleep）用
- [ ] `SystemStateProducer.latestState: StateFlow<SystemStateSnapshot>`
- [ ] Commit: `observer: expose system state snapshot flow`

### Task 32 — 註冊到 ProducerRegistry
- [ ] Commit: `observer: register system state producer`

### Task 33 — 真機驗證 (subagent)
- [ ] Subagent：切 DND / 充電插拔 / 螢幕開關；logcat 驗證
- [ ] Commit（若需）

---

## §8 Location listener

### Task 34 — LocationHint 推論邏輯（TDD）
- [ ] **Test first：** `LocationHintResolverTest.kt`：
  - 規則：SSID 對應表（SharedPreferences `location_map`：`{"HomeWifi":"home", "OfficeAP":"work"}`）
  - 無 SSID match 且無 GPS → `"unknown"`
  - GPS lat/lon 距離 home coord < 200m → `"home"`
  - 既不 match SSID 又不近白名單座標 → `"out"`
- [ ] 實作 `LocationHintResolver.kt`
- [ ] Run test → PASS
- [ ] Commit: `observer: add location hint resolver`

### Task 35 — LocationProducer（TDD）
- [ ] **Test first：** `LocationProducerTest.kt`：
  - WifiManager SSID 變化 → 呼叫 resolver → emit `location.changed {"ssid":"...","location_hint":"home"}`
  - 上次 hint 不變則不 emit（debounce）
- [ ] 實作 `LocationProducer.kt`：
  - `WifiManager.connectionInfo.ssid` 監聽 via `CONNECTIVITY_ACTION` broadcast
  - `FusedLocationProviderClient` 粗粒度位置（10 分鐘一次，`PRIORITY_BALANCED_POWER_ACCURACY`）
- [ ] Run test → PASS
- [ ] Commit: `observer: implement LocationProducer with WiFi+GPS fusion`

### Task 36 — 註冊到 ProducerRegistry
- [ ] 加入 runtime permission 檢查（若未授權則 producer start() 直接 skip + log warning，不 crash）
- [ ] Commit: `observer: register location producer`

### Task 37 — 真機驗證 (subagent)
- [ ] Subagent：設定 SSID 白名單 → 連不同 WiFi → 驗證 `home/work/out`
- [ ] Commit（若需）

---

## §9 Timer / idle

### Task 38 — IdleTracker（TDD）
- [ ] **Test first：** `IdleTrackerTest.kt`：
  - 模擬 5 分鐘無 `observation.emit` → 發出 `timer.idle {"duration_ms":300000}`
  - 有事件發生 → reset timer
  - 預設 idle threshold：5 min（可調，從 prefs）
- [ ] 實作 `IdleTracker.kt`：
  - 監聽 `EventSink.emit` 的 interceptor（EventSink 改成 emit 時也 notify IdleTracker）
  - Coroutine timer：每次 reset 建新 delayed job
- [ ] Run test → PASS
- [ ] Commit: `observer: add IdleTracker emitting timer.idle`

### Task 39 — ChargingTimer
- [ ] **Test first：** `ChargingTimerTest.kt`：
  - 接上電 → emit `timer.charging_started {}`（立即）
  - 斷電不 emit（SystemStateProducer 的 `system.battery.changed` 已表達）
- [ ] 實作 `ChargingTimer.kt`（訂閱 `SystemStateProducer.latestState`）
- [ ] Run test → PASS
- [ ] Commit: `observer: add charging_started timer event`

### Task 40 — 註冊到 ProducerRegistry
- [ ] Commit: `observer: register timer producers`

### Task 41 — 真機驗證 (subagent)
- [ ] Subagent：放著不動 5 分鐘 / 插拔充電；logcat 驗證
- [ ] Commit（若需）

---

## §10 Placement 四態 classifier

### Task 42 — PlacementClassifier（融合 prox + light + accel + gyro）（TDD）
- [ ] **Test first：** `PlacementClassifierTest.kt`：
  - prox near + lux < 3 + accel std 低 → `"pocket"`
  - prox far + lux > 50 + accel std 低 → `"stand"`（桌面朝上）
  - prox far + accel std 中 + gyro 持續 tilt → `"hand"`
  - prox near + lux < 20 + accel 低 + 特定 tilt（z 向重力 ≈ +7 m/s² 以上，代表胸口朝外）→ `"chest"`
  - 其他 → `"unknown"`
  - debounce 3s（state 連續 3s 才切換）
- [ ] 實作 `PlacementClassifier.kt`：
  - 訂閱 `AccelGyroProducer.latestGyro` + `ProximityLightProducer.latestLight/Proximity`
  - 固定每 1s 計算一次
  - rule engine（非 LLM）
- [ ] Run test → PASS
- [ ] Commit: `observer: add PlacementClassifier fusing sensors`

### Task 43 — PlacementProducer wrapper
- [ ] **Test first：** `PlacementProducerTest.kt`：state 變化 → emit `placement.changed {"from":"stand","to":"hand"}`
- [ ] 實作 `PlacementProducer.kt`（implements `Producer`）
- [ ] Run test → PASS
- [ ] Commit: `observer: emit placement.changed events`

### Task 44 — 註冊到 ProducerRegistry
- [ ] 注意啟動順序：必須在 AccelGyro + ProximityLight 之後 register（dependency；registry 記 dependency）
- [ ] Commit: `observer: register placement producer with dependency order`

### Task 45 — 真機驗證 (subagent)
- [ ] Subagent：放桌上 / 拿起 / 放口袋 / 貼胸（模擬）；logcat 驗證四態都能出現
- [ ] Commit（若需調門檻）: `observer: tune placement rules`

---

## §11 Sleep detection

### Task 46 — SleepInferenceEngine（TDD）
- [ ] **Test first：** `SleepInferenceEngineTest.kt`：
  - 輸入：近 20 分鐘 activity = STILL、placement ≠ hand、lux < 5、time 22:00-07:00、screen off → `sleeping=true, confidence=0.85`
  - 任一條件不滿足 → `sleeping=false`
  - Hysteresis：醒時要進 sleep 需 20 分鐘、睡著要醒只需 5 分鐘的不滿足
- [ ] 實作 `SleepInferenceEngine.kt`：
  - 訂閱 activity / placement / light / system state flows
  - Sliding window 20 min
  - confidence = `满足條件數 / 總條件數`，全滿 = 0.95
- [ ] Run test → PASS
- [ ] Commit: `observer: add SleepInferenceEngine`

### Task 47 — SleepProducer
- [ ] **Test first：** `SleepProducerTest.kt`：state 變化 emit `system.sleep.inferred {"sleeping":true,"confidence":0.85}`
- [ ] 實作 `SleepProducer.kt`
- [ ] 註冊到 registry
- [ ] Commit: `observer: emit system.sleep.inferred events`

### Task 48 — 真機驗證 (subagent)
- [ ] Subagent：螢幕關 + 放桌上 + 靜止 20 分鐘（可用 `adb shell` fake 時間條件手動跳 window 長度成 2 分鐘做 smoke test）
- [ ] Commit（若需）: `observer: tune sleep inference thresholds`

---

## §12 整合測試

### Task 49 — Fake Core service harness
- [ ] `app/src/androidTest/kotlin/dollos/observer/integration/FakeCoreService.kt`：implements `IDollCore.Stub`，將收到的所有 `ObservationEvent` 塞入可 query 的 list
- [ ] Instrumented test fixture：binds Observer → FakeCore
- [ ] Commit: `observer: add FakeCore test harness`

### Task 50 — E2E：完整 producer 啟動 + 事件到達
- [ ] `ObserverE2ETest.kt`（instrumented）：
  - 啟動 Observer service
  - Fake Core service 綁定
  - 等 30 秒
  - 斷言：至少 1 筆 `system.battery.changed`、1 筆 `system.screen.changed`（因為測試 device 螢幕必亮）
  - 斷言：CoreClient `isConnected = true`
- [ ] Run on emulator or real device (subagent)
- [ ] Commit: `observer: add E2E integration test`

### Task 51 — 全 producer enable/disable smoke test
- [ ] `ProducerRegistrySmokeTest.kt`：逐一 `disable`、重啟 service、驗證該 producer 不 emit
- [ ] Commit: `observer: verify producer enable/disable switches`

### Task 52 — AOSP 整合 + 真機部署 (subagent)
- [ ] 建立 `~/Projects/DollOS-build/external/DollOSObserver/Android.bp`（依 master §8.3 範本）
- [ ] `./gradlew assembleRelease` → copy APK → `prebuilt/DollOSObserver.apk`
- [ ] `rsync` 到 AOSP tree
- [ ] Subagent：`m DollOSObserver -j$(nproc)` → `adb push` → `adb reboot`
- [ ] 真機 logcat 驗證 5 類事件皆產生（activity / placement / light / battery / location）
- [ ] Commit: `observer: AOSP integration and first-device verification`

---

## §13 驗收清單

本 app plan 完成 = 以下全過：

- [ ] 所有 producers（AccelGyro / ProximityLight / MicVad + MicClassifier / SystemState / Location / Placement / Sleep）都能獨立啟停
- [ ] 所有發給 Core 的 ObservationEvent type 都在 master §6 catalog 內（已對照：`mic.vad_start`、`mic.vad_end`、`mic.classified`、`mic.transcript`、`motion.state`、`placement.changed`、`placement.stable`、`location.changed`、`system.dnd.changed`、`system.battery.changed`、`system.screen.changed`、`system.sleep.inferred`、`system.notification`、`time.hour_changed`）
- [ ] CoreClient 斷線自動重連、event buffer 不丟關鍵事件
- [ ] Foreground service 在 boot 後自動啟動、notification 常駐
- [ ] `./gradlew test` 全綠、instrumented E2E 通過
- [ ] AOSP build + 真機部署成功、logcat 看得到所有 producer 事件

---

## §14 遺漏與未處理項

- **POST_NOTIFICATIONS runtime permission flow**：API 33+ 需要 user grant。本 plan 假定 OOBE 會授權；若未授權則 foreground notification 顯示失敗（不影響 service 運作）。完整處理留給 SetupWizard plan。
- **WiFi SSID 讀取限制**（Android 10+ 需 fine location permission）：已在 manifest 列，但若使用者未授權，`LocationProducer` 降級為 GPS-only（見 Task 36）。
- **Activity Recognition API**：本 plan 用自行分類（accel std dev），未用 Google Activity Recognition API。權衡：不依賴 GMS。若未來需要更準，可另起 task 接入 Play Services（會增加 GMS 相依）。
- **Chest placement 判定**：目前用 tilt + lux + prox 推估，可能誤判。實務上需要真機 tuning（見 Task 45）。若 Character Pack 未來有特殊硬體（磁吸項鍊）帶 NFC / BLE signal，可作為更強 signal，本 plan 先不處理。
- **Aux 未就緒時 `mic.classified` 不發**（見 Task 27）— 符合 CLAUDE.md "No fallback"。Core 端收不到 `mic.classified` 時的行為由 Core plan 決定（可能僅用 VAD 事件）。
- **Sleep detection 與 Core `dnd_active` flag 的整合**：Observer 只 emit `system.sleep.inferred`，是否 auto-toggle `dnd_active` 由 Core event handler 決定。本 plan 不處理。
- **電量優化**：所有 producer 目前 full rate 跑。未來需要 `dnd_active` 時降低 sample rate（spec §8 風險表）。本 plan 先不做節流，留 v1.1。
