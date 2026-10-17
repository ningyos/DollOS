# DollOS v1 — iPhone 身體 + 混合式大腦

**狀態**：DRAFT v1（待使用者審）
**日期**：2026-10-17
**取代（部分）**：`2026-05-01-dollos-pivot-to-computer-design.md` 的 §2.1「手機不是必需品」、§3.3 Phone App 元件表（Android / `VoiceInteractionService` / Tier A–D）、§3.4 規則 1 與規則 5、§9 Phone 整合層級。電腦端架構（event loop、Memory SoT、Self-First、BYO big LLM）**不變**。
**關聯**：`2026-07-07-latency-reflex-think-design.md`（開口前延遲）、`2026-07-10-metabolic-vital-model-design.md`（vital / 能量）、`docs/RETIRED-REPOS.md`

---

## §0 一句話與驗收

> **iPhone 是 Doll 的身體，電腦是她的大腦。** 身體有自己的小神經系統（裝置上的 Apple Foundation Models），負責反射和感官；所有「她是誰、她記得什麼、她決定說什麼」都留在電腦。

v1 驗收（全部在使用者自己的 iPhone 15 Pro 上）：

1. 打開 app 看到 Live2D 的 Doll，表情跟著電腦端的 mood 走。
2. 按住說話 → 電腦端 ASR → Doll 回合 → Doll 的聲音（電腦端 TTS）從手機播出，嘴型同步。
3. 喚醒到「她有反應」（反射）≤ 500 ms；反射之後才是大腦的正式回應。
4. 在外面用行動網路也連得到家裡的 daemon。
5. 拔掉家裡網路：app 清楚顯示「連不到大腦」，身體仍然有反射，使用者說的話排進 outbox；恢復連線後依序送達，Doll 知道自己剛剛斷線過。
6. Doll 主動找人（self-directed agenda / system pulse 產生的 Say）→ 手機收到推播。

---

## §1 決策紀錄

| # | 決策 | 誰定的 | 理由 |
|---|---|---|---|
| D1 | v1 用傳統 LLM，SNN 不在 v1 關鍵路徑 | 使用者 | SNN 還在研究階段，也不太可能在手機上跑 |
| D2 | 手機是 Doll 的**身體**（不是可有可無的遙控器） | 使用者 | — |
| D3 | 身體 = **iPhone app**（不做自製 ROM、不做 Android） | 使用者 | 曾考慮 AOSP 自製 ROM；Android 16 起 AOSP 不再提供 Pixel device tree，且編譯機硬體不足。Android 之後再看 |
| D4 | 大腦**混合式**：手機上的小模型 + 電腦 daemon | 使用者 | — |
| D5 | 手機上的小模型 = **Apple Foundation Models**（系統內建，約 3B） | 使用者 | iPhone 15 Pro 支援 Apple Intelligence；免下載、免金鑰 |
| D6 | app 用**原生 Swift / SwiftUI** | 本文提議 | 見 §9 |
| D7 | 反射**只從電腦預先渲染的台詞庫挑選**，不自由生成 | 本文提議 | 見 §5.3；保住 Doll 的聲音和 Self-First |
| D8 | 連線走 **Tailscale + 配對 token** | 本文提議 | 見 §8.3 |
| D9 | iOS 程式碼放在本 repo 的 `ios/` | 本文提議 | 見 §11 |

---

## §2 跟 2026-05-01 pivot 的差別

| | 2026-05-01 pivot | v1（本文） |
|---|---|---|
| 手機的地位 | 可選的介面；「沒手機坐在電腦前一樣完整」 | **身體**。電腦是大腦，手機是她在現實世界的存在 |
| 平台 | Android app，`VoiceInteractionService` 系統助理 | iPhone app（不能當系統助理，見 §7） |
| 手機上的推理 | 「不嘗試本地推理 fallback」 | 有本地模型，但**角色是反射層，不是備援大腦**（§2.1） |
| Memory | Phone 不存任何 memory | **不變**。手機只有 outbox 和唯讀的身體快照（§4.2） |
| 金鑰 | Phone 不持 LLM API key | **不變**。Foundation Models 不需要金鑰 |

