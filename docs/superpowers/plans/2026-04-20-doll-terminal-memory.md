# DollOSMemory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 `DollOSMemory` — Doll 的記憶系統。Markdown 檔案（SOUL.md / USER.md / POLICY.md）+ Room FTS4 對話日誌 + ObjectBox 蒸餾層 embedding + Character Pack 儲存 + fork subagent 蒸餾。透過 `IDollMemory` AIDL 對外提供讀寫。

**Architecture:**
- 單一 Android app (`DollOSMemory`)，`system_ext` priv-app
- Foreground service `MemoryService` 為主入口
- 內部模組：`MdFileManager`（SOUL/USER/POLICY）、`ConversationStore`（Room FTS4）、`DistillationStore`（ObjectBox）、`CharacterPackManager`、`ForkSubagent`（背景蒸餾）
- 對外 AIDL：`IDollMemory`
- ContentProvider：`MemoryContentProvider`（供 Launcher / Settings 只讀 FTS4 query）

**Tech Stack:** Kotlin, Android AIDL, Room (FTS4), ObjectBox (vector search), Kotlin Coroutines, `kotlinx.serialization`, JUnit4 + MockK + Robolectric + Room InMemory Database.

**Spec reference:**
- Master plan §3.4, §3.7, §4, §5, §8 — `docs/superpowers/plans/2026-04-20-doll-terminal.md`
- Design spec — `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`（§5.2、§5.3、§5.5）
- Project rules — `CLAUDE.md`

**Scope boundaries：**
- 本 plan **只**負責 DollOSMemory app。Core 的 Frozen System Prompt 讀 Memory 透過 AIDL client（Core plan §8.3）。
- 本 plan **不**實作：ObjectBox 模型轉換（步驟 5 benchmark 後決定 runtime）、FTS4 全文檢索的 ranking 演算法（Room 預設相關度評分）。

---

## 路徑慣例

- App root：`/home/progcat/Projects/DollOSMemory/`
- Package：`dollos.memory`
- AIDL 檔：`/home/progcat/Projects/DollOSMemory/app/src/main/aidl/dollos/memory/`
- Kotlin src：`/home/progcat/Projects/DollOSMemory/app/src/main/java/dollos/memory/`
- Unit tests：`/home/progcat/Projects/DollOSMemory/app/src/test/java/dollos/memory/`
- Instrumented tests：`/home/progcat/Projects/DollOSMemory/app/src/androidTest/java/dollos/memory/`
- Prebuilt APK 目的地：`/home/progcat/Projects/DollOSMemory/prebuilt/DollOSMemory.apk`
- AOSP 整合：`/home/progcat/Projects/DollOSBuild/external/DollOSMemory/`

---

## 段落 1：App 骨架

### Task 1.1 建立 Gradle 專案骨架
- [ ] 建立 `/home/progcat/Projects/DollOSMemory/` 目錄
- [ ] 建立 `settings.gradle.kts`（rootProject.name = "DollOSMemory"）
- [ ] 建立 `app/build.gradle.kts`：
  - `namespace = "dollos.memory"`
  - `compileSdk = 34, minSdk = 34, targetSdk = 34`
  - 啟用 AIDL、Room plugin
  - 依賴：Room core + compiler + room-testing、ObjectBox + gradle plugin、`kotlinx-coroutines-android`、`kotlinx-serialization-json`
- [ ] 建立 `gradle.properties`、`gradle/wrapper/`
- [ ] 建立空 `app/src/main/AndroidManifest.xml`
- [ ] 寫 failing test：`ProjectSanityTest.kt` 驗證 `BuildConfig` 存在
- [ ] `./gradlew :app:compileDebugKotlin` 驗證可編譯
- [ ] Run test → 通過
- [ ] Commit: `scaffold: create DollOSMemory Gradle project`

