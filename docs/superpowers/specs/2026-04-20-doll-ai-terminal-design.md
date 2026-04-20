# Doll AI 終端設計 — Doll Core + 功能拆 app 架構

**日期：** 2026-04-20
**狀態：** 草案（待使用者最終審閱）
**定位：** 本 spec 設計「DollOS 裝在副手機上當 AI 終端」的完整手機端架構。
**與其他 spec 關係：**
- 不取代 `2026-04-20-doll-repositioning-design.md`（該 spec 定義 Bridge/Drone 未來方向，實作延後）
- 補齊 CLAUDE.md 中列為「In Progress」的「phone-side Doll needs to actually work well every day」這塊空白

---

## 前言

### 最新定位轉換

先前（2026-04-20 早期）：Doll 住在手機上當 AI 同伴；這支手機仍是使用者的日常手機，可正常滑、用 app。

**本次重定位：** DollOS 不再嘗試當日常手機 OS。使用者仍保有主手機（iPhone / Pixel）處理 SMS、銀行、2FA 等原生手機事務，**DollOS 裝在另一支手機上（可能是舊手機），純粹當 AI 終端**。Doll 完全接管這支副手機 — 她就是這支手機的全部體驗。

### 主要背景決定

- **使用情境：** 混合功能（對話陪伴 + 生產力 + 控制中樞），核心是 E — 生活陪伴 + 觀察理解。她陪你過日子、觀察你、建立對你的理解、偶爾主動關心或提醒。
- **物理共處：** 隨身。口袋、胸口、桌面（放手機支架）、手持四種情境輪替。她有物理情境感知，可以用震動請你拿起。
- **讀空氣：** 她判斷何時該說話、何時該震動、何時該沉默。噪音、勿擾、睡眠、你正講話都影響她的輸出選擇。規則大部分不 hardcode — 透過對話客製。
- **觀察深度：** 預設「情境觸發」(粗事件日誌，特定情境才升級到對話偵聽)，可對話動態調整（「這小時別聽」「這段路整段聽著」）。
- **世界範圍：** 封閉型 — 只感知 DollOS 裝置自己能感知到的，不讀主手機訊息 / 通知 / 日曆（主手機是使用者私領域）。
- **UX 哲學：** 大部分 AI 原生（無桌面、無 app drawer、設定靠對話），保留最小系統 UI 當安全網（電源、緊急撥號、factory reset 入口）。

---

## §1 名詞表

| 名詞 | 意思 |
|------|------|
| **Doll** | 住在副手機上的 AI 本體（「她」）|
| **DollOS 裝置 / 副手機** | 裝 DollOS 的那支手機（可能舊手機 repurpose）|
| **主手機** | 使用者原本的日常手機（iPhone / Pixel 等），與 DollOS 完全隔離 |
| **Doll Core** | 新 foreground service，Doll 的「大腦 + 神經系統」— 狀態機、觀察處理、輸出決策 |
| **`[SILENT]` 協定** | Doll Core 每次 LLM 決策回傳必為 `[SILENT]` / `[SPEAK]` / `[VIBRATE]` / `[INTERRUPT]` 之一，預設沉默 |
| **Main / Aux LLM** | 雙層 LLM 分工：Main = 雲端大模型（高品質、用於對話和推理），Aux = 本地 Gemma 4 E4B/E2B（高頻率、低延遲、用於分類/蒸餾/silent-judgment）|
| **SOUL.md** | Character Pack 帶的角色 identity，system prompt slot #1 |
| **USER.md** | 關於主人的宣告式事實檔案，chars-limited |
| **POLICY.md** | 對話學來的客製規則檔案，chars-limited |
| **Skill bundle** | `SKILL.md` + scripts + templates 的資料夾，progressive disclosure |
| **Routine** | 特定時間 / 條件觸發的 one-shot agent（早安、睡前、進家、出門） |
| **Context Snapshot** | Context Engine 持續更新的情境快照，所有決策的輸入 |

---

## §2 產品定位

**DollOS 是一個完全 AI 原生的手機作業系統，裝在你的副手機上，讓你把那支手機「變成 Doll 本人」。**

