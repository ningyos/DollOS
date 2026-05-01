# Doll AI Terminal — DollOSAuxEngine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `DollOSAuxEngine` — 在 Pixel 6a 宿主本地 Aux LLM（目標 Gemma 4 E4B/E2B），以獨立 Android process 提供 `IDollAuxEngine` AIDL 六個方法（`generate` / `classify` / `summarize` / `silentJudgment` / `isModelLoaded`+`loadModel`+`unloadModel` / `getEngineInfo`）。所有設計決定（runtime、quantization、prompt 結構、是否使用 Gemma 4 還是 fallback 模型）都由第一階段 benchmark 結果決定。

**Architecture:** 單一 Android app，AndroidManifest 宣告 `android:process=":aux_llm"` 讓 LLM inference 跑在獨立 process（被 OOM kill 不影響 UI / Core）。Service class 對外曝 `IDollAuxEngine`。內部結構：`AuxEngineService` (Binder) → `InferenceWorker`（single-thread queue）→ `LlmRuntime`（runtime 抽象 interface；具體實作按 benchmark 結果挑 MediaPipe LLM Inference / MLC / llama.cpp JNI / ONNX Runtime 擇一）。`ModelLoader` 管 async load / unload。`MemoryPressureObserver` (Android `onTrimMemory`) 觸發自動 unload。`PromptTemplates` 集中管 `classify` / `summarize` / `silentJudgment` 的 few-shot prompt。`MetricsLogger` 記 TPS / latency / peak RSS。

**Tech Stack:** Kotlin, Android AIDL, foreground service, Coroutines (single-dispatcher queue), JNI (若 runtime 為 llama.cpp)。Runtime 候選：
- **MediaPipe LLM Inference API** — Google 官方、支援 Gemma 3/4，最簡單但 quantization 彈性有限
- **MLC LLM** — TVM-based、quantization 選擇多、編譯複雜
- **llama.cpp JNI** — 模型支援最廣、INT4/INT8 穩定、需要自己維護 JNI binding
- **ONNX Runtime** — 若 Gemma 4 的 ONNX export + INT4 quantization 夠成熟

**Master plan references:**
- §1 app 職責總覽（DollOSAuxEngine 行）
- §3.4 `IDollAuxEngine.aidl` 完整介面契約（**本 plan 不重複定義**，以 master 為準）
- §8 Build / Deploy 慣例（system_ext priv-app、prebuilt APK + Android.bp）
- §9 測試策略（AIDL unit + 核心邏輯 unit + 至少一個 E2E integration）
- §12 v1.0 驗收條件：Gemma 4 在 Pixel 6a 成功跑、Aux 路由實際打到本地模型、Main 失敗不降級 Aux / Aux 失敗降級 Main
- §11 風險提醒：Gemma 4 上機為第一順位風險，benchmark 失敗則走 E2B 或 Phi-3-mini fallback
- Spec §5.1 LLM Router（Aux tier 的 caller 預期行為）、§10 「Gemma 4 本地推論 runtime 選型」留到本 plan 決定

**Spec reference:** `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`

---

## File Structure

### DollOSAuxEngine (new app)

```
~/Projects/DollOSAuxEngine/
├── app/
│   ├── build.gradle.kts
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── aidl/dollos/aux/
│   │   │   └── IDollAuxEngine.aidl              ← §3.3 master 契約
│   │   ├── java/dollos/aux/
│   │   │   ├── AuxEngineApp.kt                   ← Application
│   │   │   ├── AuxEngineService.kt               ← AIDL service (runs in :aux_llm process)
│   │   │   ├── AuxEngineBinder.kt                ← IDollAuxEngine.Stub impl
│   │   │   ├── runtime/
│   │   │   │   ├── LlmRuntime.kt                 ← interface
│   │   │   │   ├── MediaPipeLlmRuntime.kt        ← (if chosen)
│   │   │   │   ├── MlcLlmRuntime.kt              ← (if chosen)
│   │   │   │   ├── LlamaCppRuntime.kt            ← (if chosen) + JNI
│   │   │   │   └── OnnxLlmRuntime.kt             ← (if chosen)
│   │   │   ├── inference/
│   │   │   │   ├── InferenceWorker.kt            ← single-thread queue
│   │   │   │   ├── InferenceRequest.kt           ← sealed class (Generate/Classify/...)
│   │   │   │   └── InferenceResult.kt
│   │   │   ├── model/
│   │   │   │   ├── ModelLoader.kt                ← async load / unload
│   │   │   │   ├── ModelRegistry.kt              ← 目前選用的 model id + quant
│   │   │   │   └── ModelAssets.kt                ← 從 /system_ext/dollos/models/ 讀路徑
│   │   │   ├── prompts/
│   │   │   │   ├── PromptTemplates.kt            ← classify / summarize / silentJudgment
│   │   │   │   └── SilentJudgmentGrammar.kt     ← 四選一格式解析 / 驗證
│   │   │   ├── memory/
│   │   │   │   └── MemoryPressureObserver.kt    ← onTrimMemory → auto unload
│   │   │   ├── metrics/
│   │   │   │   ├── MetricsLogger.kt              ← TPS / latency / peak RSS
│   │   │   │   └── InferenceMetrics.kt           ← per-request stats
│   │   │   └── util/
│   │   │       ├── TokenCounter.kt
│   │   │       └── JsonUtil.kt
│   │   └── jniLibs/                               ← (if llama.cpp chosen)
│   │       └── arm64-v8a/
│   └── src/test/java/dollos/aux/...              ← unit tests
│   └── src/androidTest/java/dollos/aux/...       ← instrumented tests
├── benchmark/                                     ← 獨立 benchmark harness (Task 1)
│   ├── app/                                       ← benchmark APK
│   │   └── src/main/java/dollos/aux/benchmark/
│   │       ├── BenchmarkActivity.kt
│   │       └── BenchmarkRunner.kt
│   └── scripts/
│       ├── run_benchmark.sh                       ← adb-driven benchmark
│       └── thermal_monitor.sh                    ← temperature logging
├── prebuilt/
│   └── DollOSAuxEngine.apk
├── build.gradle.kts
├── settings.gradle.kts
├── gradle/
└── gradlew
```

