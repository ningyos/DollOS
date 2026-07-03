# P1e — External Safety(外部安全:上真伺服器前的硬紅線)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 讓 Doll 從外部通道(Discord 陌生人 / owner DM)喚醒的 turn **結構性安全** —— 保守工具集(帳號被盜也非家用電腦 RCE)、記憶寫入分層防跨界毒化、公開 turn 檢索排除主人私事防外洩、陌生社交不燒她跟主人的 energy —— 這是上任何有他人的伺服器前的硬紅線。

**Architecture:** 新增一個 **per-turn `origin_tier`**(`internal | external_public | external_dm`)在 drain 時從 bucket 的 perceptions + `author_is_owner` 算一次(與既有 `external_ctx` 不同軸:`external_ctx` 是「讀了外部內容」會被 Recall mid-cascade 升級,`origin_tier` 是「turn 從哪個通道發起」、per-turn 固定)。此 tier 驅動四個結構性閘:(1) 工具 registry 縮減(external → 保守集,贏過 reflection 展開),(2) 記憶寫入路徑路由(依 tier 分目錄),(3) 記憶檢索 scope(external_public 排除私有層 + 抑制 auto-context),(4) energy 消耗 origin-aware(external_public 不燒)。全部 gated on P1a 的單源 turn(C1),故 tier 是 bucket 級純量。

**Tech Stack:** Python 3.12、pydantic、既有 FtsMemory(sqlite fts5)的 `source_prefix` LIKE 過濾原語、GBNF grammar。無新第三方依賴。

## Global Constraints

- **語言**:comment/docstring 繁中或英文皆可(跟隨檔案風格);與使用者一律繁中。
- **No fallback**:絕不降級/fallback。backend 做不到明說。
- **owner-DM 非 RCE(硬紅線,使用者明訂)**:任何 external channel(含 owner DM)**結構性禁止** Shell / SpawnWorkflow / SpawnMonitor / InvokeSkill / WriteSchedule。Discord 帳號是第三方單因子、不在使用者控制內;帳號被盜 ≠ 家用電腦 RCE。真要遠端 Shell 的 out-of-band 本機第二因子 = §7 deferred,**不在本 plan**。
- **縮減贏過 reflection**:external turn 撞 ReflectionMoment 時,保守集必須**贏過** reflection 展開 —— 明確排除 **SelfRevision**(毒化鏈最終閘)於 external turn。
- **DiscordLookup 不在本 plan**:spec §3.4 保守集列了 DiscordLookup,但依 R2 YAGNI lens,DiscordLookup 的 novel request-response RPC 原語 defer 到 P2(成效在 P2 裁決)。P1e 保守集 = **{Recall, NoteMemory, WriteDiary, PinSelf(僅 reflection)}**,不含 DiscordLookup。P2 落 DiscordLookup 時再加進 external 保守集。
- **存內容非 hash / 記憶原語**:記憶分層用**路徑路由**(external 寫入分目錄),靠既有 `source_prefix` 分隔;**不**改 fts5 chunks schema(不加 provenance column)。檢索排除用**擴充 search 的 exclude 參數**(source NOT LIKE),不動 schema。
- **gated on 單源 turn(C1)**:所有下游閘 derive 自單一 bucket 的 perceptions;`origin_tier` per-bucket 重算,不跨 bucket 汙染(比照 P1f 的 external_ctx per-bucket)。
- **её 自己的訊息永不入 L0/L1/L2、永不入 FTS**(P1b 已做;本 plan 不回退)。

## 範圍界定

**本 plan 只加一個概念:external-origin 安全軸(`origin_tier`)+ 其四個結構性閘。** 蓋 spec §3.4 的 [R1-sec S1/S2/S3/S4/S5] + [R1-arch I4] energy origin-aware + owner-DM 4-way split 一致化(R2 coherence Minor)。

**不含**:DiscordLookup RPC(P2)、注意力 L0/L1/L2(P1c)、情境化渲染完整版(P1d;本 plan 只補「把 owner/stranger 身分 surface 進 rendered ChannelMessage」這最小一塊,因無此 Doll 讀不到自己在跟誰講、無法自己判斷)、out-of-band 本機第二因子(§7)。

---

