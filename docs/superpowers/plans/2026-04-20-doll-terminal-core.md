# Doll AI Terminal — DollOSCore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 `DollOSCore` app — Doll 的大腦與神經系統。Always-on foreground service，event-driven handler 中心，包 `IDollCore` AIDL、EventBus、Context Engine、Event Handler 框架、Output Orchestrator（`[SILENT]` 協定）、LLM Router（Main + Aux）、Frozen system prompt、Internal Life loop、Orthogonal Flags、Mood/Attention runtime state、Emergency Stop。

**Architecture:**
- 單一 Android app (`DollOSCore`)，`system_ext` priv-app
- Foreground service `DollCoreService` 為主入口
- 內部模組：`EventBus` / `ContextEngine` / `EventHandlerDispatcher` / `OutputOrchestrator` / `LlmRouter` / `InternalLifeScheduler` / `MoodAttentionState` / `FlagsRegistry`
- 對外 AIDL：`IDollCore`、`IDollCoreStateListener`、`ObservationEvent`、`SkillCallbackResult`
- Main LLM：包既有 DollOSAIService 的 cloud LLM client（HTTP adapter）
- Aux LLM：透過 AIDL 呼叫 `DollOSAuxEngine`（MVP 前可走 cloud 小模型 placeholder）

**Tech Stack:** Kotlin, Android AIDL, AndroidX Foreground Service, Kotlin Coroutines (`Dispatchers.Default` 事件流、`Dispatchers.IO` LLM/檔案), `kotlinx.serialization` (Context Snapshot JSON), JUnit4 + MockK + Robolectric (unit), AndroidX Test (instrumented).

**Spec reference:**
- Master plan §3.1, §3.2, §6, §7, §8, §9 — `docs/superpowers/plans/2026-04-20-doll-terminal.md`
- Design spec — `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`（§4 全節、§5.1、§5.3）
- Project rules — `CLAUDE.md`

**Scope boundaries：**
- 本 plan **只**負責 DollOSCore app。其他 app（Observer / Memory / Voice / Skills / AuxEngine / Launcher / Service）由各自 plan 處理。
- 本 plan **不**實作：Observer producers、Memory 檔案內容邏輯（只 stub AIDL client）、Voice pipeline、Skills runtime、Launcher UI、AuxEngine runtime。

---

## 路徑慣例

- App root：`/home/progcat/Projects/DollOSCore/`
- Package：`dollos.core`
- AIDL 檔：`/home/progcat/Projects/DollOSCore/app/src/main/aidl/dollos/core/`
- Kotlin src：`/home/progcat/Projects/DollOSCore/app/src/main/java/dollos/core/`
- Unit tests：`/home/progcat/Projects/DollOSCore/app/src/test/java/dollos/core/`
- Instrumented tests：`/home/progcat/Projects/DollOSCore/app/src/androidTest/java/dollos/core/`
- Prebuilt APK 目的地：`/home/progcat/Projects/DollOSCore/prebuilt/DollOSCore.apk`
- AOSP 整合：`/home/progcat/Projects/DollOS-build/external/DollOSCore/`

---

## 段落 1：App 骨架（Gradle / Manifest / Foreground Service）

### Task 1.1 建立 Gradle 專案骨架
- [ ] 建立 `/home/progcat/Projects/DollOSCore/` 目錄
- [ ] 建立 `settings.gradle.kts`（rootProject.name = "DollOSCore"，include `:app`）
- [ ] 建立 root `build.gradle.kts`（Kotlin + AGP plugins，參考 `DollOSAIService/build.gradle.kts`）
- [ ] 建立 `app/build.gradle.kts`：
  - `namespace = "dollos.core"`
  - `compileSdk = 34`, `minSdk = 34`, `targetSdk = 34`
  - 啟用 AIDL (`buildFeatures { aidl = true }`)
  - 啟用 `kotlinx.serialization` plugin
  - 依賴：`kotlinx-coroutines-android`、`kotlinx-serialization-json`、`androidx.core:core-ktx`、test：`junit`, `mockk`, `robolectric`, `kotlinx-coroutines-test`
- [ ] 建立 `gradle.properties`、`gradle/wrapper/gradle-wrapper.properties`（參考 DollOSAIService）
- [ ] 複製 `gradlew` / `gradlew.bat` 執行權限
- [ ] 建立空 `app/src/main/AndroidManifest.xml`
- [ ] 寫 failing test：`app/src/test/java/dollos/core/ProjectSanityTest.kt` 驗證 `BuildConfig` 存在
- [ ] `./gradlew :app:compileDebugKotlin` 驗證可編譯
- [ ] Run test → 通過
- [ ] Commit: `scaffold: create DollOSCore Gradle project`

### Task 1.2 AndroidManifest + Permissions
- [ ] 編輯 `app/src/main/AndroidManifest.xml`：
  - `<uses-permission>`: `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `POST_NOTIFICATIONS`, `WAKE_LOCK`, `INTERNET`
  - `<application>` with `android:name=".CoreApp"`
  - 宣告 `<service android:name=".service.DollCoreService" android:foregroundServiceType="specialUse" android:exported="true" android:permission="dollos.core.permission.BIND_CORE">`
  - **必要子元素**：`<property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" android:value="dollos_core_ai_companion_host" />`（targetSdk 34+ 沒宣告會 `startForeground()` crash）
  - 宣告自訂 permission `dollos.core.permission.BIND_CORE` (`protectionLevel=signature|privileged`)
