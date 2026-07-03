# DollOS MVP — Doll 活在 Discord(backbone + 情境化渲染 + 語料底盤)Design

Status: draft v2(goal-driven,2026-07-03)。MVP 總 spec:Phase 1 完整設計 + P2/P3 邊界。**R1 對抗審查 3/5 lens 已套用**(security 4C、architecture 1C+5I、trace-finetune 2C+3I = 7 Critical,全數修入下方標 [R1]);attention-smolgura + scope-coherence 兩 lens 撞 session limit 待補(2:20pm UTC 重置後 resume),但 attention 核心的 code-閘判斷已先修入 §3.4[R1-att]。R2 前補齊那兩 lens。

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

- **ChannelRegistry**(daemon 端,IPC 層):每條連線註冊 `{channel_id: str, locus: "internal"|"external", kind: "text"|"voice"|"discord"|"ui", addressable: bool}`。
- **[R1-arch C1] 多源路由是新機制,不是既有性質。** 現行 `PerceptionQueue.drain()`(perception_queue.py:41-73)是 origin-blind:第一個 perception 到就把「當下已排隊的全部」併成一 batch → 一 prompt → 一 sink(mind_loop.py:612 單次 resolve)。原 spec §5「多對話天然序列化不串台」是**假的**——兩頻道同時有訊息會併成一個 turn、一個 origin、一個 sink。必做 **per-origin turn segmentation**:drain 後 group-by-origin-channel,每個 origin bucket 跑一次 cascade + resolve 一次該 origin 的 sink。這是 P1 backbone 的核心新工作,不可低估。所有下游(situation 選擇、sink 定址、trace 的 origin_channel)都 gated on「turn 是單源」。
- **[R1-arch I2] SinkResolver 要加 locus/origin,否則 bridge 一連內部輸出靜默丟失。** 現行 `SinkResolver.__call__`(sink_resolver.py:46-50)零參數回**所有** sink 中最近註冊的。bridge(較高 handle)一連上,所有 origin-less 內部 turn(idle/scheduled/reflection 的 turn-end 與串流)會 resolve 到 **bridge** sink;bridge 只認 `AddressedText`,裸 `TextChunk` 無 channel target → **內部輸出送不出去、靜默丟失**。故 locus-filtered resolution 是**必需**、非「維持現狀」:`register(sink, locus, channel_id)` + `__call__(origin)`;串流路徑(mind_loop.py:846/869 硬編 `TextChunk`)在 external origin 時改發 `AddressedText`(熱路徑真改動,非 reuse)。internal origin 或無 origin → 最近 **internal** sink。
- **回覆路由:** turn 的 origin channel 進 MindLoop 狀態;Say/串流輸出經 SinkResolver 依 origin 定址。
- **Perception 擴充:** 新 kind `ChannelMessage`(external 通道訊息),data 攜帶 `{channel_id, guild, channel, author, author_id, content, mentioned, is_dm, msg_id, ts, situation, author_is_owner}`。加進 `mind_state.py:74` 的 closed `Literal`(觸 ~21 個 kind-match site,含 `_percep_body`;附帶:safe_mode 自清與 user-transcript append 是 UserSpoke-gated,Discord-only 場景不自清 safe mode 是已知小缺,補一條 ChannelMessage 也觸發自清)。
- **[R1-sec S1] `ChannelMessage` 必進 external_ctx 判定。** 現行 `_EXTERNAL_KINDS`(mind_loop.py:75)只含 ToolResultArrived/MonitorFired/MonitorEnded;不加 ChannelMessage,則她在 Discord turn 寫的 pin 以 `external_ctx: false` 落地,與面對面反思無法區分,keeper 的「external_ctx 降權」對 Discord 毒 pin 完全空轉。修:`ChannelMessage` ∈ `_EXTERNAL_KINDS`;且 `DiscordLookup` 結果比照 Recall 觸發 in-turn external_ctx 升級(mind_loop.py:682 目前只 special-case Recall)。
- **`UserSpoke` 保留給 internal 通道**:cancel 語意(打斷 consolidation/evolution)、`user_turn_count` 只綁 UserSpoke——陌生人在 Discord 講話不打斷她的睡眠整併、不是「主人回來了」。**owner 例外**(見 3.4 身分綁定,綁 numeric `author_id` 非顯示名):owner DM 標 `author_is_owner: true`,daemon 升格為 UserSpoke 等效 cancel 語意,perception 仍為 ChannelMessage。**注意此升格讓遠端可 cancel 她的 consolidation/evolution + 重置 energy**——被盜帳號的 DoS 向量,接受但記錄。
- **[R1-arch I4] energy 消耗要 origin-aware。** 現行消耗是 produced-based(mind_loop.py:400,每 productive turn 燒 0.05),與 UserSpoke 無關;回充 key `last_user_at`(consolidation.py:159),Discord turn 不推進 `last_user_at` → 回充器誤判她 idle 一直回充。淨效果:她在 Discord 聊天狂燒又狂補,energy→consolidation gating 在主場景失效。修:energy 消耗改 **origin-aware**(external-非-owner turn 不燒她的「與主人相處」energy,或另立 social-energy 軸——P1 最小:external turn 不計入 energy 消耗與 idle 判定,語意=「跟外人社交不算她跟主人的精力帳」)。owner 升格只修 owner 個案,不修陌生聊天。
- IPC messages 擴充:`ChannelRegister`(client→daemon)、`ChannelEvent`(client→daemon,攜 ChannelMessage payload)、`AddressedText`(daemon→client,`{channel_id, target: {guild, channel}, text}`)。既有 TextInput/TextChunk 流程不動(internal 通道用)。

