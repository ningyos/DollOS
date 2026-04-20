# Doll Repositioning — Bridge / Drone 架構設計

**日期：** 2026-04-20
**狀態：** 草案（待使用者最終審閱）
**取代：**
- `2026-04-02-dollos-protocol-memory-distillation-design.md`（整份作廢，見 §7）
- `2026-04-02-dollos-protocol-memory-distillation.md`（plan，整份作廢）

---

## 背景

DollOS 原本的心智模型是「手機是身體，電腦（DollOS-Server / GuraOS）是大腦」。實作累積 14 個 plans、9 個 specs 後，使用者發現：

1. 功能攤得太廣，每條戰線 60%，沒有一條打穿到每天會用的程度
2. 手機與電腦兩端都在爭當「大腦」，產品定位含糊
3. 近兩個月的體感結論：**有網路時，一支手機其實就夠了**

本文重定位 DollOS 產品本身，並設計隨之而來的新架構。

---

## §1 產品定位

**Doll 是住在使用者手機上的 AI 同伴。** 一支手機就是她完整的本體 — 記憶、個性、決策、對外溝通都在手機上發生。

她可以透過 **Bridge** 延伸到使用者的電腦上執行任務，但她不住在電腦裡，電腦也不是她的大腦。電腦是她的**可選身體延伸**。

### 產品光譜

```
入門                                              進階
──────────────────────────────────────────────────►
隨身 Doll     +一台 Drone        +Mesh + 多 Drone
（手機單飛）  （家裡桌機當幫手） （homelab / 智慧家居艦隊管家）
```

升級路徑自然：使用者加更多 Drone，Doll 就長出更多能力。不是換產品線。

### 目標使用者

- **一般使用者**：希望有個貼身 AI 同伴，需要時能借用電腦幫忙
- **Homelab / 智慧家居玩家**：有多台自家機器要管理，願意讓 Doll 當編排中心

---

## §2 名詞表

| 名詞 | 意思 |
|------|------|
| **Doll** | 住在手機上的 AI 本體（「她」）|
| **Bridge** | 在電腦上執行的 daemon 程式，Doll 伸到該機器的延伸 |
| **Transient Bridge** | 薄 Bridge — 透過 USB-C 暫時接管一台不受信任的機器，拔線即結束 |
| **Drone Bridge** | 厚 Bridge — 長駐在受信任機器上的持久服務 |
| **Drone** | 被 Drone Bridge 寄生的機器本體（例：「我家桌機是一台 Drone」）|
| **Doll Mesh** | 可選的 mesh VPN，由 Doll 管理或接管，用來編排使用者的裝置艦隊 |
| **Identity Vault** | 手機上的金鑰/憑證保管庫，統一管理所有身分 |
| **Policy Engine** | 手機上的授權決策模組，判斷「這個任務該用哪條路到哪台機器」|

---

## §3 元件架構

### 高階元件圖

```
┌──────────────────────────────────────────────────────────┐
│  📱 Doll（手機 / 本體）                                    │
│                                                           │
│   Identity Vault     — 所有金鑰（Bridge / SSH / mesh）    │
│   Policy Engine      — 任務 → 連線模式的授權決策           │
│   Reachability Mgr   — 每台機器走哪條路                   │
│   Action Dispatcher  — 簽章後送指令                        │
│   Drone Registry     — 已配對機器清單、狀態、能力           │
│   Memory (SoT)       — 人格、記憶、歷史                    │
│                                                           │
└──────────────────────────────────────────────────────────┘
     │
     ├── [USB-C] ───────► Transient Bridge（任何 PC）
     │
     ├── [Network + 加密] ► Drone Bridge（信任機器）
     │                        └─ 選配 modules：
     │                           • bridge-subagent
     │                           • bridge-vision
     │                           • bridge-llm
     │                           • bridge-memory
     │
     ├── [SSH] ──────────► 遠端 server / VPS（無 Bridge）
     │
     └── [Doll Mesh，可選] 覆蓋上述網路連線，提供艦隊管理
```

### Bridge 內部結構

```
libbridge-core/          ← 共享函式庫
  • 身體能力：screen, input, fs, shell, clipboard, notification
  • 訊息格式 + 加密原語 + Ed25519 簽章
  • 能力清單 / capability negotiation

  ├── bridge-transient   ← 單檔 portable binary
  │     • USB-C transport
  │     • 無狀態、拔線即死
  │     • 只有核心身體能力，不含任何 module
  │
  └── bridge-drone       ← 持久服務
        • 加密網路 transport
        • 可選 modules（動態載入）：
          - subagent：任務派發 + agent loop（GuraCore 搬家過來）
          - vision：本地 vision 模型推論
          - llm：本地 LLM 推論
          - tts：本地 TTS（fish-tts 搬家過來，語音輸出用）
          - asr：本地 ASR（FunASR 搬家過來，語音輸入用）
          - memory：subagent 執行任務時的本地工作記憶（任務 scratchpad + Drone 本地產生的檔案/紀錄索引；**不是** SoT 副本）
```

