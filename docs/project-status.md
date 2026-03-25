# DollOS 專案狀態

> 這份文件用來讓 AI 助手在 context 重置後快速了解專案全貌和當前進度。
> 最後更新：2026-03-23

## 專案概述

DollOS 是一個基於 **GrapheneOS Android 16** 的 AI 伴侶手機作業系統。目標是在手機上運行一個有人格、有記憶、能操控系統的 AI 助手。

- **GitHub Org:** `https://github.com/ningyos/`
- **目標裝置:** Pixel 6a (bluejay)
- **版本:** 0.1.0
- **Base OS:** GrapheneOS（不是純 AOSP，遷移計畫已取消）

## 目錄結構

```
~/Projects/DollOS/              ← 主 repo（文件、設計、計畫）
~/Projects/DollOSAIService/     ← AI Service Gradle 專案（獨立 repo）
~/Projects/DollOS-build/        ← GrapheneOS source tree（repo sync）
  ├── .repo/local_manifests/dollos.xml   ← DollOS 自訂 repo
  ├── packages/apps/DollOSService/       ← 系統服務
  ├── packages/apps/DollOSSetupWizard/   ← OOBE 設定精靈
  ├── packages/apps/Settings/            ← 有修改（AI Settings 頁面）
  ├── frameworks/base/                   ← 有修改（電源選單 AI 按鈕）
  ├── external/DollOSAIService/          ← AI Service（local_manifests sync）
  ├── device/dollos/bluejay/             ← Pixel 6a device config
  ├── vendor/dollos/                     ← vendor overlay + SELinux
  └── build/make/                        ← build system（移除 SetupWizard2）
```

## 自訂 Repo 一覽

| Repo | 路徑 | 用途 |
|------|------|------|
| `DollOSService` | `packages/apps/DollOSService` | 系統服務（Binder AIDL），action system，task manager |
| `DollOSSetupWizard` | `packages/apps/DollOSSetupWizard` | OOBE 9 頁設定精靈 |
| `DollOSAIService` | `external/DollOSAIService` | AI 服務（Gradle 專案 + AIDL + prebuilt APK） |
| `vendor_dollos` | `vendor/dollos` | framework overlay（電源選單）、SELinux policy |
| `device_dollos_bluejay` | `device/dollos/bluejay` | Pixel 6a device makefile + SELinux |

**非 DollOS repo 但有修改：**
| Repo | 改動 |
|------|------|
| `frameworks/base` | GlobalActionsDialogLite 加入 AI Activity + AI Stop 按鈕 |
| `packages/apps/Settings` | 原生 AI Settings 頁面（DollOSAISettingsFragment + stats card + 長條圖） |
| `build/make` | 從 PRODUCT_PACKAGES 移除 SetupWizard2 |

## DollOSAIService（AI Core Plan A）

獨立 Gradle 專案，prebuilt APK 整合進 AOSP。AIDL 在 repo 根目錄，Gradle 和 AOSP 共用，零複製。

**架構：**
```
ningyos/DollOSAIService/
  aidl/org/dollos/ai/           ← AIDL（唯一 source of truth）
  Android.bp                    ← dollos-ai-aidl + android_app_import
  app/                          ← Gradle app module
  prebuilt/DollOSAIService.apk  ← Gradle build 產出
```

**功能（Plan A — LLM + Personality）：**
- **LLM Client** — 多模型（Claude, OpenAI, Grok, Custom），suspend API，SSE streaming，coroutine delay retry
- **Personality** — 5 欄位（backstory, directive, temperature/dynamism, address, language）
- **Usage Tracking** — Room SQLite，90 天自動清理，daily/monthly 統計
- **Budget** — warning threshold + hard limit，Android notification
- **Tool Calling** — 透過 DollOSService 執行 system actions，確認流程（60s timeout）
- **AI Stop** — BroadcastReceiver 接收 `ACTION_AI_STOP`，呼叫 `pauseAll()`

**功能（Plan B — Memory + Conversation）：**
- **Memory System** — memsearch 架構，三層 Markdown 記憶（CORE/DAILY/DEEP）
  - ObjectBox HNSW vector search（384 dims）+ Room FTS4 BM25 keyword search
  - Hybrid search: RRF（70% vector + 30% BM25，union）
  - SHA-256 content hash dedup，1500 char chunking by heading
  - Serialized write queue + pending_writes.json recovery
  - Cloud embedding（OpenAI text-embedding-3-small）+ local placeholder
  - Memory export/import via ZIP + ParcelFileDescriptor
- **Conversation Engine** — 日期分段對話，Room 持久化
  - 多輪對話（sendMessage 現在保持 context）
  - Context compression at 75% capacity（async，foreground model）
  - Memory context 自動注入 LLM system prompt
  - Idle timer（5 min）觸發背景記憶提取

**AIDL 介面：** `IDollOSAIService` + `IDollOSAICallback`