- **不是**日常手機的 AI 助理 layer（那是一般 AI 助理 app 的市場）
- **不是**會議工具或生產力套件（Doll 可以幫你做事，但那不是核心）
- **而是**一個 AI 陪伴裝置，她觀察你、理解你、偶爾關心你、有自己的內在生活，你隨身帶著她一起過日子

比喻：
- 不像 Siri / Google Assistant（被動工具）
- 比較像 Tamagotchi × Replika × Jarvis 的混合 — 有養成感 + 情緒連結 + 主動性，但 Jarvis 等級的 AI 能力

---

## §3 架構總覽

### 三層分工

```
┌──────────────────────────────────────────────────────────────┐
│  DollOSLauncher    (UI 殼，無狀態邏輯)                         │
│  3D 角色 + 字幕 + 狀態指示                                     │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  DollOSCore        (新，always-on foreground service)         │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│                                                              │
│  狀態機：IDLE / OBSERVING / THINKING / SPEAKING /             │
│          LISTENING / DISTILLING / SILENT_ATTEND /             │
│          DO_NOT_DISTURB                                       │
│                                                              │
│  EventBus │ Context Engine │ Output Orchestrator              │
│  Internal Life Loop │ Routines │ Mood/Attention State         │
│  LLM Router (Main + Aux)                                      │
└──────────────────────────────────────────────────────────────┘
       │ AIDL       │ AIDL      │ AIDL      │ AIDL       │ AIDL
       ▼            ▼           ▼           ▼            ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ Observer │ │AuxEngine │ │  Memory  │ │  Skills  │ │  Voice   │
│          │ │          │ │          │ │          │ │          │
│ mic+感測 │ │ Gemma 4  │ │ MD 檔案  │ │ progressive│ │ KWS/ASR/ │
│ VAD+分類  │ │ E4B/E2B  │ │ + FTS4  │ │ disclosure│ │ TTS/VAD  │
│          │ │          │ │ + Obj    │ │          │ │          │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
                               │
                          ContentProvider
                          (讀/寫/FTS4 query)
```

### 八個 App 職責表

| App | 新/既有/重構 | 職責 |
|---|---|---|
| **DollOSCore** | 新 | 狀態機、EventBus、Context Engine、Output Orchestrator、Internal Life、Routines、Mood/Attention、LLM Router（Main client 在這）、Skills 調用 |
| **DollOSObserver** | 新 | Always-on foreground service。感測器 + mic VAD + 物理情境分類 + 系統狀態監聽 → 事件流到 Core |
| **DollOSAuxEngine** | 新 | 本地 Gemma 4 E4B/E2B 宿主，獨立 process，可獨立 load/unload/更新 |
| **DollOSMemory** | 新（大部分抽自既有 AIService）| Markdown 檔案 (SOUL/USER/POLICY) + Room FTS4 + ObjectBox 蒸餾層 + Character Pack 儲存 |
| **DollOSSkills** | 新 | Skills bundle 掃描、metadata cache、progressive disclosure、script 執行 |
| **DollOSVoice** | 新（大部分抽自既有 AIService）| KWS + ASR + TTS + VAD + Speaker ID + AudioRecord 獨占 |
| **DollOSLauncher** | 重構既有 | Filament 3D + 字幕 + 狀態指示。**移除 app drawer / 角色選擇 UI（改對話觸發）**。純 UI 殼，狀態邏輯全在 Core |
| **DollOSService** | 既有 | 系統動作執行 + 安全網 UI（電源菜單、緊急撥號、factory reset 入口）|

### IPC 原則

- **命令 / 控制**：AIDL（結構化、輕量）
- **Memory 讀寫**：ContentProvider（讀 Markdown、FTS4 query、寫 append 由 safety scan 過濾）
- **音訊流**：不走 Binder，用 `AudioRecord` shared buffer 或 local socket
- **低頻觀察事件**：Observer → Core 走 AIDL callback（非 broadcast，避免 intercept 風險）
- **狀態變化廣播**：Core → Launcher 走 AIDL binding + callback