### AOSP integration

```
~/Projects/DollOS-build/external/DollOSAuxEngine/
  Android.bp
  prebuilt/DollOSAuxEngine.apk
```

Model assets path（所有 runtime 共用）:
```
/system_ext/dollos/models/aux_llm/
  <model_id>/
    <runtime-specific model files>
    MODEL_META.json                 ← model name, quant, expected RAM, TPS target
```

---

## 第一段：Pixel 6a Gemma 4 Benchmark（**最先跑，結果決定後續設計**）

> 這段**先於任何 app 骨架 task**。Benchmark 結果寫進 `benchmark/RESULTS.md` 並以它為根據確定 Task 5 的 runtime 選型與 Task 6 的 model id。

### Task 1: Benchmark 環境準備

**Goal:** 建出可在 Pixel 6a 跑 LLM 推論的 benchmark APK + adb 腳本，不綁定 DollOSAuxEngine app 本體（獨立可丟棄）。

**Files:**
- Create: `benchmark/app/build.gradle.kts`
- Create: `benchmark/app/src/main/AndroidManifest.xml`
- Create: `benchmark/app/src/main/java/dollos/aux/benchmark/BenchmarkActivity.kt`
- Create: `benchmark/app/src/main/java/dollos/aux/benchmark/BenchmarkRunner.kt`
- Create: `benchmark/scripts/run_benchmark.sh`
- Create: `benchmark/scripts/thermal_monitor.sh`

- [ ] **Step 1:** 建 `benchmark/app/` 獨立 Gradle 子專案（applicationId `dollos.aux.benchmark`），minSdk 34。
- [ ] **Step 2:** `BenchmarkActivity` 提供 UI 按鈕觸發 benchmark；`BenchmarkRunner` 是純 Kotlin class 跑測試並輸出 JSON log 到 `/sdcard/Download/dollos_bench/`。
- [ ] **Step 3:** `run_benchmark.sh` 用 adb push 模型 → `am start` activity → 等待完成 → pull 結果。
- [ ] **Step 4:** `thermal_monitor.sh` 每 5 秒 `adb shell cat /sys/class/thermal/thermal_zone*/temp` 取樣，輸出 csv。
- [ ] **Step 5:** 寫 `benchmark/README.md` 記 benchmark 指令與目標指標（TPS >= 5 可用、>= 10 理想；memory < 3GB；5 分鐘後 thermal 無 throttle 斷崖）。
- [ ] **Step 6:** Commit。

### Task 2: 跑 MediaPipe LLM Inference + Gemma 3n（Gemma 4 正式釋出前的 placeholder）

**Goal:** 以 MediaPipe LLM Inference API 為第一候選，跑 Gemma 3n E4B（若 Gemma 4 已釋出則用 Gemma 4 E4B），收 TPS / memory / thermal 數據。

**Files:**
- Modify: `benchmark/app/build.gradle.kts`（加 `com.google.mediapipe:tasks-genai`）
- Create: `benchmark/app/src/main/java/dollos/aux/benchmark/MediaPipeBench.kt`

- [ ] **Step 1:** MediaPipe genai 依賴 + 下載 `.task` 模型檔 push 到裝置。
- [ ] **Step 2:** `MediaPipeBench` 跑三種 workload：
  - Workload A：2048-token input → 256-token output（模擬 silentJudgment + classify）
  - Workload B：4096-token input → 512-token output（模擬 summarize）
  - Workload C：連續 20 次 A（模擬高頻 inner thought / observation classify）
- [ ] **Step 3:** 記 TPS（prefill + decode 分開）、peak RSS（`Debug.MemoryInfo`）、電池 mAh 增量、CPU temperature。
- [ ] **Step 4:** 跑 5 分鐘連續 workload A 看 thermal throttle 是否觸發（TPS 下降 > 30% 視為斷崖）。
- [ ] **Step 5:** 結果寫入 `benchmark/results/mediapipe_gemma4_e4b.json`。
- [ ] **Step 6:** Commit 結果。

### Task 3: 跑 llama.cpp JNI + Gemma 4 E4B（或同級模型）INT4 quant

**Goal:** 平行 evaluate llama.cpp JNI 路線，用 GGUF Q4_K_M quant。

**Files:**
- Create: `benchmark/app/src/main/java/dollos/aux/benchmark/LlamaCppBench.kt`
- Create: `benchmark/app/src/main/cpp/llama_bench_jni.cpp`
- Create: `benchmark/app/src/main/cpp/CMakeLists.txt`

- [ ] **Step 1:** 引 llama.cpp（git submodule 或 CMake FetchContent），編 arm64-v8a so。
- [ ] **Step 2:** `llama_bench_jni.cpp` 暴露 `nativeLoadModel` / `nativeGenerate` / `nativeUnload` JNI。
- [ ] **Step 3:** 跑同一套 workload A/B/C，同樣紀錄指標。
- [ ] **Step 4:** 結果寫 `benchmark/results/llamacpp_gemma4_e4b_q4km.json`。
- [ ] **Step 5:** Commit。

