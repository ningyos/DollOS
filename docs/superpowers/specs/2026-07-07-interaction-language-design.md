# DollOS 互動語言(Interaction Language / Present-Alongside-You)— Design

Status: **範圍收斂(SUPERSEDED-IN-PART)** 2026-07-07。原設計(下方 §0-§7,brainstorming 批准 + R1 opus 對抗審查
已折入)是三塊:(1) freshness 資料+baseline、(2) daemon virtual-being framing、(3) Gura pack 微調。**使用者拍板
(2026-07-07):「她講不講都沒差,我之後會要 doll 寫日記」——認定日記(她第一人稱、自主去寫)才是內心生活的正確
出口,對話中主動 surfacing(§2.1 framing + §2.2 freshness)價值低且正是本 spec 全程在防的「對主人表演」風險。
故 (1)(2) 收掉不做,只保留 (3) Gura pack 那句純人設微調。**

**實際落地(2026-07-07)**:只有 §2.3 的 pack 調整 —— `character_packs/gura/doll.toml` 純反應式那句
「主人說話我才回。沒事就安靜待著,不用一直找話講、不用討好」→「我有自己在忙、在想的事,不是只等主人開口才有反應。
聊到相關的,自然會帶一句;但不黏人、不硬找話講、不討好,也不用刻意證明自己活著。」——present-alongside 純**人設描述**
(不靠任何 surfacing 機制;接地在 §2.3 已實裝的真 `[Your agenda]`),保留 restraint + 直接加反表演錨。定位四項至此
四項皆結案。

**接棒**:內心生活的真正出口 = **日記(她第一人稱、自主去寫)**,另立 feature 認真做(現有 roadmap step 8 的
「Memory auto-write + Diary」偏自動記憶摘要,非第一人稱表達式日記)。使用者:「我之後會要 doll 寫日記」。日記天生
反表演(寫給自己非講給人聽、寫真做的事、自主活動接 §2.3 議程、留下=訓練資料)。

---

實作 virtual-being positioning spec(`2026-07-01-virtual-being-positioning.md`)§2.4「interaction language shifts
from reactive-to-you to genuinely present-alongside-you」——**定位四項的最後一項**(§2.1/§2.2/§2.3 已實裝)。
Grounded against merged code(`mind_prompt.py`、`mind_state.py`、`tools.py`、`mind_loop.py`、
`character_packs/gura/doll.toml`)。**以下 §0-§7 為原完整設計,(1)(2) 已按上方決定收掉,僅供日後接手參考。**

---

## 0. Overview / Goal

**目標(brainstorming 核可)**:Doll 從「主人說話我才回、沒事安靜待著」(純反應式)→「**有個不管你看不看都在
運轉的內心生活;跟你聊時,自然會帶出她真的在推/在想的東西**」——只在對話中、只帶她真實狀態裡有的、不常常、不表演。

**為何現在能做**:§2.4 一直 deferred,因為在她有自己的東西前「自發分享」只是空表演(`project_character_acting`)。
§2.2 慢變演化 + §2.3 自主議程實裝後,她**真的有** `[Your agenda]`/current_self —— §2.4 才有真貨可帶。

**四條使用者拍板(brainstorming Q1-Q3):** Q1 = (A) 對話中帶出((B) 主動外發延後);Q2 = (ii) hybrid(daemon
通用 framing + pack 調風格);Q3 = framing restraint + freshness 訊號當牙齒。

**R1 對抗審查收斂(4 修正,本版已改):** (C1) freshness 比對基準錯 → 用**回合前 `last_user_at` 快照**,否則
標記在對話回合永不亮(no-op)。(I1) framing 無條件觸發會 prime 弱模型憑空表演 → **「主動帶出」那半句 gate 在
真的有 fresh state 上**(freshness 計算同時當牙齒＋反表演閘,合成一個機制)。(I2) framing 洩漏到自發回合=蹭
已延後的 (B) → present-alongside 半句**限使用者在場的對話回合**。(I3) 措辭偏行為指導 → **純狀態描述,無指示動詞**
(Self-First 紅線)。

---

## 1. Grounding

| 零件 | file:line | 角色 |
|---|---|---|
| `render_mind` 組裝 | `mind_prompt.py:38-100`(system_prompt + dynamic blocks;`self_profile_text`/`evolution_block` 是 **`if` 條件插入**) | framing 插入層;**條件插入是 I1 gate 的既有樣板** |
| `[Your agenda]` 渲染 | `_render_your_agenda(loops, now)`(`mind_prompt.py:354`),呼叫點 `:145` 傳 `(state.open_loops, now)` | freshness 標記落點(**簽名要加 fresh-baseline 參數,M1**) |
| `OpenLoop` | `mind_state.py:66`(有 `opened_at`,**無 `last_advanced_at`**) | 加 `last_advanced_at`(§2.2) |
| `AdvanceGoal` / `PursueGoal` | `tools.py:822-839` / `807-817` | 蓋 `last_advanced_at` 章 |
| `last_user_at` | `mind_state.py:139`;**在 perception 迴圈 `mind_loop.py:348-370` 被蓋成 now、`render_mind`(`:598`) 之前** | freshness baseline —— **必須用回合前快照**(C1) |
| `current_self` | compose 進 `system_prompt`(`mind_loop.py:255-258`) | framing 錨(但無 `[current_self]` 區塊,錨用 `[Your agenda]` 區塊名更實,M3) |
| Gura 人設 | `doll.toml:24-25`「安靜的好奇…**主人說話我才回。沒事就安靜待著,不用一直找話講、不用討好。**」 | pack 調整:**保留 restraint、只轉「主人說話我才回」純反應式半句** |