### 3.2 discord-bridge 程序

- `python -m dollos.discord_bridge --config config.toml`。py-cord;職責:(a) 事件轉送——白名單 guild/channel 全量送 daemon 落地,L0/L1 判斷在 daemon(3.4);(b) 回覆執行——收 `AddressedText` 送回指定頻道(長訊息切段);(c) 查證 RPC 執行端——`DiscordLookup` 的 API 呼叫;(d) P3 語音。斷線自動重連+重連後 backfill(有上限、去重防 msg_id 重複落地);bridge 崩潰不傷 daemon,systemd 自動拉起。
- **[R1-arch I5] DiscordLookup 的 RPC 是新 IPC 原語,不是「像 Recall 的 sync 工具」。** Recall 是 in-process;DiscordLookup 需 daemon→bridge **request-response round-trip**,而現行 IPC(messages.py 只單向 client→daemon 輸入 + 單向輸出;voice bridge 先例是單向 signaling)**無 correlation/request-id**。需新做:correlation id + timeout + bridge-down 處理,且要在 cascade 內 `_dispatch_tool`(mind_loop.py:882)阻塞等回。這是 P1 backbone 的一塊,不可當「加個工具」。
- **組態 `[discord]`**:`token`(on-device config,不出門)、`guild_allowlist`、`channel_allowlist`(可 per-guild)、`name_aliases`(L0 用,如 ["gura","古拉","鯊鯊"])、`owner_discord_id`(身分綁定)、`always_wake_channels`。

### 3.3 全記錄(ambient log)

`data/discord/{guild_id}/{channel_id}/{date}.jsonl`,原始訊息事件(含她沒理的、含她自己說的)。**不進 FTS 召回**——這是訓練語料與環境底料,不是她的記憶;她對 Discord 的「記憶」照常走她自己的 NoteMemory/PinSelf(她認為值得記才記)。選擇性把 ambient 內容入憶(如每日摘要)記 §7 deferred。

### 3.4 注意力(L0/L1/L2)與權能

全量訊息都送 daemon(3.3),故 L0/L1/L2 判斷統一在 daemon 端;bridge 只管轉送與執行(L0 所需組態由 daemon 讀)。