- [ ] 建立 `CoreApp.kt` 空 Application subclass
- [ ] 寫 failing test：`ManifestTest.kt`（Robolectric）驗證 service 已宣告
- [ ] 實作、驗證通過
- [ ] Commit: `scaffold: declare manifest, permissions, DollCoreService`

### Task 1.3 Foreground Service 骨架
- [ ] 建立 `service/DollCoreService.kt`：繼承 `Service`，`onCreate` 建 notification channel、`onStartCommand` 呼叫 `startForeground()`，`onBind` 回傳 placeholder IBinder（下一段覆蓋）
- [ ] 建立 `service/CoreNotification.kt` 管 channel ID `dollos.core.foreground`
- [ ] 寫 failing test：`DollCoreServiceTest.kt`（Robolectric `ServiceController`）驗證 `onStartCommand` 會呼叫 `startForeground`
- [ ] 實作、驗證通過
- [ ] Commit: `feat: DollCoreService foreground skeleton with notification channel`

### Task 1.4 CoreApp 初始化生命週期
- [ ] `CoreApp.kt` 在 `onCreate()` 建立 `CoreGraph`（內部 DI container，普通 Kotlin class holder）持有單例：`EventBus`、`FlagsRegistry`、`MoodAttentionState`、`ContextEngine`、`LlmRouter`、`OutputOrchestrator`、`EventHandlerDispatcher`、`InternalLifeScheduler`
- [ ] `DollCoreService.onCreate` 從 `CoreApp.graph` 取 dispatcher，`onStartCommand` 啟動 `InternalLifeScheduler`
- [ ] 寫 failing test：`CoreGraphTest.kt` 驗證所有單例可取出、非 null
- [ ] 實作 `CoreGraph` placeholder types (empty class stubs 後續覆蓋)
- [ ] 驗證通過
- [ ] Commit: `feat: CoreGraph DI container and service boot wiring`

---

## 段落 2：AIDL 介面定義與 Binder 實作

### Task 2.1 AIDL 檔案骨架
- [ ] 建立 `app/src/main/aidl/dollos/core/ObservationEvent.aidl`（parcelable 宣告，per master §3.3，header `// Version: 1`）
- [ ] 建立 `app/src/main/aidl/dollos/core/SkillCallbackResult.aidl`（parcelable：`skillId:String`, `status:String`, `resultJson:String`, `timestampMs:long`）
- [ ] 建立 `app/src/main/aidl/dollos/core/IDollCoreStateListener.aidl`（per master §3.2）
- [ ] 建立 `app/src/main/aidl/dollos/core/IDollCore.aidl`（per master §3.1，全部 method）
- [ ] 建立 Kotlin Parcelable impls：`aidl/ObservationEvent.kt`（`@Parcelize`）、`aidl/SkillCallbackResult.kt`
- [ ] 寫 failing test：`AidlContractTest.kt` 驗證所有 AIDL method signatures 可透過反射 locate
- [ ] `./gradlew :app:compileDebugKotlin` 讓 AIDL codegen 跑過
- [ ] 驗證通過
- [ ] Commit: `feat: IDollCore / ObservationEvent / SkillCallbackResult / IDollCoreStateListener AIDL`

### Task 2.2 IDollCore Stub 實作骨架
- [ ] 建立 `binder/DollCoreBinder.kt`：`extends IDollCore.Stub`，建構式接 `CoreGraph`，每個 method 先 `TODO` 拋 UnsupportedOperationException 除了 `emergencyStop` 和 `getContextSnapshotJson` 回空字串（避免 hang）
- [ ] `DollCoreService.onBind` 回 `DollCoreBinder(graph)`
- [ ] 寫 failing test：`DollCoreBinderTest.kt`（Robolectric ServiceTestRule bind service，確認 `onBind` 回非 null）
- [ ] 實作、驗證通過
- [ ] Commit: `feat: DollCoreBinder stub skeleton, service binds successfully`

### Task 2.3 State Listener 註冊與廣播
- [ ] 建立 `binder/StateListenerRegistry.kt`：thread-safe 管理 `RemoteCallbackList<IDollCoreStateListener>`，提供 `register`、`unregister`、`broadcastOp(opName, stateJson)`
- [ ] `DollCoreBinder.registerStateListener` / `unregisterStateListener` 委派給 registry
- [ ] 寫 failing test：`StateListenerRegistryTest.kt` — register mock listener、broadcastOp、驗證 listener.onOp 被呼叫帶正確參數；unregister 後不再呼叫
- [ ] 實作、驗證通過
- [ ] Commit: `feat: StateListenerRegistry with RemoteCallbackList`

### Task 2.4 postObservation / postSkillCallback wire 進 EventBus
- [ ] `DollCoreBinder.postObservation(event)` 轉為內部 `Event.Observation(event)` 丟進 `EventBus`
- [ ] `DollCoreBinder.postSkillCallback(...)` 建 `Event.SkillResult(...)` 丟 EventBus
- [ ] 寫 failing test：`DollCoreBinderDispatchTest.kt` 驗證 postObservation 會在 EventBus 收到對應 Event（用 fake EventBus 收 list）
- [ ] 實作、驗證通過
- [ ] Commit: `feat: AIDL post* methods forward into EventBus`

---

## 段落 3：EventBus

### Task 3.1 Event sealed hierarchy
- [ ] 建立 `event/Event.kt`：sealed class `Event`，subclasses：
  - `Event.Observation(val observation: ObservationEvent)`
  - `Event.UserTrigger(val source: String, val extras: Map<String, String>)`
  - `Event.Timer(val kind: TimerKind, val payload: Map<String, String>)` — `TimerKind` enum: `IDLE`, `CHARGING_STARTED`, `SESSION_END`, `ROUTINE`
  - `Event.SkillResult(val skillId: String, val status: String, val resultJson: String)`
  - `Event.FlagChanged(val name: String, val value: String)`
  - `Event.ReevaluatePending`
