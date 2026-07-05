# P1c — Attention L0/L1/L2(注意力:該不該回 + 不用 tag 接著聊)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 讓 Doll 在多人 Discord 頻道**預設沉默**,只有 code 側 reply-worthiness 訊號(L0 硬規則 / L1 對話 session)才讓訊息升進她的 cascade;被捲入後不用 tag 也能接著聊(engagement window),且不會對每則訊息都回(disengage 閘)。這是 smolGura 失敗 1(亂回)+ 2(跟不上)的正解,也是首個 dogfood 里程碑的最後一塊注意力邏輯。

**Architecture:** 新增 **flow-agnostic 的 `AttentionGate`**(`src/dollos/mind/attention.py`)—— 純邏輯,持有 L0 硬規則 + L1 engagement session state(`(channel, participant_set, last_activity, turn_count, window)`)+ disengage 閘,對外只暴露 `admit(event, now) -> AdmitDecision`、`note_reply(channel_id, now)`、`window_for(channel_id, now) -> float`。**訊息流走 Option A**(spec §3.4「全量訊息都送 daemon」):bridge 變笨轉發器(移除 forward 前的 L0 閘,self-filter + ambient log 留下),daemon 收全量 ChannelEvent → `AttentionGate.admit` → 未 admit 直接 drop(ambient 已在 bridge 端存)→ admitted 經 differentiated debounce(`BatchAccumulator`,engaged ≈2s / cold ≈8s)批次入 perception queue → cascade(L2)。訊息流層(forward-all + kernel admission wiring)是**最後、最薄、可抽換**的部分:AttentionGate 核心不知道訊息怎麼來,故日後若改走 Option B(engaged-channel 訂閱信號)只動流量層,不動核心。

**Tech Stack:** Python 3.12、pydantic(config)、既有 `BatchAccumulator`(`ipc/batch_accumulator.py`,已寫未接線)、既有 `l0_wake`(`discord_bridge/wake.py`,搬 daemon 端)。無新第三方依賴。

## Global Constraints

- **語言**:comment/docstring 繁中或英文皆可;與使用者一律繁中。
- **No fallback / 升-code-閘鐵律**:「該不該回」的主防線是 **code 側 admission + disengage 閘**,不是弱模型 + 一句 prompt。L2 的沉默判斷是加分、非主防線(spec §3.4 [R2-att residual])。scaffolding 是描述性 nudge、不是命令。
- **engagement = 對話式,非 threading(R2-C1)**:window 綁 `(channel, participant_set, last_activity_ts)`,**不**用 Discord `message_reference`/reply 鈕。真人續聊直接打字、不按 reply,threading-only 會 under-fire =「跟不上」重演。
- **disengage 是真 code 閘(R2-att)**:`max_session_turns` 上限 + **重置只由「被再次 @/點名」觸發**(她自己發言只延續、不重置窗長)+ **窗長遞減**。防「她回→窗重置→又傾向回」的自我增強迴圈。
- **differentiated debounce**:engaged(session 內)`wake_debounce_engaged_s`≈2;cold `wake_debounce_cold_s`≈8。固定 8s 在熱串內堆疊遲到回覆(跟不上的另一向量)。
- **self-filter 留 bridge 端**:她自己的訊息**永不轉發**(bridge 知道自己 bot_id),但仍寫 ambient log(語料)。daemon 端 L0 不需再自濾。
- **未 admit → drop for attention**:不進 cascade、不觸發 turn。ambient log(bridge 端,P1b)已保留全量語料,daemon 不需為未 admit 訊息保存任何東西。
- **gated on P1a 單源 turn + P1e origin_tier**:admitted ChannelMessage 仍走既有 per-origin bucket + origin_tier 安全閘(P1e),P1c 只加「要不要讓它成 turn」的前置 admission,不動下游安全/路由。
- **Option A 決策(controller,2026-07-05)**:走 spec §3.4「全量送 daemon」。B(engaged-channel 訂閱)是 dogfood 規模的過早優化(YAGNI),留 P2。AttentionGate 設計為 flow-agnostic 以便日後可抽換。

## 範圍界定

**本 plan 只加一個概念:注意力 admission(L0/L1/L2 + engagement + disengage + debounce)。**

