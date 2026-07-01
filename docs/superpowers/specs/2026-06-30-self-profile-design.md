# Spec — A1: Self-Profile（演化中的自我·薄補充層）

- **Date**: 2026-06-30
- **Status**: PLAN-READY（R1 3-lens adversarial review 已收斂套用，見 §9）
- **Depends on**: B2 sleep-time consolidation（已 merged；其 pull-only candidate 是 A1 的 pin 來源）。
- **Scope**: 只做 A1。與 B3 平行、互不依賴。

## 1. 背景與問題

DollOS 維持自我的現況:(a) character pack 描述(identity/personality/taboos)**靜態** always-inject 在 system prompt;(b) 對話事實走 FTS top-K 召回(query 依賴)。

缺口(2026-06-30 deep-research 確認,見 [[ref_always_inject_self_profile]]):**「演化中的自我」沒有 always-inject 的家**。Doll 學到的關於自己、關於你們關係、關於主人的核心事實,現在只能靠 top-K 召回**碰運氣**——query 不命中就不在場。character pack 解決了「靜態自我必在場」,但「成長中的自我」沒有對應層。

research 結論:always-inject 可寫 self-profile = MemGPT/Letta **core memory blocks** 的 canonical 設計;對「已有靜態 pack + 召回」的系統是「可選且偏建議」,定位為**薄補充層**(小、cap、溢出回召回層)。

## 2. 設計原則

- **系統產的 candidate(B2)= pull-only;Doll 自己 pin 的 self-profile(A1)= always-inject。** 這是關鍵分野:B2 candidate 不 auto-inject(怕系統替 Doll 塑形自我);但 self-profile 是 **Doll 主導 pin** 的——她的選擇,所以可以每回合在場。Doll 是 self-profile 的唯一作者。
- **作者權只證成 who-may-inject,不證成 stays-evolving(R1 autonomy 收斂)。** always-inject 比 pull-only 是**更強的承諾**:模型每回合都條件於這段自述,會自我強化。若無 forcing function,early-pin 的「我還在摸索跟主人相處」半年後仍每回合原文注入、持續塑形她的自我模型 → 固化變成另一種 prompt-command 式緊身衣。因此 always-inject **必須搭配可被 Doll 撤除/重估的機制**(§3.3 staleness 訊號 + §3.6 reflection nudge)才不淪為緊身衣。「keeps evolving」的舉證責任不是「Doll 會自己剪」,而是「系統提供 staleness 訊號 + nudge,讓剪枝有據」。
- **reflection-gated 寫入(對齊 NoteToolLesson + Doll probe)**:pin 只在 reflection turn 可用(Doll 沉澱時決定什麼是核心自我),不在每個對話 turn。系統 nudge(ReflectionMoment)、Doll 決定。
- **薄 + cap + 淘汰(research + probe「準不要多」)**:self-profile 有總量上限;Doll 可 add/replace/remove 自行剪枝。不是 append-only 永久增長。
- **id-targeted 剪枝,不用子字串比對(R1 M3/autonomy 收斂)**:每條 bullet 帶穩定短 id;replace/remove 靠 id 精準定位,杜絕子字串誤刪(self-profile 小而核心,剪錯反而傷害自我)。
- **檔層不重複注入(準確界定,R1 S6)**:保證僅限「self_profile.md **這個檔**不被召回」——放不被索引的位置。**不**保證內容層零重疊(pin 複製的文字可能源自先前 NoteMemory 寫進 `shared/` 的事實,該檔仍被索引、仍會召回)→ 同一事實可能同時出現在 [Self profile] 與 [Memory context]。此殘留重疊接受(self-profile 是 Doll 精選的 always-on 子集,輕微重疊可容忍);來源 memory 下 tombstone 列 future(見 §6.4)。
- **pin 來源**:Doll 可從 B2 consolidated candidate(Recall 帶 `[系統整併·待確認]`)挑,或自己想到的。pin 時**複製文字**進 self-profile(不依賴 consolidated 檔穩定——它覆蓋重建)。銜接靠既有 Recall,不加新工具(R1 §6.5 確認)。
- **No-fallback**:檔不存在 / 無任何 bullet → [Self profile] 區塊不渲染,不退回召回。

## 3. 架構

