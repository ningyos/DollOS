# P3 — Discord 語音通話 + 延遲壓縮前置 Design Proposal(v2,R1-hardened,待使用者批准)

> **狀態:2026-07-05 草案 v2(經一輪 opus 對抗式審查收斂)。** P3 的 brainstorming 設計提案。使用者不在線,我(Claude)自主探索 + R1 對抗審查 + 把架構決策與**明確開放叉路**寫清楚供批准。**未進實作**(尊重 design-before-code 閘)。批准/選定叉路後才 `writing-plans` → SDD。goal 三階段的 P3;驗收 = smolGura「無語音」失敗模式不重演。
>
> **R1 收斂重點(v1→v2):**(1) 架構叉路加入 **選項 C**(bridge endpoint→utterance PCM over 既有 WS→daemon ASR/TTS),它在**同機部署**下完勝 A 與 B —— 原 v1 推 A 是拿稻草人 B 比出來的、且 A 的「少一跳」在 localhost 可忽略、模型搬進 bridge 有 OOM 風險。**改推 C。**(2) 撤回「voice 完全走 P1c 注意力」—— 對即時語音 debounce 有害、disengage 會通話中途靜音、真正的 **turn-taking 是 P3 要新建的子系統**。(3) latency 重新界定(endpoint + debounce + TTS-TTFT 三項,非只 reflex;+speculative-decoding 姊妹 plan)。(4) 揭露隱藏依賴 P1d + voice→external_public 記憶失憶。(5) **P3 是 EPIC(6-7 概念),要拆。**

## 1. 目標與範圍

讓 Doll 加入 Discord **語音頻道**通話:聽得到頻道內 N 人說話、用注意力判斷該不該回、用她的聲音回應;含 goal 明列的**延遲壓縮前置**(通話對延遲敏感,現有回應延遲需壓到「通話可忍」)。

**scope**:py-cord voice 加入/離開語音頻道 + 收發音訊、voice 話輪接進 P1c 注意力(situation=`voice_call`)、TTS 回覆送回語音頻道、latency 壓縮(既有欠帳 plan 打包)。
**非 scope**:speaker ID(聲紋辨識,memory `project_speaker_id`,可 P3+ 或用 Discord user_id 代)、zero-shot wake word(另條線)、跨平台(Discord voice 先)。

## 2. 已有資產(重用,非重寫)

**Phase A-C voice pipeline(`src/dollos/voice/`,roadmap step 26-28,已 merged)**:
- ASR:`ASREngine.transcribe(pcm, sr) -> str`(sherpa-onnx SenseVoice,batch)。
- TTS:`TTSEngine.synthesize(text) -> AsyncIterator[pcm]`(多引擎:piper/fish/luxtts/qwen3,streaming ~10-20ms chunk)。
- VAD:`SileroVAD.speech_probability(chunk)` + `UtteranceStateMachine`(endpoint FSM)。
- `VoiceSession`(`voice/session.py`):**aiortc** peer(offer/answer/ICE)、inbound PCM utterance 緩衝 → ASR → `_on_user_text(text)`、outbound PCM streaming track、`enqueue_speak(text)`。
- 本地 mic-speaker bridge(`voice/bridge/`,Phase C)—— 獨立 CLI,開真硬體 mic+speaker,aiortc 到 daemon。**與 Discord 無關**,但證明整條 audio↔ASR↔cascade↔TTS↔audio 迴路可跑。
- daemon 已接:任何 WS client 送 `WebRTCOfferIn` → `VoiceSession` → ASR 產 `UserSpoke` perception(與文字輸入同 kind)。

**P1c 注意力(已 merged)**:`AttentionGate`(L0/L1/L2 + engagement session + disengage + debounce)。**voice 話輪可完全走這套** —— 一個 voice 頻道 = 「N 個帶語音前端的參與者」,每人說一句 = 一個帶 author 的 perception 進 admission。

**latency telemetry(已有)**:`LLMCallRecord.latency_ttft_ms/total_ms`、per-pass `latency_ms`(P1f trace)、`bucket_thought_weight`(latency→mood signal)。sentence 級 TTS streaming(`SentenceChunker`)已讓 TTFT-to-first-audio sub-turn。

## 3. 核心架構叉路(**需要你決定**)