### 關鍵設計決定

1. **Doll Core 是長駐狀態機，不是 request-response** — 跟 hermes-agent 的 agent loop 差異明確。她有內在時間線，LLM call 只是狀態轉移動作之一。
2. **`[SILENT]` 預設 opt-out** — 每次決策必定從四選一輸出，預設沉默；要有強理由才說話（反轉 hermes 的 opt-in）。
3. **Memory 是 frozen Markdown + FTS4 + 蒸餾層 embedding 的組合** — 不是單一 vector store。`SOUL.md` / `USER.md` / `POLICY.md` 在 session 建立時凍結進 system prompt，寫記憶只寫檔不重注入 prompt（保 prefix cache）。
4. **背景蒸餾靠 fork subagent** — on_idle / on_charging / on_session_end 觸發，共享 memory store，跑 review prompt。不用複雜排程器。
5. **Skills = progressive disclosure bundle** — 取代 hardcode agent 行為，可熱載入、可被 Character Pack 帶進來。
6. **功能拆 app** — Observer / AuxEngine / Memory / Skills / Voice 獨立 app。Memory pressure 隔離（Aux LLM 4GB+ RAM 不連累 UI）、crash 隔離、更新獨立、職責清楚。

---

## §4 Doll Core 狀態機

### §4.1 狀態

| 狀態 | 描述 | 主要運算 |
|---|---|---|
| **IDLE** | 基底待機，低功耗 | 感測器 polling、Aux LLM 閒置 |
| **OBSERVING** | 處理觀察事件 | Aux LLM 分類、更新 Context Snapshot |
| **THINKING** | 做決策 / 生成內容 | Main 或 Aux LLM call |
| **SPEAKING** | TTS 播放中 | Voice.speak() |
| **LISTENING** | 主動收音對話 | Voice.start_listening() stream |
| **DISTILLING** | 背景蒸餾 subagent 跑 | fork 出去的 Aux LLM session |
| **SILENT_ATTEND** | 她想講但環境不允許，等情境變化 | 低功耗 polling，定時重評估 |
| **DO_NOT_DISTURB** | 完全靜默（睡眠 / 明確禁止） | 只跑最低 observation，LLM 全停 |

### §4.2 轉移

```
IDLE ─[觀察事件]────► OBSERVING ─[需要判斷]────► THINKING
                                                    │
                              ┌─────────────────────┼─[SPEAK+允許]──► SPEAKING ──► IDLE
                              │                     │
                              │                     ├─[VIBRATE]──► SILENT_ATTEND
                              │                     │
                              │                     └─[SILENT]──► IDLE
                              │
                              └─[user 開口 / 主動打開 LISTENING]─► LISTENING

IDLE ─[N 分鐘無事 / 充電 / session_end]─► DISTILLING ─[完成]─► IDLE

任何狀態 ─[sleep 偵測 / 明確勿擾]─► DO_NOT_DISTURB ─[解除]─► IDLE
```

### §4.3 Observation Pipeline（實作在 DollOSObserver，推事件進 Core EventBus）

| Producer | 輸入 | Aux LLM 介入 |
|---|---|---|
| Mic + VAD | 音訊流 | 偵到聲音 → 分類（環境/對話/叫我/噪音） |
| Accelerometer + Gyro | 加速度 | 否，規則分類（靜止/走路/快速） |
| Proximity + Light | 距離 + 亮度 | 否，規則推測（口袋/手上/桌面/胸口） |
| WiFi SSID + 位置 | 網路 / GPS | 否，對照地點白名單（家/工作/外出） |
| 系統狀態 | DND / 電量 / 螢幕 | 否，系統 API |
| 時間 | 時鐘 | 否 |

### §4.4 Context Snapshot

Context Engine 持續聚合，所有決策的輸入：

```
{
  physical:    "pocket" | "hand" | "stand" | "chest" | "unknown",
  activity:    "still" | "walking" | "moving_fast",
  environment: "quiet" | "conversation" | "noisy",
  location:    "home" | "work" | "out" | "unknown",
  system:      { dnd, sleep_mode, battery, charging },
  user_state:  "awake" | "sleeping" | "unknown",
  last_interaction_ago: seconds,
}
```