- [ ] 寫 failing test：`EventTypesTest.kt` 驗證 sealed class exhaustive `when` 編譯
- [ ] 實作、驗證通過
- [ ] Commit: `feat: Event sealed class hierarchy`

### Task 3.2 EventBus 實作（coroutine channel）
- [ ] 建立 `event/EventBus.kt`：持有 `Channel<Event>(Channel.BUFFERED, capacity=256)`，提供：
  - `suspend fun post(event: Event)`
  - `fun events(): ReceiveChannel<Event>` 或 `Flow<Event>`
  - `fun start(scope: CoroutineScope, handler: suspend (Event) -> Unit)` — 內部 launch 單 consumer coroutine 依序處理
- [ ] 寫 failing test：`EventBusTest.kt` (`runTest`) — post 3 events、handler 依序收到；buffer 滿時呼叫方不 block > timeout (Channel.BUFFERED behavior)
- [ ] 實作、驗證通過
- [ ] Commit: `feat: EventBus single-consumer coroutine channel`

### Task 3.3 EventBus 接上 CoreGraph
- [ ] `CoreGraph` 建 `CoroutineScope(SupervisorJob() + Dispatchers.Default)`，`EventBus.start(scope, dispatcher::dispatch)` — 其中 `dispatcher` 是 `EventHandlerDispatcher`（先放 stub no-op）
- [ ] `DollCoreService.onDestroy` cancel scope、close channel
- [ ] 寫 failing test：`EventBusWiringTest.kt` — Robolectric start service、post event via binder、驗證 stub dispatcher 收到
- [ ] 實作、驗證通過
- [ ] Commit: `feat: wire EventBus into CoreGraph with service lifecycle`

---

## 段落 4：Context Engine + ContextSnapshot

### Task 4.1 ContextSnapshot data class
- [ ] 建立 `context/ContextSnapshot.kt`：`@Serializable data class` 欄位照 spec §4.5：
  - `physical: String` (enum-like: pocket/hand/stand/chest/unknown)
  - `activity: String`
  - `environment: String`
  - `location: String`
  - `system: SystemState` (dnd, sleepMode, batteryLevel, charging)
  - `userState: String`
  - `lastInteractionAgoMs: Long`
  - `updatedAtMs: Long`
- [ ] 提供 `default()` factory 回全 `"unknown"` snapshot
- [ ] 寫 failing test：`ContextSnapshotTest.kt` — serialize to JSON、deserialize 回來一致
- [ ] 實作、驗證通過
- [ ] Commit: `feat: ContextSnapshot data class with JSON serialization`

### Task 4.2 ContextEngine 聚合 observation events
- [ ] 建立 `context/ContextEngine.kt`：持有 `@Volatile var snapshot`, 提供：
  - `fun apply(event: ObservationEvent)` — 依 `event.type` 更新 snapshot 對應欄位（switch over §6 event types catalog）
  - `fun current(): ContextSnapshot`
  - `fun currentJson(): String`
  - `fun noteInteraction(nowMs: Long)` — 更新 `lastInteractionAgoMs` 基準
- [ ] 建立 `context/ObservationReducer.kt` 純函式：`reduce(snapshot, event): ContextSnapshot`（方便 unit test）
- [ ] 寫 failing tests：`ObservationReducerTest.kt`
  - `placement.changed` payload `{to:"pocket"}` → snapshot.physical == "pocket"
  - `system.dnd.changed` payload `{active:true}` → snapshot.system.dnd == true
  - `mic.classified` payload `{label:"noisy"}` → snapshot.environment == "noisy"
  - `system.battery.changed` payload `{level:45, charging:true}` → 對應欄位
  - 每個 §6 catalog 事件至少一條 case（至少 10 條）
- [ ] 實作 reducer 覆蓋所有 catalog types、驗證通過
- [ ] Commit: `feat: ContextEngine with ObservationReducer covering §6 event catalog`

### Task 4.3 Wire ContextEngine into dispatcher + AIDL
- [ ] `EventHandlerDispatcher.dispatch(event)` 遇 `Event.Observation` 呼叫 `contextEngine.apply(event.observation)`
- [ ] `DollCoreBinder.getContextSnapshotJson()` 回 `contextEngine.currentJson()`
- [ ] 寫 failing test：`ContextEngineWiringTest.kt` — post ObservationEvent via EventBus、隨後 getContextSnapshotJson() 回對應欄位
- [ ] 實作、驗證通過
- [ ] Commit: `feat: wire ContextEngine into dispatcher and AIDL getContextSnapshotJson`

---

## 段落 5：Event Handler 框架

### Task 5.1 HandlerPipeline 介面
- [ ] 建立 `handler/HandlerPipeline.kt`：
  - `data class HandlerInput(val event: Event, val snapshot: ContextSnapshot, val mood: MoodSnapshot, val flags: FlagsSnapshot)`
  - `data class HandlerDecision(val tier: LlmTier, val systemPromptKey: String, val userPromptJson: String)` — or `HandlerDecision.Skip` object for no-op
  - `interface HandlerStep { suspend fun handle(input: HandlerInput): HandlerDecision }`