## 4-way split 權威表(實作與測試都對照此表)

| 面向 | internal(UserSpoke/內部) | external_dm(owner DM) | external_public(陌生人) |
|---|---|---|---|
| perception kind | UserSpoke / 內部 | ChannelMessage | ChannelMessage |
| `origin_tier` | `internal` | `external_dm` | `external_public` |
| cancel consolidation/evolution | yes | yes(owner 升格,**kernel 已做**) | no(**kernel 已做**) |
| energy 消耗 | yes | yes(升格) | **no**(I4,本 plan) |
| `last_user_at` 推進(擋回充) | yes | yes(升格,本 plan) | no |
| 工具 registry | 完整 | **保守集(無 Shell)** | **保守集(無 Shell)** |
| SelfRevision(reflection 時) | 允許 | **禁** | **禁** |
| 記憶寫入目錄 | `shared/` | `external_dm/` | `external_public/` |
| 記憶檢索 scope | 全(含私有層) | 全(owner 可見自己記憶) | **排除私有層 + 抑制 auto-context** |
| rendered prompt 身分標示 | — | 「主人私訊」 | 「陌生人 X 在 #ch」 |

「私有層」= `{shared/, external_dm/}`(internal 私密 + owner 私密);`external_public/` 對所有 turn 可見。external_public turn 只檢索 `external_public/`。

---

## File Structure

- **Modify** `src/dollos/mind/mind_loop.py` — `_EXTERNAL_KINDS` 加 ChannelMessage(S1);drain 算 `origin_tier` 存 ctx;`_active_tool_registry` 加 external 軸(保守集,贏過 reflection);`_active_grammar` 一般化成 keyed cache;energy 消耗 + last_user_at origin-aware;三個 memsearch call site 傳 scope。
- **Modify** `src/dollos/mind/mind_ctx.py` — ctx 加 `origin_tier: str`(預設 `"internal"`)。
- **Modify** `src/dollos/tools.py` — `EXTERNAL_TOOLS` 常數;`NoteMemory`/`WriteDiary` 依 `ctx.origin_tier` 路由寫入目錄;`Recall.run` 傳 scope。
- **Modify** `src/dollos/memory/fts_store.py` — `search(...)` 加 `exclude_prefix` 參數(source NOT LIKE)。
- **Modify** `src/dollos/mind/mind_prompt.py` — ChannelMessage 渲染 surface owner/stranger 身分。
- **Modify** `src/dollos/mind/consolidation.py`(若 last_user_at 升格需在此)— 確認回充 gate 對 owner-DM 正確。
- **Tests**: `tests/test_external_safety.py`(新,4-way split 整合)、擴充 `tests/test_mind_loop*.py`、`tests/test_fts_store.py`、`tests/test_tools*.py`。

---

## Task 1: `origin_tier` per-turn 判定 + external_ctx(S1)

**Files:** Modify `src/dollos/mind/mind_ctx.py`、`src/dollos/mind/mind_loop.py`;Test `tests/test_mind_loop_origin_tier.py`

**Interfaces:**
- Produces: `MindCtx.origin_tier: str`(`"internal"|"external_public"|"external_dm"`,預設 `"internal"`);`_derive_origin_tier(perceptions) -> str` 在 `_run_one_turn` drain 段算一次並設 `self._ctx.origin_tier`。

判定規則(單源 bucket):
- 無 ChannelMessage perception → `"internal"`。
- 有 ChannelMessage 且該 perception `data.get("author_is_owner")` 為真 → `"external_dm"`。
- 有 ChannelMessage 且非 owner → `"external_public"`。

- [ ] **Step 1: 失敗測試** — `tests/test_mind_loop_origin_tier.py`:三種 bucket 各驗 `origin_tier`;`ChannelMessage(author_is_owner=True)`→`external_dm`、`=False`→`external_public`、`UserSpoke`→`internal`。另驗 per-bucket 重算(先跑 external bucket 再跑 internal bucket,tier 不殘留)。並驗 S1:external turn 後 `self._ctx.external_ctx is True`(ChannelMessage 進 `_EXTERNAL_KINDS`)。