### Task 4: Fallback 模型 benchmark（若 Task 2/3 TPS < 5）

**Goal:** 若 E4B 跑不動，跑 E2B、Phi-3-mini 3.8B Q4、Qwen 2.5 1.5B Q4 三種 fallback，各挑一個最佳 runtime 跑。

**Files:**
- Create: `benchmark/results/fallback_comparison.md`

- [ ] **Step 1:** 若 Task 2/3 有任一達標（TPS >= 5 + memory < 3GB + 無 thermal 斷崖）→ **Skip Task 4**，在 `fallback_comparison.md` 註明 "not needed"。
- [ ] **Step 2:** 否則 benchmark E2B（MediaPipe 優先）、Phi-3-mini Q4（llama.cpp）、Qwen 2.5 1.5B Q4（llama.cpp）。
- [ ] **Step 3:** 挑一個跑完達標的當 v1 model，把其餘兩個列為 Task 6 可切換選項。
- [ ] **Step 4:** Commit。

### Task 5: Runtime + Model 決議文件

**Goal:** 彙整前四個 task 的數據，**明確寫下**本 app 採用哪個 runtime + 哪個 model + 哪個 quantization，後面所有 task 以此為準。

**Files:**
- Create: `benchmark/DECISION.md`

- [ ] **Step 1:** `DECISION.md` 必須回答：
  - Runtime：MediaPipe / MLC / llama.cpp / ONNX 擇一（引數據支持）
  - Model：Gemma 4 E4B / E2B / Phi-3-mini / Qwen 2.5（引數據支持）
  - Quantization：INT4 / INT8 / FP16（引數據支持）
  - 預期 TPS（decode）、預期 peak RSS、預期 thermal 行為
  - Memory pressure unload 策略（是否需要、什麼 threshold 觸發）
- [ ] **Step 2:** 使用者審閱 / 批准後才進 Task 6。
- [ ] **Step 3:** Commit。

---

## 第二段：DollOSAuxEngine App 骨架

### Task 6: 建 Gradle 專案 + AndroidManifest（獨立 process）

**Goal:** 建起可 build 的空 app，service 在 `:aux_llm` 獨立 process。

**Files:**
- Create: `~/Projects/DollOSAuxEngine/build.gradle.kts`
- Create: `~/Projects/DollOSAuxEngine/settings.gradle.kts`
- Create: `~/Projects/DollOSAuxEngine/app/build.gradle.kts`
- Create: `~/Projects/DollOSAuxEngine/app/src/main/AndroidManifest.xml`
- Create: `~/Projects/DollOSAuxEngine/app/src/main/java/dollos/aux/AuxEngineApp.kt`
- Create: `~/Projects/DollOSAuxEngine/gradle/wrapper/*`
- Create: `~/Projects/DollOSAuxEngine/gradlew`

- [ ] **Step 1:** 參考 `~/Projects/DollOSAIService` 結構建 Gradle。package name `dollos.aux`。
- [ ] **Step 2:** `AndroidManifest.xml` 宣告 service：
  ```xml
  <service android:name=".AuxEngineService"
           android:process=":aux_llm"
           android:exported="true"
           android:permission="dollos.aux.permission.BIND_AUX_ENGINE">
    <intent-filter>
      <action android:name="dollos.aux.IDollAuxEngine" />
    </intent-filter>
  </service>
  ```
- [ ] **Step 3:** 宣告 `android:largeHeap="true"` 給 `:aux_llm` process（LLM 需要）。
- [ ] **Step 4:** 宣告 `dollos.aux.permission.BIND_AUX_ENGINE` signature-level permission（只給 DollOSCore 用）。
- [ ] **Step 5:** `./gradlew assembleRelease` 驗證能 build。
- [ ] **Step 6:** Commit。

### Task 7: 複製 `IDollAuxEngine.aidl`（master §3.3）

**Goal:** 把 master plan §3.3 的完整 AIDL 契約放進 `aidl/dollos/aux/`。

**Files:**
- Create: `app/src/main/aidl/dollos/aux/IDollAuxEngine.aidl`

- [ ] **Step 1:** 照 master §3.3 一字不差 copy AIDL。第一行加 `// Version: 1`。
- [ ] **Step 2:** `./gradlew assembleRelease` 驗證 AIDL stub 生成 OK。
- [ ] **Step 3:** Commit。

### Task 8: 骨架 `AuxEngineService` + `AuxEngineBinder`（六個方法先回 stub）

**Goal:** Service 跑在 `:aux_llm` process，Binder 六個方法皆回 placeholder 值但 return type 正確。

