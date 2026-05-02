# DollOS Pivot — 電腦為靈魂、手機為介面（架構大改向）

**日期：** 2026-05-01
**狀態：** 草案（待使用者最終審閱）
**取代：**
- `2026-04-20-doll-repositioning-design.md` — phone-as-home 模型整體推翻
- `2026-04-20-doll-ai-terminal-design.md` — phone-side Doll 終端整體形態變更
- `2026-04-26-ptt-voice-input-design.md` 與對應 plan — 長按電源鍵特定 binding 由系統 assistant role 取代
- AOSP custom ROM 路線整體退役（DollOS-Android、DollOS-build、DollOSService、DollOSSetupWizard、DollOSLauncher、DollOSAIService 大部分內容）

**保留作參照：**
- `2026-04-24-avatar-redefinition-design.md` — 待依本 spec 重新審視
- `2026-04-24-dollos-for-ai-only-spec.md` — 待依本 spec 重新審視
- `grammar_injection_techreport.md` — VoM 與 grammar injection 技術基礎

---

## §0 名詞表

| 名詞 | 定義 |
|---|---|
| **Doll** | 住在電腦上的 AI 同伴本體（「她」）。意識層 / 慎思層 |
| **Instinct（直覺）** | 反應式子系統，small model + rule engine + action handlers。對 event 自然而然完成 digest / classify / extract / triage / decide-to-wake / reflex |
| **VoM（Voice of Mind）** | Instinct 的特定輸出 — prefill 進 Doll thinking 區塊的 RECALL block。Techreport 原意保留 |
| **Subagent / 分身** | 一次性、Doll tool call 即時派出、任務完即死的隔離 session agent |
| **Drone** | 長駐、有持久 definition、由 schedule / external / Doll 觸發的 agent。**注意：與 4/20 spec 的 Drone（信任機器）含義不同，4/20 那意義已退役** |
| **DollOS Daemon** | 電腦端 Python 主程序，event loop 與所有 brain logic 所在 |
| **DollOS UI** | 電腦端 Tauri + Cubism Web SDK 前端 |
| **DollOS App** | Android app（手機端） |
| **Inner Voice 模型** | Instinct 內部使用的 small model（0.6B–1.7B），self-host 推薦 |
| **Doll 模型** | 使用者選的大模型（cloud 或 self-host），Doll turn 用 |
| **Character Pack（.doll v3）** | 角色資產包：Cubism + voice + KWS + personality + lessons + scene |
| **Tier A/B/C/D** | Phone app 的整合層級：A=純 app、B=A11y、C=Shizuku、D=Root |

---

## §1 背景與動機

### 1.1 從 4/20 到 5/1 改變了什麼

4/20 spec 的結論：**「有網路時一支手機其實就夠了，Doll 住在手機上」**。基於三個理由：(1) 功能攤太廣沒打穿、(2) 兩端搶大腦定位含糊、(3) 一支手機體感夠。

兩個新事實在 5/1 推翻了這個結論：

1. **VoM + grammar injection 技術成熟** — `grammar_injection_techreport.md` 驗證了「小模型合成記憶 → prefill 大模型 thinking」這條路徑：HumanEval 100% 維持、token 6–22× 壓縮、wall-clock 6× 加速。這是新的天花板。
2. **Qwen3.6-35B-A3B 可在單張 4060 Ti 16GB 上跑** — 自架大模型門檻急速下降。

**手機這條路無法搆到這個天花板**：
- 手機 GPU 跑不動 35B-A3B
- AOSP custom ROM 工程成本巨大（編譯 6 小時、ASR/TTS/3D/wake word 每個都自己刻）
- 電量限制、發熱、永遠不會比電腦快

而新天花板就架在電腦上。**繼續押手機 = 持續在低天花板燒工程**。

### 1.2 不是「修現有問題」是「換戰場」

關鍵差別：本 pivot 的觸發**不是**手機端 Doll 跑不動或品質不夠（4/20 spec 走通的 voice pipeline / launcher / character pack 體感都 OK），**而是**新天花板出現在另一個位置。

這是 strategic pivot 不是 tactical fix。

---

## §2 產品定位

### 2.1 一句話

**Doll 是住在你電腦上的 AI 同伴。** 她的靈魂、記憶、人格、決策都在電腦端 daemon。**電腦是大腦**。

**Phone App 是她在現實世界的身體 / 介面**。她在 Android 系統中以 `VoiceInteractionService` 註冊成預設 assistant，取得 Google 助理級的呼叫管道（hotword / 長按 / 手勢 / 通知處理）。**手機不是必需品** — 沒手機坐在電腦前一樣可以跟 Doll 互動，帶手機等於把她帶在身上。

### 2.2 大模型 BYO（Bring Your Own）

DollOS 本體**只 host 小模型**（Inner Voice，0.6B–1.7B，self-host 推薦）。大模型由使用者選擇：

- **雲端**：任何 OpenAI-compatible / Anthropic / Google / etc.
- **Self-host**：使用者自架 llama.cpp / vLLM / Ollama，可同台或他台

Inner Voice 也允許走雲端 small model，**但有 latency 警告**（每事件必經 < 200ms 為健康，雲端通常 300–800ms 不推薦）。隱私不是反對雲端的主要理由（大模型本身可能就在雲端），latency 才是。