**問題**:Discord 語音音訊怎麼接到 daemon 的 ASR/TTS?—— 這決定 voice 模型跑哪裡、transport、是否重用 `VoiceSession`。

### 叉路 A —— bridge-local voice 前端(bridge 跑 ASR/TTS/VAD,只傳 text)
Discord voice(py-cord voice receive,per-user Opus stream)→ **bridge 端** VAD+ASR → 帶 author 的 text → daemon 走**既有** `ChannelEvent`/perception 路(situation=voice_call)→ daemon 回覆 text(`AddressedText`)→ **bridge 端** TTS → Discord voice 送出。
- **優**:transport 零新增(重用既有 text WS + P1c 全套注意力,voice call = 帶語音前端的 N 文字頻道,最優雅);audio 不過 daemon↔bridge 線(頻寬小);ASR/TTS 貼近音訊(latency 少一跳)。
- **劣**:voice 模型(ASR/TTS/VAD,含 GPU)要搬進/複製到 **bridge 程序** —— bridge 從瘦變胖、需 GPU、模型兩地(daemon 也有一份給本地 mic bridge)。character-pack 的 per-pack voice 綁定要在 bridge 重現。

### 叉路 B —— audio 傳到 daemon(重用 `VoiceSession`,模型留 daemon)
Discord voice → bridge 收 Opus → 每參與者一條音訊串到 **daemon**(新 transport:每 Discord 參與者一個 aiortc peer,或新 binary channel —— 現 WS 明文拒 binary)→ daemon 既有 `VoiceSession`/ASR/TTS 處理 → 回音訊 → bridge → Discord。
- **優**:voice 模型留 daemon 一處(GPU 一處、character-pack voice 綁定不動);重用 Phase B/C 的 `VoiceSession` 整套。
- **劣**:需新 audio transport(現 WS 拒 binary → 要 N 個 aiortc peer per Discord 參與者,或新協定);多人語音(N 人)對 1-peer 的 `VoiceSession` 要 N 化 + speaker 歸屬;audio 過 daemon↔bridge 多一跳(latency)。

### 叉路 C —— bridge endpoint + utterance PCM over 既有 WS(**R1 新增,改推此案**)
Discord voice(py-cord voice receive,per-user stream)→ **bridge 端**用**既有** `SileroVAD` + `UtteranceStateMachine`(`voice/bridge/`,已存在)做 endpointing → 一段 endpoint 完的**離散 utterance PCM**(base64-in-JSON)+ user_id → 走**既有 text WS** 到 daemon → daemon 用**既有** ASR(`transcribe()` 本就是 utterance-batch)產帶 author 的 perception → daemon TTS PCM 同法回 bridge → Discord 送出。
- **模型留 daemon 一份**(勝 A:無 bridge GPU、無權重複製 OOM 風險、character-pack voice 綁定不動,合 parent-spec「daemon 保持純淨」)。
- **無新 binary transport、無 N 個 aiortc peer、無 VoiceSession N 化**(勝 B:C1 指出 B 的「audio 多一跳」只在**連續串流**才痛,C 送**離散 endpointed utterance** 避開了)。
- **同機 localhost 下,per-utterance base64 一跳(~100-200KB)幾近免費** —— A 的「少一跳 latency」在 localhost 可忽略(R1-C1)。
- **代價**:utterance PCM base64 過 WS(需給既有 text-only WS 加一個 utterance 訊息型別;非 binary frame,故不碰 `server.py` 拒 binary 的硬限制)。bridge 需 Silero VAD(輕,已在 `voice/bridge/`,非 GPU 大模型)。

### 修正後的建議:**叉路 C**(非 v1 的 A)
R1 指出 A vs B 是假二選一:A 的 localhost「少一跳」可忽略、模型搬進 bridge 有 OOM 風險(fish/qwen3-tts 拉整個 torch+CUDA,和 35B MoE 搶 VRAM);B 最重(N peer + VoiceSession 單參與者結構要 N 化)。**C 在同機部署的三個關心軸(daemon 中心、transport 便宜、latency)全勝** —— 故改推 C。**但注意**:三案的「voice 音訊怎麼進 ASR」不同,但下面 §4/§5 的**真正新工作(turn-taking、reply-routing、latency)三案共用且都比 v1 想的大**。

