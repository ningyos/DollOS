# Spec — B2: Sleep-Time Consolidation（睡眠期記憶整併）

- **Date**: 2026-06-30
- **Status**: PLAN-READY — R1(trigger)+ R2(keeper/autonomy/integration/scope)兩輪對抗 review 已收斂套用。見 §9。
- **Depends on**: B1 episodic transcript recapture（已 merged）。
- **Scope**: 只做 B2。A1 self-profile（吃 B2 產物）、B3 energy（吃 idle 訊號）各自後續獨立 spec。
- **藍圖來源**: core-loop-robustness spec §P4(quiet-pulse → memory-keeper subagent → 非破壞性)；本 spec 是 P4 首次落地(聚焦版)。

## 1. 背景與問題

B1 之後對話逐字稿會進可搜尋語料,但**沒有東西整理它**:原始 transcript + 每日零散 NoteMemory 只單調增長,重複、過時、稀釋召回。Doll 自己 probe 明講「記憶不是筆記本,塞滿讓重要的模糊」「準不要多」。三個背景迴圈(diary/schedule/ReflectionObserver)都只丟提示 perception,沒有一個會 summarize/dedup/提取結構化事實。**缺一個 sleep-time 整併 pass。**

## 2. 設計原則（research + Doll probe + 兩輪 review 收斂）

- **idle 觸發,用 conversation-idle**:整併在對話閒置時跑,不佔對話算力。**不綁** `SystemPulse.idle_s`(來自 xprintidle,Wayland/headless 永 None → 會 no-op);用即時、DISPLAY 無關的 `MindState.last_user_at`/`last_iter_at`。
- **Doll 主導自我 — candidate 是 pull-only(R2 最關鍵)**:B2 只產**中性 candidate 結構化事實**,且 candidate **不自動注入** `[Memory context]`——只進可搜尋索引,Doll(Recall)與未來 A1 **主動 pull** 才看得到,浮現時帶 `[系統整併·待確認]` provenance 前綴。這對上 probe 的「系統 nudge、我保留主導權」:candidate 是她伸手才見的提示,不是每 turn 被塞、她又刪不掉的既定事實。**召回層 gating 由 B2 自管,不下放 A1**(A1 只管 self-profile pin;它結構上 gate 不到召回池)。
- **非破壞性**:不刪/不改原始 transcript 與 NoteMemory;只新增 consolidated 層。
- **No-fallback**:任何階段失敗 → log + 跳過該次整併,不降級、不 silent。

## 3. 架構

### 3.1 ConsolidationTrigger（新背景 observer）

與 `ReflectionObserver` **只共用** `while not shutdown: await sleep(POLL)` 骨架;**restart-init 各自實作**(ReflectionObserver 丟棄 gap 不重跑;ConsolidationTrigger 相反——讀持久化狀態、重啟補跑)。kernel `asyncio.create_task(trigger.run(), name="consolidation-trigger")`,持有 task 參照供 shutdown(§10)。

**DI(ctor 依賴,§3.1 列舉)**:`state`、`persist_path`、`adapter`、`renderer`、`memsearch`、`memory_root`、`transcripts_root`、`tool_output_store`、`consolidated_dir`、`system_pulse`(optional)、config 閾值。config 放新 `[consolidation]` Settings 區段(對齊既有 `SystemPulseConfig`)。

**conversation_idle = now - max(last_user_at, last_iter_at)**。觸發條件(全滿足才跑):
1. **對話閒置**:`conversation_idle ≥ idle_threshold_s`(預設 300)。
2. **有新對話素材**:`user_turn_count > last_consolidation_turn`(§4;只在 `UserSpoke` 遞增,不用含非對話 perception 的 `iter_count`)。
3. **cooldown**:`now - last_consolidation_at ≥ min_interval_s`(預設 3600)。

**SystemPulse.idle_s = optional 加分 gate**:若 enabled 且 `latest_idle_s()` 非 None,額外要求 host 也閒置(AND);None(Wayland/headless/disabled)則**忽略,不否決**。為此 SystemPulse 加公開 `latest_idle_s() -> float | None`(含新鮮度檢查:sample 超過 ~2× `poll_interval_s` 視為過期不參與)。