- [ ] 建立 `handler/LlmTier.kt` enum `MAIN, AUX`
- [ ] 寫 failing test：`HandlerPipelineTypesTest.kt` — 可 instantiate decision 各種型態
- [ ] 實作、驗證通過
- [ ] Commit: `feat: HandlerPipeline types (HandlerInput, HandlerDecision, HandlerStep)`

### Task 5.2 EventHandlerDispatcher 核心流程
- [ ] 建立 `handler/EventHandlerDispatcher.kt`：
  - 建構式接 `ContextEngine, MoodAttentionState, FlagsRegistry, LlmRouter, OutputOrchestrator, handlers: Map<KClass<out Event>, HandlerStep>`
  - `suspend fun dispatch(event: Event)`：
    1. 若 `Event.Observation` / `Event.FlagChanged` → 先給 ContextEngine / FlagsRegistry 更新，再繼續
    2. 查 handler (kclass)；無則 return
    3. 組 `HandlerInput`
    4. `handler.handle(input)` → `HandlerDecision`
    5. 若 `Skip` → return
    6. `llmRouter.call(decision)` → `LlmResponse`
    7. `outputOrchestrator.execute(response, input)` — 由 orchestrator 解析 `[SILENT]`/`[SPEAK]`/`[VIBRATE]`/`[INTERRUPT]`
    8. 廣播 op events（`llm_in_flight` / `llm_returned` 等）via StateListenerRegistry
- [ ] 寫 failing test：`EventHandlerDispatcherTest.kt` — 用 fake LlmRouter 回 `[SILENT]`、fake OutputOrchestrator 記錄呼叫、驗證 pipeline 順序 + op broadcast
- [ ] 實作、驗證通過
- [ ] Commit: `feat: EventHandlerDispatcher main pipeline with op broadcasts`

### Task 5.3 預設 handler 註冊
- [ ] 建立 `handler/handlers/ObservationHandler.kt` — 預設回 `Skip`（只更新 context，不觸 LLM）
- [ ] 建立 `handler/handlers/UserTriggerHandler.kt` — 必 `Main` tier，prompt key `"conversation"`，userPromptJson 包 source + extras
- [ ] 建立 `handler/handlers/TimerIdleHandler.kt` — `Aux` tier，prompt key `"inner_thought"`
- [ ] 建立 `handler/handlers/SkillResultHandler.kt` — `Main` tier，prompt key `"skill_followup"`
- [ ] 建立 `handler/handlers/ReevaluatePendingHandler.kt` — `Aux` tier，prompt key `"reevaluate_pending"`
- [ ] `CoreGraph` 把 handlers 註冊到 dispatcher
- [ ] 寫 failing tests：`HandlerRegistrationTest.kt` — dispatch 每種 Event subtype、驗證對應 handler 被呼叫（fakes 確認 prompt key）
- [ ] 實作、驗證通過
- [ ] Commit: `feat: register default event handlers for each Event subtype`

---

## 段落 6：Output Orchestrator + `[SILENT]` Parser + Read-the-Air

### Task 6.1 OutputDirective parser
- [ ] 建立 `output/OutputDirective.kt`：sealed class
  - `Silent`
  - `Speak(content: String)`
  - `Vibrate(summary: String)`
  - `Interrupt(content: String)`
- [ ] 建立 `output/DirectiveParser.kt`：`parse(rawLlmText: String): OutputDirective`，grammar：
  - `[SILENT]` → Silent
  - `[SPEAK "..."]` → Speak（支援跳脫雙引號 `\"`）
  - `[VIBRATE "..."]` → Vibrate
  - `[INTERRUPT "..."]` → Interrupt
  - 空白 / 前後綴寬鬆（trim、允許 prefix chain-of-thought 的最後一行）
  - parse 失敗 → 視為 `Silent`（per spec 預設沉默）
- [ ] 寫 failing tests：`DirectiveParserTest.kt` — 18 個 cases：
  - 每個 directive 成功解析
  - 含雙引號跳脫
  - 多行前綴加 `[SPEAK "hi"]` 成功
  - 空字串 → Silent
  - 亂碼 → Silent
  - `[SPEAK ""]` → Speak("")
- [ ] 實作、驗證通過
- [ ] Commit: `feat: OutputDirective parser for [SILENT] protocol`

### Task 6.2 Read-the-Air Gate（hard rules）
- [ ] 建立 `output/ReadTheAirGate.kt`：`evaluate(directive, snapshot, flags, policyRules): OutputDirective`
  - Hard rules（spec §4.7）：
    - `flags.dndActive == true` + directive=Speak → Silent
    - `snapshot.userState == "sleeping"` + Speak → Silent
    - `flags.dndActive == true` + directive=Interrupt → 放行（emergency）
    - `snapshot.environment == "noisy"` + Speak → Vibrate（soft 之一，用 threshold）
    - `snapshot.environment == "conversation"` + Speak → Silent（延後，寫 silent_pending）
- [ ] 建立 `output/PolicyRules.kt` 空資料結構（placeholder，Memory plan 會填）— 欄位 `quietHoursStart:Int?`、`quietHoursEnd:Int?` 先固定 null
- [ ] 寫 failing tests：`ReadTheAirGateTest.kt` — 8 個 cases 覆蓋每條 hard rule
- [ ] 實作、驗證通過
- [ ] Commit: `feat: ReadTheAirGate hard rules with PolicyRules placeholder`

