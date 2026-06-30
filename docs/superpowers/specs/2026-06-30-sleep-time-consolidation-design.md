# Spec — B2: Sleep-Time Consolidation（睡眠期記憶整併）

- **Date**: 2026-06-30
- **Status**: DRAFT — R1(trigger 角度)adversarial review 已套用；keeper/autonomy/integration/scope 四角度待補(session reset 後),收斂後才進 writing-plans。見 §9。
- **Depends on**: B1 episodic transcript recapture（已 merged）。
- **Scope**: 本 spec 只做 B2。A1 self-profile（吃 B2 產物）、B3 energy（吃 B2 的 idle/活躍訊號）各自後續獨立 spec。
- **藍圖來源**: core-loop-robustness spec §P4(sleep-time consolidation:quiet-pulse → memory-keeper subagent → playbook + compact + 非破壞性衝突解決)；本 spec 是 P4 的首次落地（聚焦版）。

## 1. 背景與問題

B1 之後,對話逐字稿會進入可搜尋語料,但**沒有任何東西整理它**。原始 transcript + 每日零散 NoteMemory facts 只會單調增長:重複的事實、過時的細節、稀釋召回。Doll 自己在 probe 中明講「記憶不是筆記本,塞滿只會讓重要的變模糊」「準不要多」。

DollOS 目前三個背景迴圈(diary scheduler / schedule runner / ReflectionObserver)都只「丟提示 perception」,沒有任何一個會 summarize / dedup / 提取結構化事實。**缺一個 sleep-time 整併 pass。**

## 2. 設計原則（從 research + Doll probe 收斂）

- **sleep-time / idle 觸發**:整併在 host 閒置時跑,不佔對話算力(research:sleep-time compute;P4:quiet-pulse)。
- **Doll 主導自我的界線(關鍵)**:Doll probe 強烈反對「系統替我定義我是誰」。因此 B2 **只產出中性的 candidate 結構化事實**(關於用戶的穩定偏好/習慣、關係進展、值得記住的模式),寫進可召回層;**B2 絕不改寫 self-profile / 自我宣告**。self-profile 的寫入由 Doll 主導(A1 的 reflection-gated tool 從 candidates 中挑)。這調和「自動整併」與「Doll 主導自我」。
- **非破壞性**:不刪 / 不改原始 transcript 與 NoteMemory;整併只新增 consolidated candidate 層(P4:非破壞性衝突解決)。
- **No-fallback**:任何階段失敗 → log + 跳過該次整併,不降級、不 silent。

## 3. 架構:三個元件

### 3.1 ConsolidationTrigger（新背景 observer）

模板沿用 `ReflectionObserver`(`src/dollos/mind/reflection_observer.py`)的 poll + 重啟初始化結構;把共用的「poll 迴圈 + restart-init」抽成小 helper,但**維持兩個獨立 observer**(並行模型不同:ReflectionObserver 推 perception 走 mind_loop 串行;ConsolidationTrigger out-of-loop 直接跑 agent)。kernel 用 `asyncio.create_task(trigger.run())` wire(同 `kernel.py:655`)。

**idle 訊號用 conversation idle 為主（R1 致命修正）**:**不**綁 `SystemPulse.idle_s`——它來自 `xprintidle`(`system_pulse.py:226-235`),在 Wayland(主流桌面預設)/headless/未裝 xprintidle 時永為 `None`,會讓 B2 整個功能**靜默 no-op**。改用即時、DISPLAY 無關、已持久化的 `MindState.last_user_at` / `last_iter_at`(`mind_loop.py:152,271`):
`conversation_idle = now - max(last_user_at, last_iter_at)`。這同時語意更貼近 §2 的「不搶對話算力」。

觸發條件(全部滿足才跑一次):
1. **對話閒置**:`conversation_idle ≥ idle_threshold_s`(config,預設 300)。
2. **有新對話素材**:自上次整併後有新的 **user turn**——用只在 `UserSpoke` 遞增的計數器 `user_turn_count`(§4)判斷 `user_turn_count > last_consolidation_turn`。**不用 `iter_count`**(它含 monitor / Awoke / ReflectionMoment 等非對話 perception,會在無新對話時空跑、重讀同一份 transcript 再覆蓋)。
3. **cooldown**:`now - last_consolidation_at ≥ min_interval_s`(config,預設 3600)。