poll 5s 量級。觸發 → §3.2。

### 3.2 memory-keeper agent（driver-fed、Report-driven、cancellable）

**契約(R2 M1:candidate-only 界線靠工具集是假的——SUB_TOOLS 含 Shell/NoteMemory/SpawnMonitor)**:改成 driver 餵料 + keeper 純整併 + driver 寫回:

1. **driver 餵料**:ConsolidationTrigger 用 `Path.read_text` 讀目標日期 transcript(§3.4),截最後 N 行/M 字元上限,**inline 進 task 字串**。keeper 不需任何讀檔 tool。
2. **keeper 工具集**:`KEEPER_TOOLS = [Report, Scratchpad]`(明確 allowlist,**不含** Shell/NoteMemory/SpawnMonitor/RemoveMonitor)。`run_agent(..., tools=KEEPER_TOOLS, shell_runner=None, monitor_runner=None)`——連 Shell 都無法運作。system 用既有 `subagent_scaffolding` 模板。
3. **keeper 產出**:整併 bullets 放 `Report.details`(中性事實)。`run_agent` 回傳該 dict。
4. **driver 寫回**:run_agent 返回後,driver 寫 `consolidated/{date}.md` + `index_file`。被 cancel 時 `CancelledError` raise 在 `await run_agent` 處 → 直接跳過寫檔,不留半截檔(天然守 §2 非破壞性)。

**task prompt**:「讀以下逐字稿,提取去重成簡潔的**中性** candidate 事實——主人的穩定偏好/習慣、你們關係的進展、值得長期記住的模式。陳述為觀察(『主人偏好X』),**不要**自我宣告(『我是X』)。重複合併、過時捨棄。**不確定就不寫,寧缺勿濫。準不要多。**」

**輸入只吃 transcript**(S5:讓整併成為 transcript→fact 唯一路徑,NoteMemory 維持即時記,避免雙重蒸餾)。

**可取消(R2 M3:cancel 接縫)**:kernel 持有 `_consolidation_task`;在**兩個 UserSpoke 產生點**同步 cancel——text 路徑 `_handle_message` 的 TextInput 分支(`kernel.py:~366`)+ voice 路徑 `_on_user_text`(`kernel.py:~454`)各呼叫 `self._cancel_consolidation()`(`if t and not t.done(): t.cancel()`)。**不走** `_maybe_preempt`(idle 時 early-return,`kernel.py:404`)、**不走** mind_loop drain(延遲到下次 drain 才釋放 slot)。

**界線(R2 S3)**:driver 用 `asyncio.wait_for(run_agent(...), timeout=120)` 包(比 workflow 300s 短,返場快速讓出 semaphore slot)+ `max_tokens≈2048`。

**失敗/取消語意 + 落盤(R2 S1)**:
- `last_consolidation_at` **嘗試後一律更新**(成功/失敗/取消)→ cooldown 必前進,杜絕 5s 重試風暴。
- `last_consolidation_turn`/`last_consolidated_date` **只成功才更新** → 下次重試同批,不漏。
- trigger 不在 mind_loop 內(`save_state` 只在 mind_loop.iterate 末尾呼叫)→ **trigger 嘗試後須自己 `save_state(state, persist_path)`**,否則崩潰丟 cooldown、重啟立刻重跑。

### 3.3 產物 + 召回 gating（R2 M5）

整併結果**覆蓋重建**目標日期檔 `memory_root/shared/consolidated/{YYYY-MM-DD}.md`(覆蓋不 append;`index_file` 是 replace-by-path,`fts_store.py:236`,冪等),交 `FtsMemory.index_file` 索引。

