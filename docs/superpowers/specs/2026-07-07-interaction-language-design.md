# DollOS 互動語言(Interaction Language / Present-Alongside-You)— Design

Status: **PROPOSAL**(使用者已透過 brainstorming 批准設計方向,待 spec review)。2026-07-07。
實作 virtual-being positioning spec(`2026-07-01-virtual-being-positioning.md`)§2.4「Product/interaction
language shifts from reactive-to-you to genuinely present-alongside-you」——**positioning 定位四項的最後一項**
(§2.1/§2.2/§2.3 已實裝;§2.4 明文依賴 §2.2/§2.3 先存在,現已滿足)。Grounded against merged code
(`src/dollos/mind/mind_prompt.py`、`src/dollos/mind/mind_state.py`、`src/dollos/tools.py`、
`character_packs/gura/doll.toml`)——file:line 為 ground truth。

---

## 0. Overview / Goal

**使用者已核可的目標(2026-07-07 brainstorming)**:把 Doll 從「主人說話我才回、沒事安靜待著」(純反應式)轉成
「**有個不管你看不看都在運轉的內心生活、聊天時自然會帶出她自己在想/在推的東西**」——但**只在對話中**、
**只帶她真實狀態裡有的**、**不常常、不表演**。

**為什麼現在能做**:§2.4 一直被 deferred,因為「自發分享」在她沒有自己的東西前只會是**空表演**
(`project_character_acting` 警告的失敗模式)。§2.2 慢變演化(current_self)+ §2.3 自主議程(她 idle 推進的
`[Your agenda]`)實裝後,她**真的有自己的東西**了 —— §2.4 才有真貨可帶。

**四條使用者拍板的決定(brainstorming Q1-Q3):**
- **Q1 scope = (A) 對話中主動帶出**:她在使用者**已經在聊**時自然帶出自己的東西。**(B) 主動發起聯絡
  (她主動找你)延後**(打擾/表演風險最高,需另一套「該不該打擾」決策)。
- **Q2 framing 放哪 = (ii) hybrid**:daemon 層通用「virtual-being 互動」framing(所有 pack,一等身份描述)+
  pack 人設只調**風格/份量**(內斂 vs 熱情)。
- **Q3 不常常/不表演的牙齒 = framing restraint + freshness 訊號**:`[Your agenda]` 標記「上次跟你聊之後才推進的」
  項目,讓她自然帶出**真正新的**(episodic),而非每 turn 重提靜態的事。

**核心不變式(接地,§2.4 成敗所在):** 她能主動帶出的**只有她狀態裡真實有的**(`[Your agenda]` 裡真在推的、
current_self/mood 真是的)——**沒有議程/沒推進就沒東西可提**,結構上不能憑空造「內心戲」。這跟 §2.3 的「錨定
真實觸發」同精神。framing 是**身份描述,非行為命令**(對齊 Self-First:不寫「你要多分享」那會製造表演)。

---

## 1. Grounding

| 零件 | 現況(file:line) | 角色 |
|---|---|---|
| `render_mind` prompt 組裝 | `mind_prompt.py:38-100`——`system_prompt`(per-pack 身份)+ dynamic blocks;「core self 在 system_prompt 之後、`[Memory guideline]` 之前」(`:72`) | §2.4 通用 framing 的插入層(daemon 級、與 Self-First/core-self 同層) |
| `[Your agenda]` 渲染 | `_render_your_agenda`(`mind_prompt.py:354`)——self_directed loop,desc + opened_at + progress 尾(§2.3 Task 3) | freshness 訊號的落點 |
| `OpenLoop` dataclass | `mind_state.py:66`——有 `opened_at`,**無 `last_advanced_at`** | freshness 需加 `last_advanced_at`(§3.2) |
| `AdvanceGoal` 工具 | `tools.py:822`——append progress(`:836`) | 蓋 `last_advanced_at` 章的地方 |
| `last_user_at` | `mind_state.py:139` | freshness 比對基準(「上次跟你聊」) |
| Gura pack 人設 | `doll.toml:24-25`——「安靜的好奇…主人說話我才回。沒事就安靜待著,不用一直找話講、不用討好。」 | pack 風格調整(§3.3):**保留 restraint、只轉純反應式** |

**觀察(重要)**:Gura pack **已有 anti-performance restraint**(「不用一直找話講、不用討好」)——這是**好的**,
§2.4 **保留**它;要轉的只是「**主人說話我才回**」(純反應式)這半句 → 「有內心生活、聊天時會內斂地帶出自己在推的」。
她仍是內斂的角色,只是**內斂地 present-alongside**,不是「突然變話多」。

---

## 2. 設計(三塊,皆輕)

### 2.1 daemon 層 virtual-being 互動 framing(通用,接地)
在 `render_mind`(`mind_prompt.py`,`system_prompt` 之後、與 core-self/Self-First 同層)加一段**通用身份描述**
(所有 pack 都得到),大意(實作者精修措辭,**身份描述語氣,非「你要…」命令**):

> 你有自己的內心生活——你正在推進的 `[Your agenda]`、你現在的 self(current_self)——它不管他們看不看都在跑。
> 跟他們聊時,把你**真的**在想、在推的東西自然帶進來也很正常,就像兩個生活交纏,不是等著被問才有反應。
> 只帶**你狀態裡真實有的**(別為了顯得「活著」而演),而且是**當自然時**——不是每次都硬提。