```python
@pytest.mark.asyncio
async def test_origin_tier_owner_dm(mind_loop):
    ml = mind_loop
    await ml._run_one_turn([_channel_msg("hi", author_is_owner=True)])
    assert ml._ctx.origin_tier == "external_dm"
    assert ml._ctx.external_ctx is True  # S1: ChannelMessage now external

@pytest.mark.asyncio
async def test_origin_tier_stranger(mind_loop):
    await mind_loop._run_one_turn([_channel_msg("hi", author_is_owner=False)])
    assert mind_loop._ctx.origin_tier == "external_public"

@pytest.mark.asyncio
async def test_origin_tier_internal_and_no_bleed(mind_loop):
    await mind_loop._run_one_turn([_channel_msg("hi", author_is_owner=False)])
    await mind_loop._run_one_turn([_user_perception("hi")])
    assert mind_loop._ctx.origin_tier == "internal"  # recomputed, no bleed
```

- [ ] **Step 2: 跑確認 fail** — `uv run pytest tests/test_mind_loop_origin_tier.py -v`(fail:`origin_tier` 不存在)
- [ ] **Step 3: 實作**
  - `mind_ctx.py`:在 `external_ctx: bool = False` 旁加 `origin_tier: str = "internal"`。
  - `mind_loop.py`:`_EXTERNAL_KINDS` 加 `"ChannelMessage"`(現為 `frozenset({"ToolResultArrived", "MonitorFired", "MonitorEnded"})`,line 77)。
  - `mind_loop.py`:加 helper 並在 `_run_one_turn` drain 段(現 `self._ctx.external_ctx = batch_external(perceptions)`,line 313 附近)之後設 tier:
    ```python
    def _derive_origin_tier(self, perceptions: list[Perception]) -> str:
        for p in perceptions:
            if p.kind == "ChannelMessage":
                return "external_dm" if p.data.get("author_is_owner") else "external_public"
        return "internal"
    ```
    ```python
    self._ctx.origin_tier = self._derive_origin_tier(perceptions)
    ```
- [ ] **Step 4: 跑綠** — `uv run pytest tests/test_mind_loop_origin_tier.py -v`
- [ ] **Step 5: 全套回歸** — `uv run pytest tests/ -q`(3 torch 失敗 pre-existing 忽略)。**注意 S1 副作用**:ChannelMessage 進 `_EXTERNAL_KINDS` 會讓既有 PinSelf provenance 對 Discord turn 標 external_ctx=true —— 這正是 S1 意圖(降權 Discord turn 寫的 pin);確認既有 external_ctx/PinSelf 測試仍綠或按此意圖更新。
- [ ] **Step 6: Commit** — `feat(safety): per-turn origin_tier + ChannelMessage external_ctx (P1e Task 1, S1)` + trailers。

---

## Task 2: 保守工具集 + keyed grammar cache(S4/S5)

**Files:** Modify `src/dollos/tools.py`(`EXTERNAL_TOOLS`)、`src/dollos/mind/mind_loop.py`(`_active_tool_registry`/`_active_grammar`);Test `tests/test_external_tool_gate.py`

**Interfaces:**
- Consumes: Task 1 的 `ctx.origin_tier`。
- Produces: `EXTERNAL_TOOLS: frozenset[str]`(保守集工具名);`_active_tool_registry` 加 external 分支;`_active_grammar` 一般化成 `dict[frozenset, str]` keyed cache。

保守集 = **{Recall, NoteMemory, WriteDiary}**,加 reflection 時 **PinSelf**。硬禁:Shell, SpawnWorkflow, SpawnMonitor, RemoveMonitor, InvokeSkill, WriteSchedule, **SelfRevision**, 及其餘 MAIN_TOOLS 非保守項。external turn 的 reflection 展開**只**加 PinSelf(**不**加 SelfRevision、不加 NoteToolLesson 若非保守——保守集明確定義,不從 reflection 繼承危險項)。

