# DollOS Base -- Design Spec

## Overview

DollOS (人形OS) 是一個基於 Android 的作業系統，專為 Pixel 手機設計。核心概念是 AI 寄宿在手機中，打造 AI 專用且高度整合的 OS。AI 具有人格、記憶、語音互動能力，並能自主操控系統。

本文件為 DollOS Base 子專案的設計規格，涵蓋 OS 基底建構、裝置支援、GMS 策略、系統分區、建構產出與刷機流程。

## Project Scope

DollOS 拆分為六個子專案，本文件僅涵蓋第一個：

1. **DollOS Base** -- GrapheneOS 建構流程、裝置支援（本文件）
2. **AI Core** -- 多模型 LLM 抽象層、人格系統、記憶系統、專屬互動介面
3. **Avatar System** -- 3D/Live2D/自訂角色模型渲染引擎、表情動作系統
4. **Voice Pipeline** -- 本地 STT (Whisper)、本地 TTS (ONNX)、喚醒詞
5. **Agent System** -- 系統操控、權限分級、app 自動化
6. **System UI** -- Launcher、管理介面、角色呈現的系統級整合

開發順序依賴關係：

```
1. DollOS Base
   |
   v
2. AI Core
   |
   +---> 3. Avatar System ----+
   |                          |
   +---> 4. Voice Pipeline ---+
   |                          |
   +---> 5. Agent System -----+
                              |
                              v
                    6. System UI (整合以上所有元件)
```

Avatar System、Voice Pipeline、Agent System 各自依賴 AI Core，三者可平行開發。System UI 依賴以上所有元件。

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| OS 基底 | GrapheneOS Android 16 | 安全強化、官方 Pixel 裝置支援、sandboxed GMS、維護成本低 |
| 目標裝置 | Pixel 6 以上 (Tensor 晶片) | 開發機為 Pixel 6a (bluejay)，Tensor 系列硬體一致 |
| GMS 策略 | 使用 GrapheneOS sandboxed GMS | DollOS 核心功能不依賴 GMS，sandboxed GMS 提供 Play Store 相容性同時保持安全隔離 |
| AI 推理 | 雲端優先 (Grok, Claude)，多模型架構 | 本地算力有限，多模型避免供應商綁定 |
| 語音管線 | 本地優先 (STT: Whisper, TTS: ONNX) | 低延遲、離線可用、隱私友善 |
| AI 互動方式 | 語音為主，文字和環境融入為輔 | 最自然的互動方式 |
| AI 形象 | 可自訂 (3D/Live2D/抽象等) | 使用者個性化 |
| AI 權限 | 完全控制，分級確認（read 免確認、write 需一次確認、不可逆操作需二次確認） | 最大能力，安全有保障 |
| 記憶儲存 | 本地為主，可選雲端備份 | 隱私優先，支援跨裝置 |
| 開發語言 | Kotlin 優先，native 暫緩 | 生產力優先 |
| 目標使用者 | 先自用，目標成熟產品 | 漸進式發展 |
| OTA | 先手動刷機，成熟後建立 OTA 伺服器 | 降低初期複雜度 |

## Section 1: Build Environment and Source Management

### Source Origin

基於 GrapheneOS Android 16 作為 DollOS 的基底，直接使用 GrapheneOS 官方 manifest。

**版本策略：** 追蹤 GrapheneOS 的官方 release tag。GrapheneOS 會追蹤 AOSP 安全修補並加入自身的安全強化。

**已知風險：** Google 可能在未來的 Android 版本中調整 Pixel 6 系列的支援。DollOS 應及早擴展測試到 Pixel 8+ 裝置以降低風險。

### Repository Structure

DollOS 使用 GrapheneOS 官方 `repo` manifest，搭配 `local_manifests` 覆蓋/新增 DollOS 專屬的 repo：

- **不 fork 整個 manifest** — 直接使用 GrapheneOS 官方 manifest
- 需要修改的 repo（如 `frameworks/base`）在 `local_manifests` 中覆蓋為 DollOS 的 fork
- DollOS 專屬的 repo（DollOSService、DollOSSetupWizard、vendor、device）透過 `local_manifests` 引入
- DollOS 的 GitHub 組織：`github.com/ningyos/`

### Build Environment

- **建構主機：** Linux (Ubuntu 24.04 LTS)
- **建構系統：** 沿用 GrapheneOS/AOSP 標準建構腳本和工具鏈
- **目標裝置：** `bluejay` (Pixel 6a)，後續擴展到其他 Pixel 6+ 裝置
  - Pixel 6: `oriole`
  - Pixel 6 Pro: `raven`
  - Pixel 7: `panther`
  - Pixel 7 Pro: `cheetah`
  - Pixel 7a: `lynx`
  - Pixel 8: `shiba`
  - Pixel 8 Pro: `husky`
  - Pixel 8a: `akita`
  - Pixel 9: `tokay`
  - Pixel 9 Pro: `caiman`

### Upstream Tracking Strategy