### §2.1 為什麼這不違反「No fallback」規則

CLAUDE.md 的規則是「不做 fallback / 降級邏輯」。本設計的反射層**不是**「大腦連不上時改用小模型假裝是 Doll」：

- 反射層**連線時和斷線時做的事一模一樣**：挑反射台詞、挑表情、判斷要不要叫醒大腦。斷線時它不會升級去做更多事。
- 大腦做的事（真正的回話、記憶、mood 更新、工具）**斷線時就是不會發生**，app 明確顯示「連不到大腦」，使用者的話排隊等送達。這是把邊界講清楚，不是降級。

---

## §3 架構

```
┌──────────── iPhone 15 Pro：身體（DollOS Body app）────────────┐
│                                                                │
│  感官 Senses                     反射層 Reflex（Foundation Models）│
│   ├─ 麥克風 + VAD（按住說話）     ├─ 輸入：感官事件 + 身體快照     │
│   ├─ 觸碰（點／摸 Live2D）        ├─ 輸出：@Generable 結構化選擇   │
│   ├─ app 前景／背景、時間         │   · 反射台詞 id（或不出聲）    │
│   └─ 充電狀態                     │   · 表情 / 動作 id             │
│                                   │   · 要不要叫醒大腦             │
│  表現 Presentation                └─ 永遠不寫記憶、不決定 mood     │
│   ├─ Live2D（Cubism Native + Metal）                             │
│   ├─ 音訊播放 + 嘴型                                             │
│   └─ 推播 / Live Activity                                        │
│                                                                │
│  Link：WS（控制 + 文字）＋ WebRTC（語音）＋ Outbox（離線佇列）  │
└───────────────────────────┬────────────────────────────────────┘
                            │ Tailscale（WireGuard）+ 配對 token
┌───────────────────────────┴──── 電腦：大腦（DollOS daemon）─────┐
│  IPC Server（既有 WS + WebRTC signaling，新增 body 訊息與驗證） │
│  Event Loop → Doll Turn（大模型）→ Say / Mood / NoteMemory / …  │
│  Memory SoT · Character Pack · Voice（ASR / TTS）· Agenda · Vital │
│  新增：Body Link（身體快照推送、反射台詞庫渲染、APNs 推播）      │
└────────────────────────────────────────────────────────────────┘
```

---

## §4 分工

### §4.1 大腦 vs 身體

| 職責 | 電腦（大腦） | iPhone（身體） |
|---|---|---|
| 人格、記憶、mood、current_self | ✅ 唯一的寫入者 | ❌ 只拿唯讀快照 |
| 正式回話（Say） | ✅ 大模型 | ❌ |
| ASR / TTS | ✅ 既有 voice pipeline | ❌（只播放） |
| 工具、Workflow、Monitor、Shell | ✅ | ❌ |
| VAD、收音、播放、嘴型 | — | ✅ |
| Live2D 繪製 | —（只送 expression / motion id） | ✅ |
| 反射（≤ 500 ms 的第一反應） | ❌ 太慢 | ✅ Foundation Models |
| 判斷「這件事要不要叫醒大腦」 | — | ✅ 反射層 |
| 感官事件 → perception | 接收、決定意義 | 產生、送出 |
| 反射台詞庫 | ✅ 產生文字 + 用 Doll 的 TTS 渲染 | 快取、播放 |

### §4.2 不變式

