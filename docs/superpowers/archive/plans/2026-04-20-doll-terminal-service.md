# Doll AI Terminal — DollOSService App Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 擴充既有 DollOSService（AOSP system_ext priv-app，system UID），補齊 Doll AI Terminal v1.0 所需的系統安全網 UI（電源菜單整合、緊急撥號入口、factory reset 入口），將既有 Emergency Stop 流程接到 DollOSCore 的 `IDollCore.emergencyStop()` AIDL，並整理現有 `IDollOSService` 動作執行介面供 Skills app 呼叫。

**Architecture:**
- **不是新 app**：DollOSService 已存在於 `packages/apps/DollOSService/`，是 AOSP tree 內的 `system_ext` + `platform` certificate + `sharedUserId=android.uid.system` 系統服務。本 plan 只在其上擴充。
- **安全網 UI 分層**：(a) 電源菜單整合 AI Stop / reboot / 關機 三顆按鈕 (b) TaskManagerActivity（既有，鎖屏可達）用作 AI Stop UI (c) 緊急撥號 pass-through (d) factory reset 隱藏入口（撥號碼 secret code）。
- **Emergency Stop 流程**：TaskManagerActivity `Stop AI` 按鈕 → DollOSService bind 到 `IDollCore` → 呼叫 `emergencyStop(reason)` → Core 停所有輸出/routines/TTS、切 `dnd_active=true`。
- **Skills 動作執行**：既有 `IDollOSService.executeSystemAction()` + `getAvailableActions()` 已提供 open app / set alarm / toggle WiFi / toggle Bluetooth，本 plan 負責文件化、擴充（power off / reboot / factory reset 等 system-only 動作），並確認 DollOSSkills app 可以 bind。

**Tech Stack:** Kotlin, Android AIDL (Binder IPC), AOSP platform API, `android.uid.system` shared UID, `platform` certificate, AOSP build (`m DollOSService`, **not** Gradle).

**Spec references:**
- Master plan: `docs/superpowers/plans/2026-04-20-doll-terminal.md`（§3.1 IDollCore、§8 build、§12 交付判準、§10 Service scope）
- Design spec: `docs/superpowers/specs/2026-04-20-doll-ai-terminal-design.md`（§3 八個 app 職責、§6 既有元件改造映射、§7 實作順序 step 6）
- CLAUDE.md: 「DollOSService (in AOSP tree, system UID)」段與 `project_emergency_stop_revised.md` 記憶

---

## §1 既有狀態盤點（read-only — 改動前先確認）

既有程式碼（摘自 `packages/apps/DollOSService/`）：

```
DollOSService/
├── Android.bp                        # dollos-service-aidl + DollOSService android_app
├── AndroidManifest.xml               # sharedUserId=android.uid.system, persistent=true
├── privapp-permissions-dollos-service.xml
├── aidl/org/dollos/service/
│   └── IDollOSService.aidl           # 11 個 methods
├── src/org/dollos/service/
│   ├── DollOSApp.kt                  # Application + ActionRegistry 初始化
│   ├── DollOSService.kt              # Service binder
│   ├── DollOSServiceImpl.kt          # AIDL 實作
│   ├── action/
│   │   ├── Action.kt                 # Action interface
│   │   ├── ActionRegistry.kt
│   │   ├── OpenAppAction.kt
│   │   ├── SetAlarmAction.kt
│   │   ├── ToggleWifiAction.kt
│   │   └── ToggleBluetoothAction.kt
│   └── taskmanager/
│       ├── AITask.kt
│       └── TaskManagerActivity.kt    # 鎖屏可顯示的 modal
└── res/
    ├── layout/activity_task_manager.xml
    ├── layout/item_ai_task.xml
    └── drawable/button_primary_background.xml
```

既有 `IDollOSService.aidl` methods：`getVersion`、`isAiConfigured`、`getDataDirectory`、`setApiKey`、`setGmsOptIn`、`isGmsOptedIn`、`setPersonality`、`getPersonalityName`、`executeSystemAction`、`getAvailableActions`、`showTaskManager`。

既有雙擊電源鍵觸發 TaskManagerActivity 的 AOSP overlay 設定（Plan C 建立；需 verify 仍然生效）。

---

## §2 本 plan 新增 / 修改內容總覽