- 追蹤 GrapheneOS 官方 release tag，定期更新到最新的安全修補版本
- DollOS 的修改盡量模組化（集中在 `local_manifests` 引入的 repo），減少與上游的衝突
- 建立 CI 自動檢測上游更新，通知開發者，並執行自動化 build 測試以驗證合併結果是否可用（合併本身需人工介入）

## Section 2: GMS Strategy

### Base Mechanism

DollOS 使用 GrapheneOS 的 sandboxed Google Play Services。GMS 以普通 app 的方式運行在沙箱中，不具備特殊系統權限，保持安全隔離。

### DollOS GMS Requirements

DollOS 核心功能完全不依賴 GMS：

- Voice Pipeline 預設使用 Whisper (on-device)，不依賴 Google ASR
- DollOS 自身的元件均使用開源方案
- Sandboxed GMS 為使用者提供 Play Store 和第三方 app 相容性，同時不損害系統安全

### Default State

- 首次開機的 OOBE 中提供 sandboxed GMS 安裝選項
- 使用者可選擇安裝或跳過，稍後可在設定中變更

## Section 3: System Partitions and DollOS Mount Points

### Partition Strategy

沿用 AOSP 預設分區配置：

- Pixel 6+ 使用 A/B 分區方案 (seamless update)
- system、vendor、boot 等分區維持原有結構

### DollOS Data Directory

`/data/dollos/` 為 DollOS 專屬的資料根目錄：

| Path | Purpose |
|------|---------|
| `/data/dollos/ai/` | AI Core 的人格設定、記憶資料 |
| `/data/dollos/avatar/` | 角色模型檔案 |
| `/data/dollos/voice/` | STT/TTS 模型檔案 (Whisper ONNX, TTS ONNX) |
| `/data/dollos/config/` | DollOS 系統設定 |

### DollOS System Services

DollOS 的系統級服務預裝在 system 分區，以 **persistent system app** 的形式運行（`android:persistent="true"`，獨立進程，非 `system_server` 的一部分）。單次崩潰不會導致系統重啟（由 ActivityManagerService 自動重啟服務），但 crash loop 仍可能觸發系統級 watchdog。後續可評估改用 init.rc 管理的 native daemon 以獲得更強的崩潰隔離。

各服務透過 AIDL/Binder 提供 IPC 介面給其他元件調用，開機自動啟動。

**SELinux 策略：**
- 為 DollOS 服務定義專屬 SELinux domain（如 `dollos_service`）
- `/data/dollos/` 標記為 `u:object_r:dollos_data_file:s0`
- 僅 `dollos_service` domain 有讀寫 `/data/dollos/` 的權限
- 其他 app 透過 Binder IPC 向 DollOS 服務請求資料，不直接存取檔案系統
- Binder IPC 存取控制：需為 `dollos_service` domain 定義 `binder_call` allow rules，明確哪些 app 類別可呼叫（DollOS 簽章的 app、system app、third-party app 各自不同權限層級）
- 多用戶：初期僅支援單用戶，`/data/dollos/` 為全域共享

### Benefits

- 資料和系統分離，OTA 更新不會覆蓋使用者的 AI 記憶和角色模型
- 模型檔案放在 data 分區，使用者可自行更新或替換
- 服務崩潰隔離，不影響系統穩定性

## Section 4: Build Output and Flash Process

### Build Output

- 標準的 Pixel factory image 格式 (`.zip`)
- 包含 bootloader、radio firmware、system image 等
- 系統映像不包含 AI 模型檔案（避免映像過大及授權問題）
- STT/TTS ONNX 模型在 OOBE 首次開機流程中下載，或由使用者手動放入

### Flash Process (Initial)

- 透過 `fastboot` 指令刷入，提供刷機腳本
- 後期建立 web flash tool（類似 GrapheneOS 的網頁刷機工具，使用 WebUSB）

### OTA (Future)

- 建立 OTA 伺服器，支援 A/B seamless update
- 使用者可在系統設定中檢查更新

### First Boot Experience (OOBE)

DollOS 自訂的設定精靈，取代 Android 預設：

1. 語言/地區選擇
2. Wi-Fi 連線（必要 -- 後續步驟需要網路下載模型和驗證 API key）
3. 下載 STT/TTS 模型（顯示進度，可選擇模型大小；下載失敗可重試或跳過 -- 跳過後語音功能不可用但 OS 正常運作，可稍後在設定中重新下載）
4. DollOS AI 設定 -- 選擇 LLM 供應商、輸入 API key（可跳過 -- 跳過後 AI 功能停用，僅作為一般 Android 使用，可稍後在設定中補填）
5. AI 人格初始化 -- 命名、基本個性設定（僅在步驟 4 完成時顯示）
6. 語音設定 -- 選擇 TTS 語音、測試麥克風
7. 完成，進入桌面

## Out of Scope

以下內容不在 DollOS Base 範圍內，將在後續子專案中設計：

- AI Core 的具體架構和 LLM 抽象層設計
- Avatar System 的 3D 渲染引擎
- Voice Pipeline 的 STT/TTS 模型選型和整合細節
- Agent System 的權限模型和 app 自動化機制
- System UI 的 Launcher 和管理介面設計