### G5 一個 favor Discord voice 的點(R1 補)
Discord 給 per-user **數位**串流、且**不把 bot 自己的音訊迴授** —— 折磨本地 mic bridge 的**回音消除問題在此不存在**。Discord voice 比本地 voice 好做,且此點微偏 C(ASR/endpoint 放在 per-user 串流已在的地方=bridge)。

## 4. voice 話輪的設計(R1 大改:turn-taking 是新子系統,不是 P1c)

**v1 的「voice 完全走 P1c 注意力、不另做」被 R1 駁回(C2)。** 為什麼不成立:
- **L0 訊號大半失效**:語音沒 `mentioned`/`reply_to_bot`,cold-wake 只剩 `l0_name`(ASR 文字出現「Gura」)或 `l0_always`。
- **debounce 對即時通話有害**:P1c engaged 2s / cold 8s 窗批次化,疊在 VAD 800ms endpoint 上 = 她**已知對方講完**後再等 2-8s。VAD endpoint **就是**「講完了」訊號,debounce 冗餘且致命延遲。
- **disengage 閘會通話中途靜音**:`max_session_turns` 到了刪 session → 她在**同步通話中突然沉默**到有人再叫名 = 社交破壞。`always_wake` 逃過靜音但只剩有害的 debounce。
- **真正的問題 turn-taking 完全沒模型**:即時多人通話「現在該不該說」(有人講到一半嗎、問題落她身上嗎、有空檔可接嗎、該讓話嗎)`AttentionGate` 無此概念;barge-in「沿用 P1b preempt」是 **owner-only**(陌生人講不停她)。

**修正結論:turn-taking 是 P3 要新建的子系統。** 借 P1c 的部分(name-wake、participant 概念),但要新做:VAD-endpoint-驅動的話輪邊界(取代 debounce)、通話中讓話/搶話政策(非 owner-only preempt)、通話級 disengage(禮貌沉默 ≠ 離場)。**voice 的「該不該說」是新機制,P2 用真實通話數據調。**

**其餘要件(R1 校正大小)**:
- **[G3 真正的新工作] voice-vs-text reply routing**:現 `on_daemon_message` 無條件 text-send `AddressedText`(`controller.py:222`)。voice 回覆要走 TTS→語音頻道,需**新 reply 型別**(`SpokenText`/`VoiceReply`)或 channel-kind 查詢 —— 非 v1 說的「trivial 修 TTSObservingSink」(那缺口在 C 案下 daemon 根本不 TTS Discord 音訊,是 B 案專屬)。
- **[G4 需 spike,非既成資產] py-cord voice receive**:Discord **官方不支援** bot 收語音,實作逆向工程、跨版本脆弱;`discord.sinks` 是**錄檔導向**,即時串流要自訂 streaming Sink。**P3 第一步是 de-risk 這個 spike**,不是假設可用。speaker 歸屬先用 Discord user_id(不做聲紋)。
- **[G6] barge-in / full-duplex**:她 TTS 時偵測有人說話,需 bridge 對 inbound 跑 VAD while speaking;`abort_speak` 存在但只綁 owner-preempt。誰能打斷、多快 —— 未定,新工作。
- **[G1 隱藏依賴] situation=voice_call 需要 P1d**:目前**無 P1d plan 檔、code 無 situation 分類器**。P3 把 P1d(parent-spec §2「可稍後」)拉上關鍵路徑 —— 要嘛先做 P1d 最小 situation 軸,要嘛 P3 自帶。
- **[G2 未檢視 UX 代價] voice→external_public 記憶失憶**:無 voice_call situation 時多人語音渲染成 external_public → P1e 套保守工具 + 記憶 scope 過濾 + 抑制 auto-`[Memory context]` → **通話中預設記憶失憶**(連主人在場也撈不到共同回憶)。voice_call 可能需 owner-在場放寬記憶 scope。
- **finetune trace**:voice 話輪照 P1f(ASR text 進 perception_batch、TTS 回覆進 speech;audio 不入 trace,text 層足夠訓練)。

## 5. 延遲壓縮前置(R1 重新界定:reflex 只是三項之一,不是 the lever)