**SystemPulse.idle_s 降為 optional 加分 gate**:若 `SystemPulse` enabled 且 `latest_idle_s()` 非 `None`,額外要求 host 也閒置(AND 收緊);若為 `None`(Wayland/headless/disabled)則**忽略此 gate,不否決**——避免 idle_s 缺失導致整個 feature no-op。為此 `SystemPulse` 新增公開 `latest_idle_s() -> float | None`(取代伸手私有 `_last_sample`),並做新鮮度檢查(sample 的 `taken_at` 超過其 `poll_interval_s` 即視為過期、不參與,避免 stale 樣本在使用者剛返場時誤判閒置)。

poll 間隔沿用 5s 量級。觸發 → 交 §3.2;成敗後狀態更新見 §3.2 / §4。

### 3.2 memory-keeper agent（整併執行,可取消）

用 `agent_engine.run_agent`(`src/dollos/agent_engine.py:37`)跑 isolated sub-cascade(與 Subagent/Workflow worker 同引擎)。kernel 持有 `_consolidation_task`(`asyncio.create_task`)。

- **可取消（R1 修正:返場延遲）**:**收到 `UserSpoke` perception 時 cancel `_consolidation_task`**(整併走 asyncio,可被 `CancelledError` 中止)。使用者一返場立即讓出 LLM provider semaphore slot(對齊 llama `--parallel`),不被整併卡住——直接對應「不搶對話算力」與專案的延遲壓縮焦點。`max_tokens` / 迭代上限設保守,縮短單次佔用。
- **task prompt**:「讀『最近一個有新內容且未整併的日期』的 transcript + 近期 NoteMemory facts,提取去重成簡潔的**中性** candidate 事實——主人的穩定偏好/習慣、你們關係的進展、值得長期記住的模式。陳述為觀察(『主人偏好X』),**不要**寫成自我宣告(『我是X』)。重複合併、過時捨棄。準不要多。」
- **tools**:`SUB_TOOLS`(含 `Recall` + `ReadToolOutput`);整併產出走 agent 的結構化 `Report`。**工具集不含任何寫 self-profile 的 tool**——「不改自我」的界線由靜態工具集保證(可測),非靠 prompt 自律。
- **輸入界定**:目標日期的 transcript 檔(見 §4 跨日追蹤,可能是昨天)+ 近期 shared NoteMemory。不吃整個歷史(成本 + research:大歷史 full-context 不可行)。
- **失敗 / 取消語意（R1 修正:防 5s 重試風暴）**:
  - `last_consolidation_at` 在**嘗試後一律更新**(成功 / 失敗 / 取消皆更新)→ cooldown 一定前進,杜絕「失敗 → 每 5s poll 仍滿足 → 立刻重跑完整 LLM cascade」的無限重試。
  - `last_consolidation_turn` / `last_consolidated_date` **只成功才更新** → 下次 cooldown 過後重試同一批未整併素材,不漏資料。

### 3.3 產物:consolidated candidate facts

memory-keeper 的整併結果**覆蓋重建**目標日期檔 `memory_root/shared/consolidated/{YYYY-MM-DD}.md`(**覆蓋,不 append**——避免重複條目累積;每次整併把該日整份重來),交給 `FtsMemory.index_file` 索引 → 立即可被 `[Memory context]` / `Recall` 召回(`index_file` 是 replace-by-path,重寫同檔不會 FTS 重複)。

- 格式:markdown bullet,中性事實,每條一行。
- 非破壞性:原始 transcript / NoteMemory 不動(只新增 consolidated 層)。
- 這層同時是 **A1 self-profile 的 candidate 來源**(A1 之後讓 Doll 從這裡 pin;見 §8 接口)。
- 不做 `compact`(FtsMemory 無此 API;其 `index()` 全量重建已達等效,YAGNI)。

## 4. 狀態（MindState 擴充）

新增(納入 `save_state`/`load_state`,沿用既有 dataclass + asdict 模式):
- `user_turn_count: int = 0` — 只在 `UserSpoke` 遞增(`mind_loop.py:152` 旁);condition 2 的「新對話素材」判準,取代易誤觸的 `iter_count`。
- `last_consolidation_turn: int = 0` — 上次**成功**整併時的 `user_turn_count`。
- `last_consolidation_at: float = 0.0` — 上次**嘗試**整併時間(成敗皆更新;cooldown 用)。
- `last_consolidated_date: str = ""` — 上次成功整併的日期(跨日追蹤)。

**restart 語意（R1 修正:消除前後矛盾）**:ConsolidationTrigger **不**像 ReflectionObserver 在 `run()` 開頭丟棄 gap——它讀**持久化**狀態,**重啟後一旦 idle 會補做未整併的批次**(這才符合「consolidation 不因重啟漏資料」的初衷)。