### Task 6.3 OutputOrchestrator 執行器
- [ ] 建立 `output/OutputOrchestrator.kt`：`execute(response: LlmResponse, input: HandlerInput)`
  - Parse directive
  - Gate evaluate
  - Route to executor：
    - Silent → 若原本是 Speak/Vibrate，把原 content 寫進 `FlagsRegistry.silentPending`
    - Speak → call `SpeakExecutor`（stub 先只 broadcast `tts_playing` / `tts_ended` op；Voice app 實作時改接 IDollVoice）
    - Vibrate → call `VibrateExecutor`（呼叫 `Vibrator#vibrate(VibrationEffect)`） + broadcast `vibrate` op
    - Interrupt → force Speak path (skip gate for dnd)
  - 每次執行都 `contextEngine.noteInteraction(now)`
- [ ] 建立 `output/executors/SpeakExecutor.kt`、`VibrateExecutor.kt` 介面 + 預設 stub impl
- [ ] 寫 failing tests：`OutputOrchestratorTest.kt` — 10 cases 涵蓋所有 directive × gate 組合、silent_pending 寫入、op 廣播
- [ ] 實作、驗證通過
- [ ] Commit: `feat: OutputOrchestrator executing parsed directives with gate and silent_pending`

### Task 6.4 Silent pending re-evaluation trigger
- [ ] `FlagsRegistry` 設 silentPending 非空時，在 `ContextEngine.apply` 發現 environment 從 noisy → quiet（或 dnd off）時，post `Event.ReevaluatePending`
- [ ] `ReevaluatePendingHandler` 用 silentPending content + 當前 snapshot 重問 Aux tier 決定是否發出
- [ ] 寫 failing test：`SilentPendingReevaluationTest.kt` — 設 pending、apply noisy→quiet observation、驗證 ReevaluatePending event 被 post
- [ ] 實作、驗證通過
- [ ] Commit: `feat: silent_pending re-evaluation on environment change`

---

## 段落 7：LLM Router（Main + Aux）

### Task 7.1 LlmRequest / LlmResponse types
- [ ] 建立 `llm/LlmRequest.kt`：`data class LlmRequest(val tier: LlmTier, val systemPrompt: String, val userPrompt: String, val maxTokens: Int, val cacheKey: String?)`
- [ ] 建立 `llm/LlmResponse.kt`：`data class LlmResponse(val text: String, val tier: LlmTier, val latencyMs: Long, val cacheHit: Boolean)`
- [ ] 建立 `llm/LlmException.kt`：sealed 例外（Timeout / Network / Auth / InvalidResponse）
- [ ] 寫 failing test：`LlmTypesTest.kt` 基本 sanity
- [ ] 實作、驗證通過
- [ ] Commit: `feat: LlmRequest / LlmResponse / LlmException types`

### Task 7.2 LlmAdapter 介面 + MainAdapter
- [ ] 建立 `llm/LlmAdapter.kt`：`interface LlmAdapter { suspend fun generate(req: LlmRequest): LlmResponse }`
- [ ] 建立 `llm/MainLlmAdapter.kt`：將 DollOSAIService 既有 cloud LLM client 包一層 adapter（暫透過 interface placeholder `CloudLlmClient`，實際整合留到 AIService 抽離任務；本 plan 用 HTTP POST JSON 呼叫 Anthropic / OpenAI compatible endpoint 的最小 impl）
  - 讀 `BuildConfig.CLOUD_LLM_ENDPOINT` / `CLOUD_LLM_API_KEY`（透過 SharedPreferences key `dollos.core.cloud_llm`）
  - 實作 Anthropic `messages` API 最小 call（system + user messages）
  - 無網路 → throw `LlmException.Network`
- [ ] 寫 failing test：`MainLlmAdapterTest.kt` — 用 MockWebServer，驗證 body 包 system + user prompt、回應 parse 出 text
- [ ] 實作、驗證通過
- [ ] Commit: `feat: LlmAdapter interface and MainLlmAdapter (Anthropic-compatible)`

### Task 7.3 AuxLlmAdapter（AIDL client）
- [ ] 建立 `app/src/main/aidl/dollos/aux/IDollAuxEngine.aidl`（從 master §3.4 複製，供 client 端 codegen）
- [ ] 建立 `llm/AuxLlmAdapter.kt`：`bindService("dollos.aux/.AuxEngineService")`，`suspend generate` 呼叫 `IDollAuxEngine.generate(systemPrompt, userPrompt, maxTokens)`
- [ ] 若 service 未安裝 / bind 失敗 → throw `LlmException.Unavailable`
- [ ] 建立 `llm/AuxLlmPlaceholderAdapter.kt`：當 DollOSAuxEngine 尚未上機時，走雲端小模型（例如 `claude-haiku`）；透過 `config/LlmConfig.kt` flag `useAuxPlaceholder` 切換
- [ ] 寫 failing tests：`AuxLlmAdapterTest.kt` — fake IDollAuxEngine stub 回「[SILENT]」、驗證 generate 回該字串；unbind 情況拋 Unavailable
- [ ] 實作、驗證通過
- [ ] Commit: `feat: AuxLlmAdapter with AIDL binding and placeholder fallback`

### Task 7.4 LlmRouter 組合
- [ ] 建立 `llm/LlmRouter.kt`：
  - 建構式：`mainAdapter: LlmAdapter`, `auxAdapter: LlmAdapter`, `frozenPrompts: FrozenSystemPrompts`, `stateBroadcaster: StateListenerRegistry`
  - `suspend fun call(decision: HandlerDecision): LlmResponse`：
    1. Broadcast `llm_in_flight` with tier
    2. Build `LlmRequest`：systemPrompt = `frozenPrompts.forKey(decision.systemPromptKey, decision.tier)`, userPrompt = decision.userPromptJson
    3. Pick adapter by tier
    4. Try adapter.generate
    5. On `Aux` failure → fallback Main（per spec §5.1）
    6. On `Main` failure → **do NOT fallback to Aux**，rethrow
    7. Broadcast `llm_returned`