### Task 1.2 AndroidManifest + Permissions
- [ ] 編輯 `AndroidManifest.xml`：
  - `<uses-permission>`: `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `INTERNET`（蒸餾可能需要雲端 API）
  - 宣告 `<service android:name=".service.MemoryService" android:foregroundServiceType="specialUse" android:exported="true" android:permission="dollos.memory.permission.BIND_MEMORY">`
  - 宣告自訂 permission `dollos.memory.permission.BIND_MEMORY`
  - 宣告 `<provider android:name=".MemoryContentProvider" android:authorities="dollos.memory" android:exported="true" android:grantUriPermissions="true">`
- [ ] 建立 `MemoryApp.kt` 空 Application subclass
- [ ] 寫 failing test：`ManifestTest.kt`（Robolectric）驗證 service + provider 已宣告
- [ ] 實作、驗證通過
- [ ] Commit: `scaffold: declare manifest, permissions, MemoryService, ContentProvider`

### Task 1.3 MemoryService 骨架
- [ ] 建立 `service/MemoryService.kt`：繼承 `Service`，`onCreate` 建 channel + 初始化 `MemoryGraph`，`onStartCommand` 呼叫 `startForeground()`，`onBind` 回傳 `MemoryBinder`
- [ ] 建立 `service/MemoryNotification.kt` 管 channel
- [ ] 寫 failing test：`MemoryServiceTest.kt`（Robolectric）驗證 `onStartCommand` 呼叫 `startForeground`
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MemoryService foreground skeleton`

### Task 1.4 MemoryGraph DI
- [ ] 建立 `di/MemoryGraph.kt`：holder 持有單例：`MdFileManager`、`ConversationStore`、`DistillationStore`、`CharacterPackManager`、`ForkSubagent`
- [ ] `MemoryService.onCreate` 初始化 `MemoryGraph`
- [ ] 寫 failing test：`MemoryGraphTest.kt` 驗證所有單例可取出
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MemoryGraph DI container`

---

## 段落 2：AIDL 介面定義

### Task 2.1 IDollMemory AIDL
- [ ] 建立 `aidl/dollos/memory/IDollMemory.aidl`（per master §3.7）
- [ ] 建立 Kotlin impls：所有 parcelable（`EngineInfo` 等）
- [ ] 寫 failing test：`AidlContractTest.kt` 驗證所有 method signatures
- [ ] `./gradlew :app:compileDebugKotlin` 讓 AIDL codegen 跑過
- [ ] 驗證通過
- [ ] Commit: `feat: IDollMemory AIDL`

### Task 2.2 MemoryBinder Stub
- [ ] 建立 `binder/MemoryBinder.kt`：`extends IDollMemory.Stub`，建構式接 `MemoryGraph`
- [ ] 每個 method 先 `TODO` 拋 UnsupportedOperationException（骨架階段）
- [ ] `MemoryService.onBind` 回 `MemoryBinder(graph)`
- [ ] 寫 failing test：`MemoryBinderTest.kt` 驗證 `onBind` 回非 null
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MemoryBinder stub skeleton`

---

## 段落 3：Markdown 檔案管理（SOUL / USER / POLICY）

### Task 3.1 MdFileManager 資料結構
- [ ] 建立 `md/MdFileManager.kt`：
  - 檔案位置：`/data/system_ext/dollos/memory/`
  - 固定檔名：`SOUL.md`、`USER.md`、`POLICY.md`
  - 提供：`readFile(name): String`、`writeFile(name, content): Unit`、`appendToFile(name, content): Unit`、`listFiles(): List<String>`
  - 寫入前 safety scan：檢查 prompt injection、憑證、隱形 unicode
  - USER.md / POLICY.md chars limit：2200 字元，超出時拋 `FileFullException`（caller 需先 review + 壓縮）