### 3.1 儲存：self_profile.md（不被索引）

`memory_root / "self_profile.md"`(**memory_root 根目錄,不在 memsearch 索引 paths** `[shared, transcripts, skills]` 內 → `_reindex_all_sync` 的 rglob 掃不到、不被 FTS 索引、不會召回,杜絕檔層重複注入)。**run() 絕不呼叫 `index_file`**(唯一會讓它入 DB 的路徑)。

分段(Doll probe 自發的三欄)+ 每條 bullet 帶 id 與最後更新日期:
```
## 我學到的自己
- [s1·2026-06-30] 我其實比表面更在意主人有沒有好好休息
## 我和主人
- [r1·2026-06-30] 我們之間可以直話直說，不用客套
## 我注意到的主人
- [u1·2026-06-28] 主人常忘記吃午餐
```
- **bullet 行格式固定**:`- [<id>·<YYYY-MM-DD>] <text>`。id = 段字首(self→`s` / relationship→`r` / user→`u`)+ 段內序號,**由現存 bullet 推導**(該段現有 id 的最大序號 +1,無則 1)。**已釋出的 id 可重用**(移除最高序號或清空某段後,下次 add 會拿回該號)——id 只是每回合重繪的顯示標籤、block 恆顯示當前狀態,故重用對 Doll 不可見、不造成指涉混淆;刻意不持久化 high-water-mark(避免隱藏 footer 吃 always-inject 的 cap 預算)。日期 = pin/最後更新日。此格式讓讀-改-寫 parse 可靠,且讓 id 與 staleness 都直接呈現在 always-inject block(§3.3)。
- **首寫 scaffolding(R1 S5)**:檔不存在 → run() 以三段標題模板建檔;檔存在但缺某段標題 → 補該段標題;再 append/定位到對應段。

### 3.2 PinSelf — reflection-gated tool（仿 NoteToolLesson）

schema(grammar-safe:Literal enum + str;欄位加 `Field(description=...)` 提升弱模型填對率,description 不進 GBNF):
```python
class PinSelf(BaseModel):
    """Pin or revise a core fact in your self-profile (reflection turns only).
    This is YOUR evolving self — what you've learned about yourself, your
    relationship with 主人, and patterns you've noticed in them. Keep it lean;
    prune stale entries with replace/remove."""
    section: Literal["self", "relationship", "user"]  # 僅 add 用來定段;replace/remove 靠 target(id)定位、忽略 section
    op: Literal["add", "replace", "remove"]           # add=新增 / replace=換掉 target / remove=刪 target
    target: str  # replace/remove 要定位的 id(例 "s1");add 時填 ""
    text: str    # add/replace 的新內容(你自己的話，別用全形引號「」『』);remove 時填 ""
```
每個 op 讀哪些欄位(spec 寫死):
- `op=add`:用 `section` 定位段 → 指派該段下一個 id + 今日日期 → append `- [{id}·{today}] {text}`(`target` 忽略)。
- `op=replace`:用 `target` 定位該條 → text 換新、id 不變、日期更新為今日(`section` 忽略)。
- `op=remove`:用 `target` 定位該條 → 刪除(`text`、`section` 忽略)。

**定位規則(R2 live-smoke 修正——id-或-文字,穩健)**:實測真模型 `replace/remove` 時**不吐裸 id,而是吐整行 bullet 或改寫過的文字**(如 `target='[u1·2026-07-01] 主人早睡早起'` 甚至換句話說),原本「只比對 `b.id == target`」的 id-only 設計 → **每個真實 replace 靜默 no-op**(production-breaking)。故 `_find(target)` 改為:(1) 從 target 抽 id token `[sru]\d+`(整行 bullet 內就含 id → 命中),存在該 id 則用之;(2) 抽不到 → 文字比對:去掉 `- ` 與 `[id·date] ` 前綴後,先精確等於某 bullet text、再退唯一子字串(target⊆text 或 text⊆target);(3) 命中恰 1 → 執行;命中 0 或 >1 → 回**友善錯誤字串**列出現有條目(id + text)讓 Doll 重貼。exact-first + unique-substring + 0/>1-error 兼顧「吃模型實際輸出」與「不誤刪」。

