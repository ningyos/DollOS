# DollOS MVP — Doll 活在 Discord(backbone + 情境化渲染 + 語料底盤)Design

Status: draft(goal-driven,2026-07-03)。MVP 總 spec:Phase 1 完整設計 + P2/P3 邊界。

Goal(user-set): 讓 Doll 以正常人的姿態活在 Discord 上:待在多人伺服器,知道自己身在何處(backbone 內外通道型別化+情境化渲染),自己判斷該不該回(L0/L1/L2 注意力),被捲入後不用 tag 也能接著聊(engagement window),並能進語音頻道通話(含延遲壓縮前置);她的每一天全程留下 finetune 級語意層 trace,整套系統 systemd+dollosctl 一鍵起停 — 分三階段。驗收 = smolGura 三個失敗模式(亂回、跟不上、無語音)不再重演。

## 1. Problem

DollOS 的大腦已完整(事件迴圈、記憶、self_profile、慢變演化、工具、排程、voice pipeline),但**沒有日常可用的形態**:client 是實驗品、四個程序手動起、Doll 只活在 localhost。使用者的願景(見 memory `project-dollos-vision-2026-07`)要求 portable(手機/Discord 感知現實)且**紀錄=訓練資料**(未來 finetune 專用 LLM)。前作 smolGura 以 Discord bot 真實跑過多人伺服器,留下三個失敗模式作為本設計的驗收清單:

1. 沒有「該不該回」的判斷——別人一講話就回,即便無關;
2. 無法「接著回覆」——要嘛每句 tag、要嘛每句都回,缺注意力與「什麼是我該管的」;
3. 語音通話做過但 code 遺失。

同時,每晚不使用就流失一天的訓練語料——上線時間本身有價值。

## 2. Design rationale(拍板紀錄)

- **Bridge 為獨立程序,不進 daemon。** 照 voice bridge 既有模式:py-cord 依賴、第三方雲的例外面、斷線重連全部隔離在 `discord-bridge` 程序;daemon 保持純淨。否決 in-daemon 整合:把 Discord SDK 塞進大腦程序違反「daemon 單純、身體外接」。
- **全記錄、選擇性喚醒。** 她看得到的訊息全量落地(ambient 語料+她的環境感知底料),但只有過注意力閘的才成為 perception。記錄與注意力解耦——語料不因她沒理而流失。
- **Backbone:通道型別化。** 所有 I/O 通道註冊時宣告 `locus: internal | external`。內部=身體器官(嘴 TTS、耳 ASR/mic、本機文字 CLI=主人在家對她說話、UI=她的樣子、未來手機 app=主要感官);外部=身外物(Discord 是第一個)。規則:(a) 注意力系統只綁外部通道(耳朵不決定要不要聽見);(b) 外部通道內容天生是攻擊面(接上慢變演化的 `external_ctx`,從事後標記升級為通道型別);(c) 通道歸屬與內容 provenance 是兩條獨立軸(Shell=內部的手,摸回外部內容——現狀不變)。
- **情境化渲染:A 地板、B 湧現、成效可測。** 事件依(來源×情景)渲染 situational block 與 event 敘述。A=系統充實管線(deterministic:author 記憶查詢、頻道近況、場合描述)保底,每個外部事件喚醒前補齊;B=她自己的查證工具(bridge RPC),挖不挖她判斷,成效由 trace 在 P2 裁決(使用者原話:B 較自然但成效未知→設計成可測)。兩不變式:**Self-First**(情境描述場合,永不改身分——一個 identity,多種場合敘事,無「Discord 人格」);**prompt cache**(identity 前綴含現在的我永遠穩定;situational block 在穩定段後、有限模板集、同場合位元組穩定,cache 按場合分桶)。
- **三階段,全做:** P1 文字存在感+語料底盤+服務化(上線 dogfood);P2 注意力調參(真實數據);P3 語音通話(含延遲壓縮前置;細節 spec 屆時補——增量開發,不預寫會過時的細節)。
- **弱模型三鐵律沿用**(memory `ref-weak-model-soft-mechanism-playbook`):稀有工具三面向可見性;prompt 管不住的語意升級 code 閘;軟機制必 live smoke。