- **I1 單一寫入者**：記憶、mood、current_self、diary 只有大腦能寫。反射層的所有輸出都是「身體動作」，不是「她的想法」。
- **I2 反射必回報**：每次反射（播了哪句、做了哪個表情）都以 perception 回報給大腦（例：`BodyReflexed`）。大腦因此知道「我的身體剛剛說了『嗯？』」，正式回話不會跟它矛盾。
- **I3 大腦優先**：大腦的 Say 一到，正在播的反射立即被打斷。反射永遠不會打斷大腦。
- **I4 手機不存記憶**：手機只存 (a) outbox：還沒送達的使用者輸入和感官事件；(b) 身體快照：大腦推來的 mood、表情狀態、台詞庫版本、最近幾句對話（只用於顯示和反射判斷，覆寫式，不累積）。
- **I5 金鑰不出電腦**：手機只持有配對 token（存在 Keychain）。
- **I6 不假裝**：大腦連不上時，app 介面上必須看得出來；反射台詞庫裡的「斷線類」台詞也是 Doll 自己預先說好的話。

---

## §5 反射層（Apple Foundation Models）

### §5.1 它做什麼

輸入：一個感官事件（例：使用者按下說話、點了她的頭、深夜打開 app、斷線 5 分鐘後）+ 身體快照（mood、時間、連線狀態、最近一句對話）。

輸出（用 `@Generable` 的結構化輸出，**不產生自由文字**）：

```swift
@Generable struct Reflex {
    @Guide(description: "反射台詞 id；不出聲就填 nil") var lineId: String?
    @Guide(description: "表情 id") var expression: ExpressionId
    @Guide(description: "動作 id") var motion: MotionId?
    @Guide(description: "這個事件要不要叫醒大腦") var wakeBrain: Bool
}
```

台詞庫和表情清單由大腦隨身體快照推送；反射層每次只會看到「目前可選的 id + 一行描述」。

### §5.2 它不做什麼

- 不產生任何 Doll 要說的新句子。
- 不寫記憶、不改 mood（只**讀**快照的 mood 來挑合適的反射）。
- 不呼叫工具、不做多輪對話。

### §5.3 為什麼只從台詞庫挑（D7）

1. **聲音一致**：台詞是電腦用 Doll 的 TTS 預先渲染的，手機播出來就是她的聲音。手機上沒有她的 TTS 模型。
2. **Self-First**：Doll 說的每句話都來自她自己（大模型）。台詞庫由大腦在空閒時生成或更新（例：「被叫醒時會說的話」「斷線時想說的話」），反射層只是挑。
3. **小模型夠用**：3B 模型做「從 30 個選項挑一個」很穩；自由生成角色台詞則容易出戲。
4. **context 限制**：on-device 模型的 context 視窗約 4,096 token（輸入加輸出），選擇題放得進去。

台詞庫規模先抓 30–80 句，分類：被叫醒、被摸、等待中（大腦正在想）、斷線、重新連上、深夜、早安。

### §5.4 可用性邊界（不做 fallback）

Foundation Models 需要 Apple Intelligence 已開啟，且裝置語言或地區要被支援。**截至撰寫時查不到確定的繁體中文 / 台灣支援狀態**，所以：

- app 啟動時查 `SystemLanguageModel.default.availability`。不可用 → **反射層整個關閉**，介面顯示原因（例：「Apple Intelligence 未開啟」）。身體這時只剩感官和表現：事件直接送大腦，不會換別的模型頂替。
- 反射層的 prompt 和選項描述用英文寫（台詞本身是預先渲染的音檔，跟模型語言無關），避開中文支援問題。
- M0 第一件事就是在使用者手機上實測（§12）。

### §5.5 延遲預算

| 段 | 目標 |
|---|---|
| 感官事件 → 反射層出結果 | ≤ 300 ms（session 預熱 `prewarm()`、prompt 精簡、只輸出 id） |
| 結果 → 音檔開始播放、表情切換 | ≤ 100 ms（台詞音檔預先快取在本機） |
| 合計 | ≤ 500 ms（驗收條 3） |

如果實測做不到，也可以把某些反射改成不經模型的固定對照（例：「點頭 → 固定表情」）。這是**刻意的設計選擇**，要寫進文件，不是執行時偷偷切換。