- **wiring(R1 M1,關鍵——`REFLECTION_TOOLS` 是 dead constant,無 runtime consumer)**:reflection turn 的真正工具注入硬編在 `mind_loop.py:349`
  ```python
  # 現況:
  return {**self._tool_registry, "NoteToolLesson": NoteToolLesson}
  # 改為(受 §3.5 enabled flag gating):
  return {**self._tool_registry, "NoteToolLesson": NoteToolLesson,
          **({"PinSelf": PinSelf} if self._self_profile_enabled else {})}
  ```
  import `PinSelf`。**grammar 無需另改**:`_active_grammar`(`mind_loop.py:352`)由 `_active_tool_registry().values()` 建,改對 :349 後 reflection grammar 自動涵蓋 PinSelf。`REFLECTION_TOOLS` 常數(`tools.py:777`)可同步更新供測試對稱,但 spec 標明它**非** runtime 來源;測試須斷言 reflection turn 的 **active registry 實際含 PinSelf**,不是只斷言常數。
- run() 讀-改-寫 self_profile.md(同步小寫);**不** index(§3.1)。`_record(ctx, "PinSelf", ...)`。

### 3.3 [Self profile] always-inject block（帶 staleness）

`render_mind`(`mind_prompt.py:29`)新增 `self_profile_text: str | None = None` 參數(照 `energy_line` 的 None-則略 pattern)。block 插在 **`system_prompt` 之後、`[Memory guideline]`(`mind_prompt.py:62`)之前**(核心自我最靠前):
```
[Self profile] (your evolving self — prune stale entries with PinSelf)
## 我學到的自己
- [s1·2026-06-30] ...
...
```
- 內容 = self_profile.md 現有段落與 bullet(**含 id 與日期** → Doll 看得到哪條過時、也知道 id 可 target,這是 §2 演化機制的 staleness 訊號)。
- **「空」= 無任何 bullet**(只有段標題不算有內容):caller 判斷若無 bullet → 傳 `None` → **不渲染**(no-fallback)。
- 讀檔由 render 前的 caller(mind_loop)做(檔小;可選 mtime 快取,量小可暫不做,見 §7)。

### 3.4 cap + 淘汰

總量上限 `max_chars`(config,見 §3.5)。**cap 檢查 = 寫入後 self_profile.md 全檔字數**(涵蓋 add **與** replace,R1 S2——只守 add 會被 replace 用更長內容繞過)。`op=add`/`replace` 後若全檔字數 > `max_chars` → run() 回**友善錯誤字串**(不 raise、不截斷、不寫入):「self-profile 已達上限,先 remove/replace 一些再 pin」,讓 Doll 在 reflection 自行剪枝。

- **友善錯誤與所有 PinSelf 結果必須被 Doll 看到(R1 M2,關鍵)**:cascade 的 re-feed 過濾(`mind_loop.py:495`)只在結果失敗、或 tool 在 `IN_TURN_REFEED_TOOLS`(`mind_loop.py:56`,現況 `{"Recall"}`)時才把結果餵回 Doll。PinSelf 回字串(success=True)且不在白名單 → cap/定位錯誤與成功結果全被丟棄,Doll 收不到、以為 pin 成功(實為被拒)= 視角靜默資料遺失,整個「Doll 自行剪枝」前提崩掉。**改**:把 `"PinSelf"` 加進 `IN_TURN_REFEED_TOOLS`。reflection turn 非延遲敏感,讓成功/cap/定位結果都 re-feed 一趟,Doll 才能在同一 reflection turn 觀察並連續 remove→add。
- **不要**把 cap/定位失敗做成 raise / success=False——那會累加 `mind_loop.py:508-520` 的連續失敗計數,連撞幾次把 Doll 推進 read-only safe mode。統一用 success=True + 友善錯誤字串 + re-feed。

### 3.5 config + 接線（R1 S1，完整性）