**Files:**
- Create: `app/src/main/java/dollos/aux/AuxEngineService.kt`
- Create: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/test/java/dollos/aux/AuxEngineBinderStubTest.kt`

- [ ] **Step 1 (TDD)**：寫 `AuxEngineBinderStubTest` 驗證 `getEngineInfo()` 回含有 `"model"` key 的 JSON、`isModelLoaded()` 預設回 false、`silentJudgment` stub 回 `"[SILENT]"`（預設沉默 consistent with spec §4.7）。
- [ ] **Step 2:** `AuxEngineService.onBind()` 回 `AuxEngineBinder`。
- [ ] **Step 3:** `AuxEngineBinder` 實作六個方法 stub。
- [ ] **Step 4:** 跑 unit test 通過。
- [ ] **Step 5:** Commit。

### Task 9: 把 app 掛進 AOSP（Android.bp + external/ rsync）

**Goal:** 照 master §8 pattern 把 app 整進 AOSP build。

**Files:**
- Create: `~/Projects/DollOS-build/external/DollOSAuxEngine/Android.bp`

- [ ] **Step 1:** `Android.bp` 用 `android_app_import`（參 master §8.3）。
- [ ] **Step 2:** `./gradlew assembleRelease` → copy APK → rsync → `m DollOSAuxEngine`。
- [ ] **Step 3:** flash 上 Pixel 6a，`adb shell pm list packages | grep aux` 確認安裝。
- [ ] **Step 4:** `adb shell dumpsys activity services | grep aux` 確認 service 可 bind。
- [ ] **Step 5:** Commit（AOSP tree 和 app repo 各一 commit）。

---

## 第三段：Runtime 整合 + Model Loader

### Task 10: `LlmRuntime` interface

**Goal:** 抽出 runtime-agnostic interface，讓後面換 runtime 不需動 binder / worker 層。

**Files:**
- Create: `app/src/main/java/dollos/aux/runtime/LlmRuntime.kt`
- Create: `app/src/test/java/dollos/aux/runtime/LlmRuntimeContractTest.kt`

- [ ] **Step 1 (TDD)**：寫 contract test（任何 runtime impl 都該過這些 test，用 fake impl 先驗 interface 合理）。
- [ ] **Step 2:** Define interface：
  ```kotlin
  interface LlmRuntime {
    suspend fun load(modelPath: String, config: LlmConfig)
    suspend fun unload()
    fun isLoaded(): Boolean
    suspend fun generate(
      systemPrompt: String,
      userPrompt: String,
      maxTokens: Int,
      stopSequences: List<String> = emptyList(),
    ): GenerationResult
    fun getMeta(): RuntimeMeta   // model name, quant, param count
    fun getMemoryUsage(): Long   // bytes
  }
  ```
- [ ] **Step 3:** `GenerationResult` 含 `text: String`, `promptTokens: Int`, `completionTokens: Int`, `latencyMs: Long`, `tokensPerSec: Double`.
- [ ] **Step 4:** 寫 `FakeLlmRuntime`（deterministic echo）讓後面 test 可以不依賴真模型。
- [ ] **Step 5:** Test 過。Commit。

### Task 11: 實作 Task 5 決議的 runtime（擇一）

**Goal:** 實作具體 runtime。以下子 task 按 Task 5 `DECISION.md` 挑一支走。**不要實作 fallback 機制**（CLAUDE.md feedback_no_fallback.md）。

**Files (示意，MediaPipe 路線為例)：**
- Create: `app/src/main/java/dollos/aux/runtime/MediaPipeLlmRuntime.kt`

**Files (llama.cpp 路線為例)：**
- Create: `app/src/main/java/dollos/aux/runtime/LlamaCppRuntime.kt`
- Create: `app/src/main/cpp/aux_llm_jni.cpp`
- Create: `app/src/main/cpp/CMakeLists.txt`
- Create: `app/src/main/jniLibs/arm64-v8a/...`（如靜態連結則不需此目錄）

- [ ] **Step 1 (TDD)**：寫 instrumented test（需要真機 + 真模型），`load` → `generate("Say hi", ..., 16)` → 回文非空 + `completionTokens > 0` + `tokensPerSec` 符合 benchmark 預期（±30%）。
- [ ] **Step 2:** 實作 `load` / `unload` / `generate` / `isLoaded` / `getMeta` / `getMemoryUsage`。
- [ ] **Step 3:** Model 路徑從 `/system_ext/dollos/models/aux_llm/<model_id>/` 讀。
- [ ] **Step 4:** Instrumented test 通過。
- [ ] **Step 5:** Commit。

### Task 12: `ModelRegistry` + `ModelAssets`

**Goal:** 單一來源知道目前 active model id、從哪個路徑 load、期望 RAM 多少。

**Files:**
- Create: `app/src/main/java/dollos/aux/model/ModelRegistry.kt`
- Create: `app/src/main/java/dollos/aux/model/ModelAssets.kt`
- Create: `app/src/test/java/dollos/aux/model/ModelRegistryTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證 `ModelRegistry.active()` 回 Task 5 選的 model；`ModelAssets.pathFor(modelId)` 回絕對路徑且 `MODEL_META.json` 可讀。
- [ ] **Step 2:** 實作 `ModelRegistry`（目前 hardcode 一個 active model id，spec §10 說晚點加切換機制）。
- [ ] **Step 3:** `ModelAssets` 讀 `MODEL_META.json`（name / quant / context window / expected_peak_rss_bytes / expected_decode_tps）。
- [ ] **Step 4:** Commit。

### Task 13: `ModelLoader`（async load / unload）

**Goal:** 包 `LlmRuntime.load/unload` 成 idempotent、async、thread-safe，避免兩次 load 並發撞爛。