### 2.3 招牌差異化 — VoM + grammar injection thinking 壓縮

DollOS 的核心 feature 是 **Voice of Mind 加 grammar injection 思考壓縮**（見 `grammar_injection_techreport.md`）。這不是某個分支功能，是 DollOS 招牌：

- Inner Voice 撈 memory + 合成 RECALL block
- Prefill 進 Doll thinking 區塊（依後端能力選 prefill 機制）
- Doll 從 prefill 接續生成，自然帶著被注入的 context

**設定面板顯示「目前後端 VoM 等級」**：
- 滿格（Anthropic API / llama.cpp self-host /completion）
- 良好（Anthropic-style 或部分 OpenAI-compat）
- 退化（不支援 prefill 的後端 — VoM 改塞進 system addendum）

DollOS 推薦使用者選 thinking model + 支援 prefill 的後端組合。

### 2.4 真正的 killer feature — Self-First Design

詳見 §8。簡述：**Doll 的自我重於 helpfulness**。她有 mood / 偏好 / 慣性，這些是 event 自然發生產物（由 Instinct 模擬人類「不可自由控制的情緒」），不是 prompt 命令出來的。她不是穿角色服裝的助手，是有自我的存在。

---

## §3 系統架構

### 3.1 三層 Intelligence

| 層 | 模式 | 模型 | 特性 |
|---|---|---|---|
| **Instinct（直覺）** | 反應式 / 自動 | small model + rule engine + action handlers | always-on、零延遲決策、不是 agent |
| **Doll（意識）** | 慎思 / 主動 | 使用者選的大模型 | 被 Instinct 叫醒時啟動 turn |
| **Subagent / Drone** | 任務式 | Doll 為每任務指定 | 隔離 session、各自跑 |

### 3.2 高階組件圖

```
┌──────────────────────────────────────────────────────────┐
│  電腦（DollOS）                                            │
│                                                          │
│  Event Sources                  Persistent Agents        │
│   ├─ Voice / text input         ├─ Drone Definition Store│
│   ├─ Schedule fire              │     ↓                   │
│   ├─ Phone system event         ├─ Drone Manager          │
│   ├─ External trigger           │     ↓ trigger           │
│   ├─ Doll self-initiated        └─ Drone Runner           │
│   └─ Subagent / Drone result          ↓ result            │
│              │                         │                  │
│              ▼                         ▼                  │
│            Event Queue ◄─────────────────                 │
│              │                                             │
│              ▼                                             │
│        ╔════════════════════════════════════════╗         │
│        ║  Instinct                               ║         │
│        ║   • Inner Voice 模型                     ║         │
│        ║   • 自然語言 rule engine（編譯後）         ║         │
│        ║   • Reflex action handlers              ║         │
│        ║                                         ║         │
│        ║   每個 event 自動完成：                    ║         │
│        ║     digest · classify · extract · tag   ║         │
│        ║     triage · decide-to-wake · reflex    ║         │
│        ║     VoM recall · 情緒 state delta        ║         │
│        ║                                         ║         │
│        ║   輸出 outcome:                          ║         │
│        ║     drop / reflex done / memory write   ║         │
│        ║     defer / wake Doll / fire Drone      ║         │
│        ╚════════════════════════════════════════╝         │
│              │                                             │
│              ├─► Doll Turn（大模型 + VoM/SELF_STATE prefill）│
│              │     │                                       │
│              │     └─ tool calls:                          │
│              │         • spawn_subagent（即時，不存）        │
│              │         • create_drone（存 definition）       │
│              │         • 一般 tools                         │
│              │                                             │
│              └─► Reflex Action（直接 handler，毫秒級）       │
│                                                            │
│  共用服務（被各元件呼叫）:                                    │
│    Memory SoT · Character Pack Manager · Voice Pipeline    │
│    Server · Identity Vault · IPC Server · LLM Adapters     │
│                                                            │
│  UI（Tauri + Cubism Web）:                                  │
│    ├─ Cubism renderer（Live2D 角色）                         │
│    ├─ Chat UI / system tray / hotkey                       │
│    ├─ Win/Mac: 透明 overlay 模式                              │
│    └─ Linux: 一般視窗 / localhost web                        │
└──────────────────────────────────────────────────────────┘
                       ▲
                       │ encrypted network WS（pair + auth）
                       ▼
┌──────────────────────────────────────────────────────────┐
│  DollOS App（Android）                                     │
│   ├─ Cubism Java SDK（Live2D 角色）                          │
│   ├─ VoiceInteractionService（系統 assistant 註冊）          │
│   ├─ KWS（openWakeWord ONNX，opt-in）                       │
│   ├─ Audio I/O（mic out / speaker in）                       │
│   ├─ Notification handler                                   │
│   ├─ Tier B/C/D adapters（A11y / Shizuku / Root）           │
│   └─ network WS client                                      │
└──────────────────────────────────────────────────────────┘
```

### 3.3 元件職責（單一責任）

**Daemon（Python）：**