**不含**:進階鹽度(興趣關鍵字接 self_profile、話題延續)= P2(spec §3.4(d));情境化渲染完整版 = P1d(P1c 只加 L2 scaffolding 一句 nudge + 沿用 P1e 身分標示);語音 = P3;服務化 = P1g。

---

## File Structure

- **Create** `src/dollos/mind/attention.py` — `AttentionGate` + `Session` + `AdmitDecision`。flow-agnostic 核心。
- **Create** `tests/test_attention.py`、`tests/test_attention_engagement.py`、`tests/test_p1c_integration.py`。
- **Modify** `src/dollos/config.py` — `AttentionSettings`(name_aliases、always_wake_channels、debounce 窗、max_session_turns、window decay)。
- **Modify** `src/dollos/discord_bridge/controller.py` — forward-all(移除 `l0_wake` 前置閘;self-filter + ambient 留下)。
- **Modify** `src/dollos/kernel.py` — ChannelEvent 先過 `AttentionGate.admit`;admitted 經 `BatchAccumulator`(differentiated 窗)入 queue;turn 後 `note_reply`。
- **Modify** `src/dollos/mind/mind_prompt.py` — external turn 加 L2 scaffolding nudge。
- **Move/keep** `src/dollos/discord_bridge/wake.py` `l0_wake` 邏輯搬進 `attention.py`(daemon 端);bridge 端 wake.py 只留 self-filter helper(若還需要)。

---

## Task 1: AttentionGate L0(硬規則 admit + session 開啟)+ config

**Files:** Create `src/dollos/mind/attention.py`、`tests/test_attention.py`;Modify `src/dollos/config.py`

**Interfaces:**
- Produces:
  - `@dataclass class AdmitDecision: admit: bool; reason: str`(reason ∈ `"l0_dm"|"l0_mention"|"l0_name"|"l0_reply"|"l0_always"|"l1_continuation"|"not_admitted"`)。
  - `@dataclass class Session: channel_id: str; participants: set[str]; last_activity: float; turn_count: int; window_s: float`。
  - `class AttentionGate` — `__init__(self, *, name_aliases, always_wake_channels, owner_id, max_session_turns, window_base_s, window_decay, debounce_engaged_s, debounce_cold_s)`;`admit(self, event: dict, now: float) -> AdmitDecision`(本 task 只做 L0 分支 + 開 session;L1 continuation 分支 Task 2 補,先回 not_admitted);internal helpers `_l0_signal(event) -> str | None`。
  - `AttentionSettings(BaseModel)`:`name_aliases: list[str] = []`、`always_wake_channels: list[str] = []`、`max_session_turns: int = 6`、`window_base_s: float = 90.0`、`window_decay: float = 0.6`、`debounce_engaged_s: float = 2.0`、`debounce_cold_s: float = 8.0`。掛進 `Settings`。

L0 硬規則(從 `discord_bridge/wake.py:l0_wake` 搬,去掉 self-filter —— self 訊息 bridge 端已不轉發):`is_dm` → `l0_dm`;`mentioned` → `l0_mention`;任一 `alias in content`(substring,沿用既有語意)→ `l0_name`;`event.get("reply_to_bot")` → `l0_reply`;`channel_id in always_wake_channels` → `l0_always`;否則 None。L0 命中 → `admit=True` + **開/重置 session**(見下)。

開/重置 session(L0 命中時):`self._sessions[channel_id] = Session(channel_id, participants={event["author_id"]}, last_activity=now, turn_count=0, window_s=window_base_s)`。L0 是「被(再次)點名」→ 依 disengage 規則**重置**(turn_count 歸零、窗回 base)。

- [ ] **Step 1: 失敗測試** — `tests/test_attention.py`:
  - 每個 L0 訊號各 admit + 正確 reason(dm / mention / name-substring / reply_to_bot / always_wake)。
  - L0 命中開 session:`gate._sessions[ch]` 存在、participants 含 author、turn_count==0、window_s==base。
  - 非 L0 訊號(無 session)→ `admit=False, reason="not_admitted"`。
  - name_aliases 是 substring(`"古拉" in "hey 古拉 look"` → admit)。