- [ ] 寫 failing tests：`LlmRouterTest.kt` — 6 cases：
  - main 成功
  - aux 成功
  - aux 失敗降 main（成功）
  - main 失敗不降 aux（拋例外）
  - op 廣播順序正確
  - tier 選擇正確
- [ ] 實作、驗證通過
- [ ] Commit: `feat: LlmRouter with main/aux tiers and fallback policy`

---

## 段落 8：Frozen System Prompt + Session 管理

### Task 8.1 SystemPromptSlots 資料結構
- [ ] 建立 `prompt/SystemPromptSlots.kt`：`data class SystemPromptSlots(val soul: String, val user: String, val policy: String, val overlays: List<String>)`
- [ ] 提供 `compose(key: String): String` 方法，依 key（`conversation` / `inner_thought` / `skill_followup` / `reevaluate_pending`）選擇要放哪些 slot + 加上每個 key 專屬 instruction（例：`inner_thought` 加 `"You are reflecting internally..."`）+ 一律要求 `Output directives: [SILENT] | [SPEAK "..."] | [VIBRATE "..."] | [INTERRUPT "..."]`
- [ ] 寫 failing tests：`SystemPromptSlotsTest.kt` — 4 key 都產出包含 soul / user / policy / overlays 的字串、directive instruction 存在
- [ ] 實作、驗證通過
- [ ] Commit: `feat: SystemPromptSlots with per-key composition`

### Task 8.2 FrozenSystemPrompts manager
- [ ] 建立 `prompt/FrozenSystemPrompts.kt`：
  - 建構式接 `IDollMemory`（AIDL placeholder interface，尚未 bind → stub 回空字串）
  - `suspend fun freeze(sessionId: String)`：從 Memory 讀 SOUL/USER/POLICY MD 檔案內容、cache 進 `ConcurrentHashMap<SessionId, SystemPromptSlots>`
  - `fun forKey(key: String, tier: LlmTier): String` 從當前 active session 的 slots composed
  - `fun addOverlay(name: String)` / `fun removeOverlay(name: String)` — 動態加/減 overlay，**不破壞 base slots 的 prefix cache**（overlay 加在 tail）
  - `fun startSession(sessionId: String)` / `fun endSession()`
- [ ] 寫 failing tests：`FrozenSystemPromptsTest.kt` — freeze、forKey 回包含 soul text、addOverlay 後 forKey 包含 overlay、endSession 後 forKey throw
- [ ] 實作、驗證通過
- [ ] Commit: `feat: FrozenSystemPrompts with session-scoped cache and overlay management`