## 3. Mechanics(Phase 1)

### 3.1 Backbone — 通道註冊與回覆路由

- **ChannelRegistry**(daemon 端,IPC 層):每條連線註冊 `{channel_id: str, locus: "internal"|"external", kind: "text"|"voice"|"discord"|"ui", addressable: bool}`。取代「最近註冊 sink 即當前對話」的單對話假設:**回覆路由到喚醒該 turn 的 perception 所屬通道**(turn 的 origin channel 進 MindLoop 狀態;Say/串流輸出經 SinkResolver 依 origin 定址)。internal 無 origin 標記時維持現行為(最近 internal sink)——現有 CLI/voice 客戶端行為不變。
- **Perception 擴充:** 新 kind `ChannelMessage`(external 通道訊息),data 攜帶 `{channel_id, guild, channel, author, author_id, content, mentioned, is_dm, msg_id, ts, situation}`。**`UserSpoke` 保留給 internal 通道**(主人在家說話):cancel 語意(打斷 consolidation/evolution)、energy 計算、`user_turn_count` 都只綁 UserSpoke——陌生人在 Discord 講話不該打斷她的睡眠整併,也不是「主人回來了」。**例外:主人本人**(見 3.4 身分綁定)在 Discord 對她說話,bridge 標記 `author_is_owner: true`,daemon 端升格為 UserSpoke 等效語意(cancel+energy),但 perception 仍為 ChannelMessage(場合還是 Discord)。
- IPC messages 擴充:`ChannelRegister`(client→daemon)、`ChannelEvent`(client→daemon,攜 ChannelMessage payload)、`AddressedText`(daemon→client,`{channel_id, target: {guild, channel}, text}`)。既有 TextInput/TextChunk 流程不動(internal 通道用)。

### 3.2 discord-bridge 程序

- `python -m dollos.discord_bridge --config config.toml`。py-cord;職責:(a) 事件轉送——白名單 guild/channel 的所有訊息(含她自己的)全量送 daemon 落地,過 L0/部分 L1(見 3.4 分工)的才標記 wake;(b) 回覆執行——收 `AddressedText` 送回指定頻道(長訊息切段);(c) 查證 RPC 執行端——`DiscordLookup` 工具的實際 API 呼叫(fetch channel history / user info),經 IPC 請求-回應;(d) P3 語音。斷線自動重連+重連後 backfill 最近訊息(有上限);bridge 崩潰不傷 daemon,systemd 自動拉起。
- **組態 `[discord]`**:`token`(on-device config,不出門)、`guild_allowlist`、`channel_allowlist`(可 per-guild)、`name_aliases`(L0 用,如 ["gura","古拉","鯊鯊"])、`owner_discord_id`(身分綁定)、`always_wake_channels`。

### 3.3 全記錄(ambient log)

`data/discord/{guild_id}/{channel_id}/{date}.jsonl`,原始訊息事件(含她沒理的、含她自己說的)。**不進 FTS 召回**——這是訓練語料與環境底料,不是她的記憶;她對 Discord 的「記憶」照常走她自己的 NoteMemory/PinSelf(她認為值得記才記)。選擇性把 ambient 內容入憶(如每日摘要)記 §7 deferred。

### 3.4 注意力(L0/L1/L2)與權能

