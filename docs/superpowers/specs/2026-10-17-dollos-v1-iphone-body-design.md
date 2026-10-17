# DollOS v1 — iPhone 身體

**狀態**：DRAFT v2（待使用者審）。v1 有裝置上的反射層（Apple Foundation Models）；v2 依使用者決定拿掉，見 D4。
**日期**：2026-10-17
**取代（部分）**：`2026-05-01-dollos-pivot-to-computer-design.md` 的 §2.1「手機不是必需品」、§3.3 Phone App 元件表（Android / `VoiceInteractionService` / Tier A–D）、§3.4 規則 5、§9 Phone 整合層級。電腦端架構（event loop、Memory SoT、Self-First、BYO big LLM）**不變**。
**關聯**：`2026-07-07-latency-reflex-think-design.md`（開口前延遲）、`2026-07-10-metabolic-vital-model-design.md`（vital / 能量）、`docs/RETIRED-REPOS.md`

---

## §0 一句話與驗收

> **iPhone 是 Doll 的身體，電腦是她的大腦。** 身體負責感官（聽、被碰、知道自己被打開或放下）和表現（Live2D、聲音）；所有的思考、記憶和決定都在電腦。手機上**沒有任何模型**。

v1 驗收（全部在使用者自己的 iPhone 15 Pro 上）：

1. 打開 app 看到 Live2D 的 Doll，表情跟著電腦端的 mood 走。
2. 按住說話 → 電腦端 ASR → Doll 回合 → Doll 的聲音（電腦端 TTS）從手機播出，嘴型同步。
3. 按下說話的瞬間，畫面上的 Doll 立刻進入「聆聽」狀態（固定的 UI 回饋，≤ 100 ms）；她的回話延遲由大腦決定，目標沿用延遲壓縮 spec 的「開口前 ≤ 2 s」。
4. 在外面用行動網路也連得到家裡的 daemon。
5. 拔掉家裡網路：app 清楚顯示「連不到大腦」，使用者說的話排進 outbox；恢復連線後依序送達，Doll 知道自己剛剛斷線過。
6. Doll 主動找人（self-directed agenda / system pulse 產生的 Say）→ 手機收到推播。

---

## §1 決策紀錄

| # | 決策 | 誰定的 | 理由 |
|---|---|---|---|
| D1 | v1 用傳統 LLM，SNN 不在 v1 關鍵路徑 | 使用者 | SNN 還在研究階段，也不太可能在手機上跑 |
| D2 | 手機是 Doll 的**身體**（不是可有可無的遙控器） | 使用者 | — |
| D3 | 身體 = **iPhone app**（不做自製 ROM，Android 暫緩） | 使用者 | 曾考慮 AOSP 自製 ROM；Android 16 起 AOSP 不再提供 Pixel device tree，且編譯機硬體不足 |
| D4 | **手機上不跑模型，也沒有反射層**。大腦全在電腦 | 使用者 | 原本設計的反射層只是「從台詞庫挑一句」，價值有限：v1 沒有常駐麥克風，每次說話都是主動按的，本來就一定要叫醒大腦；去掉後也不必依賴 Apple Intelligence（繁中 / 台灣支援不確定），日後移植 Android 更簡單 |
| D5 | app 用**原生 Swift / SwiftUI** | 本文提議 | 見 §8 |
| D6 | 連線走 **Tailscale + 配對 token** | 本文提議 | 見 §7.3 |
| D7 | iOS 程式碼放在本 repo 的 `ios/` | 本文提議 | 見 §10 |

---

## §2 跟 2026-05-01 pivot 的差別

| | 2026-05-01 pivot | v1（本文） |
|---|---|---|
| 手機的地位 | 可選的介面；「沒手機坐在電腦前一樣完整」 | **身體**。電腦是大腦，手機是她在現實世界的存在 |
| 平台 | Android app，`VoiceInteractionService` 系統助理 | iPhone app（不能當系統助理，見 §6） |
| 手機上的推理 | 「不嘗試本地推理 fallback」 | **不變**：手機上沒有任何模型 |
| Memory | Phone 不存任何 memory | **不變**。手機只有 outbox 和唯讀的身體快照（§4.2） |
| 金鑰 | Phone 不持 LLM API key | **不變** |
| 斷線 | failsafe：顯示最後的對話、可錄音排隊 | **大致不變**，細節見 §5 |

### §2.1 「身體」跟「遙控器」差在哪

既然手機上沒有模型，「身體」的意義不在算力，而在於：

- **感官是 Doll 的 perception**：手機上發生的事（被打開、被放下、被摸、充電、使用者說話）都以 perception 送進大腦的 event loop。她「感覺」得到自己的身體，不只是收到聊天訊息。
- **她住在畫面裡**：Live2D 是她的身體表現，mood 和動作都反映在上面，不是聊天框。
- **她能主動透過身體找人**：推播（§5.4）。