| 區塊 | 既有 | 修改 | 新增 |
|---|---|---|---|
| AIDL 介面 | `IDollOSService`（11 methods） | 文件化、加 version 註解 | `bindDollCore()` 內部 helper；`rebootDevice()`、`powerOffDevice()`、`factoryReset()` actions |
| Actions registry | 4 個動作 | — | `RebootAction`、`PowerOffAction`、`FactoryResetAction`、`EmergencyDialAction`（4 個新 action）|
| TaskManagerActivity | modal + Resume/Cancel 基本 UI | Stop AI 按鈕呼叫 `IDollCore.emergencyStop()` | — |
| 電源菜單整合 | AI Stop（既有，雙擊電源）| verify 仍在、改為長按電源才出現（對照 Android 12+ power menu pattern）| Reboot / Power Off 選項（PowerMenuActivity 或 SystemUI overlay）|
| 緊急撥號 | AOSP 內建路徑 | 確認鎖屏與 OOBE 階段仍可達 | SetupWizard / Launcher 上暴露一個固定入口 |
| Factory reset | 無 | — | Secret code Activity（撥號 `*#*#73738#*#*`）觸發確認對話框 → `MASTER_CLEAR` broadcast |
| Build | AOSP `m DollOSService` | — | Android.bp 新增資源、確認 `dollos-service-aidl` 可被 Skills/Core 使用 |

---

## §3 依賴 / 風險

### 依賴
- **必要前置**：Core plan §1-3 AIDL 骨架必須至少**介面凍結**（`IDollCore.emergencyStop(String reason)` 簽名不再變）。介面定檔後 Service 可以用 AIDL stub 先跑通 `bindDollCore()` 流程（實際 emergencyStop 行為由 Core plan 負責）。
- **AIDL 共享**：`dollos-service-aidl` java_library（Android.bp 既有）會被 DollOSSkills 引用；另 DollOSService 要反向引用 `dollos-core-aidl` 才能 bind Core → 需在 Android.bp static_libs 加 `dollos-core-aidl`。
- **Spec §10 留給 plan 解決**：
  - Factory reset 隱藏入口的觸發路徑（本 plan 採 secret code `*#*#73738#*#*` = DOLLS-ish 編碼）
  - 電源菜單新增按鈕的技術路徑（本 plan 採 AOSP globalactions config overlay + PowerMenuActivity trampoline，不碰 SystemUI framework）

### 風險
| 風險 | 緩解 |
|---|---|
| AOSP 電源菜單 pattern（globalactions）AOSP 16 有變 | Task §4.1 先驗 current overlay 仍 valid，否則退回用 Activity + TYPE_SYSTEM_ERROR window（跟 TaskManagerActivity 同作法）|
| `MASTER_CLEAR` 需要 `android.permission.MASTER_CLEAR` signature permission | DollOSService 已 `platform` certificate + `system` UID，擁有此權限；Task §6.2 驗 |
| Emergency Stop AIDL stub 若 Core 未實作會 NPE | Task §5.3 包 try/catch，Core 未 bind 時只停 Service 自己能停的（撤回通知、切 DND） |
| Secret code 攔截需註冊 `TelephonyManager.ACTION_SECRET_CODE` | Task §6.1 驗 receiver 註冊格式 |

---

## §4 Tasks

**TDD 節奏**：每個 feature task = 寫 unit test → 驗 fail → 實作 → 驗 pass → commit。**AOSP 系統 app 無 Gradle `test` task**；unit tests 用 `android_test` module（Android.bp `android_test` rule）或 Robolectric shim。整合測試走 instrumented tests on-device（subagent 跑 adb）。

建議分 7 大段共 32 tasks。

### §4.1 既有功能 review + 文件化（前置，必做）

- [ ] T01 Subagent 跑 `m DollOSService` 確認既有 codebase 仍 build 過（read-only）
- [ ] T02 在 DollOSService repo root 新增 `README.md`，文件化既有 AIDL surface 的 11 個 methods 用途、每個 Action 的 id / 參數 / 權限
- [ ] T03 為 `IDollOSService.aidl` 每個 method 加 Javadoc（沿既有風格），並在檔頭加 `// Version: 2`（既有可視為 v1）註解，符合 master §3.7 版本化策略
- [ ] T04 跑 Instrumented test verify：綁 DollOSService、呼 `getVersion`/`getAvailableActions` 能回 non-null（建立 baseline）

### §4.2 Core AIDL binding 基礎建設

- [ ] T05 寫 failing test：`DollCoreBinder.bindAndCall(...)` 綁到一個假 IDollCore stub、呼 `emergencyStop("test")`、verify 有呼叫進去
- [ ] T06 新增 `src/org/dollos/service/core/DollCoreBinder.kt`：封裝 ServiceConnection、retry-on-disconnect、lazy bind
- [ ] T07 在 `Android.bp` `static_libs` 加 `dollos-core-aidl`（AIDL only dependency）
- [ ] T08 在 `privapp-permissions-dollos-service.xml` 加 `android.permission.BIND_SERVICE`（若需要）並驗 Core service 的 intent filter 與 Binder bind 成功