**跨日追蹤（R1 修正:隔天空檔漏整併)**:觸發時整併「最近一個有新內容且 `日期 > last_consolidated_date` 的 transcript 日期檔」,**可能是昨天**——避免「隔天開機 → today 檔為空 → 整併空檔、昨晚尾段對話永遠漏整併且被標記成已整併」。整併成功後 `last_consolidated_date` 設為該日期。

## 5. 非目標

- **不改 self-profile / 自我**(A1 的事;B2 只產 candidate)。
- **不做 selective verifier**(core-loop-robustness §6.3;P4 說它是 design intent + seam,留 future)。
- **不做 memsearch.compact()**(FtsMemory 無此 API)。
- **不吃全歷史**(只今日 transcript + 近期 facts)。
- **不做跨日 / 長期 consolidation 的二階整併**(把 consolidated 再整併;future)。

## 6. 測試（TDD）

觸發條件:
- 三條件 AND:`conversation_idle` 不足不觸發 / 無新 user turn(`user_turn_count == last_consolidation_turn`)不觸發 / cooldown 內不觸發 / 三者皆滿足才觸發。
- **`idle_s is None`(Wayland/headless)→ 忽略 optional gate,仍可觸發**(R1 核心:不 no-op)。
- `idle_s` 可得且 host 忙(idle_s < 門檻)→ optional gate 否決,不觸發。
- `idle_s` sample 過期(超過 poll_interval)→ 視為不可信、不參與 gate。
- `user_turn_count` 只在 `UserSpoke` 遞增;monitor / Awoke / ReflectionMoment perception **不**使其前進(故不誤觸發)。

失敗 / 取消 / 跨日:
- agent 失敗或取消後:`last_consolidation_at` **有**前進(cooldown,防 5s 重試風暴)、`last_consolidation_turn` / `last_consolidated_date` **未**前進(下次重試同批)。
- `UserSpoke` 進來 → 進行中的 `_consolidation_task` 被 cancel。
- 跨日:`last_consolidated_date` 之後、today 為空但昨天有內容 → 整併**昨天**的檔(不空跑、不漏昨晚尾段)。

產物 / 狀態:
- 產物**覆蓋重建**正確日期檔 + 被 `index_file` 索引;原始 transcript 未被改動(非破壞性)。
- MindState 四個新欄位 save/load round-trip。
- memory-keeper 工具集**不含** self-profile 寫入 tool(靜態斷言界線)。
- 用 fake agent/LLM(沿用 `tests/_dispatcher_helpers` 模式)隔離 LLM。

## 7. 風險 / 剩餘問題

- **candidate 品質**:LLM 提取的事實可能幻覺 / 過度概括。緩解:只是 candidate(A1 由 Doll 人工 gate 過濾)。**但召回層([Memory context]/Recall)會先看到未過濾 candidate**——此點需 autonomy 角度確認是否已在替 Doll 塑形自我(R1 未審,待補,見 §9)。
- **閾值**(idle 300s / cooldown 3600s):trigger-review 認為數量級合理;保留為 config 預設,待 `last_user_at` 落地後依實測微調。
- **per-character 路徑**:目前只有 `shared/`,B2 產物放 `shared/consolidated/`;未來 per-character 隔離時遷移。
- **ReflectionObserver double-distillation**:Doll 在 ReflectionMoment 已可能 NoteMemory,B2 又抽一次;B2 以 transcript 為主、職責切分(即時自記 vs idle 重量 dedup)緩解,但需 keeper 角度確認(待補)。

## 8. 對 A1 / B3 的接口（seam）

- **A1 self-profile**:B2 產出的 `shared/consolidated/{date}.md` candidate facts 即 A1 的 pin 來源。A1 的 reflection-gated tool 讓 Doll 從這些 candidate 中挑進 self-profile(主導權留 Doll)。B2 **不**碰 self-profile。
- **B3 energy**:本 spec 用的 `conversation_idle = now - max(last_user_at, last_iter_at)` 正是 B3 energy 衰減要的「閒置 vs 活躍」訊號;B3 可復用同一計算(考慮抽成共用 helper)。

## 9. Review 狀態

- **R1(trigger 角度)**:已套用——idle 來源(致命)、失敗 cooldown、可取消、restart、condition-2、跨日、覆蓋語意全部修入上文。
- **待補(session limit,6:40pm UTC reset 後)**:keeper(memory-keeper/candidate)、autonomy(Doll 自主一致性)、integration(kernel/依賴注入)、scope(YAGNI/完整性)四個角度的對抗 review,收斂後再進 writing-plans。