---

## §3 架構

```
┌──────────── iPhone 15 Pro：身體（DollOS Body app）────────────┐
│                                                                │
│  感官 Senses                    表現 Presentation              │
│   ├─ 麥克風 + VAD（按住說話）    ├─ Live2D（Cubism Native + Metal）│
│   ├─ 觸碰（點／摸 Live2D）       ├─ 音訊播放 + 嘴型              │
│   ├─ app 前景／背景、時間        ├─ 固定 UI 狀態（聆聽、等待、離線）│
│   └─ 充電狀態                    └─ 推播                          │
│                                                                │
│  Link：WS（控制 + 文字）＋ WebRTC（語音）＋ Outbox（離線佇列）  │
└───────────────────────────┬────────────────────────────────────┘
                            │ Tailscale（WireGuard）+ 配對 token
┌───────────────────────────┴──── 電腦：大腦（DollOS daemon）─────┐
│  IPC Server（既有 WS + WebRTC signaling，新增 body 訊息與驗證） │
│  Event Loop → Doll Turn（大模型）→ Say / Mood / NoteMemory / …  │
│  Memory SoT · Character Pack · Voice（ASR / TTS）· Agenda · Vital │
│  新增：Body Link（身體快照推送、表情指令、APNs 推播）            │
└────────────────────────────────────────────────────────────────┘
```

---

## §4 分工

### §4.1 大腦 vs 身體

| 職責 | 電腦（大腦） | iPhone（身體） |
|---|---|---|
| 人格、記憶、mood、current_self | ✅ 唯一的寫入者 | ❌ 只拿唯讀快照 |
| 決定說什麼、做什麼表情 | ✅ 大模型 | ❌ |
| ASR / TTS | ✅ 既有 voice pipeline | ❌（只播放） |
| 工具、Workflow、Monitor、Shell | ✅ | ❌ |
| VAD、收音、播放、嘴型 | — | ✅ |
| Live2D 繪製、idle 動畫 | —（只送 expression / motion id） | ✅ |
| 固定 UI 狀態（聆聽中、等待回應、離線） | — | ✅（純介面回饋，不代表 Doll 說話或做決定） |
| 感官事件 → perception | 接收、決定意義 | 產生、送出 |

### §4.2 不變式

- **I1 單一寫入者**：記憶、mood、current_self、diary 只有大腦能寫。
- **I2 身體不說話**：手機播出的每一句話都來自大腦的 Say。手機上沒有台詞庫、沒有本地 TTS、不會替她回應。
- **I3 手機不存記憶**：手機只存 (a) outbox：還沒送達的使用者輸入和感官事件；(b) 身體快照：大腦推來的 mood、表情狀態、最近幾句對話（只用於顯示，覆寫式，不累積）。
- **I4 金鑰不出電腦**：手機只持有配對 token（存在 Keychain）。
- **I5 不假裝**：大腦連不上時，app 介面上必須看得出來。

---

## §5 連線狀態與斷線行為

### §5.1 三個狀態

| 狀態 | 判定 | 介面 |
|---|---|---|
| **連線中** | WS 已驗證、心跳正常 | 正常 |
| **重連中** | 心跳斷 ≤ 30 秒 | 角落小圖示，不打擾 |
| **離線** | 斷線 > 30 秒，或一開始就連不上 | 明確標示「連不到大腦」+ 最後連線時間；Live2D 進入固定的待機動畫 |

### §5.2 各狀態下能做什麼

| 行為 | 連線中 | 重連中 | 離線 |
|---|---|---|---|
| 按住說話 | 語音即時串流到大腦 | 錄音進 outbox | 錄音進 outbox |
| 打字 | 即時送出 | 進 outbox | 進 outbox |
| 感官事件 | 即時送出 | 進 outbox | 進 outbox（只保留有意義的，例：打開 app、摸她；不保留每秒心跳） |
| Doll 回話 | ✅ | ❌ | ❌ |
| 表情 | 跟大腦的 mood 和指令 | 維持最後狀態 | 待機動畫；mood 快照標示為「過期」 |

### §5.3 Outbox 規則

- 每筆有 `client_event_id`（UUID）和手機時間戳。大腦按 id 去重，所以重送是安全的。
- 恢復連線後**依序**送出，前面先加一筆 `BodyReconnected(offline_since, offline_until, queued_count)` perception。大腦因此知道「我斷線了 X 分鐘，這是我不在時累積的東西」，可以自己決定怎麼回應（例：一次回、挑重點回、或道歉）。
- 錄音檔在 outbox 裡就是原始音訊；上線後由大腦的 ASR 處理，手機不做離線 ASR。
- 上限：outbox 保留 24 小時或 50 筆，超過就丟最舊的，並在 `BodyReconnected` 裡記錄丟了幾筆。