### 手機端模組

元件職責分開、避免一個模組什麼都做：

- **Drone Registry**：已配對機器清單的**資料庫**。每筆記錄：身分憑證、宣告的能力（modules + 硬體）、狀態、最後連線時間。使用者可見 UI。
- **Task Router**：給定一個任務（需要什麼能力 / 使用者明示指定），**挑哪台機器做**（或決定「回手機本地處理」）。讀 Drone Registry。
- **Reachability Manager**：給定一台目標 Drone，**決定走哪條路**（direct / managed-mesh / adopted-mesh）。處理網路切換（WiFi ↔ 4G）時的 transport 遷移。
- **Policy Engine**：給定 `(任務, 目標)`，**決定是否需要使用者確認**（一次、session 內、指紋確認）。不做選路決定。
- **Action Dispatcher**：給定 `(任務, transport, 已授權)`，從 Identity Vault 取金鑰、簽章、送出。
- **Identity Vault**：所有金鑰集中保管（Bridge 長期金鑰、SSH 私鑰、mesh 身分憑證、cloud API token）。受手機 secure element / StrongBox 保護。

**一個任務的完整流程：**
```
Task Router    ─► 選出目標 Drone（或決定本地處理）
Reachability   ─► 選出該 Drone 現在走哪條 transport
Policy Engine  ─► 判斷是否需要使用者授權 → （可能跳提示 / 指紋）
Action Dispatch─► 從 Vault 取金鑰、簽章、送出
```

### 手機端實作歸屬

新元件作為 `DollOSAIService` 的 sub-packages 出現（不開新 Android app）：

- `bridge/` — Bridge client、USB-C pairing 實作、Reachability Manager、Action Dispatcher
- `vault/` — Identity Vault（包裝 Android Keystore / StrongBox）
- `routing/` — Task Router + Policy Engine
- `mesh/` — mesh provider 抽象 + 三種 provider 實作
- Drone Registry UI 的 Activity/Fragment 加入 `Settings` app（與既有 AI 設定頁並排）

---

## §4 連線模式

### 連線矩陣

| 模式 | 信任度 | Transport | 認證 | 生命週期 | 能力範圍 |
|------|-------|-----------|------|---------|---------|
| Transient Bridge | 不信任 | USB-C | 物理接觸 + 一次性 session key | 拔線即死 | 螢幕 / 輸入 / 剪貼簿 / 檔案 / shell / notification |
| Drone Bridge | 信任 | TCP/QUIC over 網路 | 配對金鑰 + 指令簽章 | 長駐 | 全部 + subagent + 選配 GPU 模組 |
| SSH 遠端 | 信任（SSH 自己的信任模型）| SSH | OpenSSH key（Vault 管理）| 按需 | shell + SCP/SFTP + tmux |

### 目標選擇（Task Router 的規則）

Task Router 根據任務屬性挑目標機器：

1. **明確指定**：使用者說「在工作筆電上跑這個」→ 直接用該 Drone
2. **能力匹配**：任務需要 GPU vision → 找有 `bridge-vision` 模組的 Drone
3. **情境匹配**：任務是「看一下我現在的螢幕」→ 當下 USB-C 插著的 Transient Bridge（若存在），否則使用者就座的 Drone（由使用者在 Drone Registry 裡標註「就座中」，或手動指定）
4. **命令列任務**：純 shell 性質 + 目標是沒有 Bridge 的 server → 用 SSH
5. **沒有合適目標**：回手機本地處理（可能走雲 LLM）

使用者可以強制覆蓋任何自動選擇。

### 路徑選擇（Reachability Manager 的規則）

目標 Drone 選定後，Reachability Manager 決定走哪條路：

- Drone 設定的 provider 是 `direct` → 走 LAN mDNS / port forward / relay
- Drone 設定的 provider 是 `managed-mesh` → 走 Doll 自架的 mesh
- Drone 設定的 provider 是 `adopted-mesh` → 走使用者現有 mesh
- Transient Bridge 固定走 USB-C

網路切換時（例如手機從 WiFi 切到 4G），Reachability Manager 負責 transport 重建。

---

## §5 安全模型

### Transient Bridge（USB-C）