- [ ] 建立 `md/SafetyScanner.kt`：`scan(text: String): String`（過濾掉危險字元）
- [ ] 建立 `md/FileLimit.kt`：`data class FileLimit(val name: String, val maxChars: Int)`
- [ ] 寫 failing tests：`MdFileManagerTest.kt`
  - 寫入空檔 → 建立
  - 讀取已有檔 → 回內容
  - USER.md 寫入 2300 chars → 拋 FileFullException
  - safety scan 過濾掉 API key 格式字串
  - appendToFile → 追加不覆蓋
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MdFileManager with safety scan and char limit`

### Task 3.2 SOUL.md 管理
- [ ] `MdFileManager` 提供 `readSoul(): String` / `writeSoul(content: String)` convenience methods
- [ ] 初始 SOUL.md 不存在時，`readSoul()` 回空字串（Core 端用空 prompt）
- [ ] 寫 failing test：`SoulManagerTest.kt` — 寫入後讀回一致
- [ ] 實作、驗證通過
- [ ] Commit: `feat: SOUL.md read/write convenience`

### Task 3.3 USER.md / POLICY.md 管理
- [ ] 同 Task 3.2 的 convenience：`readUser()` / `writeUser()` / `appendUser()` / `readPolicy()` / `writePolicy()` / `appendPolicy()`
- [ ] `appendPolicy(content)` 自動在內容前加 `\n- `（列表格式）
- [ ] 寫 failing tests：`UserPolicyManagerTest.kt` — appendPolicy 格式正確、chars limit 檢查
- [ ] 實作、驗證通過
- [ ] Commit: `feat: USER.md / POLICY.md read/write/append`

### Task 3.4 Character Pack 檔案管理
- [ ] 建立 `pack/CharacterPackFileManager.kt`：
  - 目錄：`/data/system_ext/dollos/memory/packs/<packId>/`
  - 每包：`manifest.json`、`SOUL.md`、`initial_policy.md`、`overlays/`（多個 .md）
  - `savePack(packId, manifest, soulMd, initialPolicyMd, overlays: Map<String, String>)`
  - `loadPackSoul(packId): String`
  - `loadPackOverlays(packId): Map<String, String>`
  - `listPacks(): List<String>`（回 packId 清單）
  - `deletePack(packId): Unit`
- [ ] 寫 failing tests：`CharacterPackFileManagerTest.kt` — save → load 一致、listPacks 正確、delete 後 load 回空
- [ ] 實作、驗證通過
- [ ] Commit: `feat: CharacterPackFileManager with save/load/list/delete`

---

## 段落 4：Conversation Store（Room FTS4）

### Task 4.1 Room Schema
- [ ] 建立 `db/ConversationEntry.kt`：`@Entity` data class
  - `@PrimaryKey autoIncrement = true` id: Long
  - `session: String`（session ID）
  - `role: String`（"user" / "assistant" / "observation" / "system"）
  - `content: String`
  - `timestampMs: Long`
  - `@Index` on `session` + `timestampMs`
- [ ] 建立 FTS4 virtual table：`ConversationFts`（using `@Fts` annotation or Room FTS4 helper）
  - 欄位：`role`、`content`（合起來為 content 列）
  - `content_source_rowid` 自動維護
- [ ] 建立 `db/ConversationDao.kt`：
  - `suspend fun insert(entry: ConversationEntry)`
  - `suspend fun insertBatch(entries: List<ConversationEntry>)`
  - `suspend fun search(session: String?, query: String, limit: Int): List<ConversationEntry>`
  - `suspend fun deleteBefore(session: String?, beforeMs: Long)`（清理舊資料）
  - `suspend fun count(session: String?): Long`
- [ ] 建立 `db/MemoryDatabase.kt`：`@Database(entities = [ConversationEntry::class], version = 1, exportSchema = true)`
- [ ] 寫 failing tests：`ConversationDaoTest.kt`（InMemory Database）
  - insert → search 回結果
  - FTS4 match 正確（keyword search）
  - insertBatch 批量寫入
  - deleteBefore 清理舊資料
- [ ] 實作、驗證通過
- [ ] Commit: `feat: Conversation Room database with FTS4`

### Task 4.2 ConversationStore 服務層
- [ ] 建立 `db/ConversationStore.kt`：
  - `appendConversation(role, content, timestampMs)` → insert
  - `searchConversation(query, limit)` → FTS4 search + 回 content 清單（不含 session/role/timestamp）
  - `getConversationHistory(session: String, limit: Int): List<ConversationEntry>`（最近 N turn）
  - `clearSession(session: String)`
- [ ] 寫 failing tests：`ConversationStoreTest.kt` — 追加 + 搜尋 + history 取回
- [ ] 實作、驗證通過
- [ ] Commit: `feat: ConversationStore service layer`

### Task 4.3 ContentProvider（只讀 FTS4 query）
- [ ] 建立 `MemoryContentProvider.kt`：
  - `android:authorities="dollos.memory"`
  - `query()` 支援 URI：`content://dollos.memory/ftss/search?q=<query>&limit=<n>`
  - 回傳 Cursor（欄位：content, role, timestampMs）
  - 不支援 write URI（只讀）
- [ ] 寫 failing test：`ContentProviderTest.kt` — query FTS search 回正確結果
- [ ] 實作、驗證通過
- [ ] Commit: `feat: MemoryContentProvider for read-only FTS4 query`

---

## 段落 5：蒸餾層（ObjectBox）