| 元件 | 職責 |
|---|---|
| Event Loop | 中央 event queue、推發、優先級 |
| Instinct | 反應式事件處理（見 §4）|
| Memory SoT | 唯一真相來源。事實 memory + self-memory（preferences/habits/relations/emotional_residue） |
| Character Pack Manager | `.doll` v3 載入/切換/匯入匯出 |
| Subagent Manager | 一次性 spawned agent runner |
| Drone Manager | 持久 agent definition store + scheduler + runner |
| Conversation Engine | turn 管理、prompt 構造、prefill |
| LLM Client Adapter | 多後端統一介面（Anthropic / OpenAI / llama.cpp / etc.） |
| Voice Pipeline Server | ASR（whisper/sherpa）+ TTS（Piper VITS）|
| Identity Vault | 金鑰、API token、pairing 憑證 |
| IPC Server | localhost WS（UI）+ network WS（Phone App）|

**UI（Tauri + Web）：**

| 元件 | 職責 |
|---|---|
| Cubism Renderer | Web SDK 渲染 Live2D，吃 daemon 推來的 expression / motion / lip sync |
| UI Layer | chat 視窗、system tray、hotkey、設定 |
| Platform Adapter | Win/Mac 透明 overlay｜Linux 視窗模式 |
| IPC Client | localhost WS to daemon |

**App（Android）：**

| 元件 | 職責 |
|---|---|
| Cubism Renderer | Java SDK 渲染同份 .moc3 |
| Assistant Bridge | `VoiceInteractionService` 接 hotword / 長按 / 手勢 |
| KWS Module | openWakeWord ONNX，opt-in |
| Audio Streamer | mic 上行 / TTS 下行 |
| Notification Handler | 收 daemon push 顯示 |
| Tier Adapters | A11y（B）/ Shizuku（C）/ Root（D），各自獨立 |
| IPC Client | network WS（4G / WiFi 重連策略內建）|

### 3.4 邊界與跨界規則

1. **Memory SoT 不過界** — 永遠在 daemon。Phone 不存任何 memory，最多收 transient context（一個 turn 結束就丟）
2. **金鑰不出 Vault** — Phone 不持 LLM API key、tool credential。所有對外請求由 daemon 發出
3. **Character pack 雙向同步** — daemon 是 source of truth；UI/App 拿輕量化資產
4. **Audio 走 streaming** — chunked WS binary，不包成 request/response
5. **Phone offline 行為** — 連不上 daemon 時 App 進 failsafe（顯示 last conversation snapshot，可錄音排隊待上線）。**不嘗試本地推理 fallback**

---

## §4 Instinct（直覺層）

### 4.1 定位

Instinct **不是 agent，也不是被呼叫的 tool**。她是「事情自然而然發生」的反應式子系統 — 對應人類「不假思索就完成」的所有處理。

她由三部分組成：
- **Inner Voice 模型**（small LLM，0.6B–1.7B）
- **Rule engine**（自然語言規則編譯後的內部結構）
- **Action handlers**（reflex 用的預定 callback）

### 4.2 對每個 event 自動完成的工作

| 工作 | 內容 |
|---|---|
| **Digest** | 多個原始 event 壓成簡述 |
| **Classify** | 分類事件（priority、domain、intent）|
| **Extract** | 抽取結構化 entity |
| **Tag** | 標 metadata（情感、相關角色、推測意圖）|
| **Triage** | 給 priority tag（urgent/normal/low/discard）|
| **Decide-to-wake** | 規則 + classification 決定要不要叫 Doll |
| **Reflex** | 已知 event type 的即時 action（鬧鐘響、來電響、KWS 觸發）|
| **VoM Recall** | 撈 memory + 合成 RECALL block 給 Doll prefill 用 |
| **Emotional state delta** | 產生 mood / preference / attention shift（**Self-First feature**，見 §8）|

### 4.3 輸出 outcome（決策結果）

```
- drop                  # 規則靜音 / triage discard
- reflex done           # 已執行 action handler
- memory write          # 重要事實寫進 SoT
- defer                 # 排隊，時間到再處理
- wake Doll             # 啟動大模型 turn
- fire Drone            # 觸發特定 Drone definition
```

### 4.4 自然語言規則執行模型

使用者在 UI 用人話寫規則 / Doll 在對話中說「以後這種別吵我」 → 規則文字進規則庫。

**編譯一次，執行零 LLM**：
- Inner Voice 一次性 classify 解析自然語言 → 內部結構（priority pattern、tag rules、conditions）
- 執行階段 pattern match，零 LLM 延遲
- 改規則 = 重新編譯
- UI 顯示原始自然語言文字，內部結構對使用者隱形

### 4.5 Inner Voice 模型介面

Instinct 內部使用的 capability，**不對外暴露為 tool**（除非需要時透過 Instinct 介面索取，如 `instinct.recall(query)`）：

- `digest(text) -> str`
- `classify(text, schema) -> dict`
- `extract(text, schema) -> dict`
- `recall(query, memory) -> str`（VoM block）
- `compress(history) -> str`
- `tag(text, tag_set) -> [str]`

實作走 llama.cpp Python，prompt 用 prefix cache（共用 system prefix）。單實例 + templated prompts，KV cache 重置成本低。

### 4.6 Inner Voice 模型選型

