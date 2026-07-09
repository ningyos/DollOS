# System Pulse — Proactive Trigger (主動出聲那一半)

**Date**: 2026-07-09
**Status**: Design proposed, pending user approval → writing-plans
**Depends on**: existing `perception/system_pulse.py` (passive proprioception, shipped with B2 sleep-consolidation 2026-06-30), self-directed agenda (`AgendaObserver`, step 33), energy system (step 30 area).

---

## 0. 一句話

`SystemPulse` 已經讓 Doll「感覺得到」宿主機器的狀態(被動 `[Self pulse]` block),但她**不會因為機器變糟而醒來**。本設計加上主動觸發那一半:當 pulse 跨過「值得打擾人」的門檻(電池 critical / GPU 過熱 / 久卡同一視窗),fire 一個 `PulseMoment` 自發回合,讓她**選擇**要不要開口 —— 這正是 goal 的後半「主動出聲」。

## 1. 現況(已實作,不動)

`perception/system_pulse.py`(預設 `enabled=True`,poll 60s):

- 背景 poller 每 60s 讀:CPU load(`/proc/loadavg`)、RAM(`/proc/meminfo`)、GPU 溫度+記憶體(`nvidia-smi`)、電池 %+狀態(`/sys/class/power_supply`)、前景視窗(`xdotool`)、閒置秒數(`xprintidle`)、網路(TCP 到 `1.1.1.1:53`)。
- 每項分桶:`load` light/steady/heavy;`mem` comfortable/tight/pressured;`gpu_temp` cool/warm/hot;`battery` full/healthy/low/critical;`idle` present/away/long-away。
- `snapshot()` **change-triggered**:只有分桶 signature 變了才吐一則第一人稱身體感 block,插在 `[Active tasks]` 後;否則回 `None`(不是每回合都塞)。
- 缺來源靜默省略(沒 DISPLAY / 桌機無電池 / 無 nvidia-smi),無假值、無 fallback。
- 另供 `latest_idle_s()` 給 consolidation 當 idle gate。

**本質限制**:`SystemPulse` 是**被動 pull-only** —— `snapshot()` 只在「已經有一個回合正在跑」時被 `mind_loop` 拉進 prompt。它**從不 `queue.put()`、從不喚醒 Doll**。所以 idle 時(沒 UserSpoke、沒 ScheduledMoment、沒 AgendaMoment)機器變糟她根本不會醒,那則 block 只是躺著等下一個「因為別的原因」發生的回合。

## 2. 目標與非目標

**目標**
- idle 時,機器跨過負向/可行動/會惡化的門檻 → Doll 醒來一個回合,**可以主動出聲**(也可選擇沉默)。
- 沿用既有 observer pattern(`AgendaObserver`/`ReflectionObserver`),不新增子系統概念。
- 不吵人:同一個持續狀態只響一次;全域 throttle 防連環吵。

**非目標(YAGNI)**
- 不做正向觸發(網路恢復、開始充電)—— 那些留給被動 block。
- 不在自發回合給自主外部工具(Shell/Workflow)—— 主動出聲用不到,且安全考量。
- 不動 `SystemPulse` 的取樣/分桶/被動渲染。
- v1 不做 memory-pressure / network-down 觸發(語意模糊、較不可行動;保留為 config-off 待實測需求)。

## 3. 觸發原則(**開放決策 D1** — 使用者可於審查時推翻)

**只有「負向 × 可行動 × 會惡化」的轉變才觸發。** 具體是 **edge-triggered**(進入壞桶的那一刻響一次),不是 level-triggered(不是每次 poll 都響),也不對「復原邊」(hot→warm、critical→healthy)觸發 —— 復原只更新被動 block。

v1 三條 alert rule:

| rule | 觸發邊 | re-arm(可再次觸發)條件 | 需使用者在場? |
|---|---|---|---|
| `battery_critical` | battery 進入 `critical`(<15%)**且** discharging | 離開 critical **或** 開始充電 | 否(AFK 也發,訊息會排隊等他回來) |
| `gpu_hot` | 最熱 GPU 溫度進入 `hot`(>75°C) | 回到 warm/cool | 否 |
| `window_stuck` | 同一 `active_window` **連續** ≥ `window_stuck_s`(預設 90 min)**且**期間使用者在場(`idle_s < 60s`) | 前景視窗換掉 | 是(需在場;`include_active_window=false` 時整條停用) |

`window_stuck` 是唯一非分桶、時長型的規則,需要自己的累加器(記「目前視窗 + 從何時開始連續且在場」)。