### §4.3 Emergency Stop → Core 呼叫流程

- [ ] T09 寫 failing test：TaskManagerActivity 上有 `Stop AI` 按鈕；按下會呼 `DollCoreBinder.call { emergencyStop("user_pressed_stop") }`
- [ ] T10 修改 `TaskManagerActivity.kt`：新增 `Stop AI` 紅色按鈕（覆蓋既有 Resume 之上優先），onClick → `emergencyStop` + finish Activity
- [ ] T11 修改 `res/layout/activity_task_manager.xml` 加此按鈕
- [ ] T12 寫 integration test：Core 未 bind 時按 Stop AI 也不 crash（fail-safe）；Core bind 成功時 emergencyStop 被呼叫
- [ ] T13 **保留雙擊電源鍵 shortcut → TaskManagerActivity**（既有 Plan C overlay）；verify 於 AOSP 16 仍生效，若 broken 回頭改 overlay 或長按電源 trampoline

### §4.4 電源菜單整合（AI Stop / Reboot / Power Off）

- [ ] T14 調查 task：在 AOSP 16 內尋找 globalactions / power menu 自訂機制（SystemUI overlay vs config_globalActions array）— 寫 findings 進 README
- [ ] T15 寫 failing test：`PowerMenuActivity` 顯示時有三顆按鈕（AI Stop / Reboot / Power off），點擊各自行為正確
- [ ] T16 新增 `src/org/dollos/service/powermenu/PowerMenuActivity.kt`（改造自 TaskManagerActivity 同 window type）— 長按電源觸發
- [ ] T17 新增 `res/layout/activity_power_menu.xml`
- [ ] T18 新增 `RebootAction.kt`（呼 `PowerManager.reboot(null)`；需 `REBOOT` permission）
- [ ] T19 新增 `PowerOffAction.kt`（呼 `PowerManager.shutdown(false, null, false)`；需 `DEVICE_POWER` signature permission — DollOSService 有）
- [ ] T20 在 `privapp-permissions-dollos-service.xml` 加 `REBOOT`、`DEVICE_POWER`
- [ ] T21 在 `ActionRegistry` 註冊 Reboot / PowerOff，透過 `getAvailableActions` 暴露給 Skills
- [ ] T22 註冊 PowerMenuActivity 到 AndroidManifest（`showOnLockScreen`, `excludeFromRecents`）

### §4.5 緊急撥號入口

- [ ] T23 驗證既有 AOSP 鎖屏已暴露緊急撥號（`com.android.phone/.EmergencyDialer`）— subagent 用 adb 在鎖屏跑一次
- [ ] T24 若 DollOSLauncher 不經鎖屏直接進入（Launcher plan §13-a），需新增 `EmergencyDialAction`：intent action `Intent.ACTION_DIAL` with flag `FLAG_ACTIVITY_NEW_TASK` 從 DollOSService 啟動 EmergencyDialer；在 `ActionRegistry` 註冊
- [ ] T25 寫 instrumented test：從鎖屏 + 解鎖狀態兩個情境都能觸發 EmergencyDialer（subagent 跑）
- [ ] T26 文件化：README 記錄 Launcher / OOBE 應該透過什麼 UX 點 expose 緊急撥號入口（本 plan scope 僅提供 action；Launcher plan 負責 UX 呼叫）

### §4.6 Factory Reset 入口（隱藏但可達）

- [ ] T27 寫 failing test：撥打 secret code `*#*#73738#*#*`（"RESET" on T9 dialpad = 73738）會觸發 `FactoryResetConfirmActivity`，需雙重確認（輸入文字 "RESET" + 按鈕）
- [ ] T28 新增 `SecretCodeReceiver.kt` + AndroidManifest 註冊 `<receiver>` with `<action android:name="android.provider.Telephony.SECRET_CODE" />` + `<data android:scheme="android_secret_code" android:host="73738" />`
- [ ] T29 新增 `FactoryResetConfirmActivity.kt` + layout：要求使用者輸入「RESET」字樣並按下紅色確認鍵，才 fire `Intent("android.intent.action.MASTER_CLEAR")`（需 `MASTER_CLEAR` permission）
- [ ] T30 在 `privapp-permissions-dollos-service.xml` 加 `android.permission.MASTER_CLEAR`
- [ ] T31 文件化 README 記錄：主人忘記 DollOS 操作時，只要能解鎖拿到撥號器（透過緊急撥號路徑的 dialer？不行 — 緊急撥號不支援非緊急號）→ **備案**：PowerMenuActivity 長按 AI Stop 10 秒也可觸發 FactoryResetConfirm
- [ ] T32 實作備案：PowerMenuActivity 上 AI Stop 按鈕 long-press 10s 觸發 FactoryResetConfirmActivity（T16 改造）