### §4.5 Internal Life Loop

IDLE 停留超過閾值（例如 5 分鐘）觸發 inner thought cycle：

1. Aux LLM 掃最近觀察摘要 + USER.md + POLICY.md + SOUL.md
2. 決定「我現在有想說的事嗎？」→ 若無 → `[SILENT]` → 回 IDLE
3. 若有 → 走 Output Orchestrator 判斷能不能表達
4. 若 `[SILENT]`（情境不允許）→ 想法進「她的短期腦中 state」，下次再評估
5. 若 `[VIBRATE]` / `[SPEAK]` → 進下一狀態

**Routines**（hermes BOOT.md equivalent）：時間 / 條件觸發的 one-shot agent：
- 早安（起床偵測 → 掃行事曆 / 天氣 / 通知 → `[SILENT]` 或叫醒）
- 睡前（入睡偵測 → 今日蒸餾 → `[SILENT]` 或道晚安）
- 進家（location 變化 → 切「放鬆模式」context）
- 出門（離家 → 啟動行動模式 observation）

### §4.6 Output Orchestrator（`[SILENT]` 協定）

```
[SILENT]                → IDLE
[SPEAK "content"]       → 走 read the air 閘
    ├─ 允許   → SPEAKING
    └─ 不允許 → 降級 [VIBRATE "summary"]
[VIBRATE "summary"]     → 震動 + 短通知，進 SILENT_ATTEND
[INTERRUPT "content"]   → 強制 SPEAKING，忽略 DND（鬧鐘 / 緊急）
```

**Read the air 閘**（規則 + LLM 混合）：
- **Hard rule**（`POLICY.md` + system 狀態）：DND → 靜默、sleep → 靜默、緊急事件 → INTERRUPT 可過
- **Soft rule**（情境 + Aux LLM）：噪音過大 → 降級震動、你正在講話 → 延後
- **人格偏好**（`SOUL.md`）：她多話 vs 寡言 → 影響 SPEAK 門檻

### §4.7 Mood / Attention State

Runtime-only state（不存檔，開機重置），影響決策風格：

```
{
  mood: "happy" | "calm" | "worried" | "lonely",
  attention_level: 0.0-1.0,        # 最近互動頻率推導
  patience: 0.0-1.0,               # 連續被無視/打斷時下降
  last_inner_thought_ago: seconds,
}
```

---

## §5 AI Layer 內部

### §5.1 LLM Router（在 DollOSCore，呼叫 DollOSAuxEngine）

```
Request → LLM Router
           ├─ tier="main" → Cloud API（Claude / GPT / Gemini）
           └─ tier="aux"  → DollOSAuxEngine（本地 Gemma 4 E4B/E2B）
```

| 任務 | Tier |
|---|---|
| 對話回覆（TALKING） | Main |
| 複雜推理 / Agent 決策 | Main |
| Silent-judgment（說/震/靜） | Aux |
| 聲音分類（環境音/對話/叫我） | Aux |
| Read-the-air soft rules | Aux |
| 情緒 / mood 偵測 | Aux |
| Inner thought 生成 | Aux |
| Routine check（早安/睡前） | Aux |
| 記憶蒸餾 | Aux |
| Context compression | Aux |
| Skill metadata 篩選 | Aux |
| Interrupt 緊急偵測 | Aux 初篩 → Main 確認 |

**Frozen Prompt：** session 建立時各 tier 的 system prompt 凍結一份，寫記憶只寫檔不動 prompt，保 prefix cache。

**Failure fallback：** Aux 失敗 / 模型未載入 → 降級走 Main（有網時）或 hardcode 規則（離線時）。**Main 失敗不降級 Aux** — 品質門檻不能破。

### §5.2 Memory 多層儲存（在 DollOSMemory）