### §5.4 大腦主動找人

Doll 自主發話（agenda / system pulse / schedule 產生的 Say）時，手機很可能在背景：

- daemon 直接用 APNs 送推播（文字是 Doll 說的話）。點開 app → app 跟大腦拿這句的音檔播出。
- 這需要付費的 Apple Developer Program 帳號（APNs key），以及 daemon 端的 APNs sender（新的 ServiceSupervisor 服務或 kernel 內的 sink）。

---

## §6 iOS 平台限制與對應

| 限制 | 影響 | v1 對應 |
|---|---|---|
| 不能取代 Siri，沒有系統助理角色 | 沒有「長按電源鍵叫 Doll」 | **Action Button** 綁「打開 Doll 並開始說話」的 App Intent；另提供 App Shortcuts 和 Control Center 控制項 |
| app 在背景時不能冷啟動錄音（AudioRecordingIntent 從背景啟動會失敗，Apple 只對 CallKit / PushToTalk 例外） | 鎖屏時沒辦法直接對她說話 | v1：說話一定先把 app 帶到前景（Action Button 會打開 app）。之後評估 **PushToTalk framework**：它允許從鎖屏按鍵收音，也能收到大腦主動推來的語音（需要 entitlement） |
| 背景執行很快被暫停 | 不能常駐聽喚醒詞 | v1 不做喚醒詞。延續舊設計的精神：「確定的按鍵 > 一直開著麥克風」 |
| 只能透過 App Intents / 捷徑影響其他 app | 不能控制系統 | v1 不做手機控制 |
| 推播需要 APNs | daemon 要能送 APNs | §5.4 |
| 開發要 Mac + Xcode | 使用者主要開發機是 Linux | 使用者有 Mac；iOS 開發在 Mac 上做 |

---

## §7 協定與傳輸

### §7.1 沿用既有的

- 文字：`TextInput` → `TextChunk` / `TurnEnd`。
- 語音：既有的 WebRTC 流程（`webrtc_offer` / `ice_candidate` / `utterance_start` / `utterance_end` / `interrupt` / `say_aborted`，daemon 端是 aiortc）。iOS 端用 Google WebRTC 的 iOS framework，跟 aiortc 相容。VAD 在手機做，跟 `voice/bridge` 一樣只送 utterance 邊界。

### §7.2 新增訊息（草案，實作時以 `ipc/messages.py` 為準）

| 方向 | 訊息 | 內容 |
|---|---|---|
| 手機 → 大腦 | `body_hello` | 配對 token、裝置 id、app 版本 |
| 手機 → 大腦 | `body_sense` | `client_event_id`、時間、事件種類（`touched` / `app_opened` / `app_backgrounded` / `charging` …）+ 參數 |
| 手機 → 大腦 | `body_reconnected` | §5.3 |
| 大腦 → 手機 | `body_snapshot` | mood、表情狀態、可用表情 / 動作清單、最近幾句對話 |
| 大腦 → 手機 | `body_express` | 大腦要求的表情 / 動作（例：Mood tool 之後） |

daemon 端對應的 perception kind 暫定 `BodySensed`、`BodyReconnected`，origin_tier 為 **owner**（配對過的手機就是主人的身體）。

### §7.3 網路與驗證（D6）

- **Tailscale**：家裡電腦和 iPhone 都加入同一個 tailnet。不用開 port forwarding，行動網路也能連，傳輸本身就加密。daemon 的 IPC 綁 tailnet 介面（目前 `config.example.toml` 綁的是 `127.0.0.1`）。
- **配對 token**：目前 IPC server 沒有 per-connection 驗證，對外開放前必須補上。流程：`dollosctl pair` 在終端機顯示 QR code（tailnet 位址 + 一次性 token）→ app 掃描 → 存進 Keychain。daemon 對沒有通過 `body_hello` 驗證的連線一律拒絕（fail-closed，同 MCP `query_token` 的作法）。
- 不採用：自架 relay、公開 port 加 TLS。等 v1 跑穩再看要不要。

---

## §8 原生 Swift vs 跨平台（D5）

**結論：原生 Swift / SwiftUI。**

| 考量 | 原生 Swift | 跨平台（Flutter / React Native / KMP） |
|---|---|---|
| App Intents / Action Button / Live Activity / PushToTalk | 原生 | 都要寫 bridge，或乾脆寫 Swift extension |
| Live2D | Cubism SDK for Native（C++ + Metal）可直接接 | 沒有官方 Flutter / RN 版本，要自己包 |
| WebRTC | Google WebRTC iOS framework | 有套件，但又多一層 |
| Android 之後 | 另寫一個 Kotlin app | 理論上共用，但上面這些平台功能本來就各寫各的 |