**v1 只提 reflex/deliberate 被 R1 駁回(C3):語音延遲被三項主導,reflex 一項都沒碰**:
1. **VAD endpoint 靜音等待**(固定 800ms,`voice/bridge/controller.py:33`)—— 可調短(準確度換延遲)。
2. **debounce 批次窗**(2-8s,§4 指出對語音有害)—— voice 應設 ~0 / 只用 endpoint。
3. **TTS TTFT**(首音)+ ASR compute + 網路 RTT。
- reflex/deliberate(`2026-06-02-latency-compression-think-restructuring.md`,未執行)壓的是 **think 段 token 生成**,是**文字**微優化,不碰前兩大項。
- **姊妹欠帳**:`2026-06-02-latency-compression-speculative-decoding.md`(v1 漏提)—— 投機解碼壓 LLM 原始生成速度,對通話更相關。
- **Self-First 侵蝕風險**(R1 補):reflex = speak-only 無 tool、跳 MOOD/REVIEW;通話多數 turn 走 reflex → 她通話中不更新 mood、不自省、不能 Recall/NoteMemory → 拉平 Self-First。需明文取捨。
- **P3 latency 的實體 = endpoint 調短 + voice debounce≈0 + TTS-TTFT + (reflex/speculative 擇一)**,非只 reflex。目標值(「通話可忍」)需真實通話體感 → P2/dogfood 定。

## 6. 明確開放決策(給你,R1 更新)

1. **架構叉路(§3,R1 改寫):A(模型 bridge)vs B(audio→daemon)vs **C(bridge endpoint→utterance PCM over WS→daemon,R1 新增,我改推此案)**。** 決定 P3 骨架。
2. **turn-taking(§4,R1 新識別的子系統)**:接受「voice 的該不該說是新機制、非 P1c」?VAD-endpoint 話輪邊界 + 讓話政策 + 通話級禮貌沉默(非 mid-call 靜音)。
3. **latency 範圍(§5)**:endpoint 調短 + voice-debounce≈0 為主;reflex vs speculative-decoding 擇一;是否接受 reflex 的 Self-First 取捨?
4. **P1d 依賴(G1)**:先補 P1d 最小 situation 軸,還是 P3 自帶 voice_call situation?
5. **voice 記憶 scope(G2)**:owner-在場通話放寬 external_public 的記憶失憶?
6. **speaker 歸屬**:先 Discord user_id、聲紋延後?(我建議是)

## 7. 範圍:P3 是 EPIC,要拆(R1-scope)

R1 判定 P3 不是單一階段(違「每 plan 一個新概念」)。至少 6-7 個單概念 plan:
- **P3a**:py-cord voice join/leave + voice-receive **spike**(先 de-risk G4 —— 官方不支援、脆弱)。
- **P3b**:per-user VAD/endpoint/attribution(bridge,重用 `voice/bridge/` SileroVAD)+ utterance-over-WS transport(選 C)。
- **P3c**:voice-vs-text reply routing(新 reply 型別 + TTS→語音頻道,G3)。
- **P3d**:**turn-taking 子系統**(VAD-endpoint 話輪 + 讓話政策 + 通話級 disengage;新,非 P1c,C2)。
- **P3e**:barge-in / full-duplex(G6)。
- **P3f**:latency(endpoint 調短 + debounce≈0 + reflex/speculative,C3)。
- **(前置)P1d 最小 situation 軸**(voice_call,G1)+ voice 記憶 scope(G2)。

## 8. 建議下一步

- **先 dogfood P1(我的首選,R1 也強化此點)**:通話禮貌閘、latency 目標值、debounce-vs-0 —— 全需真實通話體感,現在都是猜。goal spec §3.9 自己寫「P1 上線後以實際經驗補寫」。
- **或**:你拍板 §6 的叉路(尤其 #1 的 **A/B/C**、#2 接受 turn-taking 是新子系統),我把本提案收斂成正式 spec + P3 EPIC 拆解 → `writing-plans` → SDD。

**這份是提案、非既成事實。** R1 已把 v1 的三個 mis-frame 挖出(假二選一漏了 C、誇大 P1c 重用藏了 turn-taking、latency 認錯項)。P3 的架構決策是你當設計夥伴該拍板的 —— 我不把架構悄悄寫進 code。