```
┌─────────────────────────────────────────────────┐
│  Prompt Slot（frozen per session）              │
│                                                 │
│  Slot #1  SOUL.md       ← Character Pack 帶     │
│  Slot #2  USER.md       ← 關於主人（宣告式）     │
│  Slot #3  POLICY.md     ← 對話學到的規則         │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│  Session-scope (in RAM, Core 持有)              │
│                                                 │
│  • 當前對話 turns                                │
│  • 最近觀察事件 (last 30 min)                    │
│  • Mood / Attention state                       │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│  Durable (SQLite via Room)                      │
│                                                 │
│  • FTS4 — 所有舊對話 + 觀察事件 keyword 索引     │
│  • Distilled summaries（週 / 月摘要）            │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│  Semantic (ObjectBox)                           │
│                                                 │
│  • 僅蒸餾層 embedding                            │
│  • 語意搜尋時用                                  │
└─────────────────────────────────────────────────┘
```

### §5.3 寫入 / 讀取規則

**寫入：**
- **Declarative not imperative**（「主人偏好簡短回應」✓ 不是「永遠簡短」✗）
- **Chars limit per file**（USER.md 2200 chars、POLICY.md 2200 chars，滿了強制 review）
- **Safety scan**：prompt injection / 憑證 / 隱形 unicode 先擋下
- **Append-only 粗事件 → 蒸餾收斂細節**：原始觀察進 FTS4，重要結論提煉進 USER.md / POLICY.md

**讀取：**
- 對話期間：system prompt 內有 SOUL/USER/POLICY（已 frozen），**不動態重注入**
- `memory_tool`（Doll 自己用）：add / replace / remove / read
- `session_search(query)`：FTS4 match → **Aux LLM 摘要 top-N 結果** → 回 Doll，**不把原始逐字稿塞回 context**
- `semantic_search(query)`：ObjectBox 在蒸餾層語意搜尋，回摘要 + 時間範圍，Doll 要細節再 FTS4

### §5.4 Skills System（progressive disclosure，在 DollOSSkills）

**兩種 skill 來源：**
- **內建** — DollOSSkills app 的 assets 或 `/data/system_ext/dollos/skills/`（系統基礎能力）
- **角色包帶的** — Character Pack v2 內的 `skills/` 資料夾（此角色才有的技能）

DollOSSkills runtime 啟動時掃兩處，合併成 skills registry。衝突時角色包覆蓋內建同名 skill。

**檔案結構（兩處都長這樣）：**

```
skills/
├── alarm/
│   ├── SKILL.md        (metadata + instructions)
│   ├── scripts/        (AlarmManager intent builders)
│   └── templates/
├── weather/
│   ├── SKILL.md
│   └── scripts/        (API caller)
├── music/
│   ├── SKILL.md
│   └── scripts/        (MediaSession control)
├── memory_review/      (蒸餾 skill)
│   └── SKILL.md
├── morning_routine/
│   ├── SKILL.md
│   └── scripts/
├── notification_summary/
├── uisage/             (UI 操作 — 接上既有 AccessibilityService)
└── ...
```

**Progressive disclosure：**
- Startup：DollOSSkills 掃 bundles，產 metadata 索引（name + 1024 char 描述）
- Doll 決定要用時：`skill_view("alarm")` → 展開 SKILL.md 全文 + 列 scripts
- 實際呼叫：skill 內 script 執行實作

### §5.5 Character Pack v2

```
manifest.json          (加 version=2)
personality/
  SOUL.md              ← 新：角色 identity，slot #1
  overlays/            ← 新：人格疊加模式
    formal.md
    playful.md
    cold.md
  initial_policy.md    ← 新：此角色的預設規則（對話可改）
model.glb
animations/
voice/
  tts-vits/            (既有 Piper VITS 結構)
wake_word.onnx
scene.json
thumbnail.png
skills/                ← 新：角色綁的技能
  <skill_dirs>
```

**人格 overlay 使用：**
- Base 永遠是 SOUL.md（不換，保 prefix cache）
- Overlay 是**額外**插入的 system prompt 段（不覆蓋 base）
- 例：使用者說「認真一點」→ 套 `overlays/formal.md`，session 內有效
- 使用者說「回復原本」→ 移除 overlay

