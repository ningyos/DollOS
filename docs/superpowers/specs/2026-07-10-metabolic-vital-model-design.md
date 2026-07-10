# DollOS 代謝 Vital 模型 — 設計

**Date**: 2026-07-10
**Status**: Design approved (D1/D2/D3 locked by user) → writing-plans
**Builds on**: B3 energy 系統、System Pulse(proactive trigger + presence tuning, merged fb1f961/a196a4c)。
**設計來源**: ultracode 5-視角 design workflow(bioenergetics / minimal-reuse / RL-signal-first / agency / measurement-grounded)→ 對抗評審 → opus 綜合。完整綜合稿(含逐視角評分)另存;本 spec 是鎖定決策後的權威版。

---

## 0. 一句話

Doll **是一種機器人** —— 系統狀態不是「像」她的身體,它**就是**她要**自我管理的 vital**。把唯一的 `state.energy`(欄位不變)從「每回合齊頭扣 0.05 的假數字」改成**物理誠實的代謝**:消耗由**本回合自己的 token**(effort)驅動、由**熱**放大、由**電池**封頂。改數字不改敘述;mood/克制**湧現**;每回合落一行 code-captured 訊號當**未來 LLM RL 的底材**。

**Embodiment 動機(2026-07-10)**:目標硬體是 **LattePanda Alpha + 電池 =「GLaDOS 馬鈴薯」**(見 memory)。battery=ATP=命是字面現實,不是比喻 —— v2 電池天花板是真實近程目標(dev 桌機無電池、測不了,LattePanda 就是為此)。

## 1. 現況(已驗證的 ground truth)

| 事實 | 位置 |
|---|---|
| `energy: float=1.0`,load 時 clamp [0,1] | `mind_state.py:173,334` |
| 齊頭 drain `0.05`,gate=`produced and consumes`,`external_public` 豁免 | `mind_loop.py:827-830` |
| idle restore `min(1.0, energy+0.05)`,idle≥600s,debounce 300s | `consolidation.py:156-171` |
| `energy_bucket_line` 分桶 0.7/0.4,格式 `精力: X (0.Y)`,**無感受詞** | `mind_prompt.py:22-35` |
| `bucket_gpu_temp` 55/75、`bucket_battery` 95/30/15 | `system_pulse.py:50-65` |
| `read_nvidia_smi` 只查 `memory.used,memory.total,temperature.gpu` | `system_pulse.py:168-193` |
| `LLMCallRecord`:`prompt_tokens`/`completion_tokens` nullable、`call_purpose="cascade"` 預設、**無 turn_id** | `telemetry/llm_calls.py` |
| `TurnLatencyRecord`:**有 turn_id**、think_chars/speak_chars | `telemetry/turn_latency.py` |
| transport 從 llama.cpp SSE `tokens_evaluated`/`tokens_predicted` 解析 token,寫進 `LLMCallRecord`,**但不回給 caller** | `llm/transport.py:146-175` |
| `_AGENDA_ENERGY_FLOOR=0.5`,只 gate 自主回合 | `agenda_observer.py` |

**兩個決定設計的事實(實測)**:
1. **`call_purpose` 對歸因是壞的** —— 它在**每個** call site 都預設 `"cascade"`(transport/kernel〔Workflow worker+consolidation 共用〕/mind_loop)。用它過濾 token 會**靜默混入並行 Workflow fan-out 與背景 consolidation 的 token**。**每回合 token 歸因必須 key on 唯一 turn_id,不是 call_purpose。** 這是最重要的修正。
2. **dev 桌機無電池、RAPL root-only** —— `/sys/class/power_supply/` 空;`nvidia-smi --query-gpu=power.draw` = `8.60W/165W`(免費可得);`/sys/class/powercap/.../energy_uj` = Permission denied。所以:GPU 瓦數免費真實;**電池邏輯在此桌機測不了**(必須 degrade 成 identical-to-today,留給 LattePanda 驗);CPU/RAPL power **v1/v2 出局**。

## 2. Vital 模型

**唯一一個持久 mutable stock**;其餘都是 flow / byproduct / 餵給那個 stock 的 derived modulator。**stock 本身不加新欄位。**