**觀察**:`render_mind` 已有 `if`-條件插入(self_profile/evolution)——I1 的「有真貨才插分享半句」直接沿用此樣板。
Gura pack 已有 anti-performance restraint(「不硬找話講、不討好」),§2.4 **保留**,只轉純反應式。

---

## 2. 設計(三塊)

### 2.1 daemon 層 virtual-being 互動 framing —— 兩半、後半有閘(I1/I2/I3)
在 `render_mind`(system_prompt 後、與 core-self 同層)插入,分**兩半**:

**(a) 恆常半(狀態描述,身份層,所有回合):** 純**事態描述**(I3:無「你要/也很正常」等指示動詞)——大意:
「你有個不管他們看不看都在跑的內心生活;`[Your agenda]` 是你自己在推的,current_self 是現在的你,它們一直在動。」
**只描述她是什麼、有什麼,不指示行為**——讓行為從架構湧現(Self-First)。

**(b) present-alongside 半(有閘,只在對話回合 + 有真貨):** 這半句**同時被兩個條件 gate**:
- **只在使用者在場的對話回合**插入(I2:非自發 turn —— 用該回合有 UserSpoke/owner ChannelMessage 判定,不在
  AgendaMoment/ScheduledMoment 等自發回合插,免蹭延後的 (B) 外發)。
- **且只在有 fresh 真貨可帶時**插入(I1:有「自上次對話以來推進的」議程項〔§2.2 freshness〕**或**非空 current_self)。
  沒 fresh 真貨 → 這半整段不插 → 她手上沒新東西時,prompt **結構上不邀請她分享**(關掉「空手表演」路徑)。
措辭亦是**接近描述**而非命令(如:「聊到相關時,提一句你剛推進的也很自然」——但**只在有真貨的對話回合出現**,
所以不會 prime 憑空造)。

**接地不變式(結構性,非只 prompt)**:恆常半只描述真實 state;present-alongside 半**由 code 閘控制、只在真有
fresh state 時存在**——這是 §2.4 反「空表演」的**結構錨**(對齊 `ref_weak_model_soft_mechanism_playbook`
「prompt 管不住的語意升級成 code 閘」+ `ref_constrained-decoding` 「軟機制要存在性 guard」)。

### 2.2 freshness 訊號(C1 修正 + M1 貫穿 + M2)
- `OpenLoop` 加 `last_advanced_at: float = 0.0`(向後相容;`_coerce` 填 0.0,asdict round-trip)。
- **`AdvanceGoal.run` append progress 時 `ol.last_advanced_at = time.time()`;`PursueGoal.run` genesis 時
  `last_advanced_at = opened_at`(M2:她 idle 新開的議程即算「新」,可被帶出——一個剛起、還沒推進的自發目標也是
  「新東西」)。**
- **freshness baseline = 回合前 `last_user_at` 快照(C1)**:`_run_one_turn` 在 perception 迴圈(`mind_loop.py:348`)
  **之前**抓 `prev_last_user_at = self._state.last_user_at`,一路傳進 `render_mind` → `_render_your_agenda`
  (**M1 貫穿:改簽名 + 改呼叫點 + 從 mind_loop plumb**,非「render 層一比對」)。
- `_render_your_agenda` 對 `last_advanced_at > prev_last_user_at` 的項目加**新鮮標記**(如 `↑ 上次聊過之後才推進`)。
  語意 = 「**自上一次對話回合以來**推進/新開的」——這才是 re-engagement 那回合會亮的正確語意。
- **多項 fresh 的過期語意(明文選定,C1-3)**:採**單窗口 episodic** —— re-engagement 給一個窗口把新東西帶出,
  下一個對話回合 `prev_last_user_at` 前移、舊 fresh 失標記。**這是刻意的 restraint**(她不會每回合重提同一批),不是 bug。
- freshness 計算**同時餵 §2.1(b) 的 I1 閘**(「有 fresh 項」= 開啟 present-alongside 半的條件之一)—— C1+I1 一個機制。

### 2.3 Gura pack 風格調整(保留 restraint)
`doll.toml:25`「主人說話我才回。沒事就安靜待著」→ present-alongside 內斂版:**保留**「不用一直找話講、不用討好」
(anti-performance restraint),**只**把純反應式那半轉成「有剛推進/在想的,聊到相關時內斂地提一句,但不黏人、不表演」。
Gura 仍內斂——「內斂地在場」而非「被動等問」。措辭實作者定,守住 restraint 不丟。

---