---

## §6 連線狀態與斷線行為

### §6.1 三個狀態

| 狀態 | 判定 | 介面 |
|---|---|---|
| **連線中** | WS 已驗證、心跳正常 | 正常 |
| **重連中** | 心跳斷 ≤ 30 秒 | 角落小圖示，不打擾 |
| **離線** | 斷線 > 30 秒，或一開始就連不上 | 明確標示「連不到大腦」+ 最後連線時間；Doll 進入「發呆 / 睡著」的待機表情 |

### §6.2 各狀態下能做什麼

| 行為 | 連線中 | 重連中 | 離線 |
|---|---|---|---|
| 反射層 | ✅ | ✅ | ✅（同一套規則） |
| 按住說話 | 語音即時串流到大腦 | 錄音進 outbox | 錄音進 outbox |
| 打字 | 即時送出 | 進 outbox | 進 outbox |
| 感官事件 | 即時送出 | 進 outbox | 進 outbox（只保留有意義的，例：打開 app、摸她；不保留每秒心跳） |
| Doll 的正式回話 | ✅ | ❌ | ❌ |
| 表情 | 跟大腦的 mood | 維持最後狀態 | 待機表情；mood 快照標示為「過期」 |

### §6.3 Outbox 規則

- 每筆有 `client_event_id`（UUID）和手機時間戳。大腦按 id 去重，所以重送是安全的。
- 恢復連線後**依序**送出，前面先加一筆 `BodyReconnected(offline_since, offline_until, queued_count)` perception。大腦因此知道「我斷線了 X 分鐘，這是我不在時累積的東西」，可以自己決定怎麼回應（例：一次回、挑重點回、或道歉）。
- 錄音檔在 outbox 裡就是原始音訊；上線後由大腦的 ASR 處理，手機不做離線 ASR。
- 上限：outbox 保留 24 小時或 50 筆，超過就丟最舊的，並在 `BodyReconnected` 裡記錄丟了幾筆。

### §6.4 大腦主動找人

Doll 自主發話（agenda / system pulse / schedule 產生的 Say）時，手機很可能在背景：

- daemon 直接用 APNs 送推播（文字是 Doll 說的話）。點開 app → app 跟大腦拿這句的音檔播出。
- 這需要付費的 Apple Developer Program 帳號（APNs key），以及 daemon 端的 APNs sender（新的 ServiceSupervisor 服務或 kernel 內的 sink）。

---

## §7 iOS 平台限制與對應

| 限制 | 影響 | v1 對應 |
|---|---|---|
| 不能取代 Siri，沒有系統助理角色 | 沒有「長按電源鍵叫 Doll」 | **Action Button** 綁「打開 Doll 並開始說話」的 App Intent；另提供 App Shortcuts 和 Control Center 控制項 |
| app 在背景時不能冷啟動錄音（AudioRecordingIntent 從背景啟動會失敗，Apple 只對 CallKit / PushToTalk 例外） | 鎖屏時沒辦法直接對她說話 | v1：說話一定先把 app 帶到前景（Action Button 會打開 app）。之後評估 **PushToTalk framework**：它允許從鎖屏按鍵收音，也能收到大腦主動推來的語音（需要 entitlement） |
| 背景執行很快被暫停 | 不能常駐聽喚醒詞 | v1 不做喚醒詞。延續舊設計的精神：「確定的按鍵 > 一直開著麥克風」 |
| 只能透過 App Intents / 捷徑影響其他 app | 不能控制系統 | v1 不做手機控制 |
| 推播需要 APNs | daemon 要能送 APNs | §6.4 |
| 開發要 Mac + Xcode | 使用者主要開發機是 Linux | 使用者有 Mac；iOS 開發在 Mac 上做 |

---

## §8 協定與傳輸

### §8.1 沿用既有的