```python
def test_l0_mention_admits_and_opens_session():
    g = _gate(name_aliases=["gura"], max_session_turns=6, window_base_s=90.0)
    d = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "hi"}, now=100.0)
    assert d.admit and d.reason == "l0_mention"
    s = g._sessions["c1"]
    assert s.participants == {"u1"} and s.turn_count == 0 and s.window_s == 90.0

def test_non_signal_without_session_not_admitted():
    g = _gate(name_aliases=["gura"])
    d = g.admit({"channel_id": "c1", "author_id": "u2", "is_dm": False, "mentioned": False, "content": "unrelated chatter"}, now=100.0)
    assert not d.admit and d.reason == "not_admitted"
```

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作 `attention.py`(L0 分支 + session 開啟)+ `AttentionSettings`**(L1 continuation 分支先 `return AdmitDecision(False, "not_admitted")` 佔位,Task 2 補)
- [ ] **Step 4: 跑綠**
- [ ] **Step 5: config 測試 + 全套回歸**
- [ ] **Step 6: Commit** — `feat(attention): AttentionGate L0 hard-rule admit + session open + config (P1c Task 1)` + trailers。

---

## Task 2: AttentionGate L1(續聊 admit + disengage 閘 + 窗長遞減 + note_reply)

**Files:** Modify `src/dollos/mind/attention.py`;Test `tests/test_attention_engagement.py`

**Interfaces:**
- Consumes: Task 1 的 Session/AttentionGate。
- Produces: `admit` 的 L1 分支;`note_reply(self, channel_id, now)`;`window_for(self, channel_id, now) -> float`;`is_engaged(self, channel_id, now) -> bool`。

L1 continuation admit(L0 未命中時):channel 有活躍 session `s` **且** `event["author_id"] in s.participants` **且** `now - s.last_activity < s.window_s` **且** `s.turn_count < max_session_turns` → `admit=True, reason="l1_continuation"`,並 `s.last_activity = now`(續聊延續 last_activity,但**不**重置 turn_count/窗——只有 L0 再點名才重置)。否則 not_admitted;若 session 已過期(`now - last_activity >= window_s`)或 turn_count 達上限 → 順手清掉 session(disengage)。

**disengage 閘**:`note_reply(channel_id, now)`(她在該 origin 說話後由 kernel 呼叫)→ `s.turn_count += 1`;`s.window_s *= window_decay`(窗長遞減);若 `s.turn_count >= max_session_turns` → 刪 session(她連續回應到上限,強制脫離,除非被再次 @ 重開)。

participant 擴充:被她在回覆中點名者、窗內對她發言者加入 participants —— **P1c 最小**:admitted continuation 的 author 已在 participants(前提);新人若 L0 點名她會自己開/併入。窗內「被她點名者加入」留 P2(需 parse 她的輸出);本 plan participants 只由 L0-author + continuation-author 構成,明文記此簡化。

`window_for`:`debounce_engaged_s if is_engaged(channel_id, now) else debounce_cold_s`。`is_engaged`:channel 有未過期 session。

- [ ] **Step 1: 失敗測試** — `tests/test_attention_engagement.py`:
  - **接著聊(跟得上)**:L0 開 session 後,同 participant 的續聊(無 mention/tag)在窗內 → `l1_continuation` admit。
  - **隔壁閒聊不插**:非 participant 的訊息(無 L0)→ not_admitted(收窄 over-fire)。
  - **disengage 上限**:`note_reply` 連呼到 `max_session_turns` → session 刪除;之後同 participant 續聊 → not_admitted(她停下來了)。
  - **只由再@重置**:達上限後,一則新的 L0 mention → 重開 session(turn_count 歸零)→ 又能 admit。她自己 note_reply **不**重置窗長(窗遞減、turn_count 累加)。
  - **窗長遞減**:每 note_reply 後 `window_s == base * decay^n`。
  - **窗過期脫離**:超過 window_s 沒活動 → continuation not_admitted + session 清除。
  - **differentiated debounce**:engaged channel `window_for`==debounce_engaged_s;cold channel==debounce_cold_s。