### Task 5.1 ObjectBox Schema
- [ ] 建立 `distill/DistillationEntry.kt`：`@Entity` data class
  - id: Long（auto increment）
  - `summary: String`（蒸餾後的摘要文字）
  - `embeddingJson: String`（vector embedding，JSON array of floats）
  - `periodStart: Long`（摘要涵蓋的起始時間）
  - `periodEnd: Long`（摘要涵蓋的結束時間）
  - `entryType: String`（"user_fact" / "policy_rule" / "conversation_summary" / "observation_summary"）
  - `sourcePackId: String?`（若是 character-specific 記憶）
- [ ] 建立 `distill/DistillationBox.kt`：ObjectBox wrapper
  - `suspend fun insert(entry: DistillationEntry)`
  - `suspend fun semanticSearch(queryEmbedding: FloatArray, limit: Int): List<DistillationEntry>`（餘弦相似度）
  - `suspend fun getByType(type: String, limit: Int): List<DistillationEntry>`
  - `suspend fun deleteBefore(periodEndBefore: Long)`（清理舊蒸餾）
- [ ] 建立 `distill/EmbeddingUtils.kt`：`cosineSimilarity(a: FloatArray, b: FloatArray): Float`
- [ ] 寫 failing tests：`DistillationEntryTest.kt` + `EmbeddingUtilsTest.kt`
  - cosine similarity 正確（相同 vector → 1.0、隨機 → ~0.0）
  - insert + semanticSearch 回正確排序
- [ ] 實作、驗證通過
- [ ] Commit: `feat: ObjectBox distillation layer with semantic search`

### Task 5.2 DistillationStore 服務層
- [ ] 建立 `distill/DistillationStore.kt`：
  - `appendDistillationEntry(summary, embeddingJson, timestampMs, entryType)`
  - `semanticSearch(query, limit)` — caller 需提供 embedding（由 Aux LLM 產生）
  - `getRecentEntries(limit: Int, sinceMs: Long)` — 最近 N 筆
  - `getByType(type: String): List<DistillationEntry>`
- [ ] 寫 failing tests：`DistillationStoreTest.kt` — append + search + filter by type
- [ ] 實作、驗證通過
- [ ] Commit: `feat: DistillationStore service layer`

### Task 5.3 蒸餾 embedding 產生
- [ ] 建立 `distill/EmbeddingProducer.kt`：
  - `suspend fun produce(text: String): FloatArray` — 透過 `IDollAuxEngine.generateEmbedding()` AIDL 呼叫
  - 若 Aux 未就緒 → 回零向量（placeholder，等步驟 5 benchmark 後實作）
- [ ] 寫 failing test：`EmbeddingProducerTest.kt` — fake Aux 回 embedding → produce 回相同 vector
- [ ] 實作、驗證通過
- [ ] Commit: `feat: EmbeddingProducer via AuxEngine AIDL`

---

## 段落 6：Fork Subagent 蒸餾

### Task 6.1 ForkSubagent 骨架
- [ ] 建立 `distill/ForkSubagent.kt`：
  - `suspend fun runDistillation(trigger: DistillationTrigger)`
  - `DistillationTrigger` sealed class：`Idle` / `ChargingStarted` / `SessionEnd`
  - 流程：
    1. 設 `FlagsRegistry.distilling = true`（透過 Core AIDL）
    2. 讀最近 24h 觀察事件（FTS4 `search` with `observation` role filter, limit=500）
    3. 讀 SOUL.md + USER.md + POLICY.md
    4. 組 distillation prompt（hermes 風格 review prompt）
    5. 呼叫 Aux LLM（`IDollAuxEngine.generate`）
    6. Parse response：提取新的 USER.md 條目 / POLICY.md 規則 / distilled summary
    7. 寫入對應檔案 + DistillationStore
    8. 設 `distilling = false`
- [ ] 寫 failing test：`ForkSubagentSkeletonTest.kt` — fake Core + fake Aux → runDistillation 完成不拋例外
- [ ] 實作、驗證通過
- [ ] Commit: `feat: ForkSubagent skeleton with distillation flow`