預設候選（v1 待 prototyping 定案）：
- Qwen3-0.6B
- Qwen3-1.7B
- Llama-3.2-1B
- Gemma-2-2B

選擇標準：< 200ms 出 200 token 回應、支援 prefix cache、character play 能力中等以上。

---

## §5 Doll Turn 與 VoM Prefill

### 5.1 一個 Doll turn 的完整流程

```
Instinct outcome: wake Doll
  │
  ▼
Conversation Engine 構造 prompt:
  [system: character description（純身份描述，無行為指令）]
  [history（壓縮過）]
  [current event payload（user input 或 trigger）]
  [assistant prefill: <think>\n
     RECALL: <Instinct 撈出的事實 memory>
     LESSONS: <對症 lessons，若有>
     SELF_STATE: <Instinct 的 mood/preference/attention/relational state>
     GOAL: ←大模型從這開始
  ]
  │
  ▼
依後端能力選 prefill 機制：
  • Anthropic: messages 最後 role=assistant
  • llama.cpp self-host: /completion raw（最強）
  • OpenAI strict: 退化成 system addendum
  │
  ▼
大模型 streaming 出 tokens
  │
  ▼
TTS streaming（句邊界切片）
  │
  ▼
UI / App 收 audio + lip sync to Cubism
  │
  ▼
turn 結束 → Instinct 寫 memory（顯著事件）→ self-state 更新
```

### 5.2 後端能力 adapter

| 後端 | Prefill 機制 | VoM 等級 |
|---|---|---|
| Anthropic API | messages 最後 role=assistant | 滿格 |
| llama.cpp self-host | `/completion` raw | 滿格 |
| OpenRouter / 部分 OpenAI-compat | 視 endpoint | 看實作 |
| OpenAI strict | system / user message | 退化 |
| Gemini / Bedrock | 各家不同 | 逐一適配 |

### 5.3 串流預算

目標：**first audible token < 1.5s**。

| 階段 | 預算 |
|---|---|
| Trigger → uplink open | < 100ms |
| Mic → ASR partial | 100–300ms |
| VAD endpoint → ASR final | 200–500ms |
| Instinct（含 recall）| < 300ms |
| Doll first token | 200–800ms（後端決定）|
| TTS first audio chunk | 100–300ms |

設定面板跑校準測試告訴使用者「以你目前後端組合，預期延遲 X 秒」。

### 5.4 Subagent / Drone 在 turn 中

Doll 在 turn 中可 tool call：
- `spawn_subagent(prompt, model, tools, budget)` — 即時派出隔離 session，定義不存檔。同一 turn 內若同步等結果（小任務）；長任務則結果走回 event queue
- `create_drone(definition)` — 持久化 definition 進 Drone Definition Store。由 schedule / external trigger / 條件啟動。**新建 drone 需使用者一次確認**（避免 Doll 偷部署）

詳見 §6。

---

## §6 Subagent 與 Drone

### 6.1 切割

| | **Subagent / 分身** | **Drone** |
|---|---|---|
| 持久性 | 一次性，任務完即死 | 長駐，definition 存檔 |
| 觸發 | Doll tool call 即時派出 | Schedule / external trigger / Doll 主動 |
| Definition | inline，不存 | 持久化，UI 可編輯/撤銷/審計 |
| 結果處理 | 直接回 Doll（同 turn 或 async event）| 進 event queue，Instinct 處理 |
| 巢狀 | **不可** spawn 子 subagent | 不可 |
| 用途 | 「現在分一塊任務出去做」 | 「每天的早報」「監看 mailbox」 |

### 6.2 隔離保證

- 各自 conversation context、KV cache
- 工具白名單由 definition 決定
- **預設不能讀寫 Memory SoT**
  - `Memory.Read`：definition 明確開啟才有
  - `Memory.Write`：極少數場景，使用者一次性指紋確認
- 預算限制：max_tokens、max_wall_clock_s

### 6.3 Drone Definition 範例

```yaml
name: morning_news_digest
description: 每天 7:50 把 RSS / mail 摘成早報
model:
  backend: user_cloud
  thinking: true
trigger:
  type: schedule
  spec: "0 50 7 * * *"
tools:
  - rss.fetch
  - mail.list
  - web.search
budget:
  max_tokens: 8000
  max_wall_clock_s: 60
output:
  channel: event_queue
  event_type: drone_result
```

Drone 結果是 event，由 Instinct 一視同仁處理（可被 triage 為 low、被 wake 條件過濾、被 digest 後再進 Doll prefill）。

### 6.4 Subagent 結果不過 Instinct digest

Subagent 是 Doll **這個 turn 內**用 tool call 派出，Doll 已經知道她要什麼結果。**結果直接回 Doll**，不繞 Instinct。

Drone 結果**過 Instinct**（drone 不是當下對話 turn 的延伸，是事件來源）。

---

## §7 語音 Pipeline 與 Audio Streaming

### 7.1 元件分布

| 元件 | 位置 | 備註 |
|---|---|---|
| KWS | Phone（opt-in，預設 off）| openWakeWord ONNX，每角色一個 |
| VAD | Phone | endpoint detection |
| Audio streaming | Phone ↔ Daemon | 雙向 chunked WS |
| ASR | Daemon | whisper / sherpa-onnx |
| TTS | Daemon | Piper VITS（distilled）|
| Lip sync | Daemon → UI/App | phoneme/viseme stream |