- 文字：`TextInput` → `TextChunk` / `TurnEnd`。
- 語音：既有的 WebRTC 流程（`webrtc_offer` / `ice_candidate` / `utterance_start` / `utterance_end` / `interrupt` / `say_aborted`，daemon 端是 aiortc）。iOS 端用 Google WebRTC 的 iOS framework，跟 aiortc 相容。VAD 在手機做，跟 `voice/bridge` 一樣只送 utterance 邊界。

### §8.2 新增訊息（草案，實作時以 `ipc/messages.py` 為準）

| 方向 | 訊息 | 內容 |
|---|---|---|
| 手機 → 大腦 | `body_hello` | 配對 token、裝置 id、app 版本、反射層可用性 |
| 手機 → 大腦 | `body_sense` | `client_event_id`、時間、事件種類（`touched` / `app_opened` / `app_backgrounded` / `charging` …）+ 參數 |
| 手機 → 大腦 | `body_reflexed` | 反射層挑了什麼（台詞 id、表情 id、`wakeBrain`），供 I2 |
| 手機 → 大腦 | `body_reconnected` | §6.3 |
| 大腦 → 手機 | `body_snapshot` | mood、表情狀態、台詞庫版本、可選表情 / 動作清單、最近幾句對話 |
| 大腦 → 手機 | `body_express` | 大腦要求的表情 / 動作（例：Mood tool 之後） |
| 大腦 → 手機 | `reflex_bank` | 台詞庫：id、分類、一行描述、音檔 URL 或內嵌 |

daemon 端對應的 perception kind 暫定 `BodySensed`、`BodyReflexed`、`BodyReconnected`，origin_tier 為 **owner**（配對過的手機就是主人的身體）。

### §8.3 網路與驗證（D8）

- **Tailscale**：家裡電腦和 iPhone 都加入同一個 tailnet。不用開 port forwarding，行動網路也能連，傳輸本身就加密。daemon 的 IPC 綁 tailnet 介面（目前 `config.example.toml` 綁的是 `127.0.0.1`）。
- **配對 token**：目前 IPC server 沒有 per-connection 驗證，對外開放前必須補上。流程：`dollosctl pair` 在終端機顯示 QR code（tailnet 位址 + 一次性 token）→ app 掃描 → 存進 Keychain。daemon 對沒有通過 `body_hello` 驗證的連線一律拒絕（fail-closed，同 MCP `query_token` 的作法）。
- 不採用：自架 relay、公開 port 加 TLS。等 v1 跑穩再看要不要。

---

## §9 原生 Swift vs 跨平台（D6）

**結論：原生 Swift / SwiftUI。**

| 考量 | 原生 Swift | 跨平台（Flutter / React Native / KMP） |
|---|---|---|
| Foundation Models | 原生 Swift API | 要寫 bridge |
| App Intents / Action Button / Live Activity / PushToTalk | 原生 | 都要寫 bridge，或乾脆寫 Swift extension |
| Live2D | Cubism SDK for Native（C++ + Metal）可直接接 | 沒有官方 Flutter / RN 版本，要自己包 |
| WebRTC | Google WebRTC iOS framework | 有套件，但又多一層 |
| Android 之後 | 另寫一個 Kotlin app | 理論上共用，但上面這些平台功能本來就各寫各的 |

這個 app 的價值幾乎都在平台專屬的功能上（裝置模型、Action Button、背景限制的處理、Live2D）。跨平台能共用的只剩 UI 外殼和 WS 協定，而協定本來就用 JSON schema 共享。Android 真的要做時，用 Kotlin 原生另寫，共用的是 `protocol/` 的 schema 和台詞庫格式。

---

## §10 Live2D 在 iOS 上

- 用 **Cubism SDK for Native** 的 Metal renderer，用 Swift 包一層 view。SDK 不進 repo，照 `vendor/README.md` 的方式由使用者自行下載放入（授權條款同既有作法）。
- 角色資產（`.moc3`、texture、motion、expression）從 character pack 取得。大腦在配對後透過 `body_snapshot` 告知版本，手機比對後下載。
- 嘴型：先用音量驅動 `ParamMouthOpenY`（簡單、可靠）。phoneme / viseme 之後再說。
- 觸碰：用 Cubism 的 hit area（頭、身體）產生 `body_sense(touched, area)`。