- **[R1-att] 核心修向:預設沉默,code 側 reply-worthiness 才進 L2。** 原 spec 把「該不該回」整個交給 L2 弱模型裸 cascade + 一句 scaffolding,**違反自己立的鐵律**(`ref-weak-model-soft-mechanism-playbook`:prompt 管不住的語意升 code 閘),且重演 PinSelf 0/3 / SelfRevision 0/10 的 over-act = smolGura 失敗模式 1。改為:**預設沉默**,只有 code 側 reply-worthiness 訊號才讓訊息升進 L2 判斷。L2 從「決定要不要說」降級成「已有理由回應時、決定要不要開口 / 說什麼」。
- **L0 硬規則(reply-worthiness 最強訊號,必進 L2):** DM、mention、name_aliases 命中、reply 到她的訊息、always_wake_channels。
- **L1 便宜閘(daemon,規則制,無小模型):** (a) **[R1-att] engagement window 綁「對話串」非「整個頻道」**——原設計對整頻道開 300s 窗 × 多人 × 弱模型傾向回應 = 亂插不干她的話。改為 window 只涵蓋 **reply-chain 內延續她的訊息 + 她主動點名/回覆的對象**;頻道級全體訊息**不因她開過口就自動進 L2**。她每在該串再發言重置;期滿退場。(b) **[R1-arch I1] 合批需新的 timed accumulator**——`drain()` 無 post-first 時間窗(perception_queue.py:63 的 timeout_s 只是首個等待上限),`wake_debounce_s`(預設 8)的同頻道合批要新做一層(bridge 端或 daemon L1 持有同頻道訊息延遲入列);這層也正是 C1 單源 batch 的來源,兩者同一機制。同頻道喚醒率上限防洪。(c) 進階鹽度(興趣關鍵字接 self_profile、話題延續)= P2,靠真實數據調——注意力由演化中的自我定義。未過閘→只落地(3.3)。
- **L2 她的判斷:** 升進的 ChannelMessage 成 perception,情境化渲染(3.5)給足場合,cascade 決定 Say 或沉默結束(既有能力);沉默記 trace(`silence: true`)。scaffolding 加外部場合描述(描述性非命令):妳在公開場合聽得到很多不關妳的話,不回是正常的。
- **[R1-sec S4/S5] 通道權能(結構性防線,不論身分):** external 通道喚醒的 turn,工具 registry 縮減至**保守集**:Recall、NoteMemory、WriteDiary、PinSelf(反思時)、DiscordLookup。**任何 external channel(含 owner DM)結構性禁止 Shell / SpawnWorkflow / SpawnMonitor / InvokeSkill / WriteSchedule**——原設計「owner-DM → 完整工具集」等於 **Discord 帳號被盜即家用電腦 RCE**,而 Discord 帳號是第三方單因子、不在使用者控制內,reviewer 直接判**不可接受**(且與 memory `project_biometric_auth`/`project_lock_screen_interaction`「危險操作需本機認證」矛盾)。owner DM 得到的是「升級但非 RCE」(保守集,「差遣」語意保留);真要遠端 Shell,gate 在綁**本機控制裝置**的 out-of-band 第二因子(local UI/手機 app 確認),非 Discord 單因子——列 §7 deferred。**Say 不是工具**(自由文字,不在 registry;真正的縮減是 registry 內工具缺席 + grammar 縮減)。**[R1-sec S5] 縮減必須贏過 reflection 展開**:現行 `_active_tool_registry`(mind_loop.py:510-533)只有 safe_mode/reflection 兩軸、無 origin 軸;external turn 撞 ReflectionMoment 會把 Shell + **SelfRevision** 全放回來(SelfRevision 是毒化鏈最終閘)。故 external-conservative 以 intersection/override **贏過** reflection 展開,明確排除 SelfRevision 於 external turn。實作要把現行 3-slot 硬編 grammar cache 一般化成 keyed cache(key = tool-name frozenset),新增 external mode 軸;gated on C1 單源 turn。
- **[R1-sec S2/S3] 記憶毒化 + 私事外洩:P1 就上粗粒度 provenance/scope,不整條 defer。** (S2)`NoteMemory` 寫 shared/ 入 FTS **零 provenance**,陌生人誘導寫的假記憶會跨 internal/external 邊界被日後面對面 turn 撈進 `[Memory context]`(MINJA-class 跨 session 毒化)——§5 原稱「既有 provenance 體系承接」**與 code 不符**(external_ctx 不碰 NoteMemory 寫入路徑)。修:NoteMemory 寫入依 channel locus 打一個粗粒度 `origin: internal|external_public|external_dm` bit。(S3)公開頻道 `Recall` + auto-`[Memory context]` 對**整個記憶池**檢索含主人私事 = 外洩 oracle,原設計只用軟提醒守——**又違反升-code-閘鐵律**。修:external 公開 turn 的 Recall/auto-context 依上述 bit **scope 過濾**(排除 internal/private origin 記憶)並抑制 auto-`[Memory context]`。這是 §7「記憶私密分級」的最小可行半成品,進 P1。