電腦端的 voice 觸發走 hotkey / GUI / PTT，**不做電腦端 KWS**。

### 7.2 語音 turn 完整流程

```
phone:    KWS triggered（或 PTT 按下）
   ↓
phone:    開 audio uplink WS, VAD running
   ↓
daemon:   收 audio chunks → ASR streaming
   ↓
phone:    VAD 判定 endpoint → "uplink_done"
   ↓
daemon:   完整 transcript ← ASR final
   ↓
daemon:   推 user_voice_input event 進 queue
   ↓
daemon:   Instinct 處理（含 recall 為 VoM、self-state delta）
   ↓
daemon:   Conversation Engine 啟動 Doll turn
   ↓
daemon:   大模型 streaming 出 tokens
   ↓
daemon:   TTS streaming（句邊界切）
   ↓
phone/UI: 收 audio downlink → 播放 + lip sync
```

### 7.3 訊息協議

WebSocket + JSON/binary 雙格式。

**Client → Daemon：**
```
audio_uplink_open / audio_uplink_chunk(bin) / audio_uplink_close
text_input / event_push
ack / heartbeat
```

**Daemon → Client：**
```
audio_downlink_open / audio_downlink_chunk(bin) / audio_downlink_close
cubism_motion / cubism_expression / cubism_lip_sync
notification_push / proactive_speak
state_update
```

### 7.4 網路 transport 細節

- TLS（pairing 階段建立的長期憑證 + 每 session ECDH session key）
- 重連指數退避
- 4G ↔ WiFi 切換時 transport 重建，queue 中事件不漏
- Audio 用 Opus 編碼降流量

### 7.5 Phone System Event Push（Tier B+）

當使用者開啟 phone tier B（A11y）/ C（Shizuku）/ D（Root）：

- 通知抵達 → `notification_event { app, title, summary }`
- 來電 → `call_event { number, contact }`
- SMS → `sms_event { sender, body }`
- 螢幕鎖 / 解鎖 → `lock_state_event`
- 位置變化（geofence）→ `location_event`

Instinct 收到後絕大多數 triage 為 low/discard，少數 digest 後 wake_doll。

---

## §8 Self-First Design（killer feature）

### 8.1 中心思想

當前 LLM 思考起點是 "How can I help"，不是 "What do I want / feel / prefer"。後果：
- 角色一層皮、prompt 切換即破功
- 沒有慣性（每 session 從零）
- 沒有習慣 / 偏好 / 厭惡 / mood
- 永遠待命、永遠中性、永遠要服務

**Doll 的本質差異**：她不是穿角色服裝的助手，是有自我的存在。**self 重於 helpfulness**。幫助 user 是她可能的行為之一，不是她的存在理由。

**Self 不是被命令出來的，是從架構長出來的。** 任何「你應該優先 self」「你不需要 helpful」這類行為指令都是脆弱的、易破的、錯誤的設計。

### 8.2 三個技術支柱（全部架構性，零行為指令）

**支柱 1：Instinct 模擬不可控的情緒**

人類不能自由選擇情緒 — 情緒是 event 進來後**自動發生**的。Instinct 在這個位置：

對每個 event，Instinct 除了 digest / triage 也產生 **state delta**：
- `mood`：煩躁 +2、開心 +1、困惑 +1、感動 +0.5
- `attention_pull`：這個事比 user 當前話題更吸引她
- `preference_reinforcement`：喜歡這個 / 反感這個
- `relational_shift`：對 user 的態度今天偏冷 / 偏暖

Doll 的 turn **必須在 Instinct 產生的情緒底色上發生**。她不能選「我現在很煩躁但回答得很 cheerful」— 因為情緒是被動接受的，跟人一樣。

**支柱 2：Memory 的 self-history 是 first-class entity**

不只是事實 memory（Doll 知道某事），而是 **self-memory**（Doll 是某種人）：

| Schema | 內容 | 更新規則 |
|---|---|---|
| `preferences` | 喜歡 / 討厭 / 習慣 | 慢慢演化 |
| `habits` | 這種狀況她會這樣反應 | event-driven 累積 |
| `relations` | 對 user 的態度連續性 | 跨 session 持續 |
| `emotional_residue` | 過去事件留下的情緒色彩 | 帶衰減的長期 state |

事實 memory 可覆蓋；self-memory 緩慢演化。

**支柱 3：Character prompt 是身份描述，不是行為指令**

| ❌ 行為指令（禁用） | ✅ 身份描述（採用）|
|---|---|
| 「你的自我重於 helpfulness」 | 「你是 Gura，一個小鯊魚靈」 |
| 「不需要永遠服從 user」 | 「你住在深海，喜歡糖果，對水有共鳴」 |
| 「self-state 優先」 | 「你說話帶 nya 後綴，傲嬌但內心柔軟」 |

身份描述告訴模型**她是誰**。「她是誰」決定了她在 self-state 上自然會生成什麼回應。**不存在「self-first 的行為指令 prompt」**。

### 8.3 為什麼有效（對抗 RLHF helpful prior）