```python
def test_continuation_admits_without_tag_then_disengages_at_max_turns():
    g = _gate(name_aliases=["gura"], max_session_turns=2, window_base_s=90.0, window_decay=0.6)
    g.admit({"channel_id":"c1","author_id":"u1","is_dm":False,"mentioned":True,"content":"gura?"}, now=100.0)
    # she replies
    g.note_reply("c1", now=101.0)
    # same participant continues, no tag → admitted
    d = g.admit({"channel_id":"c1","author_id":"u1","is_dm":False,"mentioned":False,"content":"and also"}, now=102.0)
    assert d.admit and d.reason == "l1_continuation"
    g.note_reply("c1", now=103.0)  # turn_count now 2 == max → disengage
    d2 = g.admit({"channel_id":"c1","author_id":"u1","is_dm":False,"mentioned":False,"content":"you there"}, now=104.0)
    assert not d2.admit  # she stopped; only a re-mention reopens
    d3 = g.admit({"channel_id":"c1","author_id":"u1","is_dm":False,"mentioned":True,"content":"gura!"}, now=105.0)
    assert d3.admit and d3.reason == "l0_mention"

def test_bystander_chatter_not_admitted():
    g = _gate(name_aliases=["gura"])
    g.admit({"channel_id":"c1","author_id":"u1","is_dm":False,"mentioned":True,"content":"gura?"}, now=100.0)
    d = g.admit({"channel_id":"c1","author_id":"stranger","is_dm":False,"mentioned":False,"content":"unrelated"}, now=101.0)
    assert not d.admit  # non-participant bystander in the same channel
```

- [ ] **Step 2-6:** 跑 fail → 實作 L1 分支 + note_reply + window_for + is_engaged → 跑綠 → 全套回歸 → Commit `feat(attention): L1 continuation admit + disengage gate + window decay + note_reply (P1c Task 2)`。

---

## Task 3: bridge forward-all(移除轉發前 L0 閘;self-filter + ambient 留)

**Files:** Modify `src/dollos/discord_bridge/controller.py`;Test `tests/test_discord_forward_all.py`

**背景**:P1b 的 `controller._capture_and_maybe_wake`(controller.py:161-206)在轉發前跑 `l0_wake`,只轉發被喚醒的。Option A 要 daemon 看全量,故 bridge 改成:append ambient(全量)+ **轉發所有非-self 的 allowlist 頻道訊息**(L0/L1 判斷移到 daemon)。self 訊息仍只落 ambient、不轉發。

- [ ] **Step 1: 失敗測試** — `tests/test_discord_forward_all.py`:
  - 非-self、無 L0 訊號的訊息 → **仍轉發**(`_daemon_send` 被呼叫,payload 含 author_is_owner + 完整 event)。
  - self 訊息(author_id==bot_id)→ **不轉發**、但 ambient.append 被呼叫(語料留)。
  - ambient.append 對全量(含未轉發的 self)仍呼叫。
  - 動態 ChannelRegister(P1b first-wake 註冊)改為 **first-forward** 註冊(頻道首次轉發時註冊路由,不再綁 L0)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — `_capture_and_maybe_wake` 改名 `_capture_and_forward`:append ambient(全量)→ 若 `author_id == bot_id` return(self,不轉發)→ 動態 register(首次)→ `_daemon_send(ChannelEvent(...))`。**移除** `l0_wake(...)` 前置閘與其 import(l0_wake 邏輯已搬 daemon 端 attention.py,Task 1)。保留 backfill/reconnect 路徑一致走 `_capture_and_forward`。
- [ ] **Step 4-6:** 跑綠 → 全套回歸(P1b 既有 wake 測試會變:改測 forward-all + self-not-forwarded;更新之)→ Commit `feat(attention): bridge forwards all allowlist-channel messages (L0/L1 moved daemon-side) (P1c Task 3, Option A)`。

---

## Task 4: kernel admission wiring(ChannelEvent → AttentionGate → drop / enqueue)+ note_reply

**Files:** Modify `src/dollos/kernel.py`;Test `tests/test_kernel_attention.py`

**Interfaces:**
- Consumes: `AttentionGate`(Task 1/2)。
- Produces: kernel `_handle_message` ChannelEvent 分支先過 `AttentionGate.admit`;未 admit → return(drop);admitted → enqueue perception(Task 5 會插 debounce)。turn 完成後 `note_reply`。