- [ ] **Step 1: 失敗測試** — `tests/test_external_tool_gate.py`:
  - external_public turn 的 `_active_tool_registry()` keys ⊆ 保守集,且 `"Shell" not in`、`"SpawnWorkflow" not in`、`"WriteSchedule" not in`、`"InvokeSkill" not in`。
  - external_dm turn 同樣無 Shell(owner 也砍)。
  - internal turn 仍有 Shell(完整集)。
  - **S5 關鍵**:external turn **且** `is_reflection=True` → registry 有 PinSelf 但**無 SelfRevision**、**無 Shell**(縮減贏過 reflection 展開)。internal reflection turn 仍有 SelfRevision(對照組)。
  - grammar keyed cache:external turn 的 `_active_grammar()` 非 None、與 internal grammar 不同物件;同 tier 重複呼叫回同一 cached 物件(不重建)。

```python
def test_external_reflection_excludes_selfrevision(mind_loop):
    ml = mind_loop
    ml._ctx.origin_tier = "external_public"
    ml._is_reflection = True
    ml._evolution_enabled = True
    reg = ml._active_tool_registry()
    assert "PinSelf" in reg              # reflection expansion kept (safe)
    assert "SelfRevision" not in reg     # S5: reduction wins over reflection
    assert "Shell" not in reg
```

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**
  - `tools.py`:加 `EXTERNAL_TOOLS: frozenset[str] = frozenset({"Recall", "NoteMemory", "WriteDiary"})`(reflection 時另加 PinSelf,在 registry 邏輯處理)。
  - `mind_loop.py` `_active_tool_registry`(現 line 640-663):在 **safe_mode 之後、reflection 之前**插 external 分支(external 縮減優先於 reflection 展開,但 safe_mode 仍最高優先):
    ```python
    if self._state.safe_mode:
        return {n: c for n, c in self._tool_registry.items() if n in SAFE_MODE_TOOLS}
    if self._ctx.origin_tier != "internal":
        # S4/S5: external turns (incl owner DM) get the conservative set;
        # reduction WINS over reflection expansion — PinSelf allowed on
        # reflection turns, but never Shell/Workflow/Monitor/Skill/Schedule/SelfRevision.
        allowed = set(EXTERNAL_TOOLS)
        if self._is_reflection and self._self_profile_enabled:
            allowed.add("PinSelf")
        return {n: c for n, c in self._tool_registry.items() if n in allowed}
    if self._is_reflection:
        extra = {"NoteToolLesson": NoteToolLesson}
        if self._self_profile_enabled:
            extra["PinSelf"] = PinSelf
        if self._evolution_enabled:
            extra["SelfRevision"] = SelfRevision
        return {**self._tool_registry, **extra}
    return self._tool_registry
    ```
    (PinSelf 在 reflection 才加;注意 `EXTERNAL_TOOLS` 的工具需真的在 `self._tool_registry`——它們都在 MAIN_TOOLS,故 `if n in allowed` 過濾安全。)
  - `mind_loop.py` `_active_grammar`(現 line 665-688,三個硬編 slot):一般化成 keyed cache。以 active registry 的 tool-name frozenset 當 key:
    ```python
    def _active_grammar(self) -> str | None:
        tools = self._active_tool_registry()
        key = frozenset(tools.keys())
        if key == self._base_tool_key:      # 熱路徑:完整集,回 __init__ 建好的 self._grammar
            return self._grammar
        cached = self._grammar_cache.get(key)
        if cached is None:
            cached = build_voice_first_grammar(list(tools.values()))
            self._grammar_cache[key] = cached
        return cached
    ```
    `__init__`:加 `self._grammar_cache: dict[frozenset, str] = {}`;`self._base_tool_key = frozenset(self._tool_registry.keys())`。**移除**舊 `self._safe_grammar`/`self._reflection_grammar` 三 slot(keyed cache 取代;確認無其他 reference——grep `_safe_grammar`/`_reflection_grammar`)。
- [ ] **Step 4: 跑綠**
- [ ] **Step 5: 全套回歸** — 確認既有 safe_mode / reflection grammar 測試仍綠(keyed cache 對這兩 key 行為等價)。
- [ ] **Step 6: Commit** — `feat(safety): conservative external tool registry + keyed grammar cache; reduction wins over reflection (P1e Task 2, S4/S5)` + trailers。

---

## Task 3: 記憶寫入 origin 路由(S2)

**Files:** Modify `src/dollos/tools.py`(`NoteMemory.run`/`WriteDiary.run`);Test `tests/test_memory_origin_routing.py`