**Files:**
- Create: `app/src/main/java/dollos/aux/model/ModelLoader.kt`
- Create: `app/src/test/java/dollos/aux/model/ModelLoaderTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證
  - `loadModel()` 兩次並發只 load 一次（用 `FakeLlmRuntime` 計數）
  - `loadModel()` 後 `isLoaded() == true`
  - `unloadModel()` 後 `isLoaded() == false`
  - `unloadModel()` 於 load 進行中 → 等 load 完再 unload（不 race）
  - `loadModel()` → `unloadModel()` → `loadModel()` cycle 正確（可重複使用）
- [ ] **Step 2:** 實作用 `Mutex` + `CoroutineScope(Dispatchers.IO)`。
- [ ] **Step 3:** Test 過。
- [ ] **Step 4:** Commit。

### Task 14: Binder 串入 `ModelLoader`（`isModelLoaded` / `loadModel` / `unloadModel` 真實實作）

**Goal:** 三個 model lifecycle AIDL method 從 stub 變真實。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/androidTest/java/dollos/aux/ModelLifecycleAidlTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test 經 AIDL call `loadModel()` → 輪詢 `isModelLoaded()` 變 true → `getEngineInfo()` 回正確 model name + quant → `unloadModel()` → `isModelLoaded()` 變 false。
- [ ] **Step 2:** Binder `loadModel()` 打 `ModelLoader.startLoad()`（non-blocking，回 void 符合 AIDL oneway 風格）。
- [ ] **Step 3:** Binder `unloadModel()` 打 `ModelLoader.unload()`。
- [ ] **Step 4:** Binder `isModelLoaded()` 代理 `ModelLoader.isLoaded()`。
- [ ] **Step 5:** Binder `getEngineInfo()` 組 JSON：`{model, quant, loaded, ramBytes, decodeTps, contextWindow}`。
- [ ] **Step 6:** Test 過。
- [ ] **Step 7:** Commit。

---

## 第四段：AIDL 六個方法實作

### Task 15: Single-worker Inference Queue

**Goal:** 所有 `generate` / `classify` / `summarize` / `silentJudgment` 進 queue，單一 worker dispatcher 順序執行；不同時跑兩個 inference（LLM 一次只能一個 forward pass）。

**Files:**
- Create: `app/src/main/java/dollos/aux/inference/InferenceRequest.kt`
- Create: `app/src/main/java/dollos/aux/inference/InferenceResult.kt`
- Create: `app/src/main/java/dollos/aux/inference/InferenceWorker.kt`
- Create: `app/src/test/java/dollos/aux/inference/InferenceWorkerTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證
  - 提 3 個並發 request → worker 依序完成（`FakeLlmRuntime` 記錄順序）
  - Model 未 load 時提 request → worker 等 load 完再跑（或依 policy 拒絕 — 本 plan 選「拒絕並回 error」以保 crash-fast）
  - Unload 進行中 → in-flight request 允許完成，新 request 拒絕
- [ ] **Step 2:** `InferenceRequest` sealed class：`Generate` / `Classify` / `Summarize` / `SilentJudgment`.
- [ ] **Step 3:** `InferenceWorker` 用 `Channel<InferenceRequest>` + single `CoroutineScope` 單線程消費。
- [ ] **Step 4:** 每個 request 有 `CompletableDeferred<InferenceResult>` 讓 caller 等結果（Binder 同步 AIDL call 用 `runBlocking { deferred.await() }`）。
- [ ] **Step 5:** Timeout（預設 30s）到自動 cancel 並回 error。
- [ ] **Step 6:** Test 過。
- [ ] **Step 7:** Commit。

### Task 16: `generate` AIDL method

**Goal:** 通用文字生成。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/androidTest/java/dollos/aux/GenerateAidlTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test `generate(system, user, 32)` 回非空字串且 token count <= 32。
- [ ] **Step 2:** Binder `generate` 包 `InferenceRequest.Generate` 丟進 worker，`runBlocking` 等結果。
- [ ] **Step 3:** 結果用 `MetricsLogger` 記一筆。
- [ ] **Step 4:** Commit。

### Task 17: `PromptTemplates` — `classify`

**Goal:** Classify prompt 強制模型回恰好一個 label，不回其他內容。

**Files:**
- Create: `app/src/main/java/dollos/aux/prompts/PromptTemplates.kt`
- Create: `app/src/test/java/dollos/aux/prompts/ClassifyPromptTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證 `PromptTemplates.classify(input, labels)` 生的 prompt 含：
  - 所有 label 明確列出
  - Few-shot 範例（至少 3 個）
  - 明確指令「Output EXACTLY one of the labels above, nothing else.」
  - System prompt 加 stop sequence = `"\n"` 限制輸出
- [ ] **Step 2:** 實作 template（範例）：
  ```
  SYSTEM: You are a strict classifier. Output EXACTLY one label from: {labels}. No punctuation, no explanation.

  Example:
  Input: "someone knocking at the door"
  Output: ambient

  Input: "hey doll can you help"
  Output: calling_me

  USER: Input: "{input}"
  Output:
  ```
- [ ] **Step 3:** Test 過。Commit。

### Task 18: `classify` AIDL method

**Goal:** Binder 串 template + runtime，若 output 不在 labels 內 → 最接近 label（fuzzy match）或 raise error（**不做 fallback**：若 fuzzy match 也無則 throw，讓 caller 知道）。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/androidTest/java/dollos/aux/ClassifyAidlTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test
  - `classify("someone knocking", ["ambient","conversation","calling_me","noise"])` 回 `"ambient"` 或 `"noise"`
  - `classify("DOLL HELP", ["calling_me","ambient"])` 回 `"calling_me"`
  - 若模型亂回（測試用 FakeRuntime mock 亂字串）→ throw `IllegalStateException`
- [ ] **Step 2:** Binder `classify` 打 template → runtime → trim/lowercase → 檢查在 labels 內。
- [ ] **Step 3:** 不在 labels 內時嘗試 case-insensitive substring match 找最接近 label；仍不到 → throw（記 metric 標記 classify miss）。
- [ ] **Step 4:** Commit。

### Task 19: `PromptTemplates` — `summarize`

**Goal:** Summarize prompt 控制在 `targetChars` 字數以內。

**Files:**
- Modify: `app/src/main/java/dollos/aux/prompts/PromptTemplates.kt`
- Create: `app/src/test/java/dollos/aux/prompts/SummarizePromptTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證 prompt 含 `targetChars` 數字明確嵌入 + few-shot 範例 + stop sequence。
- [ ] **Step 2:** Template（範例）：
  ```
  SYSTEM: Summarize the text below in at most {targetChars} characters. Preserve key facts, drop filler.
  USER: {longText}
  Summary:
  ```