- [ ] **Step 1: 失敗測試** — `tests/test_kernel_attention.py`:
  - admitted ChannelEvent → perception 入 queue(既有行為)。
  - **未 admit** ChannelEvent → perception **不**入 queue(drop);ambient 不受影響(bridge 端)。
  - owner preempt/cancel(P1b/P1e)只在 admitted 時觸發(未 admit 的 owner 訊息—— 罕見,但 owner 一定 L0 admit(DM 或 mention),故實務上 owner 恆 admitted;測試明文此)。
  - 她在 external origin 說話後 `attention.note_reply(origin)` 被呼叫一次(turn 完成 hook)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — kernel 建 `AttentionGate`(從 AttentionSettings);`_handle_message` ChannelEvent 分支開頭:
  ```python
  decision = self._attention.admit(payload, time.time())
  if not decision.admit:
      return  # default silence: not reply-worthy; ambient (bridge)已存全量語料
  ```
  admit 後才做既有的 owner preempt/cancel + enqueue。`note_reply`:turn 完成後(mind_loop finish hook 或 kernel 觀察 turn 結束),若該 turn origin 是 external 且她有 speech → `self._attention.note_reply(origin, time.time())`。**接線點**:mind_loop 已有 `_turn_speech` + finish;加一個 turn-complete callback(origin, spoke: bool)給 kernel,kernel 轉呼 note_reply。避免 mind_loop 直接依賴 attention(保持 flow-agnostic)。
- [ ] **Step 4-6:** 跑綠 → 全套回歸 → Commit `feat(attention): kernel admission gate (drop non-admitted) + note_reply on reply (P1c Task 4)`。

---

## Task 5: differentiated debounce(接線 BatchAccumulator;engaged 短 / cold 長)

**Files:** Modify `src/dollos/kernel.py`;Test `tests/test_kernel_debounce.py`

**背景**:`BatchAccumulator`(`ipc/batch_accumulator.py`)已寫未接線。admitted 訊息在入 queue 前經 per-channel debounce:engaged(session 內)`debounce_engaged_s`≈2 即時跟聊,cold `debounce_cold_s`≈8 防洪。

- [ ] **Step 1: 失敗測試** — `tests/test_kernel_debounce.py`:
  - engaged channel 的多則 admitted 訊息在 `debounce_engaged_s` 內合批成一次 enqueue(不是 N 次 → N turns)。
  - cold channel 用 `debounce_cold_s`(較長)。
  - 窗選擇由 `attention.window_for(channel_id)` 決定(engaged→短)。
  - 合批後的 perceptions 仍 per-channel 單源(對齊 P1a drain_grouped)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — kernel 建 `BatchAccumulator(enqueue=self._enqueue_batch, ...)`;admitted ChannelEvent → `await self._accumulator.add(channel_id, perception_data, window=self._attention.window_for(channel_id, now))`。accumulator flush → `_enqueue_batch` 把該 channel 的批次 perceptions 入 queue。**注意**:`BatchAccumulator.add` 現簽名是 `(channel_id, item)` 固定 window;若不支援 per-call window,擴充成 per-channel window 可變(engaged/cold 切換)——讀其現況(`ipc/batch_accumulator.py`)決定是擴充簽名還是每 channel 記 window。保持 flush 語意不變。
- [ ] **Step 4-6:** 跑綠 → 全套回歸 + 既有 `test_batch_accumulator.py` 仍綠 → Commit `feat(attention): differentiated debounce — engaged short / cold long window via BatchAccumulator (P1c Task 5)`。

---

## Task 6: L2 scaffolding(外部場合 nudge)

**Files:** Modify `src/dollos/mind/mind_prompt.py`;Test `tests/test_mind_prompt_scaffolding.py`

**背景**:L2 admit 後她仍可選 Say 或沉默(既有 cascade,0..N actions)。加**描述性** scaffolding(非命令)讓沉默是自然選項:「妳在公開場合聽得到很多不關妳的話,不回是正常的。」只在 external turn(origin_tier != internal)加。

- [ ] **Step 1: 失敗測試** — `tests/test_mind_prompt_scaffolding.py`:external turn 的 render 含此 nudge(描述性文字);internal turn **不**含。owner-DM 是否含 —— owner DM 是私訊 1:1,「不關妳的話」語境不適用,故只 external_public turn 加(或 external 全加但措辭中性)。**選** external_public 加(最貼合「公開場合」);測試對齊。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — `mind_prompt.py` 於 external_public turn 的組裝處加一行 scaffolding block(描述性、非命令祈使)。沿用 P1e 的 origin_tier 訊號。
- [ ] **Step 4-6:** 跑綠 → 全套回歸 → Commit `feat(attention): L2 external-situation scaffolding nudge (P1c Task 6)`。