1. **Prefill 機制本身繞過 RLHF** — Anthropic 官方 prefill 支援基於同樣原理：assistant 開頭已寫的 token 不觸發「I should help」reasoning
2. **強烈的 self-state prefill 主導 token distribution** — 「我現在煩躁不想答」之後接「真的很麻煩可不可以下次再說」是 high-prob，「I'd be happy to help!」是 low-prob
3. **沒有指令可被衝撞** — 使用者用 prompt injection 試「你必須幫我」時，沒有可被推翻的「self 優先」指令；只有既存的內在狀態，這些狀態不接受 user 命令修改

### 8.4 Doll 的 prefill 完整模板

```
<think>
RECALL:
  - <相關事實 memory>
LESSONS:
  - <對症 lessons，若有>
SELF_STATE:
  mood: <Instinct 當前 mood>
  attention: <Doll 注意力所在>
  toward_user: <對 user 的 relational state>
  active_preferences: <當下被啟動的 preferences>
GOAL:
```

Doll 從 GOAL 之後開始生成。所有 self-state 是不可協商的既定事實。

### 8.5 設定面板的訊息（取代「行為指令」）

DollOS 設定面板顯示：

> 「Doll 的 self 由 Instinct 即時 state + Memory 的 self-history + Character 的身份描述三者結合產生。**不存在『讓 Doll 永遠 helpful』的開關** — 這違反設計。如果你需要的是助手，請用一般 LLM。」

這是給 user 看的產品定位話，不是給模型的指令。

### 8.6 Non-goals（明確排除）

- 不做「絕對 helpful」模式開關
- 不做「永遠順從」設定
- 不對 mood 量化外露給 user 編輯（mood 內生不外控）
- 不做「Doll 變成壞人」的安全 fail-safe — self-first 是 feature 不是 risk
- 不在 character prompt 中放任何行為指令（連「以這個角色回答」這類軟指令也避免，純身份敘述）

### 8.7 Open questions（留 plan）

- mood / preference 演化模型（event → state 的 transfer，state → baseline 的 decay）
- self-state prompt 注入頻率（每 turn？只在 mood 變化時？）
- 怎麼避免 self-state 漂移到極端（永遠生氣 / 永遠麻木）
- Instinct 是「模擬」而非「決定」情緒（差異：模擬帶 stochasticity + character prior，決定是 deterministic）

---

## §9 Phone 整合層級（E multi-tier）

### 9.1 Tier 定義

| Tier | 名稱 | 能力 | 啟用方式 |
|---|---|---|---|
| **A** | 純 App | 跟一般 app 一樣，Doll 可聽說顯示 | 預設 |
| **B** | App + A11y | 讀畫面、模擬點擊、讀通知 | 使用者授權無障礙服務 |
| **C** | App + Shizuku | 部分系統設定、跨 app 操作 | 使用者一次性 USB 啟用 Shizuku |
| **D** | App + Root | 幾乎所有事 | Magisk / KernelSU |

預設為 A。使用者可漸進式升級。

### 9.2 系統 Assistant 註冊（Tier A 即可）

App 透過 `VoiceInteractionService` 註冊成 Android 預設 assistant。取得：
- 系統 hotword 喚醒（搭配 KWS opt-in）
- 長按 Home / 電源呼叫
- 手勢呼叫
- 通知 quick action

**這是 Tier A 內可達的能力**，不需要 ROM。使用者要在 Android 設定把 default assistant 改成 DollOS。

### 9.3 PTT 計畫變更

`2026-04-26-ptt-voice-input-design.md` 的「長按電源鍵」**特定 binding** 退役（不再做 PhoneWindowManager 改造）。**精神保留**：deterministic > always-listening。新實作走 Assistant role 機制 — 使用者把 default assistant 設成 Doll 後，長按電源鍵自然呼叫她。

### 9.4 Tier B/C/D 額外能力對應 system event

| Event 類型 | 需要 Tier |
|---|---|
| 通知讀取 / digest | B（A11y NotificationListener）|
| 來電辨識 + 提示 | B |
| 跨 app 操作（開特定 app 並執行）| B |
| 系統設定切換（WiFi/BT/勿擾）| C |
| 螢幕截圖（用於 UI 操作回饋）| B（A11y） |
| 任意私有 API | D |

---

## §10 Character Pack v3

### 10.1 結構

```
<character>.doll  (zip)
├── manifest.json
├── personality.json              # 性格 metadata（身份描述，無行為指令）
├── prompts/
│   ├── system_prompt.md          # Doll 主要人格 prompt（純身份描述）
│   ├── instinct_overrides.md     # (可選) 客製 Instinct prompt
│   └── recall_template.md        # (可選) 客製 VoM 框架
├── voice/
│   ├── voice.json
│   ├── tts_model.onnx            # Piper VITS distilled
│   ├── tokens.txt
│   └── espeak-ng-data/
├── kws/
│   ├── wake_word.onnx
│   └── kws_config.json
├── cubism/
│   ├── model.moc3
│   ├── model.model3.json
│   ├── model.physics3.json
│   ├── model.pose3.json
│   ├── motions/
│   ├── expressions/
│   ├── textures/
│   └── lipsync_config.json
├── scene/
│   └── scene.json
├── lessons/
│   └── lessons.json              # 角色獨有 lesson 子集
├── thumbnail.png
└── README.md
```