**Build 流程：**
```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
# 同步到 AOSP tree
rsync -av --exclude='.gradle' --exclude='build' --exclude='local.properties' \
    ~/Projects/DollOSAIService/ ~/Projects/DollOS-build/external/DollOSAIService/
```

## AI Settings（原生整合在 Settings app）

在 `packages/apps/Settings` 裡的 `DollOSAISettingsFragment`（DashboardFragment），完全整合在系統設定中。

**頁面結構：**
1. **Stats Card** — 今日/本月 token 數 + active model + 7 天長條圖（UsageBarChartView）
2. **Personality** — Backstory, Response directive, Temperature slider, Address, Language
3. **API & Model** — Provider（Claude/OpenAI/Grok/Custom）, API Key, Model
4. **Budget** — Warning threshold + period, Hard limit + period

透過 AIDL bind 到 DollOSAIService 讀寫設定。

## OOBE 頁面流程

```
welcome → theme → wifi → gms → model_download → api_key → personality → voice → complete
```

- `api_key` / `personality` 頁面透過 DollOSAIService AIDL 設定（無 fallback）
- `theme`: Light/Dark/Auto，用 `UiModeManager`
- `gms`: sandboxed Google Play 開關
- `finishSetup()` 設定 monochrome 系統色彩 + provisioned

## DollOSService 功能

- **AIDL 介面:** `IDollOSService`
- **Action System:** OpenApp, SetAlarm, ToggleWiFi, ToggleBluetooth
- **Task Manager Activity:** 從電源選單 AI Activity 按鈕開啟

## 電源選單（frameworks/base 修改）

- **AI Activity** (`aiactivity`) — 開啟 TaskManagerActivity
- **AI Stop** (`aistop`) — broadcast + toast

## Build 指令

```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh
lunch dollos_bluejay-bp2a-userdebug
m -j$(nproc)                    # 全系統 build
m Settings -j$(nproc)           # Settings（AI 設定頁）
m DollOSSetupWizard -j$(nproc)  # OOBE
m DollOSService -j$(nproc)      # 系統服務
m DollOSAIService -j$(nproc)    # AI 服務（prebuilt）
m SystemUI -j$(nproc)           # 電源選單
```

**注意：** build 指令不能接 `| tail` 等 pipe。背景執行用 `run_in_background`。

## 刷機流程

```bash
ADB=~/Projects/DollOS-build/out/host/linux-x86/bin/adb
$ADB root && $ADB remount

# 同步整個分區
ANDROID_PRODUCT_OUT=~/Projects/DollOS-build/out/target/product/bluejay $ADB sync system_ext

# 或推送單一模組
$ADB push out/target/product/bluejay/system_ext/priv-app/Settings/Settings.apk \
    /system_ext/priv-app/Settings/Settings.apk

$ADB reboot
```

## 設計文件

| 文件 | 內容 |
|------|------|
| `docs/superpowers/specs/2026-03-17-dollos-base-design.md` | DollOS Base 完整設計 |
| `docs/superpowers/specs/2026-03-19-ai-core-design.md` | AI Core 架構設計 |
| `docs/superpowers/plans/2026-03-17-dollos-base.md` | Base 實作計畫 |
| `docs/superpowers/plans/2026-03-19-ai-core-plan-a-*.md` | AI Service + LLM + Personality |
| `docs/superpowers/plans/2026-03-19-ai-core-plan-b-*.md` | Memory + Conversation |
| `docs/superpowers/plans/2026-03-19-ai-core-plan-c-*.md` | Agent + Emergency Stop |
| `docs/superpowers/plans/2026-03-19-ai-core-plan-d-*.md` | 整合計畫 |

## 開發階段

1. **DollOS Base** — 完成
2. **AI Core Plan A** — 完成（LLM client, personality, usage, settings）
3. **AI Core Plan B** — 完成（Memory + Conversation Engine）
4. **AI Core Plan C** — 下一步（Agent + Emergency Stop）
5. **AI Core Plan D** — 未開始（整合測試）
6. **Avatar System** — 未來
7. **Voice Pipeline** — 未來（STT/TTS/Wake Word）
8. **Agent System** — 未來
9. **System UI** — 未來

## 待辦

- **電源鍵長按 Push-to-Talk** — 需要修改 `PhoneWindowManager`，依賴 Voice Pipeline

## 重要決策記錄

- **GrapheneOS base 不遷移** — 留在 GrapheneOS
- **Sandboxed GMS** — GrapheneOS 自帶，OOBE 提供開關
- **GrapheneOS SetupWizard2 + InfoApp** — 已移除
- **系統色彩** — 預設 monochrome
- **AI Settings** — 原生整合在 Settings app（不是獨立 Activity）
- **DollOSAIService 架構** — 合併 repo（AIDL + Gradle + AOSP 整合在同一個 repo）
- **不實作 fallback** — 新介面直接取代，無舊路徑保留
- **UsageTracker** — 用 Room SQLite（不是 SharedPreferences）
- **LLM Client** — suspend API + coroutine delay retry（不是 Thread.sleep）
- **寫程式用 subagent 並行**