**Interfaces:**
- Consumes: Task 1 的 `ctx.origin_tier`。
- Produces: `NoteMemory`/`WriteDiary` 依 tier 寫入 `shared/`(internal)、`external_public/`、`external_dm/`。

- [ ] **Step 1: 失敗測試** — 三種 tier 各觸發 NoteMemory,斷言檔案落在對應目錄:
  - internal → `memory_root/shared/{date}.md`(不變)
  - external_public → `memory_root/external_public/{date}.md`
  - external_dm → `memory_root/external_dm/{date}.md`
  且 `memsearch.index_file` 被以該路徑呼叫(檢索能靠 source_prefix 分隔)。WriteDiary 同款。

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — `NoteMemory.run`(現 `tools.py:108-121`,硬編 `ctx.memory_root / "shared" / ...`):
  ```python
  _ORIGIN_DIR = {"internal": "shared", "external_public": "external_public", "external_dm": "external_dm"}
  # in run():
  subdir = _ORIGIN_DIR.get(getattr(ctx, "origin_tier", "internal"), "shared")
  path = ctx.memory_root / subdir / f"{date.today():%Y-%m-%d}.md"
  ```
  `WriteDiary.run`(`tools.py:143-153`)同款路由。`build_heading` 不變(origin 由**目錄**編碼,非 heading——這樣 source_prefix 可分隔)。
- [ ] **Step 4: 跑綠**
- [ ] **Step 5: 全套回歸**
- [ ] **Step 6: Commit** — `feat(safety): route NoteMemory/WriteDiary writes by origin_tier (P1e Task 3, S2)` + trailers。

---

## Task 4: 記憶檢索 scope(S3)

**Files:** Modify `src/dollos/memory/fts_store.py`(`search` 加 exclude)、`src/dollos/mind/mind_loop.py`(三 call site + 抑制 auto-context)、`src/dollos/tools.py`(`Recall.run`);Test `tests/test_fts_store_exclude.py`、`tests/test_recall_scope.py`

**Interfaces:**
- Produces: `FtsMemory.search(..., exclude_prefix: str | Path | None = None)`(source NOT LIKE);external_public turn 的三個檢索點排除私有層 + 抑制 auto-`[Memory context]`。

私有層 prefix = `{memory_root/shared, memory_root/external_dm}`。external_public turn 檢索只准 `external_public/`。實作以 exclude:排除 `shared/` 與 `external_dm/`(或等價地 include-only `external_public/`——選 exclude 較不動既有 include 語意)。search 目前只有單一 `source_prefix`;加 `exclude_prefix`(可為單一 prefix;私有層有兩個目錄 → 傳 list 或呼叫端合併。**最小**:`exclude_prefixes: list[str] | None`,SQL 生成多個 `AND source NOT LIKE ?`)。

- [ ] **Step 1: 失敗測試**
  - `tests/test_fts_store_exclude.py`:index 一份 `shared/x.md`(私)+ 一份 `external_public/y.md`(公);`search(q, top_k=5, exclude_prefixes=[shared_dir])` → 只回 y,不回 x。無 exclude → 兩者都可回(對照)。
  - `tests/test_recall_scope.py`:external_public turn 的 `_derive_memory_hits()` 不含私有層命中;`Recall.run` 於 external_public turn 排除私有層。internal turn 不排除(全檢索)。
  - external_public turn **抑制 auto-context**:`_derive_memory_hits` 於 external_public 回 `[]`(或 render_mind 不注入 `[Memory context]`)——選其一,測試對齊。**建議**:external_public turn `_derive_memory_hits` 直接回 `[]`(抑制 auto-context),Recall(顯式工具)才做 scoped 檢索(排除私有層)。這對齊 spec「抑制 auto-`[Memory context]` + Recall scope 過濾」。