拿掉反射層之後，app 變得更薄，跨平台能共用的部分比例稍微提高。但這個 app 最難的部分仍然是平台專屬功能（Action Button、背景限制、Live2D 的原生 renderer），共用的部分則只有 UI 外殼和 WS 協定，而協定本來就用 JSON schema 共享。Android 真的要做時，用 Kotlin 原生另寫，共用 `protocol/` 的 schema。

---

## §9 Live2D 在 iOS 上

- 用 **Cubism SDK for Native** 的 Metal renderer，用 Swift 包一層 view。SDK 不進 repo，照 `vendor/README.md` 的方式由使用者自行下載放入（授權條款同既有作法）。
- 角色資產（`.moc3`、texture、motion、expression）從 character pack 取得。大腦在配對後透過 `body_snapshot` 告知版本，手機比對後下載。
- 嘴型：先用音量驅動 `ParamMouthOpenY`（簡單、可靠）。phoneme / viseme 之後再說。
- 觸碰：用 Cubism 的 hit area（頭、身體）產生 `body_sense(touched, area)`。被摸的反應由大腦決定，所以會有大腦延遲；觸碰當下只播放 Cubism 內建的 hit 動畫（固定 UI 回饋）。

---

## §10 Repo 與開發流程（D7）

- iOS 程式碼放在本 repo 的 **`ios/`**（Xcode project，SwiftUI）。理由：協定改動和 app 改動可以在同一個 commit 裡一起落地，也方便 code review。
- Mac 上 clone 本 repo 開發；daemon 照舊跑在 Linux 主機上，Mac 透過 tailnet 連。
- 協定 schema：daemon 的 pydantic model 匯出 JSON Schema 到 `protocol/`，Swift 端寫對應的 `Codable`，加一個 snapshot test 防止兩邊漂移。
- 帳號：個人使用至少需要 Apple Developer Program（每年 99 美元），APNs 和 PushToTalk entitlement 都需要；免費帳號安裝的 app 每 7 天要重簽一次。

---

## §11 里程碑

每個里程碑結束都要能在真機上展示。

| M | 內容 | 完成條件 |
|---|---|---|
| **M0 探路** | 兩個 spike：(a) Cubism Native + Metal 在 SwiftUI 裡繪出 Gura；(b) iOS WebRTC 跟 daemon 的 aiortc 互通 | 兩個都在真機上跑通；不行就回頭改設計 |
| **M1 連上大腦** | Tailscale + `dollosctl pair` + `body_hello` 驗證；文字對話；`body_snapshot` 推 mood | 在行動網路下打字跟 Doll 對話 |
| **M2 有聲音的身體** | 按住說話 → WebRTC → ASR → Doll → TTS → 播放 + 音量嘴型；Action Button；聆聽 / 等待 UI 狀態 | 驗收條 1、2、3 |
| **M3 感官** | `body_sense`（觸碰、打開、放下、充電）→ perception；`body_express` | 摸她、她會（經過大腦）有反應 |
| **M4 斷線** | 三種連線狀態、outbox、`body_reconnected` | 驗收條 5 |
| **M5 主動** | APNs sender、推播點開播放 | 驗收條 6 |

之後（不在 v1）：PushToTalk 鎖屏對講、更多感官（位置、行事曆、健康）、viseme 嘴型、Android。

---

## §12 Non-goals（v1 不做）

- 手機上的任何模型（LLM、反射層、ASR、TTS）
- 喚醒詞 / 一直開著麥克風
- 手機上的記憶、台詞庫
- 控制手機或其他 app
- 自製 ROM、Android app
- 多台手機同時當身體
- SNN（研究線另外進行，見 `snn-vlm/`）

---

## §13 待決問題

1. **Q1**：要不要在 v1 就申請 PushToTalk entitlement，把「鎖屏對講」提前到 M2？
2. **Q2**：推播文字是否要遮蔽（鎖屏只顯示「Doll 找你」），避免隱私外洩？
3. **Q3**：哪些感官事件值得送給大腦？太多會讓她一直被打擾、也燒 token（每個 perception 都可能觸發一個 Doll 回合）。建議 v1 只送：打開 app、離開 app、觸碰、開始 / 停止充電；並沿用既有的 wake gating 讓大腦自己決定要不要為它開一個回合。

---

## §14 對既有文件的影響

- `2026-05-01-dollos-pivot-to-computer-design.md`：§2.1、§3.3 Phone App、§3.4 規則 5、§9 由本文取代（在該文件頂端加註）。
- `CLAUDE.md`、`README.md`：「手機是可選介面 / Android app」改為「iPhone 是身體」，並指向本文。
- `DollOS-App`（Android）從「Future」改為「暫緩，待 v1 之後評估」。