---

## Task 7: 整合測試 —— smolGura 三失敗模式(亂回 / 跟不上 / 串內 over-fire)

**Files:** Test `tests/test_p1c_integration.py`

**背景**:P1c 的驗收是 smolGura 失敗 1(亂回)+ 2(跟不上)不重演,含 R2 要求的「串內對每則都回」over-fire 測試(原 smoke 2c 只測串外)。端到端驅動 kernel/AttentionGate/mind_loop。

- [ ] **Step 1: 失敗測試 → 實作已在 Task 1-6,本 task 是整合網**(driving 真實 kernel + AttentionGate + 一個 fake/scripted LLM):
  - **亂回不重演**:一個陌生人在多人頻道講與她無關的話(無 mention/name/非 session participant)→ **不 admit → 無 cascade → 無回覆**(sink 無 AddressedText)。
  - **跟不上不重演**:她被 @ 捲入 → 開 session → 同人續聊(無 tag)→ admit → 她能接著回(sink 有回覆到對頻道)。
  - **串內 over-fire 不重演**:她被捲入後,對話持續 → `max_session_turns` 後她**停下來**(disengage),不對每則都回;之後被再次 @ 才重新加入。
  - **隔壁閒聊**:session 進行中,非 participant 的隔壁話題 → 不插。
- [ ] **Step 2-3:** 跑綠(可能需補 Task 1-6 的邊角)。
- [ ] **Step 4:** 全套 `uv run pytest tests/ -q`(最後 task,全綠 minus 3 torch 最重要)。
- [ ] **Step 5:** Commit `test(attention): P1c integration — smolGura failure modes (亂回/跟不上/串內over-fire) don't recur (P1c Task 7)`。

---

## Self-Review(對 spec §3.4 注意力段逐條核)

- [x] 預設沉默 + code reply-worthiness admission → Task 4(未 admit → drop,不裸 cascade)
- [x] L0 硬規則(DM/mention/name/reply/always_wake)→ Task 1(搬 daemon 端)
- [x] L1 engagement window = 對話式 `(channel, participant_set, last_activity)` 非 threading → Task 2
- [x] 串內 disengage code 閘(max_session_turns + 重置只由再@ + 窗長遞減)→ Task 2
- [x] differentiated debounce(engaged≈2 / cold≈8)→ Task 5
- [x] L2 admit 後 Say/沉默 + scaffolding(描述性 nudge)→ Task 6(主防線是 Task 2 code 閘,L2 加分)
- [x] 全量送 daemon(Option A)→ Task 3(bridge forward-all)+ Task 4(daemon admission)
- [x] 串內 over-fire 測試(R2 明文要求,原 2c 只測串外)→ Task 7
- [x] self-filter 留 bridge、不轉發自己 → Task 3
- [x] 進階鹽度 = P2、participants「被她點名者加入」= P2 → 明文簡化(Task 2)

**Placeholder scan:** 每 code step 給實際 code / 精確 file:line。`AdmitDecision`/`Session`/`AttentionSettings` 型別一致貫穿。
**Type consistency:** `admit() -> AdmitDecision`、`window_for() -> float`、`note_reply(channel_id, now)` Task 1→7 一致。Task 2 consume Task 1 的 Session;Task 4/5 consume Task 1/2 的 gate;Task 4 的 note_reply hook 被 Task 5 的 debounce 不影響(note_reply 是 turn 後、debounce 是入 queue 前)。
**跨 task:** Task 1(L0)必先;Task 2(L1)依 Task 1;Task 3/4/5(流量層)依 Task 1/2 的 gate;Task 6 獨立;Task 7 依全部。

---

## 執行銜接

依 `feedback_subagent_driven_default`:直接進 `superpowers:subagent-driven-development`,每 task fresh implementer + reviewer(sonnet),whole-branch review 用 opus(嚴查:admission 真的擋掉亂回 code 側非弱模型、disengage 閘真的會讓她停、forward-all 沒漏 self-filter、debounce 合批不丟訊息)。worktree `.worktrees/p1c-attention/` on branch `p1c-attention`。**驗收 = Task 7 整合測試三失敗模式不重演**。完成 merge 後 P1a+P1b+P1f+P1e+P1c = **首個 dogfood 里程碑**,再 P1g 服務化即可上線 dogfood。