```python
@pytest.mark.asyncio
async def test_external_public_suppresses_auto_context(mind_loop_with_mem):
    ml = mind_loop_with_mem
    ml._ctx.origin_tier = "external_public"
    hits = await ml._derive_memory_hits()
    assert hits == []  # auto-[Memory context] suppressed on public turns

@pytest.mark.asyncio
async def test_recall_external_public_excludes_private(mem_with_private_and_public):
    # Recall on an external_public turn must not surface shared/ (owner private) content
    ...
```

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**
  - `fts_store.py` `search`/`_search_sync`(現 `source_prefix` 於 line 174-218):加 `exclude_prefixes: list[str | Path] | None = None`,每個生成 `AND source NOT LIKE ? ESCAPE '\\'`(用既有 `_like_prefix` helper)。
  - `mind_loop.py` `_derive_memory_hits`(現 line 595-629):開頭加 `if self._ctx.origin_tier == "external_public": return []`(抑制 auto-context)。
  - `tools.py` `Recall.run`(現 line 277-286):external_public turn 傳 `exclude_prefixes=[shared_dir, external_dm_dir]`。取 private 目錄 helper(可放 mind_ctx 或 tools,回 `[ctx.memory_root/"shared", ctx.memory_root/"external_dm"]`)。
  - `associative_search`(`associative_search.py:111`,side-channel):external_public turn 亦排除私有層(或 external_public turn 不跑 associative——最小:排除私有層,與 Recall 一致)。
- [ ] **Step 4: 跑綠**
- [ ] **Step 5: 全套回歸**
- [ ] **Step 6: Commit** — `feat(safety): external_public retrieval scope — exclude private tier + suppress auto-context (P1e Task 4, S3)` + trailers。

---

## Task 5: energy origin-aware(I4)

**Files:** Modify `src/dollos/mind/mind_loop.py`(消耗 + last_user_at);Test `tests/test_energy_origin.py`

**Interfaces:**
- Consumes: `ctx.origin_tier`、`data["author_is_owner"]`。
- Produces: external_public turn 不消耗 energy;owner-DM(external_dm)升格——消耗 energy + 推進 `last_user_at`(擋回充);stranger 不推進 last_user_at。

- [ ] **Step 1: 失敗測試** — `tests/test_energy_origin.py`:
  - external_public turn 有 speech/tool → `energy` **不變**(不燒)。
  - internal turn 產出 → energy 燒 `cost_per_turn`(對照,不變)。
  - external_dm(owner)turn 產出 → energy 燒(升格)且 `last_user_at` 推進到該 turn 時間。
  - external_public turn → `last_user_at` **不**推進。

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**
  - `mind_loop.py` energy 消耗(現 line 528-531,gate `produced`):加 origin guard:
    ```python
    produced = bool(self._turn_speech) or self._turn_had_tool
    consumes = self._ctx.origin_tier != "external_public"  # stranger social不燒她跟主人的精力帳
    if self._energy_enabled and produced and consumes:
        self._state.energy = max(0.0, self._state.energy - self._cost_per_turn)
    ```
  - `mind_loop.py` last_user_at(現 line 290-292,只 UserSpoke):owner-DM 升格。在 drain 段加:若 bucket 為 external_dm(owner),推進 last_user_at + user_turn_count(比照 UserSpoke 升格語意)。精確:
    ```python
    for p in perceptions:
        if p.kind == "UserSpoke" or (p.kind == "ChannelMessage" and p.data.get("author_is_owner")):
            self._state.last_user_at = p.t
            self._state.user_turn_count += 1
    ```
    (確認不重複既有 UserSpoke 分支;若既有是 `if p.kind == "UserSpoke"`,擴成上式。)**注意**:此升格讓遠端 owner 帳號被盜可 thrash energy/consolidation gate —— spec §3.4 明文接受並記錄的 DoS 向量(非本 plan 修)。
- [ ] **Step 4: 跑綠**
- [ ] **Step 5: 全套回歸** — 確認既有 energy/consolidation 測試仍綠。
- [ ] **Step 6: Commit** — `feat(safety): energy origin-aware — stranger turns don't drain, owner-DM upgraded (P1e Task 5, I4)` + trailers。

---

## Task 6: 身分 surface 進 prompt + 4-way split 整合測試

**Files:** Modify `src/dollos/mind/mind_prompt.py`(ChannelMessage 渲染);Test `tests/test_external_safety.py`(4-way 整合)、`tests/test_mind_prompt_channel.py`

**Interfaces:**
- Consumes: perception `data`(author、author_is_owner、channel where)。
- Produces: ChannelMessage 渲染標示身分,讓 Doll 讀得到自己在跟誰講(否則她無法自己判斷該不該回/如何回)。