- [ ] **Step 3:** Commit。

### Task 20: `summarize` AIDL method

**Goal:** Binder 串 template，若 output 超過 `targetChars * 1.5` → 截斷（不走 fallback，但長度輕微超標不算失敗）。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/androidTest/java/dollos/aux/SummarizeAidlTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test `summarize(longText, 100)` 回字串 <= 150 chars 且含原文關鍵詞。
- [ ] **Step 2:** Binder 實作 + 長度 guardrail。
- [ ] **Step 3:** Commit。

### Task 21: `PromptTemplates` — `silentJudgment` + Grammar

**Goal:** **關鍵 prompt**。確保模型回傳正好四選一字串之一：`[SILENT]` / `[SPEAK "..."]` / `[VIBRATE "..."]` / `[INTERRUPT "..."]`。

**Files:**
- Modify: `app/src/main/java/dollos/aux/prompts/PromptTemplates.kt`
- Create: `app/src/main/java/dollos/aux/prompts/SilentJudgmentGrammar.kt`
- Create: `app/src/test/java/dollos/aux/prompts/SilentJudgmentPromptTest.kt`
- Create: `app/src/test/java/dollos/aux/prompts/SilentJudgmentGrammarTest.kt`

- [ ] **Step 1 (TDD grammar)**：test 驗證 `SilentJudgmentGrammar.parse(output)` 四種 case:
  - `"[SILENT]"` → `SilentJudgmentResult.Silent`
  - `"[SPEAK \"hi there\"]"` → `SilentJudgmentResult.Speak("hi there")`
  - `"[VIBRATE \"new email\"]"` → `SilentJudgmentResult.Vibrate("new email")`
  - `"[INTERRUPT \"alarm\"]"` → `SilentJudgmentResult.Interrupt("alarm")`
  - 任何其他格式 → `SilentJudgmentResult.Invalid(raw)`
- [ ] **Step 2 (TDD prompt)**：test 驗證 prompt 含明確指令：
  ```
  Decide output mode. Output EXACTLY one of:
  [SILENT]
  [SPEAK "{text}"]
  [VIBRATE "{short summary}"]
  [INTERRUPT "{text}"]

  No other format. No explanation. Default to [SILENT] if unsure.
  ```
  + 3 個 few-shot（ambient quiet → SILENT、noisy env → VIBRATE、user is in conversation → SILENT、alarm firing → INTERRUPT）
- [ ] **Step 3:** 實作 template + grammar parser（regex / state machine 皆可，要處理嵌套引號）。
- [ ] **Step 4:** Tests 過。
- [ ] **Step 5:** Commit。

### Task 22: `silentJudgment` AIDL method

**Goal:** Binder 串 template + grammar，**回傳值 spec 規定必為四種字串之一**（master §3.3 comment 寫「`[SILENT]` | `[SPEAK]` | `[VIBRATE]` | `[INTERRUPT]`」— 實作照 spec §4.7 完整四選一 tag 含內容）。

**實作規定：**
- 模型 output 經 grammar parse → 若 `Invalid` 則 retry 一次（同 prompt，temperature=0）→ 仍 Invalid 則**回 `"[SILENT]"`**（預設沉默符合 spec §4.7）並記 metric `silent_judgment_fallback_silent`。
- 有效 output 原樣回傳（caller Core Output Orchestrator 自己 parse）。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/androidTest/java/dollos/aux/SilentJudgmentAidlTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test
  - `silentJudgment("hi", contextQuietJson)` 回字串以 `[SILENT]` 或 `[SPEAK` 開頭
  - `silentJudgment("morning", contextSleepingJson)` 回 `[SILENT]`（使用者睡覺）
  - `silentJudgment("alarm fires", contextAlarmJson)` 回 `[INTERRUPT ...]`
  - 回傳值永遠四選一字串其中一種（regex assert）
- [ ] **Step 2:** Binder 實作包 prompt → generate → grammar parse → retry / default。
- [ ] **Step 3:** Commit。

### Task 23: `getEngineInfo` 完整化

**Goal:** 之前 Task 14 已有基本 JSON，現在加 runtime metrics（累積 TPS 平均、最近 10 次 latency p50/p99、request count）。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineBinder.kt`
- Create: `app/src/test/java/dollos/aux/GetEngineInfoTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證 JSON shape 包含 `{model, quant, loaded, ramBytes, contextWindow, metrics: {requestCount, avgDecodeTps, latencyP50Ms, latencyP99Ms}}`。
- [ ] **Step 2:** 串入 `MetricsLogger.snapshot()`。
- [ ] **Step 3:** Commit。

---

## 第五段：Memory Pressure Handling

### Task 24: `MemoryPressureObserver` (`onTrimMemory`)

**Goal:** 監聽 Android memory pressure signal → 超過 threshold 自動 unload model。