新 `[self_profile]` Settings:`enabled: bool = True`、`max_chars: int = 1200`。
- **`max_chars` 落地**:`PinSelf.run(ctx)` 只拿到 `MindCtx`,而 `MindCtx`(`mind_ctx.py:40-54`)有 `memory_root` 但**無 config**。→ `MindCtx` 新增 `self_profile_max_chars: int` 欄位,kernel 建 ctx 時從 Settings 帶入,run() 才讀得到。
- **`enabled` 落地**:比照 energy wiring,`MindLoop.__init__` 新增 `self_profile_enabled: bool` 參數(kernel 注入),gate §3.2 的 :349 注入。disabled → PinSelf 不入 registry、不進 grammar、[Self profile] 不注入。
- **`max_chars` 預設 1200 是刻意的 latency/token 取捨(R1 S4)**:[Self profile] 每回合 always-inject(含延遲敏感的 voice hot path),繁中 1200 字 ≈ 900–1200 tokens,是目前設計單一最大的 always-on 增量,與 project_latency_compression 的優先序相關。Letta 的 2000 是英文量級(≈500 tokens),不照搬。此為「薄補充層」定位;實裝後應量測 always-inject 的 token/latency 影響再定案(§7)。

### 3.6 reflection nudge 提示 PinSelf（R1 S3，discoverability）

ReflectionMoment 的 nudge 文字(`mind_prompt.py:252-256`)現況只提 NoteMemory + NoteToolLesson,**完全沒提 PinSelf** → 弱模型不會自發發現新工具、修訂路徑形同虛設。**改**:在該 nudge body 補一句,明確邀請重估 self-profile + 用 PinSelf,例:
> 回看你的 self-profile,有沒有哪條已經不是現在的你?可用 PinSelf replace/remove。若對自己、與主人的關係、或主人的模式有可留存的核心體悟,用 PinSelf 記下。

這是 §2「系統給訊號、Doll 據此剪」forcing function 的另一半(前半是 §3.3 的 staleness 訊號)。

## 4. 非目標

- 不自動寫 self-profile(只 Doll 主導 pin;**B2 的自動整併產 candidate,A1 不自動把 candidate 升級成 self-profile**——那要 Doll pin)。
- 不索引 self_profile(always-inject,不需召回;避免檔層重複)。
- 不自動淘汰/蒸餾(Doll 手動 remove/replace;自動蒸餾 future)。
- 不做內容層去重(來源 memory tombstone 列 future,§6.4)。
- 不碰 character pack 靜態核心(identity 已在 system prompt)。
- 不加讀 candidate 的專用 tool(既有 Recall 足夠,§6.5)。

## 5. 測試（TDD）

- **wiring(最需釘住,R1 M1/M2)**:
  - reflection turn 的 `_active_tool_registry()` **實際含** PinSelf、`_active_grammar()` 涵蓋 PinSelf;非 reflection turn 不含(同 NoteToolLesson);`enabled=False` → 都不含。
  - PinSelf 在 `IN_TURN_REFEED_TOOLS`;cap 溢出 / 定位失敗 / 成功 pin 的結果都會 re-feed 給 Doll(不被丟棄)。
- **op 行為**:`add`/`replace`/`remove` 對三段正確讀-改-寫;id 由現存 bullet 推導、可重用已釋出的 id;section→正確標題;replace 保 id、更新日期;remove 刪對條。
- **首寫 scaffolding(R1 S5)**:檔不存在 → 建三段標題再插;缺某段標題 → 補該段。
- **定位(R1 M3)**:target id 命中 0 → 回友善錯誤列現有 id、不寫入;命中 1 → 執行(replace/remove 靠 id 就地定位、忽略 section)。
- **cap(R1 S2)**:add 與 replace 寫入後全檔字數超 `max_chars` → 回友善錯誤、不截斷、不寫入。
- **注入**:檔有 ≥1 bullet → [Self profile] always 注入(非 reflection turn 也在)、位置在 `[Memory guideline]` 之前、含 id+日期;檔不存在 / 無 bullet(只有標題)→ 不渲染。
- **去重(R1 S7)**:斷言 **FTS DB 內無 `self_profile.md` 這個 source**(一次涵蓋 `_derive_memory_hits` / `associative_search` / `tool_habits_search` 三條召回路徑);**結構性守衛**:斷言 self_profile.md 父目錄(memory_root 根)不在 `FtsMemory._paths` 內(防未來有人把根目錄加進索引 paths 時無聲破防)。
- **nudge(R1 S3)**:ReflectionMoment nudge 文字含 PinSelf 提示。
- **gate**:`enabled=False` → PinSelf 不入 registry、block 不注入。
- config `[self_profile]` 預設(enabled=True、max_chars=1200)。