- [ ] **Step 1: 失敗測試**
  - `tests/test_mind_prompt_channel.py`:owner-DM ChannelMessage 渲染含「主人」標示;stranger ChannelMessage 含「陌生人」+ author + channel。現行渲染(`mind_prompt.py:330-332`,`[{where}] {author}:{content}`)不標 owner/stranger —— 驗新渲染區分。
  - `tests/test_external_safety.py`(整合,對照 4-way split 權威表):一個 external_public turn 端到端跑,斷言:(a) registry 無 Shell、(b) energy 不燒、(c) NoteMemory 寫 external_public/、(d) auto-context 抑制、(e) last_user_at 不推進;一個 external_dm turn 斷言:(a) registry 無 Shell 但(b) energy 燒 +(c) last_user_at 推進 +(d) 檢索不排除私有層;一個 internal turn 斷言完整能力。這是防「piecemeal 漏接某一面向」的迴歸網。

- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — `mind_prompt.py` ChannelMessage 分支(現 line 330-332):依 `d.get("author_is_owner")` 與 channel 型別分敘:
  ```python
  if p.kind == "ChannelMessage":
      d = p.data
      where = d.get("where") or d.get("channel_name") or "?"
      if d.get("author_is_owner"):
          return f"[主人私訊] 主人:{d.get('content','')}"
      return f"[{where}] 陌生人 {d.get('author','?')}:{d.get('content','')}"
  ```
  (敘事描述性、非命令;P1d 情境化渲染會再細緻化,本 plan 只補最小身分標示。)
- [ ] **Step 4: 跑綠** — `uv run pytest tests/test_external_safety.py tests/test_mind_prompt_channel.py -v`
- [ ] **Step 5: 全套回歸** — `uv run pytest tests/ -q`(這是最後 task,全綠 minus 3 torch 最重要)。
- [ ] **Step 6: Commit** — `feat(safety): surface owner/stranger identity in prompt + 4-way split integration test (P1e Task 6)` + trailers。

---

## Self-Review（對 spec §3.4 逐條核）

- [x] S1 external_ctx provenance → Task 1(ChannelMessage 進 _EXTERNAL_KINDS)
- [x] S2 NoteMemory origin bit(path-routing,非 schema column)→ Task 3
- [x] S3 external_public 檢索 scope + 抑制 auto-context(三 call site + associative)→ Task 4
- [x] S4/S5 保守工具集 + 贏過 reflection + 排除 SelfRevision + keyed grammar cache → Task 2
- [x] I4 energy origin-aware + owner-DM last_user_at 升格 → Task 5
- [x] owner-DM 4-way split 一致化(R2 coherence)→ 權威表 + Task 6 整合測試
- [x] DiscordLookup defer P2(R2 YAGNI)→ Global Constraints 明文,保守集不含
- [x] owner-DM 非 RCE(硬紅線)→ Task 2 external 分支含 owner(author_is_owner 不豁免工具縮減)

**Placeholder scan:** 每 code step 給實際 code + 精確 file:line。`_ORIGIN_DIR`/`EXTERNAL_TOOLS`/`exclude_prefixes` 型別一致。
**Type consistency:** `origin_tier: str` 貫穿 Task 1→6;`EXTERNAL_TOOLS: frozenset[str]`;`_grammar_cache: dict[frozenset, str]`。
**跨 task 依賴:** Task 2-6 全 consume Task 1 的 `ctx.origin_tier` —— Task 1 必須先做。Task 4 的私有層目錄 = Task 3 建立的目錄結構(shared/external_dm),一致。

---

## 執行銜接

依 `feedback_subagent_driven_default`:直接進 `superpowers:subagent-driven-development`,每 task fresh implementer + reviewer(sonnet),whole-branch review 用 opus(安全關鍵,務必嚴查工具縮減贏過 reflection、記憶 scope 排除私有層兩點)。worktree `.worktrees/p1e-safety/` on branch `p1e-safety`。**安全關鍵**:reviewer 要特別驗每個閘的 teeth(external turn 真的拿不到 Shell、公開 turn 真的撈不到私有記憶),反轉斷言要 fail。