**Files:**
- Create: `app/src/main/java/dollos/aux/memory/MemoryPressureObserver.kt`
- Create: `app/src/test/java/dollos/aux/memory/MemoryPressureObserverTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證
  - `onTrimMemory(TRIM_MEMORY_RUNNING_LOW)` 不 unload（太頻繁會 thrash）
  - `onTrimMemory(TRIM_MEMORY_RUNNING_CRITICAL)` 觸發 unload
  - `onTrimMemory(TRIM_MEMORY_COMPLETE)` 觸發 unload
  - 多次 critical signal 只 unload 一次（idempotent via `ModelLoader`）
- [ ] **Step 2:** `AuxEngineApp.onCreate()` 註冊 `ComponentCallbacks2` observer。
- [ ] **Step 3:** Observer `onTrimMemory` 依 level 打 `ModelLoader.unload()`。
- [ ] **Step 4:** 記 metric `model_unloaded_by_pressure`。
- [ ] **Step 5:** Commit。

### Task 25: 之後重 load 測試

**Goal:** 驗證 unload 後下次 AIDL 方法呼叫能自動 reload，或 caller 明確 `loadModel()` 後再 call 能恢復。

**實作決定：** AIDL 六個 inference method 若遇 `isLoaded() == false` → **throw IllegalStateException**（不做自動 reload fallback，符合 CLAUDE.md no fallback 原則）。Caller（Core LLM Router）看 exception 決定自己重 load 或走雲端 Main。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/UnloadReloadCycleTest.kt`

- [ ] **Step 1 (TDD)**：instrumented test
  - `loadModel()` → `generate(...)` OK
  - 觸發 `TRIM_MEMORY_COMPLETE`（用 `runtime.getRuntime().gc()` + 自觸）→ `isModelLoaded() == false`
  - `generate(...)` → throw
  - `loadModel()` → `generate(...)` 再次 OK
- [ ] **Step 2:** 修 bug（如果有），確保 cycle 穩定。
- [ ] **Step 3:** Commit。

---

## 第六段：Performance 監控 + Log

### Task 26: `InferenceMetrics` + `MetricsLogger`

**Goal:** 每次 inference 記 TPS / latency / input token / output token / peak RSS delta。

**Files:**
- Create: `app/src/main/java/dollos/aux/metrics/InferenceMetrics.kt`
- Create: `app/src/main/java/dollos/aux/metrics/MetricsLogger.kt`
- Create: `app/src/test/java/dollos/aux/metrics/MetricsLoggerTest.kt`

- [ ] **Step 1 (TDD)**：test 驗證 `MetricsLogger.record(metric)` + `snapshot()` 回正確統計（count、avg、p50、p99）。
- [ ] **Step 2:** 實作 ring buffer（size 100）存最近 request metrics。
- [ ] **Step 3:** 寫 Logcat tag `DollAuxMetrics`，每次 record 印一行 JSON。
- [ ] **Step 4:** Commit。

### Task 27: Logcat 串接 + dumpsys 支援

**Goal:** 開發期除錯方便。

**Files:**
- Modify: `app/src/main/java/dollos/aux/AuxEngineService.kt`

- [ ] **Step 1:** Service `dump(fd, writer, args)` 寫 engine info + 最近 20 筆 metrics + model 狀態。
- [ ] **Step 2:** `adb shell dumpsys activity service dollos.aux/.AuxEngineService` 看到輸出。
- [ ] **Step 3:** Commit。

---

## 第七段：整合測試

### Task 28: E2E integration test（bind AIDL + 跑四種 method）

**Goal:** 最小驗收：由 test app bind AIDL → `loadModel()` → 依序跑 `generate` / `classify` / `summarize` / `silentJudgment` → 確認回傳 shape 正確 + 無 crash。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/AuxEngineE2EIntegrationTest.kt`

- [ ] **Step 1:** Test bind service (`bindService` with `Intent("dollos.aux.IDollAuxEngine")`)，等 `onServiceConnected`.
- [ ] **Step 2:** 呼叫 `loadModel()` + 輪詢 `isModelLoaded()` 變 true（timeout 60s）。
- [ ] **Step 3:** 跑完四種 method，每種 assert 回傳 shape。
- [ ] **Step 4:** `getEngineInfo()` 檢查 metrics.requestCount >= 4。
- [ ] **Step 5:** `unloadModel()` → `isModelLoaded() == false`.
- [ ] **Step 6:** Commit。

### Task 29: 壓力測試 — 連續 50 次 `classify`

**Goal:** 驗證 queue + single worker 能穩定跑久，無 OOM、無 thermal 斷崖。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/AuxEngineStressTest.kt`

- [ ] **Step 1:** 連續 50 次 `classify`（不同 input，同 labels），記每次 latency。
- [ ] **Step 2:** Assert：no exception、p99 latency < 2x p50、peak RSS 不持續上升（leak detection）。
- [ ] **Step 3:** Commit。

### Task 30: Memory pressure cycle 壓力測試

**Goal:** 反覆 load → inference → pressure unload → load 十次。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/PressureCycleStressTest.kt`

- [ ] **Step 1:** Loop 10 次：`loadModel()` → 3 次 `generate` → 模擬 `TRIM_MEMORY_COMPLETE` → `isModelLoaded() == false` → 下次 iteration。
- [ ] **Step 2:** Assert：最終 state clean，peak RSS 穩定（每 cycle 不累加）。
- [ ] **Step 3:** Commit。

### Task 31: 熱機測試（5 分鐘連續 inference）

**Goal:** 驗證 Pixel 6a 連續跑不會 thermal throttle 到不堪用。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/ThermalEnduranceTest.kt`
- Modify: `benchmark/scripts/thermal_monitor.sh`（可重用）

- [ ] **Step 1:** Test 5 分鐘連跑 `classify`，同時背景 shell 取 thermal zone 溫度。
- [ ] **Step 2:** Assert：5 分鐘後 TPS 下降 < 40%（比前 30 秒平均），且過程無 crash。
- [ ] **Step 3:** 若失敗 → 記入 `benchmark/thermal_findings.md`，回頭考慮 Task 5 決議是否要換 model / quant。
- [ ] **Step 4:** Commit。