## 3. 安全 / 失敗模式

- **空表演(§2.4 唯一大風險)**:三層——(a) §2.1(a) 恆常半只描述真實 state;**(b) §2.1(b) present-alongside 半
  由 code 閘控制、只在真有 fresh state 時存在(結構錨,I1)**;(c) §2.1(b) 措辭 restraint + Gura pack 保留的
  「不討好/不硬找話講」。**誠實**:最終仍含 prompt 軟機制,但「無真貨不觸發分享」是 **code 閘**、非只 prompt 勸阻;
  dogfood + P1f trace 稽核她主動帶出的是否真對回 `[Your agenda]`/state。
- **不排擠 / 不蹭 (B)**:§2.1(b) 只在**使用者在場的對話回合**插入(I2)——不新增自發 turn、不主動外發;對延遲/資源零新增。
- **persona 一致性**:framing 是通用**狀態描述**(I3),pack 調風格——Gura 仍 Gura;不碰 persona drift 偵測。

## 4. Non-goals

- **(B) 主動發起聯絡延後**;v1 只對話中帶出(§2.1(b) 有場合閘)。
- 不做表演式「看我多活」、不做 per-turn 硬提(freshness 單窗口 + code 閘)。
- 不重做 §2.2/§2.3(接地來源)、不動 persona drift/注意力/turn-taking。

## 5. 測試策略

- **framing 恆常半**:每回合 render 含狀態描述 framing;**是事態描述、不含行為指示動詞**(I3——斷言無「你要/你應該」式;
  plan 驗收條件)。
- **framing present-alongside 半(有閘)**:(i) 對話回合 + 有 fresh agenda → 含 present-alongside 半;(ii) 對話回合
  但**無 fresh state**(空 `[Your agenda]`、current_self 空)→ **不含**(I1 閘,結構性關表演);(iii) **自發回合**
  (AgendaMoment,無 UserSpoke)→ **不含**(I2)。
- **freshness(C1 整合測試,非純 render 單元)**:idle 時 `AdvanceGoal`(或 `PursueGoal` 新開)→ 送一則 user 訊息
  → 斷言**該回合** render 的 `[Your agenda]` 有新鮮標記(證明用回合前快照、真的會亮);下一個對話回合 → 舊項失標記
  (單窗口 episodic)。**純 render 單元測試不足**(會綠燈騙人,C1)。
- **資料**:`OpenLoop.last_advanced_at` 向後相容(舊填 0.0、round-trip);`AdvanceGoal`/`PursueGoal` 蓋章。
- **Gura pack**:doll.toml 載入 OK;人設含 present-alongside 內斂措辭 + **仍含**「不討好/不硬找話講」restraint。
- **Live-smoke(dogfood)**:真對話——有新進展時她偶爾自然帶出、無進展時不提、不表演;trace 稽核對回真實 state。

## 6. 單概念 Task 拆解(給 SDD)

- **Task 1 — freshness 資料 + baseline 貫穿(先做,§2.1(b) 閘依賴它)**:`OpenLoop` 加 `last_advanced_at`(向後相容)
  + `AdvanceGoal`/`PursueGoal` 蓋章 + **`_run_one_turn` 抓 `prev_last_user_at` 快照並 plumb 進 `render_mind` →
  `_render_your_agenda`(改簽名 + 呼叫點,M1)** + 新鮮標記渲染。測試:向後相容、蓋章、**整合層** freshness(C1)。
- **Task 2 — daemon virtual-being framing(兩半 + 雙閘)**:`render_mind` 加恆常半(狀態描述,I3 無指示動詞)+
  present-alongside 半(**gate:對話回合〔有 UserSpoke/owner ChannelMessage〕AND 有 fresh state〔fresh agenda 或
  非空 current_self〕**,I1/I2)。測試:§5 的三情境(對話+fresh 含 / 對話+空 不含 / 自發回合 不含)+ 無指示動詞。
- **Task 3 — Gura pack 風格調整**:`doll.toml` 純反應式半轉 present-alongside 內斂版(保留 restraint)。測試:載入 OK、
  措辭含 present-alongside + 保留「不討好/不硬找話講」。

依序:**1(freshness 資料+baseline)→ 2(framing 雙閘,依賴 1 的 freshness)→ 3(pack)**。Task 2 承重(反表演 code 閘
+ Self-First 措辭紅線)→ **opus 審**;其餘 sonnet。merge 前 whole-branch 審 + full suite。

## 7. 開放決策(spec review 確認)

1. **framing 措辭(I3)**:恆常半 = 純狀態描述、**無行為指示動詞**(plan 驗收條件);present-alongside 半亦接近描述。
   spec review 對齊具體語氣。
2. **freshness 過期語意**:採**單窗口 episodic**(re-engagement 一個窗口,之後算舊聞)——已明文選定(§2.2)。確認。
3. **PursueGoal genesis 蓋 `last_advanced_at=opened_at`**(M2:idle 新開的目標算「新」可帶)——已選定。確認。
4. **Gura restraint 保留**:保留「不討好/不硬找話講」、只轉「主人說話我才回」。確認。