**召回 gating(pull-only)**:
- `_derive_memory_hits` 的 auto-inject 池**排除 `consolidated/`**(用 hit 的 `source` 欄位過濾,`fts_store.py:222`:`'consolidated/' not in source`)。candidate **不**進每 turn 的 `[Memory context]`。
- candidate 仍被索引、**可搜尋**:`Recall`、未來 A1 主動 pull 時拿得到。
- candidate 浮現處(Recall 結果渲染、未來 A1)加 **provenance 前綴** `- [系統整併·待確認] {fact}`,讓 Doll/A1 能辨識「機器觀察、非我的認知」,可降權/採用/否決。
- 非破壞性:原始 transcript/NoteMemory 不動。不做 compact(FtsMemory 無)。

### 3.4 日期選擇（R2 M2）

- **只整併已封日 `date < today`**:today 留到隔天當「昨天」併,避開 today 邊聊邊整併 race(代價:延一天;可接受)。因 today 永不入選,「同日重併」情境不存在。
- **watermark 用 strict `>`**:`_pick_target_date` 取 `date > last_consolidated_date` 的已封日(`>=` 會在 watermark 當天無限重併、永不前進)。覆蓋重建本身冪等(`index_file` 是 replace-by-path),不靠日期語意去重。
- **多日空窗 = oldest-first drain**:取「`last_consolidated_date` 之後、有內容、且 `< today` 的**最舊**日期」,每 cooldown 推進一天,離開多天回來逐日追上、**不漏**(對齊 §4「不漏資料」與 B1 投資)。

## 4. 狀態（MindState 擴充）

新增四欄;**`save_state` 是顯式列舉非 `asdict`(`mind_state.py:173`,缺欄位 loudly raise)→ 新增要同步改三處:dataclass 定義 + save_state dict + load_state 建構子**(R2 S1):
- `user_turn_count: int = 0` — 只在 `UserSpoke` 遞增(`mind_loop.py:152` 旁)。
- `last_consolidation_turn: int = 0` — 上次**成功**整併時的 `user_turn_count`。
- `last_consolidation_at: float = 0.0` — 上次**嘗試**時間(成敗皆更新)。
- `last_consolidated_date: str = ""` — 上次成功整併日期(跨日 watermark)。

**restart**:trigger 讀持久化狀態,重啟後一旦 idle 補做未整併批次(oldest-first,§3.4)。

## 5. 非目標

- **不改 self-profile / 自我**(A1;B2 只產 pull-only candidate)。
- **candidate 不 auto-inject `[Memory context]`**(R2:pull-only)。
- **不做 selective verifier**(core-loop-robustness §6.3;future)。
- **不做 compact**(FtsMemory 無)。
- **不吃 NoteMemory facts / 不吃全歷史**(只目標日 transcript)。
- **不做二階整併 / 不拆「去重層 vs candidate 觀察層」**(§8 長期形狀,future)。

## 6. 測試（TDD）

觸發:
- 三條件 AND:`conversation_idle` 不足/無新 user turn/cooldown 內 各不觸發;三者滿足才觸發。
- **`idle_s is None` → 忽略 optional gate,仍可觸發**(R1 核心,防 no-op);`idle_s` 可得且 host 忙 → 否決;sample 過期 → 不參與。
- `user_turn_count` 只 `UserSpoke` 增;monitor/Awoke/ReflectionMoment 不增(不誤觸發)。

keeper 契約(R2):
- `KEEPER_TOOLS` **正面 allowlist 斷言**:`⊆ {Report, Scratchpad, Recall}` 且 `∩ {Shell, NoteMemory, SpawnMonitor, RemoveMonitor} == ∅`(不 by-reference SUB_TOOLS)。
- driver 餵料(transcript inline 進 task)+ keeper Report.details + driver 寫檔三段;run_agent 以 `shell_runner=None` 呼叫。
- `asyncio.wait_for` timeout 觸發 → 當失敗處理。