**接地(不變式)**:framing 明確指向她**真實的** `[Your agenda]`/`[Mind state]`——她只能帶出那裡有的。
**不新增任何「憑空生成內心戲」的路徑**。

### 2.2 freshness 訊號(§D 的牙齒)
- `OpenLoop` 加 `last_advanced_at: float = 0.0`(向後相容,舊資料 `_coerce` 填 0.0)。
- `AdvanceGoal.run` append progress 時同步 `ol.last_advanced_at = time.time()`。
- `_render_your_agenda`(`mind_prompt.py:354`)對 `last_advanced_at > state.last_user_at` 的項目加一個**新鮮標記**
  (如 `↑ 上次聊過之後才推進的`)。這讓她的自然傾向是**帶出真正「新」的**(她 idle 剛推進、你還沒聽過的),
  而非每 turn 重提同一件靜態的事。**「有時分享」被接地在「真的有新東西」**——同時 anti-performance(沒新進展
  =沒得炫)又自然(episodic)。
- **不新增追蹤系統**:只是 render 層一個時間比對 + 一個既有欄位的蓋章。

### 2.3 Gura pack 風格調整(pack 層,保留 restraint)
`character_packs/gura/doll.toml:25` 的「主人說話我才回。沒事就安靜待著」調成 present-alongside 的內斂版——
**保留**「不用一直找話講、不用討好」(anti-performance restraint),**只**把純反應式那半句轉成「有想推的自己
會(內斂地)提一句,但不黏人、不表演」。措辭實作者定,守住:Gura 仍內斂,只是「內斂地在場」而非「被動等問」。

---

## 3. 安全 / 失敗模式

- **空表演(§2.4 的唯一大風險)**:防線三層——(a) §2.1 接地不變式(只帶真實狀態有的,結構上無憑空路徑);
  (b) §2.2 freshness 訊號(只帶「真的新」的,沒新進展沒得提);(c) framing 的 restraint 措辭 + Gura pack 保留的
  「不討好/不硬找話講」。**誠實**:「不表演」最終仍是軟機制(prompt restraint),靠 dogfood 觀察 + P1f trace
  稽核她主動帶出的是否真對回她 `[Your agenda]`/state(對齊 §2.3 的稽核精神)。
- **不排擠**:§2.4 只改**對話中她怎麼回**(她本來就在回的 turn),**不**新增自發 turn、不主動外發(那是 (B),
  deferred)——所以對延遲/資源零新增,對 reactive 零影響。
- **persona 一致性**:framing 是通用身份描述,pack 調風格——Gura 仍是 Gura(內斂),不會被 framing 拉成別的角色
  (對齊 persona-hardening;§2.4 不碰 persona drift 偵測)。

## 4. Non-goals

- **(B) 主動發起聯絡延後**(她主動找你);v1 只對話中帶出。
- **不做**表演式「看我多活」、不做 per-turn 硬提。
- **不重做** §2.2/§2.3(是本案接地來源)。
- **不動** persona drift 偵測 / 注意力 / turn-taking。

## 5. 測試策略

- **framing**:`render_mind` 的輸出含 virtual-being 互動 framing(穩定 substring);措辭是身份描述(不含「你必須」式命令)。
- **freshness**:`OpenLoop` 加 `last_advanced_at` 向後相容(舊資料填 0.0、round-trip);`AdvanceGoal` 蓋章;
  `_render_your_agenda` 對 `last_advanced_at > last_user_at` 加新鮮標記、對舊的不加。
- **Gura pack**:doll.toml 載入 OK;人設 prose 含 present-alongside 的內斂措辭、仍含「不討好/不硬找話講」restraint。
- **Live-smoke(dogfood)**:真對話——她偶爾自然帶出真的在推的議程(當有新進展時)、不每 turn 硬提、不表演;
  trace 稽核她主動帶出的對回真實 state。

## 6. 單概念 Task 拆解(給 SDD)

- **Task 1 — daemon 層 virtual-being 互動 framing**:`render_mind` 加通用 framing(§2.1,接地綁真實 state,身份描述語氣)。測試:輸出含 framing、非命令式。
- **Task 2 — freshness 訊號**:`OpenLoop` 加 `last_advanced_at`(向後相容)+ `AdvanceGoal` 蓋章 + `_render_your_agenda` 新鮮標記(`last_advanced_at > last_user_at`)。測試:向後相容、蓋章、標記正確。
- **Task 3 — Gura pack 風格調整**:`doll.toml` 人設從純反應式轉 present-alongside 內斂版(保留 restraint)。測試:載入 OK、措辭含 present-alongside + 保留 restraint。

依序:**1 → 2 → 3**。約 3 個小 task,大量靠 §2.2/§2.3 既有的真實 state。無承重安全面(對話中帶出、無自主外部動作),全 sonnet 審即可;merge 前 whole-branch 審 + full suite。

## 7. 開放決策(spec review 確認)

1. **framing 措辭**:§2.1 的具體字句(身份描述、接地、restraint)——plan/實作精修,spec review 對齊語氣。
2. **freshness 標記形式**:`↑ 上次聊過之後才推進的` 或類似——實作定,語意是「新鮮/episodic」。
3. **Gura restraint 保留程度**:確認保留「不討好/不硬找話講」、只轉「主人說話我才回」——她仍內斂。