- **信任根基**：物理接觸。插著 USB-C 就代表使用者同意。
- **Session 建立**：USB channel ECDH 交換 session key
- **指令**：無需簽章（channel 本身就是物理認證）
- **狀態**：零持久狀態，拔線後 Bridge 程式自刪 session state
- **使用者看得到**：手機持續顯示「正在 Bridge 到 [機器名]」，一鍵結束

### Drone Bridge（網路）

- **配對儀式（一次性）**：
  1. 使用者 USB-C 插 Drone 候選機
  2. 雙方 ECDH 交換長期金鑰對
  3. Drone 收到手機簽發的 Drone 身分憑證（包含能力清單、mesh 設定）
  4. 拔線，Drone 進入網路長駐模式
- **日常通訊**：
  - Channel 加密：Noise Protocol（pre-shared identity 比 TLS 適合此場景）
  - 每個指令 Ed25519 簽章，Drone 驗章才執行
  - 重放保護：nonce + timestamp
- **金鑰生命週期**：
  - 長期金鑰可從手機 Drone Registry 一鍵撤銷
  - Drone 定期向手機續約憑證（例：30 天）
  - 手機遺失情境：新手機透過備份還原 Vault 後，使用者需重新對每台 Drone 做一次 USB-C 配對（安全優先於便利）

### SSH

- 不重新發明，用 OpenSSH 原生信任模型（`known_hosts` + public key auth）
- SSH 私鑰存在 Identity Vault，受手機生物辨識保護
- 簽章動作在手機 StrongBox 做，私鑰永遠不落地到遠端裝置
- 手機充當 ssh-agent

### 統一 Identity Vault

- 所有金鑰集中在一個 vault：Bridge 金鑰、SSH 私鑰、mesh 身分、未來的 cloud token
- 存取分級：
  - 讀取（常態使用）：手機解鎖即可
  - 匯出 / 撤銷 / 新配對：指紋確認
- 備份：加密後可選匯出，還原需要原始 passphrase + 新裝置指紋

### 任務授權

Policy Engine 根據任務敏感度決定是否要使用者確認：

| 類型 | 範例 | 授權方式 |
|------|------|---------|
| 讀取類 | 看螢幕、讀檔 | 一次授權後 session 內不再問 |
| 修改類 | 編輯檔案、安裝軟體 | 每次確認（但可「這次 session 都允許」）|
| 危險類 | 刪檔、撤銷 Drone、修改 system config | 指紋確認 |

---

## §6 Doll Mesh（可選能力）

### 定位

Mesh VPN 不是 Doll 工作的前提 — 沒 mesh 她照樣服務。Mesh 的真正價值是 **Doll 成為使用者裝置艦隊的編排/管理中心**，對 homelab 和智慧家居玩家特別有吸引力。

### 抽象層

Doll 的 `NetworkLayer` 有三種 provider，每台 Drone 可以獨立設定：

| Provider | 行為 | 適合情境 |
|---------|------|---------|
| `direct` | LAN mDNS、port forward、或簡易 relay | 一兩台 Drone 的輕量使用 |
| `managed-mesh` | Doll 自架 Headscale 或 Netbird，當 coordinator | 想讓 Doll 當家裡管家 |
| `adopted-mesh` | 掛進使用者現有的 Tailscale / Netbird，Doll 當 node | 公司/家裡已有 mesh |

### managed-mesh 模式下 Doll 提供的管理功能

- Mesh node 註冊、撤銷、命名
- ACL 設定（哪台機器能看哪台）
- Subnet routing（讓 Doll 走 Drone 到家裡內網其他機器）
- DNS（為 Drone 分配可讀主機名）
- 可觀測性儀表板（誰上線、流量、異常）

實作上 `managed-mesh` 預期建在 Headscale 或 Netbird 之上（使用者偏好兩者皆可），Doll 當 UI + policy 層。

### adopted-mesh 模式

Doll 只要有 mesh node 身分即可工作。使用者自己管理 coordinator。

---

## §7 程式碼遷移

### 留下來、重新安家

| 原來住哪 | 搬到哪 | 狀態 |
|---------|--------|------|
| GuraCore agent loop | Drone Bridge 的 subagent 引擎 | 程式碼大幅保留，改介面 |
| vLLM / Qwen3-VL | Drone Bridge 的 `bridge-llm` / `bridge-vision` 模組 | 改包裝，不改核心 |
| fish-tts | Drone Bridge 的 `bridge-tts` 模組 | 改包裝 |
| FunASR | Drone Bridge 的 `bridge-asr` 模組 | 改包裝 |
| memsearch（Markdown + sqlite-vec + FTS5）| Drone Bridge 本地工作記憶（**不是** SoT）| SoT 移到手機，memsearch 改成任務期工作記憶 |
| GuraVerse / TinyGura sub-agent spawning 概念 | Drone 的 subagent spawning — 找到正確的家 | 概念保留，實作重做 |