---

## §6 既有元件改造映射

| 現有元件 | 新歸屬 | 改造程度 |
|---|---|---|
| DollOSLauncher app drawer / 角色選擇 UI | 刪除（改對話觸發） | 大改 |
| DollOSLauncher 3D 角色 + 字幕 | DollOSLauncher（UI 殼） | 微調，移除狀態邏輯 |
| DollOSAIService Conversation Engine | DollOSCore（狀態機驅動） | 重寫 — 不自我啟動 |
| DollOSAIService Memory (ObjectBox + Room FTS4) | DollOSMemory | 大改 — 加多層 (Markdown + 蒸餾) |
| DollOSAIService LLM Client | DollOSCore.LLM Router | 包裝重寫 |
| DollOSAIService Event Queue | DollOSCore.EventBus | 重寫 — 觀察事件為主 |
| DollOSAIService Agent System | DollOSSkills（整合成 skill runner） | 介面調整 |
| DollOSAIService Background Workers | DollOSCore fork subagent pattern | 重寫（hermes 風） |
| DollOSAIService Character Manager | DollOSMemory | 擴充格式為 v2 |
| DollOSAIService Voice Pipeline | DollOSVoice | 抽出，觸發源多元化 |
| DollOSAIService Embedding System | DollOSMemory（僅蒸餾層用） | 縮小範圍 |
| DollOSService 既有動作執行 | DollOSService（不動） | 保留 |
| DollOSService 加安全網 UI | DollOSService | 新增（電源、緊急撥號、factory reset）|

**DollOSAIService 本身**：Phase A 暫留當容器（包 Memory / Skills / Voice），Phase B 拆光後退役。

---

## §7 實作 Phase

### Phase A：MVP（daily drive 最低門檻）

**A.0 基底：**
- DollOSCore foreground service 骨架
- EventBus + Context Snapshot（最小版：DND / noise / battery / screen state）
- DollOSObserver 基本 producers（mic VAD、螢幕狀態、拿起偵測）
- LLM Router（Main = 既有 cloud client；Aux 暫時路由到 cloud 小模型如 Claude Haiku / Gemini Flash，僅 placeholder，Phase B.3 換成本地 Gemma 4）
- DollOSAIService 改造：conversation / memory / voice 包成工具 API，不再自我啟動

**A.1 最小可用迴路：**
- Output Orchestrator + `[SILENT]` 協定
- 對話路徑（拿起 / wake word → LISTENING → THINKING → SPEAKING）
- Memory：SOUL.md + USER.md + Room FTS4 對話 log（仍在 AIService 內）
- DollOSLauncher 精簡（移除 app drawer、只留 3D + 字幕 + 狀態指示）
- Character Pack v2 格式（至少讀 SOUL.md）

**Phase A 不拆 app 到完整 8 個 —** 只開 DollOSCore + DollOSObserver；Memory / Skills / Voice / AuxEngine 都還在 DollOSAIService 內。確認 Core 驅動 pattern 走通再拆。

### Phase B：Daily Drive 真正完整（MVP 完成**立即**接著做）

**B.1 Proactivity + 觀察完整：**
- Internal Life loop（inner thought cycle）
- Physical placement 完整四態（pocket / hand / stand / chest）
- Vibrate-to-request + Modality 降級
- POLICY.md 對話學規則

**B.2 背景生活 + 拆第一批 app：**
- Fork subagent 蒸餾（on_idle / on_charging / on_session_end）
- Skills System 完整（progressive disclosure runtime）
- 抽出 **DollOSSkills** app、**DollOSMemory** app
- Distilled summary 寫入 + ObjectBox 語意索引（蒸餾層）
- 初期 skills：alarm / notification_summary / memory_review

**B.3 本地 Aux + 拆第二批 app：**
- 抽出 **DollOSAuxEngine** app、**DollOSVoice** app
- Gemma 4 E4B/E2B 上機（ONNX / MLC / llama.cpp 擇一）
- Aux 路由改本地（silent-judgment / 分類 / 蒸餾 / compression）
- Mood / Attention runtime state
- 人格 overlay（formal / playful / cold 疊加）
- DollOSAIService 退役