### 2.1 `energy` — ATP 儲備(STOCK,唯一)
- `MindState.energy`(`mind_state.py:173`)欄位/型別/持久化**不變**。reframe + re-drive,不 rename。
- [0,1]。意義:可用的認知/代謝儲備。做真實認知工作時消耗、休息時回復、有電池的宿主上永遠不會讀得比實際燃料還滿。

### 2.2 消耗 = token flow(取代 `mind_loop.py:829-830` 的齊頭 0.05)
- **本回合自己的** `prompt_tokens + completion_tokens`(由 turn_id / in-loop 歸因,見 §4.2),是**唯一 per-turn drain driver**。
- **為何 token 而非瓦數**:token 是**唯一能因果歸因到 Doll 自己認知動作**的訊號(一次 LLM call **就是**她在思考)。瓦數/電池是整機的,並行的遊戲/轉碼會污染。5 個評審全部收斂於此。
- `token_cost = (completion_tokens + 0.25·prompt_tokens) / TOKENS_PER_ENERGY_UNIT`,校準到**典型回合 ≈ 今天的 0.05**。completion 權重高於 prompt(prompt 隨累積歷史膨脹,非新 effort — 見 §8)。

### 2.3 熱 = 乘數(BYPRODUCT/constraint,不是第二軸)
- 最熱 GPU 溫度(既有 `PulseSample.gpus`,~60s 已在讀),用既有 `bucket_gpu_temp`(55/75)分桶。
- cool/warm/hot → 乘數 ×1.0 / ×1.15 / ×1.4(illustrative,smoke 校準,如剛出的 severity 門檻)。
- `drain = token_cost × thermal_multiplier`。只在 `latest_sample()` **新鮮**時套(內建 staleness guard → 死 poll loop→乘數省略=1.0,絕不猜)。這就是 brief 要的「熱調制、不競爭」的具體方程。

### 2.4 GPU 瓦數 = 只記錄不扣血(BYPRODUCT,ambient)
- **NEW**:`power.draw,power.limit` 加進 `read_nvidia_smi` 查詢(實測免費 8.6W/165W)。~60s 取樣。
- **為何 log-only**:60s 整板讀數無法誠實歸因到 ~2s 的回合(結構性 aliasing:取樣通常落在 call 之間的空檔),且被任何其他 GPU 消費者污染。它是有價值的 **RL context/constraint 特徵 + 可選 `[Self pulse]` 敘述**,不是 cost。
- `None`(無 nvidia-smi)→ 省略,不造假。

### 2.5 電池 = ATP 天花板(STOCK BOUND,不是第二 stock)
- `PulseSample.battery_pct`/`battery_status`(既有 `read_battery()`),既有 `bucket_battery`(95/30/15)。桌機→`None`→整個電池故事**惰性**(跟今天 byte-identical)。
- 三個效果**全在回充端**(**D2 鎖定:絕不做回合中 hard-cap** —— 拔插頭時數字不該跳,不像真疲勞):
  - **天花板 clamp**:idle restore clamp 到 `E_max = f(bucket_battery)` 而非齊頭 1.0。「休息不會讓你回得比手上的燃料還滿。」
  - **critical 暫停**:`bucket_battery==critical and discharging` → 該 tick 回充=0。**與 `evaluate_alerts` 的 `battery_critical` PulseMoment 同一條件** → 機械耦合與敘述警報共用一個 source of truth。
  - **充電回血**(v2):`battery_status=="charging"` 開一條 idle-independent 回充路徑(brief 的「充電回滿」)。插電**就是**餵食,她不必也跟著沉默。

### 2.6 方程