### Task 8.3 Memory AIDL stub client
- [ ] 在 `aidl/dollos/memory/` 建 `IDollMemory.aidl`（從 master §3.7 複製）
- [ ] 建立 `memory/MemoryClient.kt`：binds `dollos.memory/.MemoryService`；若 service 不存在 → 所有 read 回空字串、所有 write no-op（允許 Core 單獨部署測試）
- [ ] 寫 failing test：`MemoryClientStubTest.kt` — unbound state read 回空字串、write 不拋例外
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MemoryClient AIDL stub for FrozenSystemPrompts`

### Task 8.4 Wire FrozenSystemPrompts 進 LlmRouter
- [ ] `CoreGraph` 建立 FrozenSystemPrompts，inject 進 LlmRouter
- [ ] `DollCoreService.onStartCommand` 呼叫 `frozenSystemPrompts.startSession(UUID.random)`
- [ ] `EventHandlerDispatcher` session 結束（`Event.Timer(SESSION_END)`）時 endSession + 重 startSession
- [ ] 寫 failing test：`SessionLifecycleTest.kt` — service start 後 session 存在、SESSION_END 後換新 session id
- [ ] 實作、驗證通過
- [ ] Commit: `feat: wire FrozenSystemPrompts session lifecycle to service`

---

## 段落 9：Internal Life Loop + Idle Timer

### Task 9.1 IdleTimer
- [ ] 建立 `life/IdleTimer.kt`：
  - 建構式接 `EventBus`, `clock: () -> Long`, `idleThresholdMs: Long = 5*60*1000`
  - `fun onEvent()` 重置 last-event timestamp
  - `fun start(scope: CoroutineScope)` 啟 coroutine tick 每 30s 檢查 `(now - lastEvent) >= threshold` → post `Event.Timer(IDLE)` 一次、重置計數器避免連發
- [ ] 寫 failing test：`IdleTimerTest.kt` (`runTest` + virtual time) — advance 6 分鐘 → 觀察到 Idle event；advance 另外 6 分鐘 → 又一個 Idle event
- [ ] 實作、驗證通過
- [ ] Commit: `feat: IdleTimer emits Event.Timer(IDLE) after threshold`

### Task 9.2 InternalLifeScheduler
- [ ] 建立 `life/InternalLifeScheduler.kt`：持 IdleTimer + 充電偵測監聽器 (`BroadcastReceiver` for `Intent.ACTION_POWER_CONNECTED`)
- [ ] 充電事件 → post `Event.Timer(CHARGING_STARTED)`
- [ ] `start(scope, service context)` / `stop(service context)`
- [ ] `DollCoreService.onCreate` start、`onDestroy` stop
- [ ] 寫 failing test：`InternalLifeSchedulerTest.kt`（Robolectric send POWER_CONNECTED intent）→ 驗證 EventBus 收到 CHARGING_STARTED
- [ ] 實作、驗證通過
- [ ] Commit: `feat: InternalLifeScheduler with idle and charging triggers`

### Task 9.3 Reset idle 計時器 on observation / user events
- [ ] `EventHandlerDispatcher` 遇 `Event.Observation`（只算 mic / placement / user triggers，不算純系統事件如 battery）+ `Event.UserTrigger` → 呼叫 `idleTimer.onEvent()`
- [ ] 寫 failing test：`IdleResetTest.kt` — 送 5 分鐘計時、4 分時送 UserTrigger、再等 4 分、確認沒觸發 idle
- [ ] 實作、驗證通過
- [ ] Commit: `feat: reset idle timer on relevant events`

---

## 段落 10：Orthogonal Flags Registry

### Task 10.1 FlagsRegistry 實作
- [ ] 建立 `flags/FlagsRegistry.kt`：
  - `@Volatile var dndActive: Boolean`
  - `@Volatile var distilling: Boolean`
  - `@Volatile var silentPending: String?`
  - `@Volatile var listeningOpen: Boolean`
  - 每個 setter 設值時 post `Event.FlagChanged(name, newValue)` 到 EventBus、broadcast `flag_changed` op
  - `snapshot(): FlagsSnapshot` 不可變 copy
- [ ] 寫 failing tests：`FlagsRegistryTest.kt` — set dndActive → EventBus 收到 FlagChanged；set silentPending → 同樣；concurrent set 不掉 broadcast
- [ ] 實作、驗證通過
- [ ] Commit: `feat: FlagsRegistry with event/op broadcast on change`

### Task 10.2 AIDL setDndActive
- [ ] `DollCoreBinder.setDndActive(active, reason)` → `flagsRegistry.dndActive = active`，記 reason 到 debug log
- [ ] 寫 failing test：`SetDndActiveBinderTest.kt` — 透過 binder 呼叫、驗證 flag 更新 + FlagChanged event posted
- [ ] 實作、驗證通過
- [ ] Commit: `feat: AIDL setDndActive updates FlagsRegistry`

### Task 10.3 DND 影響 Output Orchestrator
- [ ] 確認 `ReadTheAirGate.evaluate` 已讀 flagsSnapshot.dndActive（段落 6.2 已含）
- [ ] 加整合測試：`DndIntegrationTest.kt` — set dndActive=true、post UserTrigger 事件、fake LLM 回 `[SPEAK "hi"]`、驗證 SpeakExecutor 沒被呼叫且 silentPending 有內容
- [ ] 實作（若需調整）、驗證通過
- [ ] Commit: `test: DND integration test across AIDL → dispatcher → orchestrator`

---

## 段落 11：Mood / Attention Runtime State

### Task 11.1 MoodAttentionState 資料
- [ ] 建立 `state/MoodAttentionState.kt`：
  - `@Serializable data class MoodSnapshot(val mood: String, val attentionLevel: Float, val patience: Float, val lastInnerThoughtAgoMs: Long)`
  - `class MoodAttentionState { fun snapshot(): MoodSnapshot; fun noteInteraction(); fun noteInterrupted(); fun noteIgnored(); fun noteInnerThought(nowMs: Long); fun setMood(mood: String) }`
  - 規則：
    - `noteInteraction` 每次 UserTrigger 提高 attentionLevel (decayed moving avg)
    - `noteIgnored` / `noteInterrupted` 降 patience
    - 開機 reset（不持久化）
- [ ] 寫 failing tests：`MoodAttentionStateTest.kt` — 連 5 次 noteIgnored → patience < 初始值；noteInteraction → attentionLevel 上升
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MoodAttentionState runtime state with decay rules`

### Task 11.2 Wire MoodAttentionState 進 dispatcher
- [ ] `EventHandlerDispatcher` 組 `HandlerInput` 時帶 `mood = moodAttentionState.snapshot()`
- [ ] UserTrigger → noteInteraction；ReadTheAirGate 把 Speak 降 Silent → noteIgnored；Interrupt → noteInterrupted
- [ ] 寫 failing test：`MoodWiringTest.kt` — 連 post UserTrigger、驗證 HandlerInput.mood 與預期一致
- [ ] 實作、驗證通過
- [ ] Commit: `feat: wire MoodAttentionState updates into event pipeline`

---

## 段落 12：Emergency Stop

### Task 12.1 emergencyStop AIDL impl
- [ ] `DollCoreBinder.emergencyStop(reason: String)`：
  1. Cancel EventBus scope（停 handler 處理）
  2. `outputOrchestrator.abortAll()` — 停 TTS、取消 vibration
  3. `flagsRegistry.dndActive = true`
  4. Broadcast op `emergency_stop` with reason
  5. `stopForeground(STOP_FOREGROUND_REMOVE)` + `stopSelf()`
- [ ] `OutputOrchestrator.abortAll()` 呼叫各 executor 的 `abort()`
- [ ] 寫 failing test：`EmergencyStopTest.kt` — invoke via binder、驗證 op 廣播 + scope cancelled + service stopped (Robolectric shadowOf(service).isForegroundStopped)
- [ ] 實作、驗證通過
- [ ] Commit: `feat: emergencyStop AIDL halts pipeline, aborts outputs, stops service`