- **L0 硬規則(bridge 端,零成本):** DM、mention、name_aliases 命中、always_wake_channels → 標記 wake。
- **L1 便宜閘(daemon 端,規則制,無小模型):** (a) **engagement window**——她在某頻道發言後,該頻道接下來 `engagement_window_s`(預設 300)內的訊息 wake;她每再發言即重置;期滿自然退場。(b) **合批與防洪**——同頻道 wake 訊息在 `wake_debounce_s`(預設 8)內聚合為一個 perception batch(drain 天然支援);同頻道喚醒率上限。(c) 規則掛點預留 P2(興趣關鍵字、話題延續)。未過閘→只落地。L0 放 bridge(省 IPC 噪音之外的 wake 判斷都在 daemon,因 engagement 狀態在 daemon)——修正:**全量訊息本來就都送 daemon(3.3),故 L0/L1 判斷統一在 daemon 端執行**,bridge 只管轉送與執行;L0 規則所需組態由 daemon 讀。
- **L2 她的判斷:** 過閘訊息成為 ChannelMessage perception,情境化渲染(3.5)給足場合,她的 cascade 決定 Say 或沉默結束(既有能力,零新機制);沉默也記 trace(`silence: true`)。scaffolding 增加外部場合行為描述(描述性,非命令):妳在公開場合聽得到很多不關妳的話,不回是正常的。
- **通道權能(外部攻擊面的結構性防線):** external 通道喚醒的 turn,工具 registry 縮減——**保守集**:Say、Recall、NoteMemory、PinSelf(反思時)、DiscordLookup;**絕不進**:Shell、SpawnWorkflow、SpawnMonitor、InvokeSkill、WriteSchedule(陌生人不能誘導她的手)。`author_is_owner` 的 DM → 完整工具集(主人在外面也能差遣她)。公開頻道即使主人在場也用保守集(公開場合不動手,防 injection 混入)。Grammar 隨 registry 縮減(既有 per-registry grammar 機制)。
- **隱私(公開場合的記憶洩漏):** [Memory context] 自動檢索照跑(她需要記憶來判斷),但 external 公開場合的 situational block 內含描述性提醒(這裡是公開場合,主人的私事不屬於這裡)。記憶私密分級是更根本的解,記 §7 deferred;P1 以情境提醒+dogfood 觀測為之,trace 可稽核。

### 3.5 情境化渲染(situated rendering)