### Task 32: 與 DollOSCore（mock）整合 smoke test

**Goal:** 模擬 Core 真的 bind + 高頻 call 的流量 profile。

**Files:**
- Create: `app/src/androidTest/java/dollos/aux/CoreIntegrationSmokeTest.kt`

- [ ] **Step 1:** Test 模擬 Core 的調用流量（每 10s 一次 silentJudgment + 每 30s 一次 classify + 每 60s 一次 summarize）跑 5 分鐘。
- [ ] **Step 2:** Assert：latency p50 在 benchmark 目標內、無 exception、`getEngineInfo()` 顯示 metrics 合理。
- [ ] **Step 3:** Commit。

---

## 第八段：收尾

### Task 33: 撰寫 `AuxEngine` 使用 doc（給 Core 實作者看）

**Goal:** 簡短使用說明，包括 bind intent、權限、六個 method 的期望輸入 / 輸出、error 行為（throw IllegalStateException when model not loaded）、no fallback 約定。

**Files:**
- Create: `docs/AUX_ENGINE_USAGE.md`（在 DollOSAuxEngine repo 內）

- [ ] **Step 1:** 寫內容。
- [ ] **Step 2:** Commit。

### Task 34: 驗收 checklist 跑過 master §12

**Goal:** 逐條驗 master plan §12：
- Gemma 4 E4B / E2B 在 Pixel 6a 成功跑 ✓（或記錄實際用哪個 fallback model）
- Aux 路由實際打到本地模型（不是雲端 placeholder） ✓（E2E test 覆蓋）
- Main 失敗不降級 Aux；Aux 失敗降級 Main（有網時） — 這條是 Core LLM Router 的責任，本 plan 只確保「Aux 失敗時 throw exception 讓 Core 知道」

**Files:**
- Create: `docs/V1_ACCEPTANCE_REPORT.md`

- [ ] **Step 1:** 寫驗收報告。
- [ ] **Step 2:** Commit。

### Task 35: 最終 AOSP 整合 build + flash 驗證

**Goal:** 完整 build system image + flash Pixel 6a，確認 boot 後 `DollOSAuxEngine` service 正常起，可 bind。

**Files:** 無新增。

- [ ] **Step 1:** `cd ~/Projects/DollOSAuxEngine && ./gradlew assembleRelease && cp ... prebuilt/`.
- [ ] **Step 2:** `rsync` 到 AOSP tree。
- [ ] **Step 3:** `m DollOSAuxEngine -j$(nproc)`.
- [ ] **Step 4:** `adb reboot`，boot 完後 `adb shell dumpsys activity services | grep AuxEngine` 確認存活。
- [ ] **Step 5:** 跑 E2E test（Task 28）再過一次。
- [ ] **Step 6:** Commit。

---

## 非本 plan 範圍

- **Main LLM 路由** — Core plan 的 LLM Router 做
- **Observer 事件分類邏輯** — Observer 用 `classify` AIDL，但 prompt / labels 是 caller（Core event handler）給的
- **Skills 調用** — Skills plan 做
- **Memory `session_search` Aux 濃縮** — 用本 plan `summarize`，但 caller 是 Memory ContentProvider，本 plan 只保證 `summarize` API 行為
- **Silent Judgment 的 context snapshot JSON 組裝** — Core 做，本 plan 只定義 prompt 把它 serialize 進去
- **Aux 失敗降級 Main** — Core LLM Router 做（Aux 本身 throw 後 Core 自己決定下一步）

---

## 風險 / 不確定性

| 風險 | 影響 | 緩解 |
|---|---|---|
| Gemma 4 正式釋出時間 vs Gemma 3n | 可能要先以 3n 上機，4 出了再換 | Task 12 `ModelRegistry` 可切換，不 hardcode |
| 選的 runtime 模型支援變動 | MediaPipe 新版可能 break | 測試 pin 版本；DECISION.md 記錄 SDK 版號 |
| `silentJudgment` grammar 模型不守 | 高 — 破壞 Output Orchestrator | Task 22 retry + default `[SILENT]` 已處理；若 miss rate > 5% 回頭調 prompt |
| 8 個 process 同時 foreground Pixel 6a OOM | 高 | `onTrimMemory` unload + `:aux_llm` 獨立 process 已處理；最壞 Core 降級雲端 Main |
| Single-worker queue 延遲堆積 | 中 | Task 15 timeout + metrics 監控；Core 可依 latency p99 決定是否切雲端 |
| llama.cpp JNI 維護負擔 | 中（若選它） | DECISION.md 列出 trade-off，如選 llama.cpp 須加每季更新節奏 |

---

## 任務依賴摘要

```
Task 1-5 (Benchmark)  ──────────────┐
                                     ▼
                           Task 6-9 (app 骨架)
                                     │
                                     ▼
                          Task 10-14 (runtime + loader)
                                     │
                                     ▼
                          Task 15-23 (6 AIDL methods)
                                     │
                                     ▼
                          Task 24-25 (memory pressure)
                                     │
                                     ▼
                          Task 26-27 (metrics / logcat)
                                     │
                                     ▼
                          Task 28-32 (整合 / 壓力測試)
                                     │
                                     ▼
                          Task 33-35 (收尾)
```

Task 1-5 **必須最先完成**。Task 6-9 可在 Task 5 產出 DECISION.md 後立即開始。Task 17/19/21（三個 PromptTemplates）彼此獨立可平行寫。