**開放決策 D1 的替代選項**(若使用者要):加入 `memory_pressured`(RAM 進入 pressured >85%)、`network_down`(網路從 open→silent)、或正向的 `network_restored`。預設不納入。

## 4. 架構

沿用 observer pattern。`SystemPulse` 維持**純感測器**;觸發**政策**放在新的 `PulseObserver`,讀 `SystemPulse` 的樣本、擁有 alert 狀態機與 gating、把 `PulseMoment` 塞進 `PerceptionQueue`。與 `AgendaObserver` 讀 `MindState`、把 `AgendaMoment` 塞進佇列完全對稱。

```
SystemPulse (純感測器)                 PulseObserver (薄 IO 殼)
  _run() 每60s _poll_once()             run() 每 POLL s:
    → _last_sample                        s = system_pulse.latest_sample()
  latest_sample()  ← 新增公開存取器  ─→   alerts, self._state =
  snapshot() (被動,不動)                     evaluate_alerts(self._state, s, now, cfg)
  latest_idle_s() (consolidation 用)       for a in alerts:            # 已含 throttle
                                              queue.put(Perception("PulseMoment", data=a))
                                                            │
                                                            ▼
                              PerceptionQueue → MindLoop 一個 PulseMoment 回合
                                (發話 ON、工具收窄 PULSE_TOOLS、無自主外部)
```

**設計要點**:所有政策(edge 偵測、re-arm、throttle、deferred-retry)都在**純函式 `evaluate_alerts`** 裡,`PulseObserver` 只做 IO(取樣本、put 佇列)。這讓觸發行為 100% 可用純函式測試(無 mock 時鐘/佇列),也讓 throttle 的 deferred 語意有單一權威實作點。

### 4.1 `SystemPulse.latest_sample()`(新增,小)
回傳最後一筆 `PulseSample`,或 `None`(disabled / 尚無樣本 / 樣本過期,>2× poll interval)。與既有 `latest_idle_s()` 同款 staleness 保護。保持 `SystemPulse` 不依賴佇列、不懂 alert 政策。

### 4.2 `PulseObserver`(新,`src/dollos/mind/pulse_observer.py`)
鏡像 `AgendaObserver`,但**薄** —— 所有政策在 §4.3 的純函式裡:

```python
class PulseObserver:
    def __init__(self, *, system_pulse, queue, config):
        self._state = AlertState.initial()  # last_fire_at 起始 = 0.0
    async def run(self):
        # boot 不立即開火:首個 evaluate 用「當下」當 last_fire_at 基準,
        # 由 evaluate_alerts 內部處理(見 §4.3),不需 observer 額外記時。
        while not self._shutdown:
            await asyncio.sleep(_POLL_INTERVAL_S)  # 對齊 pulse poll,~60s
            s = self._system_pulse.latest_sample()
            if s is None:
                continue
            alerts, self._state = evaluate_alerts(
                self._state, s, time.time(), self._config)
            for a in alerts:
                self._queue.put(Perception(kind="PulseMoment", t=time.time(),
                                           data={"concern": a.slug, "detail": a.text}))
    def shutdown(self): self._shutdown = True
```

**gating**(全部在 `evaluate_alerts` 內)
- **edge + re-arm**:每條 rule 進壞桶只響一次;`AlertState` 記每條 rule 的 armed 位,**只有 sample 顯示復原才 re-arm**。持續 critical 不會每 60s 洗版。
- **全域 throttle** `alert_throttle_s`(預設 15 min)+ **deferred-retry**(關鍵正確性,self-review 修正):任兩則 alert 至少隔這麼久。被 throttle 擋下的 candidate **不消耗 arm、不標為 fired** —— 它保持 armed,下一 tick 重試,**throttle 一過就發**。所以「電池 critical 剛好撞上前一個 alert 的 throttle 窗」**不會被永久吞掉**(它會在 throttle 過後補發),只是延後幾分鐘。arm 只在「真的 put 進佇列」那一刻才消耗。
- **無 idle gate / 無 energy floor**:pulse alert 是環境驅動的準反應事件(電池要沒電了跟她有沒有精力無關),重要度夠高,不套 agenda 的 energy floor;但仍受 throttle 限制。產生發話的回合照常經 `cost_per_turn` 扣能量。
- **在場判定**:用 sample 的 `idle_s`(X 層真實輸入閒置),比 `state.last_user_at`(最後一則訊息)更準 —— 所以 `PulseObserver` **不需要 `MindState`**,只吃 `system_pulse` + `queue` + `config`,依賴面更小。

### 4.3 純函式 `evaluate_alerts`(政策的單一權威實作點)
仿 `schedule.due_entries` —— rule 判定 + re-arm + throttle + deferred-retry 全抽成**純函式**,吃 `(prev_state, sample, now, config)` 回 `(alerts, new_state)`,測試不用 mock 時鐘/佇列/子行程:

```python
@dataclass(frozen=True)
class Alert:
    slug: str        # "battery_critical" | "gpu_hot" | "window_stuck"
    text: str        # 自足中文描述,進 PulseMoment.data.detail
@dataclass(frozen=True)
class AlertState:
    battery_armed: bool          # True = 可觸發(目前不在 critical)
    gpu_armed: bool              # True = 可觸發(目前不在 hot)
    stuck_window: str | None     # 目前連續在場的前景視窗
    stuck_since: float | None    # 從何時開始連續且在場
    stuck_armed: bool            # True = 這個 stuck 事件尚未發過
    last_fire_at: float          # 全域 throttle 基準
    @staticmethod
    def initial() -> "AlertState": ...   # armed 全 True、last_fire_at=0.0、stuck 全 None

def evaluate_alerts(st, sample, now, cfg) -> tuple[list[Alert], AlertState]:
    # 1) 依 sample 對每條 rule 算「目前是否在壞桶」+ 復原時 re-arm。
    # 2) 蒐集 candidates = 在壞桶 且 該 rule armed。
    # 3) 依優先序(battery > gpu > window)套 throttle:
    #    若 now - last_fire_at >= alert_throttle_s → 這則 emit、消耗該 rule 的 arm、
    #    更新 last_fire_at = now(同一 tick 後續 candidate 因此被 throttle 擋)。
    #    否則 → 不 emit、不消耗 arm(保持 armed,下一 tick 重試 = deferred)。
    # 回傳 (emitted_alerts, new_state)。
```

**re-arm 細節**:`battery_armed`/`gpu_armed` 在 sample 顯示離開壞桶(battery 離開 critical 或充電中;gpu 回 warm/cool)時設回 True。`window_stuck` 用 `stuck_window`/`stuck_since`/`stuck_armed`:前景視窗一換就重置三者(`stuck_armed=True`、`stuck_since=now`);連續在場(`idle_s < 60`)且 `now - stuck_since >= window_stuck_s` 且 `stuck_armed` → 產生 candidate;離場(idle_s ≥ 60)時**凍結累積**(不重置視窗,但 `stuck_since` 順延,避免 AFK 時間灌水)。**開放實作細節**:離場後回來要「續算」還是「重零」——傾向續算但把離場那段扣掉(見 plan 決定)。

## 5. `PulseMoment` 回合語意(mind_loop)

### 5.1 新 perception kind
`mind_state.py` 的 `Perception.kind` Literal 加 `"PulseMoment"`。

### 5.2 pure-batch 判定(reactive-first,沿用既有守則)
`_run_one_turn` 加:
```python
self._is_pulse = bool(perceptions) and all(p.kind == "PulseMoment" for p in perceptions)
```
與 `_is_agenda`/`_is_diary` 同款「整批都是」守則:一個 `PulseMoment` 若跟真 `UserSpoke` co-batch,`_is_pulse` 為 False → 落到正常回合(完整工具、發話正常),**使用者的真請求永不被收窄** —— 直接複用 agenda/diary 已驗證過的 whitewash 防護。

### 5.3 工具收窄:`PULSE_TOOLS`(發話 ON、無自主外部)
`tools.py` 加 `PULSE_TOOLS = frozenset({"Recall", "NoteMemory", "MoodTool"})`。
`_active_tool_registry` 在 `_is_diary`/`_is_agenda` 分支旁加 `_is_pulse` 分支,narrow 到 `PULSE_TOOLS`。她可以 Recall 查、NoteMemory 記、MoodTool 調情緒,但**不能 Shell / 不能 SpawnWorkflow**(自發回合不給自主外部,安全)。

### 5.4 發話:**ON**(與 agenda/diary 相反)
發話抑制的唯一 chokepoint 是 `_emit_sentence` 的 `if self._is_agenda or self._is_diary: return`。**`_is_pulse` 不加進這條** —— 主動出聲正是本 feature 的目的。這是 pulse 回合跟 agenda(cognition-only、抑制發話)在意圖上的根本差異。

### 5.5 她怎麼知道自己為何醒來
`PulseMoment` perception 落在 `[Recent perceptions]`,其 `data.detail` 帶**自足的中文描述**(例:「電量掉到 12% 而且在放電」)。**不依賴** `[Self pulse]` block 同時出現 —— 因為 `snapshot()` 的 `_last_emitted_sig` dedup 可能在更早的被動回合已吐過同一桶變化,導致 wake 回合 block 缺席。所以 detail 必須自帶足夠語境。