### §4.7 Skills 動作執行 AIDL 介面

- [ ] T33 文件化 `IDollOSService.executeSystemAction()` 的 contract（actionId + JSON params → JSON result）並列出目前所有 actions 的 JSON schema（README）
- [ ] T34 寫 integration test：建立一個 dummy client app 綁 DollOSService、呼 `executeSystemAction("toggle_wifi", "{\"enabled\":true}")`、verify 結果
- [ ] T35 為 DollOSSkills 預留：在 `Android.bp` 確認 `dollos-service-aidl` 是 `java_library` 可被其他 app module `static_libs` 引用（既有應已滿足，verify）

### §4.8 整合測試 + 驗收

- [ ] T36 E2E instrumented test（subagent 跑）：
  1. 雙擊電源 → TaskManagerActivity 出現（master §12 emergency stop 可達）
  2. 按 Stop AI → Core stub 收到 `emergencyStop` 呼叫
  3. 長按電源 → PowerMenuActivity 出現，有三顆按鈕
  4. 撥 `*#*#73738#*#*` → FactoryResetConfirm 出現
  5. 從鎖屏按緊急撥號 → EmergencyDialer 開啟
- [ ] T37 Build + deploy verify（subagent）：
  ```bash
  cd ~/Projects/DollOS-build
  source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
  m DollOSService -j$(nproc)
  adb root && adb remount
  adb push out/target/product/bluejay/system_ext/priv-app/DollOSService/DollOSService.apk \
           /system_ext/priv-app/DollOSService/
  adb reboot
  ```
  reboot 後跑 T36 的 E2E。
- [ ] T38 Master §12 交付判準逐條打勾、記錄進 master plan 的 §12 checkbox

---

## §5 Build / Deploy 慣例

**這不是 Gradle app**（與 AIService / Launcher 不同）。直接在 AOSP tree build：

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m DollOSService -j$(nproc)
```

**Deploy to device**（subagent 執行）：

```bash
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push out/target/product/bluejay/system_ext/priv-app/DollOSService/DollOSService.apk \
         /system_ext/priv-app/DollOSService/
adb reboot
```

**測試**：
- Unit tests：放 `tests/src/org/dollos/service/`，Android.bp 定 `android_test` module，`atest DollOSServiceTests`
- Instrumented tests：同上 target，subagent 執行 `atest` + 收 log
- 手動 E2E：subagent 跑 adb 雙擊電源 / 撥 secret code / 長按電源

---

## §6 自我審查 checklist（在進 §4 前 / 完成後都跑一次）

- [ ] Master §12 交付判準每條都有至少一個 task 對到：
  - AI Stop 按鈕 → T09-T13, T36
  - Reboot / 關機 → T14-T22, T36
  - 緊急撥號 → T23-T26, T36
  - Factory reset 隱藏入口 → T27-T32, T36
- [ ] Emergency Stop AIDL 呼叫流程清楚：TaskManagerActivity → DollCoreBinder → IDollCore.emergencyStop（T05-T12）
- [ ] Factory reset 入口具體路徑寫清楚：`*#*#73738#*#*` secret code + PowerMenu long-press AI Stop 10s 兩條（T27-T32）
- [ ] AOSP build（`m DollOSService`）路徑 vs Gradle 區分寫明（§5）
- [ ] `dollos-core-aidl` 依賴在 Core plan AIDL 凍結前不能寫死（§3 依賴節已標明）
- [ ] 新增 permissions（REBOOT / DEVICE_POWER / MASTER_CLEAR）都在 privapp-permissions XML 裡（T20, T30）
- [ ] TaskManagerActivity 既有能力保留，不破壞既有雙擊電源流程（T13）
- [ ] Skills app 可以 bind 並呼叫 executeSystemAction（T33-T35）

---

## §7 Non-goals

- 不重構既有 ActionRegistry 架構（維持原狀加 action）
- 不實作 Doll Core 本體 — 本 plan 只呼叫其 AIDL
- 不動 DollOSLauncher 的 UX（緊急撥號 UX 屬 Launcher plan）
- 不動 DollOSSetupWizard（OOBE 屬 Launcher plan / SetupWizard 精簡在該 plan 處理）
- 不實作 OTA / 系統更新相關 reboot recovery 入口（v1.0 scope 外）
- 不做多使用者或 guest mode（spec §9 Non-goal）

---

## §8 後續步驟

1. 使用者審閱本 plan
2. 等 Core plan §1-3 AIDL 骨架定稿後開工（§3 依賴節）
3. 使用 `superpowers:subagent-driven-development` 派 subagent 逐 task 推進
4. 完成後勾 master plan §12 交付判準