**Drain**(取代 `mind_loop.py:829-830`;gate 逐字不變):
```
produced = bool(self._turn_speech) or self._turn_had_tool
consumes = self._ctx.origin_tier != "external_public"
if energy_enabled and produced and consumes:
    if turn_tokens is not None:                    # measured
        token_cost = (completion + 0.25*prompt) / TOKENS_PER_ENERGY_UNIT
        cost_mode  = "measured"
    else:                                          # D1=a: sanctioned degrade
        token_cost = cost_per_turn                 # 今天的 0.05
        cost_mode  = "flat_legacy"
    mult  = thermal_multiplier(latest_sample())    # stale/absent → 1.0
    energy = max(0.0, energy - token_cost * mult)
```
**Recover**(擴充 `consolidation.py:156-171`):
```
E_max = energy_ceiling(sample)   # 無電池→1.0;否則 f(bucket_battery)
if not (battery_critical_discharging):
    if idle_ok and debounce_ok:                    # 既有 gate 不變
        energy = min(E_max, energy + restore_per_tick)
    if battery_status == "charging":               # v2
        energy = min(E_max, energy + charge_restore_per_tick)
```
`energy_ceiling`:full/healthy→1.0、low→0.7、critical→0.4(illustrative,複用 `bucket_battery`)。桌機(`battery_pct is None`)→ E_max=1.0 恆真 → 跟今天一樣。

## 3. Surfacing & Agency —— **改數字,不改敘述**

2026-06-30 energy spec 已切分語言域(energy=節律/休息、Cognition=資源/預算、`[Self pulse]`=主機),並警告近義疲憊句。本設計**加零個新疲憊句、零個新 prompt block**。

- **`[Mind state]` `energy_bucket_line`** — 格式/分桶/措辭**不變**(`精力: 飽滿/普通/偏低 (0.x)`)。只是底下的數字**第一次**反映真實 effort+熱+燃料。
- **`[Self pulse]` 熱行** — 可選裝飾:查到 `power.draw` 後在既有 `vital heat` 行後綴 ` · {watts}w`,presence-gated(None 省略)。無新行概念。
- **`[Body signal]` PulseMoment** — 剛出的 severity 框架(e9ab112/fb1f961)**不動**。它是離散的**危機喚醒**通道;連續 drain 調制是互補的**ambient** 層。純 pulse 回合她已同時看到 `energy_bucket_line`(多累)+`[Body signal]`(多急),複合判斷不需新 code。
- **mood/自保湧現**:mood 100% 由 `<think>` 經 MoodTool 驅動(不變)。要不要因「偏低」變簡短/拒重度 Workflow/求插電,是她的 `<think>` 決定。**唯一行為 gate `_AGENDA_ENERGY_FLOOR=0.5` 複用不變**,只是收到更誠實的數字 → 「燙+真的做很多工→更早停止追自己的議程」成為一個誠實數字跨一個既有門檻的**湧現結果**。
- **克制有了理由**:齊頭 0.05 下一個 `Shell` 跟 5-task `SpawnWorkflow` 同價,克制無從湧現;token+熱下重度工具**真的貴** → 克制變**經濟理性**(湧現,非命令)。這是 Self-First 的核心 payoff。
- **調節動作全既有**:沉默(0 action 最便宜)、`Say`(求插電/我簡短點)、`MoodTool`、拒/縮 Workflow。**不加新工具**。對稱的顯式 `ConserveMode` 工具**延後**(能讓「選擇保守」變 RL-labelable 正向動作,但超出 vital 模型範圍)。

## 4. 接線(精確 touch points)

### 4.1 Drain/Recover
- `mind_loop.py:827-830` — gate 逐字保留,齊頭 `self._cost_per_turn` 換 §2.6 乘積。需 per-turn token 和(§4.2)。
- `consolidation.py:156-171` — idle/debounce gate 保留;`min(1.0,…)`→`min(E_max,…)`;加 critical-pause guard;(v2)加充電回血。`ConsolidationTrigger` 已持 `system_pulse`,天花板讀取無需新 wiring。

### 4.2 污染安全的 token 歸因(唯一真正必要的新 wiring)—— **D1 相關**
**採 (A) in-loop accumulation(構造上污染安全、無 telemetry read-back)**:
- `transport.py:151-156` 已解析 `tokens_evaluated`/`tokens_predicted` 但從 caller 視角丟棄。**把它回傳給 `MindLoop`** —— 在 `finally` 呼叫注入的 `on_usage(prompt, completion)` callback(或 stream 尾端 usage sentinel)。
- `MindLoop` 累加 `_turn_prompt_tokens`/`_turn_completion_tokens`(在既有 `_turn_speech`/`_turn_had_tool` 旁),每回合清。因為**只有這個 loop 自己的 call 打到自己的 callback**,並行 Workflow/consolidation 的 token 永不洩入。於 §2.6 消耗。
- **另加 `turn_id: str|None` 到 `LLMCallRecord`**(RL join key,§5),與 (A) 並行。
- **絕不用 `call_purpose` 過濾**(到處 "cascade",已驗證壞)。