## 6. Config

擴充 `SystemPulseConfig`(`config.py`):
```python
alerts_enabled: bool = True          # 主動觸發總開關(關掉 = 只剩被動 block,回到現況)
alert_throttle_s: float = 900.0      # 全域最小間隔 15 min
window_stuck_s: float = 5400.0       # 久卡視窗門檻 90 min
```
`window_stuck` 額外受既有 `include_active_window` 節制(隱私關掉視窗追蹤時,這條 rule 自動停用)。`alerts_enabled=False` 時 `PulseObserver` 不啟動,行為與今天完全一致(純加法、可完全關閉)。

## 7. Kernel 接線

`kernel.py`:
- 建 `self._pulse_observer = PulseObserver(system_pulse=self.system_pulse, queue=self._perception_queue, config=settings.system_pulse)`(在 `_agenda_observer` 旁)。
- `alerts_enabled` 為真時,於 `self.system_pulse.start()` 之後 `self._pulse_task = asyncio.create_task(self._pulse_observer.run())`(與 `_agenda_task` 並列)。
- shutdown 時 `self._pulse_observer.shutdown()` + await task(對稱既有 observer 收尾)。

## 8. 測試

- **`evaluate_alerts` 純函式**(主力,無 mock):
  - `battery_critical`:healthy→critical(discharging)開火一次;連兩筆 critical 只開一次;critical→charging re-arm;critical→healthy re-arm。
  - `gpu_hot`:warm→hot 開火;hot 持續不重複;hot→warm re-arm。
  - `window_stuck`:同視窗累積過門檻且在場 → 開火;期間 idle(離場)不累積/不開火;換視窗重置;開火後不重複直到換視窗。
  - throttle + **deferred-retry**:同輪兩 candidate 只放行第一則、第二則不消耗 arm;下次 call 在 throttle 窗內 → 仍不發、arm 仍在;throttle 過後 → 補發(**證明不吞掉**)。
  - re-arm 與 throttle 正交:critical 期間被 throttle 擋 → 之後仍能發;真正復原才清 arm。
- **`SystemPulse.latest_sample()`**:無樣本回 None;過期(>2× poll)回 None;新鮮回樣本。
- **mind_loop**:
  - 純 `PulseMoment` 批 → registry == `PULSE_TOOLS`、`_emit_sentence` **不**抑制(發話送達 sink)。
  - `PulseMoment` + `UserSpoke` co-batch → 完整 registry、發話正常(reactive-first 未被 whitewash)。
- **PulseObserver.run**:注入假 `system_pulse`,edge → `queue` 收到一則 `PulseMoment`;throttle 內第二 edge 不進佇列。

## 9. Live smoke(人工,CI 跑不到)

軟機制(prompt 管不住的語意)必 live smoke —— 依 `ref_weak_model_soft_mechanism_playbook`:
1. 造 `latest_sample()` 回傳 battery critical/discharging(或臨時把門檻調高觸發 gpu_hot)。
2. 讓 daemon idle,觀察是否 fire `PulseMoment`、Doll 是否**真的開口**且內容合理(不是每 60s 洗版)。
3. 確認持續 critical 不重複吵;充電後 re-arm。
4. co-batch:pulse 觸發的同時使用者講話 → 確認正常回合、full registry。

## 10. 開放決策彙整(審查請拍板)

- **D1 觸發清單**:v1 = {battery_critical, gpu_hot, window_stuck}。要不要加 memory_pressured / network_down / 正向 network_restored?(§3)
- **D2 throttle 值**:全域 15 min、window_stuck 90 min 合理嗎?
- **D3 工具面**:`PULSE_TOOLS = {Recall, NoteMemory, MoodTool}`(發話 ON、無自主外部)。要不要更嚴(拿掉 NoteMemory)或更寬(給唯讀 Shell 讓她自己查 `nvidia-smi`)?我傾向就這樣 —— 更寬會把自發回合變成自主外部行動,踩 agenda 設計刻意避開的線。

## 11. 為何不選其他方案

- **併進 `AgendaObserver`**:否。agenda 是「因為想做而追求自己的 loop」、cognition-only、抑制發話;pulse alert 是「環境變糟、要出聲」、發話 ON。語意相反,合併會污染兩邊守則。
- **在 `SystemPulse` 內部直接 emit perception**:可行且最省 code(重用既有 poll 迴圈),但會讓 SystemPulse 同時是「被動感測器」又是「主動政策源」,並被迫依賴佇列。拆出 `PulseObserver` 讓「何時該打擾人」的政策集中在一個可測的地方,對稱既有 observer 架構,勝出。