### Task 6.2 Distillation Prompt Template
- [ ] 建立 `distill/DistillationPrompt.kt`：
  - `compose(trigger, soul, user, policy, recentEvents): String`
  - 結構：
    ```
    You are reviewing Doll's recent observations and conversations.
    
    SOUL: <soul>
    USER: <user>
    POLICY: <policy>
    
    Recent observations (last 24h):
    <recentEvents>
    
    Task:
    1. Extract new facts about the user → output as "- <fact>" lines
    2. Extract new rules/preferences → output as "- <rule>" lines
    3. Create a distilled summary of recent interactions (max 200 chars)
    
    Output format:
    [NEW_USER]
    - <new fact 1>
    - <new fact 2>
    
    [NEW_POLICY]
    - <new rule 1>
    
    [SUMMARY]
    <distilled summary>
    
    [DONE]
    ```
- [ ] 寫 failing tests：`DistillationPromptTest.kt` — compose 回包含所有 section、events 超過 limit 時截斷
- [ ] 實作、驗證通過
- [ ] Commit: `feat: DistillationPrompt template`

### Task 6.3 Response Parser
- [ ] 建立 `distill/DistillationParser.kt`：
  - `parse(response: String): DistillationResult`
  - `DistillationResult` data class：`newUserFacts: List<String>`, `newPolicyRules: List<String>`, `summary: String`
  - Grammar：以 `[NEW_USER]` / `[NEW_POLICY]` / `[SUMMARY]` / `[DONE]` 為 section markers
  - Parse 失敗 → 回空 `DistillationResult`（不寫入）
- [ ] 寫 failing tests：`DistillationParserTest.kt` — 10 cases 涵蓋各種 response 格式
- [ ] 實作、驗證通過
- [ ] Commit: `feat: DistillationResponse parser`

### Task 6.4 Write-back with limit enforcement
- [ ] `ForkSubagent.runDistillation` 解析 response 後：
  - `newUserFacts` → 計算追加後 USER.md 總 chars → 若 > 2200 → 先調用 `compressFile("USER.md")`
  - `newPolicyRules` → 同上對 POLICY.md
  - `summary` → 寫入 DistillationStore（entryType="conversation_summary"）
- [ ] 建立 `md/FileCompressor.kt`：`compress(fileName: String): String`（call Aux LLM 壓縮到原始長度 80%）
- [ ] 寫 failing tests：`WriteBackTest.kt` — 2200 chars 滿時觸發 compress、summary 寫入 DistillationStore
- [ ] 實作、驗證通過
- [ ] Commit: `feat: distillation write-back with char limit enforcement`

---

## 段落 7：Character Pack Manager