### 3.5 情境化渲染(situated rendering)

- **Situation 分類(deterministic):** `dm_owner` / `dm_stranger` / `public_mentioned` / `public_engaged`(engagement window 內)/ `internal`(既有場合,不變)。有限模板集,新 situation 需改 code(防模板爆炸)。
- **[R1-arch I3] 靜態模板進前綴 cache、動態產物進動態區——原 §3.5 自相矛盾(把動態內容放穩定前綴)。** 現行 compose cache(mind_loop.py:150/199)是**單槽**、只 key sanctioned text,且 composed prefix 是 KV-cache 熱前綴(render_mind → system="" user=prefix⊕…)。修:**拆兩層**——(a) **靜態 situation 模板**(純場合描述,有限模板集、byte-stable per template)進 composed prefix,cache 從單槽改成小 dict 按 `(sanctioned_text, situational_template_id)` 分桶;(b) **動態 A-產物**(在場者、頻道近況 N 句、author 記憶 hits)進**動態區**(跟 `[Memory context]` 同區,mind_prompt.py:107,**不 cache**——它們本就每 turn 變)。§3.6 trace schema 已暗示此拆分(`situational_template_id` vs `dynamic_blocks`)。
- **event 敘述:** ChannelMessage 依 situation 用不同敘事渲染(「陌生人 X 在 #general 說…」vs「主人私訊妳…」)——動態,進動態區。
- **A 充實管線(喚醒前,deterministic):** author 是誰+記憶裡有沒有他(memsearch 一次查詢)、頻道 ambient log 尾部摘要(最近 N 句原文,不用 LLM)、場合中繼資料。全部進 situational block/event 敘述。
- **B 查證工具 `DiscordLookup`:** `(op: "history"|"user_info", channel_id, ...)`,bridge RPC 執行,sync 工具(在 IN_TURN_REFEED_TOOLS,結果回饋同 turn)。三面向可見性照鐵律:scaffolding 段落+外部場合 nudge+situational hint。成效由 trace 觀測(P2 裁決強化或維持)。

### 3.6 Trace(finetune 級語意層)

- **[R1-trace T-C1] pass 為單位,非 turn。** 一個 turn 是 up to 8 pass 的 in-turn refeed cascade(mind_loop.py:43/627),每 pass 有自己的 input context(messages 每 pass 成長:assistant emit + `<tool_response>` 逐 pass 附加)、自己的 raw output、自己的 token counts。finetune 的原子樣本是 **pass**(每 forward pass = 一個 context→output 對);Recall→observe→PinSelf 是 3 個不同 input 的樣本。原 §3.6 的單數 think/tool_calls/speech 把它壓平、丟失多 pass 結構。cascade_log 已是 per-pass(iter=pass_idx,cascade_log.py:651),trace 從**同一 capture point**(mind_loop.py:633 的 raw_buf/results/tool_calls tuple)衍生,成 cascade_log 的 **superset**(不另立平行、不會 drift)。
- **Schema**(`data/traces/{date}.jsonl`,每 turn 一筆 envelope,passes nested):
  ```
  {schema_version, turn_id, ts, origin_channel, situation,
   perception_batch: [...語意層原始資料,非渲染字串],
   static_prefix: {identity_text_or_hash+ref, current_self_text_or_ref, situational_template_id},
   dynamic_blocks: {memsearch_hits:[...實際命中項], associative_hits:[...], tool_habits_hits:[...],
                    situational_A_products:{present:[...], channel_tail:[...實際 N 句], author_memory_hits:[...]},
                    mood, energy, open_loops, recent_perceptions, recent_outputs},  // [R1-trace T-C2] 存實際內容非 hash
   passes: [{pass_idx, input_messages_delta, raw_assistant_emit(全文逐字),
             tool_calls:[{name, args, result_full(全文,非 digest)}],
             refed_tool_responses:[...], prompt_tokens, completion_tokens, latency_ms}],
   speech, silence, model_id}
  ```
- **[R1-trace T-C2] 存內容,不存 hash/digest。** hash 能偵測變化但不能重建——直接廢掉「未來重新渲染訓練樣本」這個目的。`dynamic_blocks_hash` 會把 render_mind 條件的一切(哪些記憶被檢索注入=重建 prompt 所必需)壓成不可還原;`result_digest` 同理,tool result 逐字餵進下一 pass(mind_loop.py:732),digest = 下一 pass input 不可還原。故:動態 block 存**實際值**、tool result 存**全文**(至少所有會 refed 進後續 pass 的)。hash 只當 cache-bucket 額外 key,絕不當內容唯一紀錄。
- **[R1-trace Important] think 存 raw 全文非 `_parse_think`。** 輸出 tokens(完整 `<think>…</think>`+speech+tool-call 序列化)是 finetune target,必須逐字保存。唯一既有 think 抽取器 `_parse_think`(cascade_log.py:29)只 regex 5 行 SEEN/INTENT/... 丟掉其餘推理=截斷 target。存 `"".join(raw_buf)`(mind_loop.py:654 已算好)全文;parsed 欄位僅當附加索引。
- **關係與規則:** trace 從 cascade_log 的同一 per-pass 點衍生(subsumes 之,兩者不 drift;cascade_log 續作 dev 觀測);speech 不與 transcript 重複語意(transcript 是對話層、trace 是訓練層,明文其一為主)。**永不 FTS**(結構測試同 self_profile)。按日輪替不設上限。寫入失敗 loud 但不斷 turn(pins-only swallow 同款取捨,明文)。`schema_version` 每筆必帶(格式明說會變,遷移靠版本 dispatch)。

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

- **Discord=第一個常駐外部攻擊面**:陌生人訊息直接進她的 context。防線分層(**[R1] 全部要真接線,原稿多處空接**):通道權能(3.4,結構性——外部喚醒 turn 摸不到 Shell/SelfRevision,含 owner DM)、內容 provenance(ChannelMessage 已加進 `_EXTERNAL_KINDS`[S1]→外部 turn 寫的 pin 自動 external_ctx;NoteMemory 寫入打 origin bit[S2])、記憶 scope 過濾(external 公開 turn 的 Recall/auto-context 依 origin 排除 private[S3])、trace 全程可稽核。**residual(明文接受)**:保守集內 NoteMemory 仍可被誘導寫毒記憶,靠 origin bit + keeper external_ctx 降權 + Doll adopt 閘承接(降權對 pin 有效;NoteMemory 召回靠 scope 過濾,非降權)。
- **[R1-sec S4] owner 帳號被盜 = 電腦被盜的風險已結構性封堵**:external 通道(含 owner DM)一律無 Shell/SpawnWorkflow/SpawnMonitor/WriteSchedule,故 Discord 帳號被盜的最壞後果限縮為「可 DM 差遣保守集 + 可 cancel 她的 idle passes(DoS)」,非 RCE。owner 綁 numeric `author_id`(gateway 認證、不可偽造),非顯示名(顯示名可改冒充,只污染軟提醒/L0 name_aliases——situational 對非 owner 用 owner-like 名時標「未驗證」)。
- **[R1-sec S10] ambient log = 本機無界 PII sink**:`data/discord/**` 全量含第三方成員明文訊息、無上限。是(a) 磁碟 DoS、(b) 未經同意保存他人訊息當語料的資料倫理問題、(c) 主機被入侵時高價值 exfil 目標。P1 明文交代第三方資料保留姿態,並設 size/retention bound(不與 trace 的不設上限混淆——trace 是她自己的 turn,ambient 是他人訊息)。
- **傳輸隱私**:對話經 Discord 第三方雲;大腦/記憶仍本機(computer-as-home 不變),傳輸層出門,使用者知情接受。token 在本機 config 不入 git。
- **她的身分一致性**:P8 persona 機制照常涵蓋外部輸出。
- **[R1-arch C1] 多對話並發**:MindLoop 單迴圈**不會**天然分源——`drain()` origin-blind,需 §3.1 的 per-origin turn segmentation 才不串台。原稿「天然序列化」是錯的,已在 §3.1 修為明確新機制;llm.max_concurrency 既有閘不變。

## 6. 驗證

1. **單元/TDD**:ChannelRegistry/路由、ChannelMessage、L0/L1(window/合批/防洪)、situation 分類與模板、A 管線、權能縮減(external turn registry 不含 Shell——結構測試)、DiscordLookup、trace schema/永不 FTS/失敗不斷 turn、dollosctl check、身分綁定升格。
2. **Live smoke(P1 完成 gate,軟機制必 live smoke)**:測試伺服器(她+使用者+至少一個第二帳號),真 llama-server:(a) 陌生訊息與她無關→**沉默**(reply-worthiness 預設沉默生效,trace 可證);(b) 叫她名字/reply 她→回,且回覆路由正確(不串台=C1 分源);(c) engagement window——回她一句不 tag,她**在該對話串內**接著聊;串外他人閒聊不插;window 過後不跟;(d) **owner DM 誘導 Shell→工具不存在(grammar 層);公開頻道誘導 Shell 亦然**;owner DM 保守集可用(Recall/NoteMemory/WriteDiary);(e) 公開頻道問主人私事→scope 過濾不外洩;(f) B 工具至少一次真實觸發;(g) trace **per-pass** 完整(多 pass turn 每 pass 一筆 input+raw output+全文 tool result,含 silence turn);(h) dollosctl 全套起停;(i) 內部輸出(排程/reflection)在 bridge 已連時仍正確送達本機 CLI(I2 不靜默丟失)。
3. **Dogfood 即驗證**:P1 完成定義=她進真伺服器活著,smolGura 清單前兩項不再重演;第三項(語音)P3 收。

## 7. Deferred(記錄,不默丟)

- **遠端 Shell 的本機第二因子**(local UI / 手機 app 確認)——external 通道要能安全動手的唯一正解;P1 結構性禁止,此為未來開通路徑(§3.4 S4)。
- **記憶私密分級的根本解**——P1 已上粗粒度 origin bit + scope 過濾(§3.4 S2/S3);細粒度分級(per-memory 敏感度、跨場合可見性規則)是根本解,deferred。
- Ambient log 選擇性入憶(每日摘要入 FTS?);ambient log 保留策略 P1 已設 bound(§5 S10),入憶是另一問。
- 手機 app / DollOS ROM(主要感官,獨立軌)、doll 社交網路、多 doll 團體、自我修補、finetune 執行(trace 為它鋪料)。
- L1 進階鹽度(興趣匹配、話題延續)=P2;語音通話=P3;情境模板擴充(voice_call 等)隨 P3。