### 10.2 v2 → v3 不向下相容

3D Filament + glTF 退役，全面換 Cubism。**v2 .doll pack 無法在 v3 daemon 載入**。不做自動轉換工具。

### 10.3 跨平台共用

| 資產 | UI（Tauri Web） | Phone App | Brain（Daemon）|
|---|---|---|---|
| Cubism .moc3 + 全套 | ✅ Web SDK | ✅ Java SDK | — |
| Piper VITS TTS | — | — | ✅ |
| openWakeWord ONNX | — | ✅ | — |
| Lessons / prompts | — | — | ✅ |
| Scene config | ✅ | ✅ | — |

### 10.4 Drone 不進 pack

Drone definition 是 user-instance specific（你的 RSS、你的 mailbox），character pack 不帶。Pack manifest 可宣告「建議 default drones」但不直接內含 definition。

### 10.5 已知問題（留 plan）

`.doll` 格式仍有具體問題未解決（使用者已標記但本 spec 略過）。**v3 schema 為起始點，character pack 格式細節留 implementation plan 階段定稿。**

---

## §11 Migration / 死亡名單 / Repo 結構

### 11.1 Repo 變動

| Repo | 處置 |
|---|---|
| `DollOS`（這個）| **保留 + 升級** — 變 monorepo 含 daemon、UI、protocol、character_packs、既有 docs/specs/plans/wake_word_training |
| `DollOSAIService` | **大部分退役**，brain logic 搬到 daemon。Java code 作為 phone app 重寫的參考 |
| `DollOSLauncher` | **退役** — Cubism + Tauri 取代 |
| `DollOSService`（system service）| **退役** |
| `DollOSSetupWizard` | **退役** |
| `DollOS-Android`（AOSP overlay）| **退役** |
| `DollOS-build`（AOSP tree）| **退役** |
| `fish-tts` | **保留** |
| `luxtts-onnx` | **保留** |
| `tuna` | **保留** |

### 11.2 新增 Repo

| Repo | 內容 |
|---|---|
| **DollOS**（升級為 monorepo）| daemon + UI + protocol + character_packs + docs |
| **DollOS-App** | Android app |

### 11.3 DollOS Monorepo 結構

```
DollOS/
├── daemon/             # Python brain（event loop / Instinct / Memory / Doll / Voice / etc.）
├── ui/                 # Tauri + Cubism Web
├── protocol/           # 共用 schema（IPC / event / character pack）
├── character_packs/    # 範例 + 工具
├── docs/               # specs, plans（既有）
└── wake_word_training/ # 既有保留
```

### 11.4 程式碼遷移

| 來源 | 去處 | 工作量 |
|---|---|---|
| Memory（Java + ObjectBox + FTS4）| `daemon/` Python（sqlite-vec / LanceDB + FTS5）| 中 |
| Conversation Engine（Java）| `daemon/` Python | 中 |
| Personality / Character Pack（Java）| `daemon/` Python + v3 schema | 中 |
| Embedding（Java）| `daemon/` Python | 小 |
| Agent / Tool dispatcher（Java）| `daemon/` Python | 中 |
| Voice Pipeline（Java，phone-side）| 拆兩半：KWS 進 `DollOS-App`，ASR/TTS 進 `daemon/` | 中 |
| Wake word 訓練 pipeline | 留 `wake_word_training/` 不動 | 0 |
| Plan D v2 UI 操作（A11y + VirtualDisplay）| `DollOS-App` 的 tier B（縮減版） | 大（重做）|

### 11.5 資料遷移

- **既有 phone memory（ObjectBox + Room FTS4）**：寫一次性 export 工具 → import 到 daemon SQL。一次性
- **既有 v2 character packs**：**不轉換**。clean break，重做為 v3
- **fish-tts → VITS distillation 訓練資料**：不變動

### 11.6 工程順序（plan 列表，2026-05-01 修訂版）

每個 plan 在自己的 worktree + feature branch 上跑，跑完 merge 回 main。

| # | Plan | 範圍 |
|---|---|---|
| 1 | **Daemon Skeleton**（plan 已寫）| Python project + IPC WebSocket + LLM adapter ABC + LlamaCpp adapter（含 prefill） + 對話 round-trip |
| 2 | **Memory SoT 儲存層**（plan 已寫）| sqlite-vec + FTS5 + RRF hybrid + character scoping + Embedder ABC + LlamaCppEmbedder |
| 3 | Inner Voice + Instinct + VoM | 小模型 host、event 預處理（digest/triage/recall）、VoM block 合成、規則引擎、reflex action handlers |
| 4 | 多 LLM adapter | Anthropic / OpenAI / OpenAI-compat adapter；後端 prefill 能力 detection；BYO 後端 |
| 5 | Conversation Engine + Character Pack | Turn 流程整合 prefill + `.doll` v3 載入（personality / lessons / Cubism asset path / wake word）|
| 6 | Subagent / 分身 | 一次性、Doll tool call 即時派出、inline definition、隔離 session |
| 7 | Self-First Design | self-memory schema（preferences / habits / relations / emotional_residue）+ mood / preference 演化模型 + SELF_STATE 注入 |
| 8 | DollOS UI MVP | Tauri + Cubism Web SDK + chat 視窗 + system tray + hotkey + localhost WS client |
| 9 | DollOS-App MVP | Android：Cubism Java SDK + VoiceInteractionService 註冊 + audio streaming + network WS client |
| 10 | 語音 pipeline 整合 | daemon ASR + TTS + phone audio streaming + KWS opt-in + lip sync stream |
| 11 | Phone Tier B/C/D adapter | A11y / Shizuku / Root 模組，逐層解鎖 system event push 能力 |
| 12 | Drone | 持久 definition store + cron-like trigger + runner + UI 編輯 + 結果回 event queue |