失敗/取消/跨日:
- 失敗/取消後 `last_consolidation_at` 前進、`last_consolidation_turn`/`date` 不前進;trigger 自己 `save_state`。
- `UserSpoke`(text 與 **voice 兩路徑**)→ 進行中 `_consolidation_task` 被 cancel;cancel 時不寫半截檔。
- 同一已封日二次整併(watermark 未前進前重跑)→ 覆蓋重建(replace-by-path 冪等,不重複);today 不入選。
- 多日空窗 oldest-first:watermark 06-20、06-25/26 有料 → 先併 06-25(不跳過)。
- 隔天 today 空、昨天有料 → 併昨天。

產物/狀態/shutdown:
- 產物覆蓋重建正確日檔 + index;**`_derive_memory_hits` auto-inject 不含 consolidated/**;Recall 可 pull 到且帶 provenance 前綴。
- 原始 transcript 未改(非破壞性)。
- MindState 四欄位 save/load round-trip(顯式三處)。
- shutdown:in-flight keeper 在 `memsearch.close()` 前被 cancel(§10)。
- fake agent/LLM 隔離(沿用 `tests/_dispatcher_helpers`)。

## 7. 風險 / 剩餘

- **candidate 品質**:LLM 幻覺/過度概括。緩解:pull-only(不 auto-push)+ provenance 前綴 + prompt「寧缺勿濫」+ A1 由 Doll 人工 gate;一條幻覺 candidate 不會每 turn 自動冒出。
- **conversation_idle 被密集 monitor/schedule 餓死**:若主機有高頻背景 perception,`last_iter_at` 一直更新 → consolidation 長期不觸發。意圖即「不與 Doll 任何 LLM 活動爭算力」,可接受;記為已知後果。
- **閾值**(300/3600):數量級合理,config 化,實測微調。
- **per-character 路徑**:目前只 `shared/consolidated/`;未來 per-character 隔離時遷移。

## 8. 對 A1 / B3 的接口（seam）+ 長期形狀

- **A1 self-profile**:B2 的 `shared/consolidated/{date}.md` candidate 是 A1 的 pull 來源(帶 `[系統整併·待確認]`)。A1 的 reflection-gated tool 讓 Doll 從 candidate 挑進 self-profile(**pin 時複製文字進 self-profile,不依賴 consolidated 檔穩定**——覆蓋重建語意下檔會變)。
- **B3 energy**:本 spec 的 `conversation_idle = now - max(last_user_at, last_iter_at)` 正是 B3 衰減要的「閒置 vs 活躍」訊號;抽成共用 helper 供 B3 復用。
- **長期形狀(future,不在 B2)**:把產物拆成兩種 artifact——(a) 安全可 auto-inject 的「去重層」(壓縮 Doll **自己**的 NoteMemory/diary,她是作者)+ (b) pull-only 的「機器 candidate 觀察層」。B2 v1 只做 (b) 的 pull-only,不交付「candidate auto-inject 改善召回」那個好處(那好處應來自 (a),更乾淨)。

## 9. Review 狀態

- **R1(trigger)**:idle 來源(致命 no-op)、失敗 cooldown、可取消、restart、condition-2、跨日、覆蓋語意 → 已套用。
- **R2(keeper/autonomy/integration/scope,5 lens 全 code-verified)**:M1 keeper driver-fed 契約(收掉 Shell/NoteMemory 逃生口)、M2 日期 strict `>` watermark + oldest-first + 只併已封日(today 不併)、M3 cancel 接縫(kernel 兩 ingress,涵蓋 voice)、M4 shutdown 拆除、M5 召回 pull-only + provenance;S1-S5(落盤責任/DI+模板/wait_for+max_tokens/poll-helper 各自 restart-init/只吃 transcript)→ 全部套用。
- **狀態:plan-ready。** 進 writing-plans。

## 10. Shutdown 時序（R2 M4）

kernel `finally`(`kernel.py:664+`)在 `await self.workflow_runner.stop()` 同段、**`memsearch.close()` 之前**,對 `_consolidation_trigger_task`(poll task)與 `_consolidation_task`(in-flight keeper)各 `cancel()` + `await asyncio.gather(..., return_exceptions=True)`。否則 in-flight keeper 會用到已關的 sqlite 連線而 crash。沿用 `WorkflowRunner.stop()` 現成範式。
