# P3 — Discord 語音通話 + 延遲壓縮前置 Design Proposal(待使用者批准)

> **狀態:2026-07-05 草案。** 這是 P3 的 brainstorming 設計提案。使用者不在線,我(Claude)自主探索 + 把架構決策與**明確開放叉路**寫清楚供批准。**未進實作**(尊重 design-before-code 閘)。批准/選定叉路後才 `writing-plans` → SDD。goal 三階段的 P3;驗收 = smolGura「無語音」失敗模式不重演。

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

### 我的建議:**叉路 A**(但把 voice 引擎打包成 bridge 可選 sidecar)
理由:(1) **與 P1c 注意力天然契合** —— voice 話輪變帶 author 的 ChannelMessage,直接吃 L0/L1/L2 + engagement + disengage,不必為 voice 另立注意力;(2) transport 零新增(不用碰「WS 拒 binary」這個硬限制,不用 N 個 aiortc peer);(3) latency 少一跳(ASR 貼音訊)—— 對「延遲壓縮前置」有利。**代價是模型搬進 bridge**;緩解:bridge 的 voice 引擎做成**可選 sidecar / 同進程 lazy-load**(如同 py-cord lazy import),character-pack voice 綁定用同一 `voice/pack.py` 讀取。若你更看重「模型單一處/daemon 中心」,則選 B(我會改設計)。

**其餘設計兩案共用**(下列)。

## 4. 兩案共用的設計

- **voice 話輪 → 注意力**:一段 endpoint 完的語音 utterance = 一個 perception。走 P1c admission:situation=`voice_call`,author=Discord user_id,participant_set/engagement 照 P1c(進語音頻道被 @/叫名 → 開 session → 續聊不用再叫名);disengage 閘防她在通話裡搶話停不下來。**voice call 的「該不該回」= P1c 注意力,不另做。**
- **[缺口] TTS 對 external origin 不發聲**:現 `TTSObservingSink` 只對 `TextChunk`(internal)觸發 TTS,對 `AddressedText`(Discord origin)不發聲(`voice/sink.py:38`)。P3 必修:voice_call turn 的 `AddressedText`(或新 voice-reply 型別)要觸發 TTS → 送回該語音頻道。
- **N-participant turn-taking**:每 Discord 參與者一條 VAD/utterance FSM(py-cord voice receive 給 per-user stream);speaker 歸屬用 Discord user_id(先不做聲紋)。她說話時的 barge-in / 插話處理(她講到一半有人說話)—— 沿用既有 interrupt/preempt 語意(P1b owner preempt)。
- **situation=voice_call 渲染**:P1d 情境渲染的一個 situation;prompt 標「妳在語音通話中,聽到 X 說…」。P1e origin_tier 安全閘照樣套(voice 頻道是 external_public → 保守工具集 + 記憶 scope)。
- **finetune trace**:voice 話輪照 P1f trace(ASR text 進 perception_batch、TTS 回覆進 speech;audio 本身不入 trace,text 層足夠訓練)。

## 5. 延遲壓縮前置(goal 明列「含延遲壓縮前置」)

通話對延遲敏感(文字可等,語音不能)。既有欠帳:`docs/superpowers/plans/2026-06-02-latency-compression-think-restructuring.md`(**未執行**,全 `- [ ]`)+ 其 design。核心構想:voice-first grammar 讓模型選 **REFLEX 分支(零 think 立刻說)** vs 完整 **DELIBERATE 分支(SEEN/INTENT/REVIEW/MOOD/TOOL)**。
- **P3 打包**:把這條欠帳拉進 P3 —— 通話中允許 REFLEX 快路徑(簡單回應零 think 秒回),複雜才 DELIBERATE。這是 goal「延遲壓縮前置」的實體。
- 現有 sub-turn TTS streaming(SentenceChunker)已壓 TTFT-to-first-audio,是基礎;REFLEX 分支壓的是 think 段的 TTFT。
- **開放**:REFLEX/DELIBERATE 的 grammar 分支要不要只在 voice_call 開,還是全域?(voice 先,文字保守)。

## 6. 明確開放決策(給你)

1. **架構叉路 A vs B**(§3):voice 模型跑 bridge(A,我建議)還是 daemon(B)?這決定整個 P3 骨架。
2. **latency 壓縮範圍**:REFLEX 分支只 voice_call 還是全域?
3. **speaker 歸屬**:P3 先用 Discord user_id(不做聲紋),聲紋(`project_speaker_id`)延後?(我建議是)
4. **多人語音的她該不該說**:通話中沿用 P1c 注意力就夠,還是 voice 需要額外的「搶話/讓話」禮貌閘(P2 用真實通話數據調)?

## 7. 建議下一步

- **先 dogfood P1(我的首選)**:P1 文字存在感一跑,P2 注意力調參 + P3 voice 都有真實地基(通話禮貌閘、latency 目標值都需真實體感)。P3 spec §細節,goal spec §3.9 自己也寫「P1 上線後以實際經驗補寫」。
- **或**:你選定 §6 的叉路(尤其 #1 A/B),我把本提案收斂成正式 spec → `writing-plans` → SDD 開 P3。

**這份是提案、非既成事實。** P3 是大階段、且 §6 的叉路(尤其模型跑哪)是你當設計夥伴該拍板的。我不把架構悄悄寫進 code。