### 4.3 SystemPulse 量測新增
- `system_pulse.py:168-193` — `power.draw,power.limit` 加進查詢字串。**`PulseSample.gpus` 形狀保持 additive**:加**平行** `gpu_power: list[tuple[float,float]]|None`(draw,limit),與 `gpus` 1:1,**不**加寬既有 `(mem,temp)` tuple(避免波及 `signature()`/`render_block()`/tests)。瓦數用 **draw/limit 比值**分桶(仿 `bucket_mem`),不用固定瓦數門檻(TDP 跨 75–450W)。
- 新純 helper 與既有 bucket 同置(IO-free、可測、仿 `evaluate_alerts` 風格):`thermal_multiplier(temp_c)->float`、`energy_ceiling(sample)->float`。
- **防禦性解析**:`read_nvidia_smi` 對單一欄位 ValueError 會丟掉整條 GPU 行 —— 新欄位獨立 `None` 解析,不讓它 gate 既有 mem/temp。

### 4.4 Telemetry / config
- `telemetry/turn_latency.py` — `TurnLatencyRecord` 已有 turn_id。加 nullable:`tokens_total`、`energy_cost`、`energy_after`、`cost_mode`(`measured|flat_legacy`)、ambient `gpu_hottest_c`、`gpu_power_w`、`battery_pct`。一回合一行 JSONL = (state, action, cost, state') tuple。從 drain 已抓的同一 `latest_sample()` 讀,零額外 poll。
- `telemetry/llm_calls.py` — 加 `turn_id: str|None=None`。
- `config.py` `EnergyConfig` — 加 `token_per_energy_unit`、`thermal_multiplier_warm/hot`、`energy_ceiling_low/critical`、(v2)`charge_restore_per_tick`。同 `extra="forbid"`。NB:`config.example.toml` 今天**無 `[energy]` 表**(default 只在 Pydantic model)—— 這是首次出現,順便把既有 `cost_per_turn`/`restore_per_tick` 也曝出去。

### 4.5 明確不動
`pulse_observer.py`/`evaluate_alerts`(危機喚醒政策)、`MoodTool`、`energy_bucket_line` 措辭、`produced and consumes` gate、`external_public` 豁免、`CognitionWorker`/`daily_token_quota`(**不同時間尺度**的 token 概念,見 §8)、idle-restore gate。

## 5. RL-signal readiness

每回合、key on 既有 turn_id:
- **乾淨 per-turn cost scalar(可歸因)**:`energy_cost`(實際套用的 drain)、`tokens_total`。可直接當 per-action cost:`reward -= λ·energy_cost`。**只有 `cost_mode=="measured"` 進乾淨訓練集**;`flat_legacy` 靠 tag 排除。
- **Ambient context/constraint 特徵(非 per-turn reward 主項)**:`gpu_power_w`、`gpu_hottest_c`、`battery_pct`。整機、非回合可歸因 → 當 conditioning state 或 constraint penalty,絕不當單回合的 credit-assigned cost。
- **建議 reward shaping**:對單一 stock 做 potential-based shaping `F(s,s')=γΦ(energy')−Φ(energy)`(Ng et al.),Φ 是在健康帶(~0.4–0.8)峰值、危機處低、囤積到 1.0 略折的凹函數。**構造上不可 game**(任何 round-trip telescope 到 0)、**dense**(每回合可從 trace 的 energy 重算)、**零 runtime code**(交給 fine-tune recipe 的函數)。
- **Provenance 硬規則**:每個 cost/effort 欄位**只 code-captured**(仿 `OpenLoop.provenance`「code-filled、模型寫不到」)。**永不給 Doll 自報 effort 的工具** —— 那會重開記憶寫攻擊面、讓 policy game 自己的 reward。
- **raw SI 進 log 保持 raw**(°C/W/tokens/Δenergy);分桶只在給 Doll 看的 render 層,不得洩進 telemetry → reward 斷點可改而不用重收資料。

## 6. 分期(增量,一 merge 一概念)

- **v1a — 誠實 FLOW(核心)**:齊頭 0.05 → token drain + 污染安全 per-turn 歸因(§4.2-A)+ `TurnLatencyRecord` RL 欄位。無熱、無電池。**桌機全可 smoke**。出 RL 底材。*驗收:重度 tool-cascade 回合可測地比一句話貴;`None`-usage 回合正好 0.05 標 `flat_legacy`;`energy_bucket_line` 格式不變。*
- **v1b — 熱乘數 + power.draw 記錄**:nvidia 查詢加瓦(平行欄位)、drain 上的 `thermal_multiplier`、ambient `gpu_power_w`/`gpu_hottest_c` 進 per-turn log + 可選 `[Self pulse]` 瓦顯示。乘數 live-smoke 校準。桌機可測。
- **v2 — 電池天花板(STOCK ceiling)**:`energy_ceiling` clamp + critical-pause + 充電回血。**dev 桌機測不了(無電池)→ 留給 LattePanda+電池驗**;在此之前靠 `None` guard 可證惰性。這塊讓 battery% **字面**封住 ATP 儲備。
- **v3 — richer RL 底材**:全解析 `VitalsRecorder` JSONL、Φ(energy) reward recipe、EPOC 式「熱→回復更慢」、有授權的部署才上 CPU/RAPL、可選 `ConserveMode` 工具。

## 7. 鎖定決策

- **D1 = a(user)**:token 缺失時**允許 `flat_legacy` 降級**(今天的 0.05,打 `cost_mode` tag 供 RL 排除)。這是 **[[feedback_no_fallback]] 鐵律唯一被授權的例外** —— 理由:不是捏造 sensor 值,是**今天的機制 made observable**;自架 llama.cpp 一定回 token 故**主路徑不觸**。若某 provider 系統性不回 usage,升級成 log/warn 邊界,不做隱形永久降級。
- **D2 = OK(user)**:電池只在**回充端封頂 + critical-pause**,**不做回合中 hard-cap**。
- **D3 = OK(user)**:首發 **v1a+v1b**(桌機全可測);**v2 電池留給 LattePanda**。

## 8. 風險

- **整板瓦數不可歸因** → 瓦數 log-only 的原因。若日後有人要把瓦數放進 drain,得重新面對 per-board(非 per-process)量測;nvidia-smi 無 cgroup 級功耗歸因。
- **`call_purpose` 污染(graft-killer)** — 已驗證:用 `call_purpose=="cascade"` 過濾會含並行 token。靠 turn_id/in-loop 解。若未來有人「簡化」回 purpose 過濾,per-turn cost 在並行負載下靜默腐化。
- **prompt_tokens 隨歷史膨脹非 effort** — 靠 completion 主導(prompt 0.25 權重)緩解。
- **rolling baseline 抹掉持久 verbosity 訊號** — self-normalize 會 re-center,對 stock 動態可接受;RL 要絕對趨勢就用 log 的 raw token。
- **`PulseSample.gpus` 形狀改動是 breaking** — 靠平行 `gpu_power` 欄位(additive)緩解。
- **近義疲憊句地雷** — 不加第四句「代謝率/effort」敘述;effort 當隱藏乘數,讓既有 `energy` 數字說話。
- **`energy` vs `daily_token_quota` 混淆** — 都碰 token 但不同時間尺度(可 idle 回復的短程疲勞 vs 永不 idle 回復的日曆日預算)。單軸 = **一條疲勞軸**,非「所有 token 相關數字合併」。記錄以防後人誤 merge。
- **人造簡短** — 緊耦合 cost 到 token 數會製造「為省而簡短」的壓力(非真自我照顧)。live smoke 觀察湧現的克制讀起來是真誠而非小氣。
- **電池邏輯 dev 桌機測不了** — v2 必須 LattePanda 驗;在此之前可證惰性、不得宣稱可用。