---

## §11 Repo 與開發流程（D9）

- iOS 程式碼放在本 repo 的 **`ios/`**（Xcode project，SwiftUI）。理由：協定改動和 app 改動可以在同一個 commit 裡一起落地，也方便 code review。
- Mac 上 clone 本 repo 開發；daemon 照舊跑在 Linux 主機上，Mac 透過 tailnet 連。
- 協定 schema：daemon 的 pydantic model 匯出 JSON Schema 到 `protocol/`，Swift 端寫對應的 `Codable`，加一個 snapshot test 防止兩邊漂移。
- 帳號：個人使用至少需要 Apple Developer Program（每年 99 美元），APNs 和 PushToTalk entitlement 都需要；免費帳號安裝的 app 每 7 天要重簽一次。

---

## §12 里程碑

每個里程碑結束都要能在真機上展示。

| M | 內容 | 完成條件 |
|---|---|---|
| **M0 探路** | 三個 spike：(a) Foundation Models 在使用者手機上的可用性、語言設定、`@Generable` 選擇題的延遲；(b) Cubism Native + Metal 在 SwiftUI 裡繪出 Gura；(c) iOS WebRTC 跟 daemon 的 aiortc 互通 | 三個問題都有實測數字；任何一個不行就回頭改設計 |
| **M1 連上大腦** | Tailscale + `dollosctl pair` + `body_hello` 驗證；文字對話；`body_snapshot` 推 mood | 在行動網路下打字跟 Doll 對話 |
| **M2 有聲音的身體** | 按住說話 → WebRTC → ASR → Doll → TTS → 播放 + 音量嘴型；Action Button | 驗收條 1、2 |
| **M3 反射** | 台詞庫生成與渲染（daemon）、`reflex_bank` 同步、反射層、`body_reflexed` 回報、大腦打斷反射 | 驗收條 3 |
| **M4 斷線** | 三種連線狀態、outbox、`body_reconnected` | 驗收條 5 |
| **M5 主動** | APNs sender、推播點開播放 | 驗收條 6 |

之後（不在 v1）：PushToTalk 鎖屏對講、更多感官（位置、行事曆、健康）、viseme 嘴型、Android。

---

## §13 Non-goals（v1 不做）

- 喚醒詞 / 一直開著麥克風
- 手機上的 ASR、TTS、記憶、自由生成台詞
- 控制手機或其他 app
- 自製 ROM、Android app
- 多台手機同時當身體
- SNN（研究線另外進行，見 `snn-vlm/`）

---

## §14 待決問題

1. **Q1**：反射台詞庫要由 Doll 在 agenda 回合自己寫（更 Self-First），還是由使用者審過的固定清單起步？（建議：先固定清單起步，M3 再讓她自己更新）
2. **Q2**：Foundation Models 在使用者的語言 / 地區設定下不能用時，要不要把裝置語言改成英文來開啟 Apple Intelligence？（只影響系統設定，Doll 的台詞是中文音檔，不受影響）
3. **Q3**：要不要在 v1 就申請 PushToTalk entitlement，把「鎖屏對講」提前到 M2？
4. **Q4**：推播文字是否要遮蔽（鎖屏只顯示「Doll 找你」），避免隱私外洩？

---

## §15 對既有文件的影響

- `2026-05-01-dollos-pivot-to-computer-design.md`：§2.1、§3.3 Phone App、§3.4 規則 1 與 5、§9 由本文取代（在該文件頂端加註）。
- `CLAUDE.md`、`README.md`：「手機是可選介面 / Android app」改為「iPhone 是身體」，並指向本文。
- `DollOS-App`（Android）從「Future」改為「暫緩，待 v1 之後評估」。