### 死亡名單

| 死掉的東西 | 死因 |
|-----------|------|
| NATS 作為中央訊息匯流排 | 手機 ↔ Bridge 直連，不需要 bus |
| kmod microkernel 抽象 | 過度抽象，Bridge 模組化就夠了 |
| Docker compose 整套 infra | Bridge 是單一可安裝服務，不是一堆 container |
| 「server 當大腦」心智模型 | 被產品重定位殺死 |
| `dollos-server` CLI / bootstrap | 重寫成 `bridge-drone` |
| **4/2 DollOS Protocol v1 spec & plan** | 為舊心智模型設計，作廢 |

### 全新元件

- `libbridge-core` — 共享函式庫
- `bridge-transient` — USB-C 薄 Bridge binary
- `bridge-drone` — 網路厚 Bridge 服務
- Bridge modules（subagent / vision / llm / memory）
- Doll Mesh 的 managed-mesh coordinator 邏輯
- 手機端 Identity Vault + Policy Engine + Reachability Manager + Drone Registry UI
- USB-C pairing handshake 協議（雙端實作）

### 對現有手機端影響

DollOSAIService、DollOSLauncher、AOSP overlay、語音 pipeline、角色包系統**全部保留，方向正確**，不受此重定位影響。

---

## §8 Non-goals & 未決事項

### 非目標

- **不做「所有 PC 都需要 DollOS」** — Doll 只與裝了 Bridge 的機器互動
- **不做「Doll 有 server-side 常駐副本」** — Doll 只活在手機上；memory 可備份但不是活動副本
- **不做「Bridge 之間直接通訊」** — 所有指令經手機，Bridge 間不橫向溝通
- **不支援 Android / iOS 當 Drone** — Bridge 只跑桌面 OS（Win / Mac / Linux）
- **不做 Bridge 之間狀態同步** — 每台 Drone 是獨立的工作點，不共享狀態

### 留到 plan 階段決定

- Bridge 實作語言（Rust / Go，傾向 Rust — 小 binary、無 runtime、cross-platform）
- USB-C 層的實際協議（自訂於 raw USB，或改造 ADB，或走 USB CDC）
- 離線情境手機端 Doll 的行為（與目前 cloud LLM 依賴的互動）
- 第一個 Drone 平台目標（建議 Linux 先，使用者自家桌機做 dogfood）
- Memory 從 server 搬到手機的遷移路徑（既有 Markdown / sqlite-vec 資料）
- Bridge 模組的發行/更新機制（手機推還是獨立 package manager）

### 後續可能的擴充（不在本 spec 範圍）

- iOS 上的 Doll（目前 DollOS-Android 已綁 Pixel 6a）
- Bridge-as-plugin 生態（第三方寫模組）
- 多位使用者共用同一 Drone（目前假設 1 phone : N drones，單向擁有關係）

---

## 後續步驟

1. 使用者審閱本 spec
2. 審閱通過後，以本 spec 為輸入，用 `superpowers:writing-plans` 建立實作計畫
3. 實作計畫預期至少包含這些階段：
   a. `libbridge-core` 最小可用版（身體能力 + 加密）
   b. `bridge-transient` + USB-C pairing 原型
   c. 手機端 Identity Vault + Drone Registry UI 骨架
   d. `bridge-drone` 最小可用版 + 網路 transport
   e. 第一台 Drone（使用者家桌機）dogfood
   f. `bridge-subagent` 模組（把 GuraCore agent loop 搬進來）
   g. Mesh provider 抽象 + 第一個 adopted-mesh 實作
   h. SSH provider 整合
   i. （選配）managed-mesh 實作
4. 舊 DollOS-Server 程式碼盤點與退役（獨立工作）

---

## 附錄 A — 被取代的 4/2 spec 概要

4/2 的 `DollOS Protocol v1` 設計了：
- 手機→伺服器 WebSocket 推送對話記錄
- 伺服器夜間執行 memory distillation
- 伺服器端 Python + NATS + 本地 LLM

這個設計假設「伺服器是記憶大腦，手機是客戶端」— 被本 spec §1 的重定位直接否定。相關 plan 任務全部停止。
Memory distillation 這個**概念**在新架構下仍有意義，但會搬到手機端執行（或透過 Drone 借算力），形式會在後續 plan 重新設計。