### Task 12.2 Resumable boot after emergency stop
- [ ] 記錄 emergency stop 原因到 `SharedPreferences("dollos.core.state")` key `last_emergency_stop_reason`
- [ ] Service 下次啟動時讀此值、broadcast op `emergency_stop_recovered`、清除 flag
- [ ] 寫 failing test：`EmergencyStopRecoveryTest.kt` — 模擬 emergency stop、restart service、驗證 recovery op 廣播
- [ ] 實作、驗證通過
- [ ] Commit: `feat: emergency stop recovery broadcast on next boot`

---

## 段落 13：整合測試

### Task 13.1 triggerConversation E2E
- [ ] 建立 `app/src/androidTest/java/dollos/core/TriggerConversationE2ETest.kt`
- [ ] 用真 AIDL binding (ServiceTestRule) + fake MainLlmAdapter 回 `[SPEAK "hi"]`
- [ ] `binder.triggerConversation("wake_word", Bundle())` → 驗證：
  - StateListener 收到 `llm_in_flight` → `llm_returned` → `tts_playing` → `tts_ended` ops 順序
  - MoodAttentionState 的 attentionLevel 上升
  - Session id 未變
- [ ] 實作、驗證通過
- [ ] Commit: `test: triggerConversation E2E with AIDL binding`

### Task 13.2 Observation → Context snapshot E2E
- [ ] 建立 `ObservationSnapshotE2ETest.kt`
- [ ] 透過 binder postObservation 一連串：placement=pocket → placement=hand → mic.classified=noisy → system.dnd.changed=true
- [ ] 每次 `getContextSnapshotJson()` 驗證對應欄位
- [ ] 最後 DND=true + Speak directive → 驗證 Silent（透過 triggerConversation + fake LLM）
- [ ] 實作、驗證通過
- [ ] Commit: `test: observation → context snapshot → DND gate E2E`

### Task 13.3 Idle → Inner thought E2E
- [ ] 建立 `IdleToInnerThoughtE2ETest.kt`
- [ ] 使用 test clock override（inject via CoreGraph testing entry）
- [ ] Advance 6 minutes → 驗證 Aux tier LLM 被呼叫、prompt key = `inner_thought`、fake Aux 回 `[SILENT]` → 無輸出但 noteInnerThought 被呼叫
- [ ] 實作、驗證通過
- [ ] Commit: `test: idle timer triggers inner thought via Aux tier`

### Task 13.4 Aux 降 Main fallback E2E
- [ ] 建立 `AuxFallbackE2ETest.kt`
- [ ] Fake AuxAdapter throw `Unavailable` → LlmRouter 應改 call MainAdapter → 驗證最終有 response
- [ ] 反向：Fake MainAdapter throw → 驗證 exception propagate (no Aux fallback)
- [ ] 實作、驗證通過
- [ ] Commit: `test: LLM tier fallback policy E2E`

### Task 13.5 Emergency stop full loop
- [ ] 建立 `EmergencyStopE2ETest.kt`
- [ ] 先 triggerConversation 使 TTS stub 「在播」→ emergencyStop → 驗證 TTS abort、service stop、flag dndActive=true、op broadcast 送出
- [ ] 實作、驗證通過
- [ ] Commit: `test: emergency stop full loop E2E`

### Task 13.6 AOSP build 整合
- [ ] 建立 `/home/progcat/Projects/DollOS-build/external/DollOSCore/Android.bp`（per master §8.3）
- [ ] `./gradlew :app:assembleRelease` → `cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSCore.apk`
- [ ] `rsync -av --delete /home/progcat/Projects/DollOSCore/ /home/progcat/Projects/DollOS-build/external/DollOSCore/`
- [ ] `cd /home/progcat/Projects/DollOS-build && source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug && m DollOSCore -j$(nproc)`
- [ ] 寫 smoke script `/home/progcat/Projects/DollOSCore/scripts/smoke_core.sh`：adb push APK → adb reboot → dumpsys 確認 service running + bind test（adb shell `service call`）
- [ ] Commit: `build: AOSP integration for DollOSCore (Android.bp + smoke script)`

### Task 13.7 裝機 smoke test（subagent）
- [ ] 派 subagent 執行 smoke_core.sh（避免截圖吃 context，依 CLAUDE.md 規則）
- [ ] 驗證 service 運作、AIDL bind 成功、foreground notification 顯示
- [ ] Commit: `docs: record on-device smoke test results`（更新 plan 檔加 status note）

---

## 依賴清單

本 plan 引用但不實作的 AIDL / 外部介面（由其他 plan 負責，本 plan 用 stub）：

- `dollos.memory.IDollMemory` — 段落 8.3 stub，真實作在 memory plan
- `dollos.aux.IDollAuxEngine` — 段落 7.3 AIDL 宣告，runtime 在 aux-engine plan
- `dollos.voice.IDollVoice` — 段落 6.3 SpeakExecutor 預留接口，實際在 voice plan
- `dollos.skills.IDollSkills` — 未在本 plan 直接呼叫（skill 執行 callback 僅透過 IDollCore.postSkillCallback 進來）
- `dollos.service.*` — emergencyStop 由 DollOSService 端呼叫進來

Observer / Launcher / Service 以 IDollCore **client** 身份連線，本 plan 不需 stub 它們。

---

## 交付判準

本 plan 完成 =
- 所有 60 個 task 的 commit 都在 main
- `./gradlew test` 全綠（單元測試）
- `./gradlew connectedAndroidTest` 全綠（整合測試，需要 device/emulator）
- AOSP `m DollOSCore` 編得出 APK
- 裝機 smoke：foreground service running、AIDL IDollCore bind 得到、triggerConversation 走通 fake-Main 回應路徑、emergencyStop 能停 service

之後可讓其他 6 份 app plan 並行開工（依 master §10 順序建議）。
