# Spec — B3: Energy System（精力系統）

- **Date**: 2026-06-30
- **Status**: PLAN-READY（含 smoke gate 條件）— R1 3-lens adversarial review 已收斂套用。見 §9。
- **Depends on**: B2 sleep-time consolidation（已 merged；其 `ConsolidationTrigger.run()` 的 5s poll loop 即現成 idle ticker）。
- **Scope**: 只做 B3。與 A1 self-profile 平行、互不依賴。

## 1. 背景與問題

DollOS 沒有「精力/疲累」概念:Doll 不論連續對話多久、或剛「睡」過,回應狀態都一樣。缺一個客觀節律:活躍久了累、休息後恢復。這是使用者明確想要的(consolidation 順便回復 = 睡眠功能)。

**架構事實(修正前版的錯誤前提)**:舊 `persistent-mind-design` 的 energy 綁 `IdleTick`(已被 commit `4a6ff76` 砍)。但**系統並非無 idle 訊號**——B2 的 `ConsolidationTrigger.run()` 是 `POLL_INTERVAL_S=5.0` 的 poll loop,每輪已算閒置(`consolidation.py:141`)。因此 energy 的**消耗**走 `MindLoop.iterate`(eager,事件驅動),**回復**掛 ConsolidationTrigger 的 poll loop(現成 5s idle ticker),不需復活已砍的 IdleTick。

## 2. 設計原則

- **活躍消耗、閒置回復**:Doll 真正做認知工作(產生回應/工具呼叫)時消耗;一段時間沒有使用者互動(休息)時回復。像人:做事累、休息恢復。
- **回復脫鉤 consolidation 成敗(R1 M1 致命修正)**:回復由「閒置」觸發,**不**綁「剛好有舊逐字稿可整併」。否則最常見的「今天聊久→休息」(今天檔未封日、無 target)永遠回不了 energy → 永久黏 0 = dead state。
- **消耗只算真正的認知工作(R1 M1 拍板)**:只有「本 turn 產生了 output(speech 或 tool call)」才消耗;被動 perception(monitor tick / schedule tick 但 Doll 只 Idle)**不**累。回復的閒置基準用 **`last_user_at`**(不含 `last_iter_at`),避免高頻 monitor 餵死閒置訊號。
- **energy 是客觀自動狀態,Mood 由 Doll 主導(對齊 B2 autonomy)**:energy 自動消耗/回復、注入 prompt 讓 Doll **看到**作為設情緒的參考輸入;**不自動改 Mood**。
- **軟性影響,不硬性限制(Self-First)**:低 energy 不禁工具、不截斷、不改 LLM 參數。只注入客觀狀態描述,行為(簡短/不主動 vs 活潑)由 Doll 自 emerge。
- **proprioception 家族定位(R1 S2)**:energy 與既有 `[Self pulse]`(host)、`[Cognition]`(token quota/stamina)同屬「系統客觀本體感知」。三者語域分開:energy 講**節律/休息**、Cognition quota 講**資源/預算**、Self pulse 講**主機**。避免渲染近義疲勞句(例:午夜後 quota 歸零=fresh,但 energy 可能仍低——兩軸動力學不同,不可互相暗示)。
- **No-fallback**:缺值/越界 → clamp [0,1],不降級。

## 3. 架構

### 3.1 狀態（MindState 擴充）

新增(`save_state`/`load_state` 顯式三處,同 B2):
- `energy: float = 1.0` — 0.0(精疲力竭)~1.0(飽滿)。
- `last_energy_restore_at: float = 0.0` — 上次回復時間(回復防抖用)。

**restart**:保留 disk 值(連續性,同 mood;非 refresh 1.0)。downtime 期間 energy 凍結,重啟後由回復機制(§3.3)在閒置時拉回。

**load clamp(R1 S4)**:`load_state` 對 energy 做 `min(1.0, max(0.0, float(data.get("energy", 1.0))))`(非法型別走既有 quarantine);兌現 §2「越界→clamp」承諾。

### 3.2 消耗（MindLoop.iterate，只算認知工作）

在 `iterate()` 末尾(早退 `:146-148` 之後、`iter_count += 1` 旁、`try/finally` 之外),**僅當本 turn 產生 output 時**消耗:
```
produced = bool(self._turn_speech) or <本 turn 有 tool call dispatch>
if energy_enabled and produced:
    self._state.energy = max(0.0, self._state.energy - cost_per_turn)
```
- `self._turn_speech` 是 B1 既有的 turn-local speech buffer(非空=有說話)。tool-call 旗標:cascade 期間記一個 `self._turn_produced_tool`(或檢查本 turn dispatch 記錄)。
- 純被動 turn(monitor/schedule tick、Doll 只 Idle 無 output)→ 不消耗。
- 一個 turn 至多扣一次(不論 cascade 幾 pass;粗近似,刻意,避免與 Cognition 的 per-token 重疊)。clamp 下界 0.0。

### 3.3 回復（ConsolidationTrigger poll loop，idle-triggered）

在 `ConsolidationTrigger.run()` 的 poll 迴圈內(現成 5s tick),**獨立於 consolidation 是否有 target**:
```
user_idle = now - state.last_user_at
if energy_enabled and user_idle >= energy_idle_threshold_s \
   and now - state.last_energy_restore_at >= energy_restore_debounce_s:
    state.energy = min(1.0, state.energy + restore_per_tick)
    state.last_energy_restore_at = now
    save_state(state, persist_path)
```
- 用 `last_user_at`(非 `max(last_user_at, last_iter_at)`)當閒置基準 → 不被 monitor/schedule 餵死。
- 防抖 `energy_restore_debounce_s`(獨立於 consolidation 1h cooldown)。
- 與 §3.2 構成 sawtooth:活躍走低、休息回升。**速率標定**:目標「一段正常休息(數十分鐘)由偏低拉回普通」+「一天活躍由高走低」,而非小時級整併才回滿。預設見 §3.5,實作以典型每日活躍 turn 量校準。
- save_state:回復發生時自己落盤(trigger 不在 mind_loop 內)。