- **Situation 分類(deterministic):** `dm_owner` / `dm_stranger` / `public_mentioned` / `public_engaged`(engagement window 內)/ `internal`(既有場合,不變)。有限模板集,新 situation 需改 code(防模板爆炸)。
- **渲染兩處:** (a) **situational block**——插在穩定 identity 前綴之後(沿用三段組合 seam 的機制與 cache 論證;同 situation 位元組穩定):場合描述+在場者+這個頻道最近在聊什麼(A 管線產物);(b) **event 敘述**——ChannelMessage 依 situation 用不同敘事渲染(「陌生人 X 在 #general 說…」vs「主人私訊妳…」)。
- **A 充實管線(喚醒前,deterministic):** author 是誰+記憶裡有沒有他(memsearch 一次查詢)、頻道 ambient log 尾部摘要(最近 N 句原文,不用 LLM)、場合中繼資料。全部進 situational block/event 敘述。
- **B 查證工具 `DiscordLookup`:** `(op: "history"|"user_info", channel_id, ...)`,bridge RPC 執行,sync 工具(在 IN_TURN_REFEED_TOOLS,結果回饋同 turn)。三面向可見性照鐵律:scaffolding 段落+外部場合 nudge+situational hint。成效由 trace 觀測(P2 裁決強化或維持)。

### 3.6 Trace(finetune 級語意層)

- `data/traces/{date}.jsonl`,每 turn 一筆:`{turn_id, ts, origin_channel, situation, perception_batch(語意層原始資料,非渲染字串), prompt_parts: {identity_hash, current_self_hash, situational_template_id, dynamic_blocks_hash}, think, tool_calls: [{name, args, result_digest}], speech, silence, latency_ms, model_id, prompt_tokens, completion_tokens}`。
- 語意層存原料+模板 id 而非渲染後 prompt 字串——未來換 context 格式可重新渲染訓練樣本。**與 cascade_log 的關係**:cascade_log(regex 抽 think 欄位,dev 觀測)保留不動;trace 是語料 SoT,兩者互不依賴。**永不 FTS**(結構測試同 self_profile 模式)。按日輪替、不設上限(語料就是要留)。寫入失敗 loud(語料是 first-class,不靜默吞)但不斷 turn(記錄失敗不該讓她失語——pins-only swallow 的同款取捨,明文)。

### 3.7 服務化與 CLI

- systemd **user units**:`dollos-llm.service`(llama-server 大模型)、`dollos-embed.service`、`dollos.service`(daemon,After=llm)、`dollos-discord.service`、`dollos-voice.service`(現有 bridge,選配)。
- `dollosctl`(bash 或 python 單檔):`start|stop|restart|status|logs [unit]`,包 systemctl --user;`dollosctl check` 驗 config+連通。
- CLI:`experiments/ws_client.py` 升格 `scripts/dollos_cli.py`:互動模式含歷史、串流顯示、Ctrl-C interrupt(既有 Interrupt 訊息)。debug 用,internal 通道。

### 3.8 P2 邊界(注意力調參)

輸入=trace(她醒了說/沒說什麼)× ambient log(她沒醒時錯過什麼);方法=人工標註該醒沒醒/不該醒醒了 + L1 規則迭代(興趣關鍵字可接 self_profile——注意力由演化中的自我定義);B 工具成效裁決。不預設新機制,證據說話。

### 3.9 P3 邊界(語音通話)

py-cord voice receive/send ↔ daemon 既有 ASR/TTS/Opus 管線(Phase B/C 資產);通話中「該不該說話」沿用注意力+情境(situation: `voice_call`);**前置綁定:延遲壓縮**(roadmap 既有欠帳,P3 打包處理到通話可忍);spec 細節 P1 上線後以實際經驗補寫。

## 4. What does NOT change

Daemon 核心迴圈、記憶系統、self_profile/慢變演化、pack、no-fallback/friendly-error/Doll-sovereign 家規、無小模型原則(L1 是規則不是 LLM)、現有 internal 客戶端(CLI/voice/UI spike)行為(僅補通道型別宣告)。

## 5. 威脅模型與邊界

- **Discord=第一個常駐外部攻擊面**:陌生人訊息直接進她的 context。防線分層:通道權能(3.4,結構性——外部喚醒的 turn 摸不到 Shell)、內容 provenance(ChannelMessage 天生 external;她在外部 turn 寫的 pin 自動 `external_ctx: true`)、情境提醒(軟)、trace 全程可稽核。residual:保守工具集內的 NoteMemory 仍可被誘導寫入毒記憶——既有 provenance 體系承接(consolidation/keeper 已按 external_ctx 降權),明文接受。
- **傳輸隱私**:對話經 Discord 伺服器(第三方雲)。大腦與記憶仍在本機(computer-as-home 不變),但傳輸層出門——使用者已知情接受(memory 記錄)。token 在本機 config,不入 git(config.toml 已 untracked)。
- **她的身分一致性**:P8 persona 機制照常涵蓋外部通道輸出。
- **多對話並發**:MindLoop 單迴圈依序處理 perception batch——Discord 多頻道同時喚醒時天然序列化,回覆按 origin 定址不串台;llm.max_concurrency 既有閘不變。

## 6. 驗證

1. **單元/TDD**:ChannelRegistry/路由、ChannelMessage、L0/L1(window/合批/防洪)、situation 分類與模板、A 管線、權能縮減(external turn registry 不含 Shell——結構測試)、DiscordLookup、trace schema/永不 FTS/失敗不斷 turn、dollosctl check、身分綁定升格。
2. **Live smoke(P1 完成 gate,軟機制必 live smoke)**:測試伺服器(她+使用者+至少一個第二帳號),真 llama-server:(a) 陌生訊息與她無關→沉默(trace 可證);(b) 叫她名字→回,且回覆路由正確;(c) engagement window——回她一句不 tag,她接著聊;window 過後不再跟;(d) DM(主人)→完整權能;公開頻道誘導她跑 Shell→工具不存在(grammar 層面);(e) B 工具至少一次真實觸發;(f) trace 完整記錄以上全部,含 silence turn;(g) dollosctl 全套起停。
3. **Dogfood 即驗證**:P1 完成定義=她進真伺服器活著,smolGura 清單前兩項不再重演;第三項(語音)P3 收。

## 7. Deferred(記錄,不默丟)

- Ambient log 選擇性入憶(每日摘要入 FTS?)、記憶私密分級(公開場合檢索 scope 的根本解)
- 手機 app / DollOS ROM(主要感官,獨立軌)、doll 社交網路、多 doll 團體、自我修補、finetune 執行(trace 為它鋪料)
- L1 進階鹽度(興趣匹配、話題延續)=P2;語音通話=P3;情境模板擴充(voice_call 等)隨 P3