### Task 7.1 CharacterPackManager 服務層
- [ ] 建立 `pack/CharacterPackManager.kt`：
  - 接 `CharacterPackFileManager` + `MdFileManager`
  - `saveCharacterPack(packId, modelPfd, soulMd, overlaysDirPath, initialPolicyMd, skillsDirPath)`
    - 寫 manifest.json
    - 複製 SOUL.md、initial_policy.md 到 pack 目錄
    - 複製 overlays/*.md
    - 複製 skills/ 目錄
    - 複製 model.glb（從 ParcelFileDescriptor）
  - `loadCharacterPackSoul(packId): String`
  - `loadCharacterPackOverlays(packId): Map<String, String>`
  - `listCharacterPacks(): List<String>`
  - `setActivePack(packId): Unit`（寫 `/data/system_ext/dollos/memory/active_pack_id`）
  - `getActivePackId(): String`
  - `deletePack(packId): Unit`
- [ ] 寫 failing tests：`CharacterPackManagerTest.kt` — save → load → setActive → getActive 一致
- [ ] 實作、驗證通過
- [ ] Commit: `feat: CharacterPackManager service layer`

### Task 7.2 v1 → v2 Migration
- [ ] 建立 `pack/V1Migration.kt`：
  - 掃描 `/data/system_ext/dollos/characters/`（v1 路徑）
  - 對每個既有 character pack：
    - 讀 `personality.json`（既有格式）
    - 轉為 `SOUL.md`（extract name, description, personality traits）
    - 寫新格式到 `/data/system_ext/dollos/memory/packs/<id>/`
    - 更新 manifest.json version=2
  - 標記 migration 完成（`/data/system_ext/dollos/memory/.v2_migrated`）
- [ ] 寫 failing test：`V1MigrationTest.kt` — 模擬 v1 檔案 → 執行 migration → 驗證 v2 格式
- [ ] 實作、驗證通過
- [ ] Commit: `feat: Character Pack v1 → v2 migration`

---

## 段落 8：Session Search 工具

### Task 8.1 sessionSearch 實現
- [ ] `ConversationStore` 加 `sessionSearch(query: String, limit: Int): List<String>`
  - FTS4 match → 回 content 字串清單（不回完整 entry，節省 context）
  - Aux LLM 摘要 top-N 結果（透過 `IDollAuxEngine.summarize`）
- [ ] 寫 failing test：`SessionSearchTest.kt` — FTS4 match + Aux summarize
- [ ] 實作、驗證通過
- [ ] Commit: `feat: sessionSearch with FTS4 + Aux summarization`

### Task 8.2 semanticSearch 實現
- [ ] `DistillationStore` 加 `semanticSearch(query: String, limit: Int): List<String>`
  - 先 call Aux LLM 把 query 轉成 embedding
  - 再 ObjectBox cosine search
  - 回 summary + 時間範圍字串清單
  - Doll 要細節再 FTS4 查原始對話
- [ ] 寫 failing test：`SemanticSearchTest.kt` — query → embedding → search → 回摘要
- [ ] 實作、驗證通過
- [ ] Commit: `feat: semanticSearch via ObjectBox + Aux embedding`

---

## 段落 9：整合測試

### Task 9.1 Memory E2E：write → freeze → read
- [ ] 建立 `MemoryE2ETest.kt`（instrumented）
- [ ] 寫 SOUL.md + USER.md + POLICY.md → 透過 AIDL bind service → readFile 回一致
- [ ] Commit: `test: MD file write/read E2E`

### Task 9.2 Conversation FTS4 E2E
- [ ] 建立 `ConversationFtsE2ETest.kt`
- [ ] appendConversation 10 turns → searchConversation keyword → 回正確 turns
- [ ] Commit: `test: FTS4 conversation search E2E`

### Task 9.3 Distillation E2E
- [ ] 建立 `DistillationE2ETest.kt`
- [ ] insert DistillationEntry → semanticSearch → 回正確 entry
- [ ] ForkSubagent.runDistillation(ChargingStarted) → 驗證檔案被寫入 + DistillationStore 有 entry
- [ ] Commit: `test: distillation fork subagent E2E`

### Task 9.4 Character Pack E2E
- [ ] 建立 `CharacterPackE2ETest.kt`
- [ ] savePack → listPacks → loadSoul → setActive → getActive 全通
- [ ] Commit: `test: character pack save/load/activate E2E`

### Task 9.5 AOSP build 整合
- [ ] 建立 `/home/progcat/Projects/DollOSBuild/external/DollOSMemory/Android.bp`
- [ ] `./gradlew :app:assembleRelease` → `cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSMemory.apk`
- [ ] `rsync -av --delete . ~/Projects/DollOSBuild/external/DollOSMemory/`
- [ ] AOSP build: `m DollOSMemory -j$(nproc)`
- [ ] 寫 smoke script
- [ ] Commit: `build: AOSP integration for DollOSMemory`

### Task 9.6 裝機 smoke test（subagent）
- [ ] 派 subagent 執行 smoke script
- [ ] 驗證 service 運作、AIDL bind 成功、MD 檔案寫入
- [ ] Commit: `docs: record on-device smoke test results`

---

## 依賴清單

本 plan 引用但不實作的 AIDL / 外部介面：

- `dollos.core.IDollCore` — Task 6.1 fork subagent 設 distilling flag（Core plan 負責）
- `dollos.aux.IDollAuxEngine` — Task 5.3 embedding、Task 6 distillation prompt + summarize（AuxEngine plan 負責）

---

## 交付判準

本 plan 完成 =
- 所有 task 的 commit 都在 main
- `./gradlew test` 全綠
- `./gradlew connectedAndroidTest` 全綠
- AOSP `m DollOSMemory` 編得出 APK
- 裝機 smoke：MD 檔案寫入讀取正確、FTS4 search 回結果、Character Pack save/load 正確、distillation fork 完成不拋例外

---

**Plan complete.** 總 task 數：約 45。涵蓋 DollOSMemory 從 skeleton 到 distillation 的完整路徑：MD 檔案管理（§3）+ FTS4 對話日誌（§4）+ ObjectBox 蒸餾層（§5）+ fork subagent 蒸餾（§6）+ Character Pack Manager（§7）+ session/semantic search（§8）。