### 3.4 注入（[Mind state]，proprioception）

`render_mind`/`_render_mindstate`(`mind_prompt.py:13,159`)在 mood 旁加 energy 行,**數值 + 客觀/成因式描述,無感受詞(R1 S1)**:
```
精力: 偏低 (0.3) — 活躍一陣子了，還沒休息整併
```
- bucket(客觀區間,**不含**「累」這類替 Doll 命名的感受):`≥0.7` 飽滿 / `0.4–0.7` 普通 / `<0.4` 偏低。可選成因式 gloss(描述機制,非情緒:「活躍一陣子了,還沒休息」)。
- render 固定 `f"{energy:.1f}"` 避浮點噪音。
- 要不要詮釋成「累」、要不要因此簡短,全留給 Doll 在 think/`MoodTool` emerge。

### 3.5 接線（R1 M2，完整性，三處 signature）

`enabled=False → 不注入` 目前無法實作(render 不收 config)。spec 明列改動:
- `MindLoop.__init__` 增 `energy_enabled: bool`、`cost_per_turn: float`,kernel(`kernel.py:~306`)注入。
- `ConsolidationTrigger.__init__` 增 `energy_enabled`、`restore_per_tick`、`energy_idle_threshold_s`、`energy_restore_debounce_s`,kernel(`kernel.py:~327`)注入;回復行用 `if energy_enabled` 包住(energy 關、consolidation 開時不回復)。
- `render_mind` 增 `energy_line: str | None`(disabled 時傳 None,`_render_mindstate` 略過該行)。
- config 新 `[energy]`:`enabled=True`、`cost_per_turn=0.05`、`restore_per_tick=0.05`、`idle_threshold_s=600`、`restore_debounce_s=300`(預設待 §3.3 標定法 + smoke 校準)。

## 4. 非目標

- 不硬性限制行為、不改 LLM 參數、不自動改 Mood(軟性)。
- 不做 idle 線性衰減(架構無 mind-loop idle tick;活躍消耗取代)。
- 不抽 conversation_idle 共用 helper:energy 的閒置用 `last_user_at`(語意不同於 consolidation 的 `max(last_user_at,last_iter_at)`),刻意不共用。

## 5. 測試（TDD）

- `energy`/`last_energy_restore_at` save/load round-trip(顯式三處);restart 保留 disk 值;load 遇越界值被 clamp(R1 S4)。
- 消耗:本 turn 有 speech/tool → 扣 `cost_per_turn`;**純被動 turn(無 output)不扣**;clamp ≥0;空 drain early-return 不扣。
- 回復:poll loop 中 `user_idle ≥ threshold` 且過防抖 → 回升 `restore_per_tick`;**consolidation 無 target(target=None)時仍回復**(M1 核心:脫鉤);防抖內不重複回;`last_user_at` 近期(非閒置)不回;clamp ≤1。
- 注入:energy 行格式 + bucket 邊界(0.7/0.4)+ **無感受詞**(斷言不含「累」);`f"{:.1f}"`。
- gate:`enabled=False` → 不消耗、不回復、**不注入**(render 層也測,R1 M2)。
- config `[energy]` 預設。

## 6. Smoke Gate（R1 S3 + 必要性條件，實作後必跑）

energy 全部效果是一行軟 prompt(不禁工具/不改參數/不自動改 Mood)。**實作完成後,必須跑行為 smoke 驗證效果真的 emerge**:用真實 LLM(port 8001)+ 實際 character pack,固定高 energy(~0.9)vs 低 energy(~0.2)各跑同一情境,啟發式比較回應長度 / 主動性 / 語氣。
- **過(有可觀察差異)** → B3 完成、可 merge。
- **不過(無差異 = dead state)** → **B3 held**:保留 state/接線(廉價),但在 roadmap 標記「需更強掛點(energy→LLM 參數 或 energy→Mood nudge)才有意義」,**不假裝補完**。誠實記錄,轉 A1。

## 7. 風險 / 剩餘

- **軟 prompt 效果可能太弱**(autonomy 限制不能硬影響)→ 由 §6 smoke gate 把關;成因式 gloss(§3.4)提升 salience 而不越界。
- **速率**(0.05/turn、0.05/tick、idle 600s、debounce 300s)是初值,smoke 校準。
- **與 Cognition quota 語域混淆** → §2 proprioception 定位 + §3.4 用機制式描述緩解。

## 8. 對 A1 / 既有的接口

- A1 self-profile 與 B3 平行、無耦合。
- energy 與 `[Cognition]`/`[Self pulse]` 並列為 proprioception;render 順序:mood → energy(緊鄰,軟耦合)→ 其餘 mind state。

## 9. Review 狀態

- **R1(design/autonomy/scope,3 lens,code-verified)**:M1 回復重設計(idle-triggered、脫鉤 consolidation target、消耗限縮認知工作、`last_user_at` 基準、速率重標定)、M2 接線三處 signature + gate、S1 bucket 去感受詞、S2 proprioception 定位 + 與 Cognition 區分、S3 smoke gate、S4 load clamp → 全部套用。
- **必要性結論**:energy 與 Mood 正交、值得做,**但條件性**——M1 修對(否則 dead state)+ §6 smoke 過(否則 held)。
- **狀態:plan-ready**,但帶 §6 smoke gate 出場條件。