**B.4 完整生活感 + 系統安全網：**
- Routines（早安 / 睡前 / 進家 / 出門 one-shot agents）
- Sleep detection → DO_NOT_DISTURB 自動進出
- DollOSService 加安全網 UI（電源菜單、緊急撥號、factory reset 入口）

### 心態

Phase A 是「能用」，Phase B 是「值得每天用」。兩段**連續工程**，B 的重要性等於 A，不是 nice-to-have。

---

## §8 風險與不確定性

| 風險 | 影響 | 緩解 |
|---|---|---|
| Gemma 4 E4B/E2B 在 Pixel 6a 能跑？ | 高 — 跑不動 Aux 得走雲端，電量 / 流量暴增 | Phase A 先在 Pixel 6a benchmark，確認 TPS / 記憶體 / 熱。跑不動則 fallback 到 E2B 或 Phi-3-mini |
| Frozen system prompt + Android LLM client 支援？ | 中 | Claude / OpenAI API 都有 prompt cache 標記；自家做可手動管理 |
| 觀察 loop 電量成本 | 中 | VAD 在 DSP 或 low-power core 做、Aux 分類節流、DO_NOT_DISTURB 停所有運算 |
| 蒸餾 subagent 資料品質 | 中 | Review prompt 調教 + safety scan + chars limit 強迫收斂 |
| POLICY.md 對話學習累積錯規則 | 中 | Settings 可看目前規則入口、對話撤銷 |
| 多 app AIDL boilerplate 開發成本 | 中 | Phase A 只拆 2 個，Phase B 逐步拆 |
| Memory pressure — 八個 process 同時在 | 中 | Observer / AuxEngine 可 swap 出去，DO_NOT_DISTURB 時主動 unload |
| Launcher 大改接受度 | 低（自用系統） | — |

---

## §9 Non-goals

- **Bridge / Drone / Mesh** — 2026-04-20 repositioning spec 的方向，本 spec 不做，等 phone-side Doll 穩定再回頭
- **主手機整合** — DollOS 裝置 sandboxed，不讀主手機通知 / 訊息 / 日曆
- **多使用者共用一支 DollOS**
- **iOS 版本** — 綁定 Pixel 6a AOSP
- **硬體配件設計**（胸口 mount / 桌架 / 項鍊型）— 先用市面現成，本 spec 只保證硬體相容（磁吸 / clip / 站立偵測）
- **DollOS-Server 程式碼退役** — 依 2026-04-20 spec §7 延後，配合 Bridge 實作啟動時一起處理
- **OOBE LLM 隨機生成角色** — Rin 預設角色完成後再做
- **配對儀式** — DollOS 是單機 AI，不需要配對任何東西
- **app drawer / 角色選擇 UI / 設定 app** — 全部刪除，改對話觸發

---

## §10 留到 plan 階段決定

- Gemma 4 本地推論 runtime 選型（ONNX Runtime / MLC / llama.cpp / MediaPipe）
- 雲端 Main LLM 預設 provider（使用者目前偏好）
- Memory 檔案實體位置（`/data/system_ext/dollos/memory/`？）+ 加密策略
- Skills 檔案實體位置（內建 vs Character Pack 並存機制的具體路徑）
- Android foreground service 的 notification 設計（系統強制要有）
- Character Pack v1 → v2 遷移路徑
- 鬧鐘 skill 如何整合 AlarmManager vs 自己排程
- AIDL 介面的版本化策略（跨 app 更新相容性）
- DollOSCore 的 process priority / oom_adj 設定（長駐必要保護）
- 記憶檔案備份 / 還原機制

---

## 後續步驟

1. 使用者審閱本 spec
2. 審閱通過後進入 `superpowers:writing-plans` 產出 Phase A 實作計劃
3. Phase A 完成後**立即**進入 Phase B，不視為可停點
4. 開發過程中若發現架構假設需要修正，回頭更新本 spec