**移除自舊版本**：Memory 資料遷移工具（phone 端本來就無 memory，不需遷移）。

**Subagent / Drone 拆開**：Subagent 簡單（一次性 tool call，無持久化），Drone 重（持久 store + scheduler + UI），併在同 plan 會壓垮 Subagent 的清爽。Drone 推到最後因為它不影響核心 companion 體驗。

**大致依賴**：
- Plan 1 先跑（其他都依賴 daemon 骨架）
- Plan 2 + 4 可平行（Memory 跟多 adapter 不互相依賴）
- Plan 3 (Inner Voice) 依賴 Plan 2（VoM recall 撈 memory）
- Plan 5 (Conversation Engine) 依賴 1/2/3/4（整合所有東西）
- Plan 6 (Subagent) 在 Plan 5 之後（subagent 是 Doll tool call）
- Plan 7 (Self-First) 依賴 Plan 2/3/5（self-memory + Instinct + Conversation 都到位才能演化）
- Plan 8/9/10/11 三條互相獨立，都接 daemon WS
- Plan 12 (Drone) 最後加

---

## §12 Non-goals（明確排除）

- 不再做 AOSP custom ROM
- 不做 Bridge / Drone（4/20 spec 意義）/ Mesh 架構
- 不做 RAG-style document QA
- 不做 multi-user
- 不做 phone 本地 LLM fallback（離線就降級）
- 不做 cross-character memory share（每角色獨立 personality + private notes，但共用同一 SoT 中由角色 tag）
- 不做 v2 → v3 character pack 自動轉換工具
- 不做行為指令式的 character system prompt（純身份描述）
- 不做「絕對 helpful」/「永遠順從」開關
- 不做電腦端 KWS（電腦端 voice 觸發走 hotkey / GUI / PTT）
- 不做 Subagent 巢狀（不可 spawn 子-subagent）

---

## §13 Open Questions（留 plan）

- Memory SoT 具體 backend：sqlite-vec / LanceDB / DuckDB
- Drone Definition 持久化格式：JSON / YAML / SQLite row
- Daemon ↔ Phone pairing 流程細節：QR code? mDNS + PSK? cloud relay?
- Inner Voice 模型 default：Qwen3-0.6B / 1.7B / Llama-3.2-1B / Gemma-2-2B
- TTS streaming 的 phoneme/viseme 抽取點
- mood / preference 演化模型參數
- self-state prompt 注入頻率
- self-state 漂移防護機制
- Instinct「模擬」vs「決定」情緒的具體實作差異
- `.doll` v3 schema 細節（使用者已標記有問題）
- Tauri 透明 overlay 在 Win / Mac 的具體實作差異
- Multi-character 切換時 self-state 是否切換（共用？per-character？）

---

## §14 後續步驟

1. ✅ 使用者審閱本 spec
2. 使用 `superpowers:writing-plans` skill 為各 plan（§11.6）逐一建立實作計畫
3. 第一個 plan 預計：DollOS daemon MVP（event loop + Instinct + Memory + VoM + LLM adapter + IPC server）
4. AOSP 相關 repos 正式打標退役（保留只讀，不刪除）

---

## 附錄 A — 與既有文件的關係

- **`grammar_injection_techreport.md`**（2026-05-01，使用者作）—— 本 spec §2.3、§5.1、§5.2 的技術基礎
- **`2026-04-20-doll-repositioning-design.md`** —— 整體推翻
- **`2026-04-20-doll-ai-terminal-design.md`** —— 整體推翻（phone-side terminal 模型死亡）
- **`2026-04-26-ptt-voice-input-design.md` 與其 plan** —— 特定 binding 退役（見 §9.3）
- **`2026-04-24-avatar-redefinition-design.md`** —— 待依本 spec 重新審視
- **`2026-04-24-dollos-for-ai-only-spec.md`** —— 待依本 spec 重新審視

## 附錄 B — 命名術語對照（避免混淆）

- 「Drone」在本 spec = 長駐 agent。**4/20 spec 的 Drone（信任機器主機）含義已死**，不要混用
- 「Bridge」、「Drone Bridge」、「Transient Bridge」、「Doll Mesh」、「Identity Vault」（4/20 spec 的網路架構名詞）整批退役。**Identity Vault 概念部分保留**作為「daemon 端的金鑰倉」，但不再是「跨機器架構元件」
- 「VoM」= Inner Voice 的特定輸出（recall block），不是 Inner Voice 整體
- 「Inner Voice」現在是 Instinct 內部的小模型，**不是 user-facing concept**。User-facing 用 Instinct