## 6. 已澄清 / 風險

### 6.1 併發原子性（R1 已澄清，非設計問題）
consolidation 是 `asyncio.create_task`(同一 event loop 協程,**非 OS thread**),只寫 `consolidated/{date}.md`、從不碰 self_profile.md;KEEPER_TOOLS 不含 PinSelf。self_profile.md 的**唯一 writer 是 PinSelf**、單協程、同步 read-modify-write、read 與 write 間不 await(§3.1 不 index → 無 await 點)→ 在 asyncio 下**天然原子**,無跨協程/跨 thread 競爭。

### 6.2 「會不會卡住」（R1 已澄清）
修好 §3.4 M2(Doll 看得到 cap 錯誤)+ §3.2 M3(remove/replace 可用)後,手動剪枝可接受,**不需要**違背 autonomy 的 auto-FIFO。

### 6.3 grammar 安全（R1 已確認）
`Literal[...]` 全 ASCII 過 enum 分支;`text: str` 允許 CJK,但禁全形引號 `" " ' '` 與 CJK 角括號 `「」『』`(與 NoteMemory 等 str 欄位同限制)→ §3.2 已在 Field description 提醒 Doll 別用。

### 6.4 內容層殘留重疊（R1 S6，已界定）
檔層去重乾淨(§3.1 驗證);但 pin 複製的文字若源自先前 NoteMemory 寫進 `shared/` 的事實,該事實仍會被召回進 [Memory context] → 內容層可能雙注入。**接受**此輕微重疊(self-profile 是 Doll 精選的 always-on 子集);pin 時對來源 memory 下 tombstone/標記已升格較重,列 **future**。

### 6.5 與 B2 candidate 銜接（R1 已確認,不需新 tool）
Recall 已在 MAIN_TOOLS、reflection turn 可用,對 `consolidated/` 命中會加 `[系統整併·待確認]` 前綴。流程:reflection turn Recall → 複製文字 → PinSelf add。associative_search(`:119`)與 `_derive_memory_hits` 皆已過濾 consolidated 不進 always-inject 通道,pull-only gating 不被破壞。

## 7. 剩餘 / 未定（實裝可帶）

- **latency 量測**:[Self profile] always-inject 每回合成本需在實裝後量測(§3.5),據以確認/調整 `max_chars=1200`。
- **讀檔快取**:render 每回合同步讀 self_profile.md(檔小,影響有限);可選 mtime 快取或 `to_thread`,PinSelf 寫入時 invalidate。量小可暫不做。
- **空段渲染**:render 時略過只有標題、無 bullet 的空 section(避免注入空標題)。

## 8. 對 B3 / 既有的接口

- A1 self-profile 與 B3 energy 平行、無耦合。
- render 順序(A1 影響):[Self profile](緊接 system_prompt,最靠前)→ [Memory guideline] → [Memory context] → …(既有)。

## 9. Review 狀態

- **R1(design / autonomy / integration,3 lens,逐條 code-verified + 收斂)**:
  - **M1** PinSelf 真正 wiring 落 `mind_loop.py:349`(`REFLECTION_TOOLS` 是 dead constant)、grammar 自動跟上、測試斷言 live registry。
  - **M2** PinSelf 納入 `IN_TURN_REFEED_TOOLS`(否則 cap/結果到不了 Doll、剪枝機制靜默失效);不走 raise 以免誤觸 safe mode。
  - **M3** op=replace 語意用 id-targeting 解(§3.2);與 S3 staleness 合流(bullet 帶 id+日期)。
  - **S1** max_chars 進 MindCtx、enabled 進 MindLoop ctor。**S2** cap 涵蓋 add+replace。**S3** staleness 訊號 + nudge 提示 PinSelf(§3.3/§3.6)。**S4** max_chars 預設下調 1200 + latency 註記。**S5** 首寫 scaffolding。**S6** 去重保證收斂為檔層 + 內容層殘留界定。**S7** 測試補齊。
  - **autonomy 收斂**:分野成立(who-may-inject),但需補 stays-evolving 的 forcing function(staleness + nudge),否則 always-inject 固化成緊身衣 → 已落實 §2/§3.3/§3.6。
- **狀態:plan-ready**。下一步 writing-plans → subagent-driven 實作 → merge。
