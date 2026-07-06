# Part A — Self-Learned Name Aliases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use `- [ ]` syntax.

**Goal:** 讓 Doll 從**與 owner 的第一手相處**中自學別人怎麼叫她(暱稱),餵回 daemon-side L0 name-wake;pack 正名當永久 seed(冷啟動可醒);陌生人**結構上無法**寫入 alias(不重開亂回)。取代 bridge 死 config `name_aliases`。

**Architecture:** 新增 `mind/name_aliases.py` 標準庫 store(`{memory_root}/name_aliases.json`,仿 `self_profile.py`)+ `LearnName` pydantic 工具(**只在 owner-present live 回合入工具集** —— `external_dm` owner DM 或帶 `UserSpoke` 的 `internal`;`external_public`/純反思回合工具**不存在**)+ `AttentionGate` 改注入 `alias_provider` callable(turn-time 讀 `{pack seed} ∪ {config floor} ∪ {active learned}`,mtime-cache)+ L0 比對硬化(word-boundary/CJK≥2/denylist,seed 也套)。

**Tech Stack:** Python 3.13、pydantic、既有 mind_loop registry、GBNF grammar(tool 進 registry 自動納入)。無新第三方依賴。依據 spec:`docs/superpowers/specs/2026-07-06-self-learned-aliases-owner-guild-design.md`(§3、§8 Part A,D 值已定案 §7)。

## Global Constraints

- **語言**:comment/docstring 繁中或英文皆可;與使用者一律繁中。
- **No fallback / 升-code-閘鐵律**:wake trigger 是安全敏感,prompt 管不住的比對要升 code 閘(`ref_weak_model_soft_mechanism_playbook`)。
- **陌生寫入面歸零(spec R1-C1 核心)**:`LearnName` **只在 owner-present 回合出現在工具集**,不是事後讀 `origin_tier` 擋。信任邊界由「工具在不在那回合的 registry」強制。**A2 絕不接回失效的 origin_tier active/pending 路由。**
- **seed floor 也過 guard(spec I3)**:pack `meta.name`/`meta.aliases`、config floor 不落 json、但 **match 時**套同一套硬化規則,且 kernel 建 provider 時過 load-time guard(denylisted/過短 → `logger.warning` + 剔除,不納 wake-eligible)。**誠實邊界**:過短/常見的 pack 正名 cold-start-by-name 失效並 log 明講,不讓不可刪 seed 變不可刪亂回源。
- **provider fail-closed(L5)**:`stat()`/讀檔失敗 → 回既有 cache 或空 learned 集(seed+config 仍在),不 raise、不斷 turn。
- **D 值(定案)**:D1=pack `[meta] aliases` 多正名 seed;D2=ASCII `\b` 界 / CJK≥2 / 混合含 ASCII 詞走 `\b` 界 / 硬編 denylist(`你 妳 hey hi hello the everyone all 大家 各位`)+ config 可加;D3=平行 `aliases_history.jsonl`;D6=base **不做** A4(陌生暱稱學習)。
- **測試走 Fake / 純邏輯**,不碰網路。既有 `tests/test_attention*.py` 改傳 `alias_provider=`。

## 範圍界定

**本 plan 只加一個概念:自學 name_aliases。** 蓋 spec §3 全部 + §8 Part A(A1/A2/A3/A5)。**A4 不做**(D6:base 不學陌生暱稱)。Part B(owner_guild_only)是獨立 plan。

---

## File Structure

- **Create** `src/dollos/mind/name_aliases.py`(store)、`tests/test_name_aliases.py`。
- **Modify** `src/dollos/tools.py`(`LearnName` 工具)、`src/dollos/mind/mind_loop.py`(`_active_tool_registry` branch)、`src/dollos/mind/attention.py`(`alias_provider` 注入 + match 硬化)、`src/dollos/kernel.py`(建 union+mtime-cache provider + load-time seed guard)、`src/dollos/character.py`(pack `meta.aliases` 選用欄位,D1b)。
- **Modify** `src/dollos/discord_bridge/{controller.py,__main__.py}` + `bridge.toml`/`bridge.example.toml`(移除死 `name_aliases`,A5)+ daemon config 文件註記 + 中文正名遷移。
- **Test**: `tests/test_tools*.py`(LearnName + registry gating)、`tests/test_attention*.py`(provider + match 硬化)、`tests/test_kernel*.py`(provider union + seed guard)。

---

## Task 1: (A1) `name_aliases.py` store module

**Files:** Create `src/dollos/mind/name_aliases.py`、`tests/test_name_aliases.py`

**Interfaces:**
- Produces(純 read-modify-write,永不 raise):
  - `@dataclass class AliasEntry: token: str; state: str; origin: str; added_at: float; note: str | None = None`(base:state 恆 `"active"`、origin 恆 `"owner"`)。
  - `class NameAliasStore` — `__init__(self, path: Path)`;`add(self, token, *, origin="owner", note=None, now) -> None`(冪等:同 token 覆寫)、`remove(self, token) -> None`、`active_tokens(self) -> frozenset[str]`(state=="active" 的 token 集)、內部 JSON load/save(讀壞 → 空、log)。**base 不含 `adopt`/pending**(D6)。

- [ ] **Step 1: 失敗測試** — `tests/test_name_aliases.py`:add→active_tokens 含之;remove→不含;重複 add 同 token 冪等(不重複);讀壞檔 → 空集不 raise;寫入 round-trip(add→新 store 讀同 path→仍在)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**(仿 `self_profile.py` 的 read-modify-write + JSON;`active_tokens` 只回 state=="active")
- [ ] **Step 4-5: 跑綠 + 全套回歸**
- [ ] **Step 6: Commit** `feat(aliases): NameAliasStore read-modify-write store (Part A / A1)` + trailers。

---

## Task 2: (A2) `LearnName` 工具 + owner-context registry gating + 機械 guard(承重 security)

**Files:** Modify `src/dollos/tools.py`、`src/dollos/mind/mind_loop.py`;Test `tests/test_tools_learnname.py`、`tests/test_mind_loop_learnname_gate.py`

**Interfaces:**
- Consumes: A1 `NameAliasStore`(經 ctx 或 kernel 注入,比照 self_profile 的取用方式 —— 讀 `mind_loop`/`ctx` 怎麼給 PinSelf 存取 self_profile,沿用)。
- Produces:
  - `class LearnName(BaseModel)` — `op: Literal["add","remove"]`、`token: str`、`note: str | None = None`;`async def run(self, ctx) -> str`。
  - `mind_loop._active_tool_registry` 新 branch:`LearnName` **只在** `ctx.origin_tier == "external_dm"`(owner DM)**或**(`origin_tier == "internal"` 且該 turn 有 `UserSpoke` perception)時入 registry。`external_public` + 純反思(`ReflectionMoment` 無 `UserSpoke`)**不入**。

`LearnName.run`:
1. **機械 guard(L2,一律套用,連 owner 也擋)**:`token` strip 後長度 < 門檻(ASCII <2 / CJK <2,D2)、落硬編 denylist(§Global D2)、或違反 L0 比對規則 → 回 friendly-error 字串,**不寫檔**。
2. 通過 → `store.add(token, origin="owner", note=..., now=time.time())`(恆 active)。`op=remove` → `store.remove(token)`。
3. provenance:把 `ctx.external_ctx` + `ctx.origin_tier` 記進 D3 的 `aliases_history.jsonl`(比照 PinSelf 記 self_history)。回 friendly success。

- [ ] **Step 1: 失敗測試**
  - **registry gating(承重)**:`tests/test_mind_loop_learnname_gate.py` —— external_dm turn 的 `_active_tool_registry()` **含** `LearnName`;帶 UserSpoke 的 internal turn **含**;external_public turn **不含**;純反思 turn(ReflectionMoment 無 UserSpoke)**不含**。teeth:反轉(斷言 external_public 有 LearnName)會 fail。
  - **guard**:`tests/test_tools_learnname.py` —— add 正常暱稱→寫入 + active_tokens 含;add 過短/denylist 字→friendly-error + **不**寫;remove→移除。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — `tools.py` `LearnName`(比照既有 pydantic tool 結構 + `_record`);`mind_loop._active_tool_registry`(spec 指 `mind_loop.py:778-820`,對照 line 810-811 加 PinSelf 的 pattern,但條件是 `origin_tier=="external_dm"` 或 `internal+UserSpoke`,**非** `_is_reflection`)。注意:external turn 的保守集(P1e `EXTERNAL_TOOLS`)在 external_dm 時要**額外納入** LearnName(不放寬其他工具)。
- [ ] **Step 4-5: 跑綠 + 全套回歸**(確認既有 P1e external-tool-gate 測試不破:external_dm 仍無 Shell,只多 LearnName)
- [ ] **Step 6: Commit** `feat(aliases): LearnName tool gated to owner-present turns + mechanical guard (Part A / A2)` + trailers。

---

## Task 3: (A3) AttentionGate alias provider + L0 match 硬化 + seed guard

**Files:** Modify `src/dollos/mind/attention.py`、`src/dollos/kernel.py`、`src/dollos/character.py`(D1b);Test `tests/test_attention*.py`、`tests/test_kernel_alias_provider.py`

**Interfaces:**
- Consumes: A1 store、A2 寫入的 learned tokens。
- Produces:
  - `AttentionGate.__init__` 收 `alias_provider: Callable[[], frozenset[str]]`(**取代** `name_aliases: list[str]`)。`_l0_signal`(`attention.py:84-97`)match 時呼叫 `self._alias_provider()` 拿當前集合。
  - **L0 比對硬化**(`_l0_signal` 的 name-match)：ASCII token → lowercased **word-boundary**(`\b`,用 `re`);CJK token → 子字串但**強制來源集已過 min-length≥2**;混合含 ASCII 詞 → 走 `\b` 界(D2)。gate 維持 pure-logic。
  - `kernel.py` 建 provider 閉包:union `{pack meta.name + meta.aliases}`(`character.py`,D1b 加選用 `aliases: list[str] = []`)∪ `{settings.attention.name_aliases}`(admin floor,選用)∪ `{store.active_tokens()}`;**load-time guard**:每個 seed/floor token 過 min-length + denylist,不合 → `logger.warning` + 剔除;`mtime`-gated cache(只有 json mtime 變才重建 learned 部分)。fail-closed on stat 失敗。

- [ ] **Step 1: 失敗測試**
  - `tests/test_attention*.py`:`alias_provider=lambda: frozenset({"gura"})` → 說 "hey gura" admit(l0_name);"gurapp" **不** admit(word-boundary,teeth);provider 回空 → l0_name 從不 fire(其餘 L0 訊號不受影響)。CJK:`frozenset({"古拉"})` → "古拉在嗎" admit;單字 CJK 來源被 guard 剔除(不 admit)。
  - `tests/test_kernel_alias_provider.py`:provider union 三來源;denylisted/過短 seed(如 pack name="你")→ warn + 不在 wake-eligible;mtime-cache(store 未變不重讀);stat 失敗 fail-closed(回 seed+config,不 raise)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**(attention `alias_provider` + `re` word-boundary;kernel union+guard+mtime-cache;character `meta.aliases`)。**更新既有 `tests/test_attention*.py`** 全部改傳 `alias_provider=` 而非 `name_aliases=`(P1c 建構點)。
- [ ] **Step 4-5: 跑綠 + 全套回歸**(既有 attention/kernel 測試改建構後仍綠)
- [ ] **Step 6: Commit** `feat(aliases): AttentionGate alias_provider + L0 word-boundary/CJK match hardening + seed guard (Part A / A3)` + trailers。

---

## Task 4: (A5) bridge alias config 清理 + 中文正名遷移

**Files:** Modify `src/dollos/discord_bridge/{controller.py,__main__.py}`、`bridge.toml`、`bridge.example.toml`、`character_packs/gura/doll.toml`(遷移「古拉」)、daemon config 文件註記;Test 既有 bridge 測試更新

**背景**:bridge `name_aliases` 是死 config(L0 早搬 daemon;A3 確立 provider 從 daemon 來)。移除之。**中文正名遷移(M6)**:現 bridge `name_aliases=["Gura","gura","古拉"]`,但 pack `meta.name="Gura"` 不含「古拉」→ 移除後 cold-start 不會被「古拉」叫醒。把「古拉」port 進 pack `[meta] aliases=["古拉"]`(D1b,過 A3 的 guard —— 2 字 CJK 合格)。

- [ ] **Step 1: 失敗測試** — bridge config load 不再要求/讀 `name_aliases`(移除後 `_load_bridge_config` 不炸);`character_packs/gura/doll.toml` 有 `[meta] aliases` 含「古拉」;pack load 讀得到 `meta.aliases`(character.py 測試)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — 移除 bridge `name_aliases`(controller `BridgeConfig`、`__main__._load_bridge_config`、兩個 toml);`doll.toml` 加 `aliases=["古拉"]`;daemon `config.example.toml` `[attention]` 註記「name_aliases 是選用 admin floor,主要靠 Doll 自學」。
- [ ] **Step 4-5: 跑綠 + 全套回歸**
- [ ] **Step 6: Commit** `feat(aliases): remove dead bridge name_aliases + migrate 古拉 to pack seed (Part A / A5)` + trailers。

---

## Self-Review（對 spec §3/§8 Part A 逐條核）

- [x] A1 store(add/remove/active_tokens,無 pending)→ Task A1
- [x] A2 LearnName **只在 owner-present 回合**入 registry(非 origin_tier 事後擋,C1 修法)+ 機械 guard → Task A2
- [x] A3 alias_provider 注入 + word-boundary/CJK match 硬化 + seed load-time guard + mtime-cache + fail-closed → Task A3
- [x] seed floor 也過 guard(I3 誠實邊界)→ Task A3
- [x] A5 bridge 死 config 移除 + 中文正名遷移(M6)→ Task A5
- [x] A4 不做(D6)→ 明文排除
- [x] provenance 記平行 aliases_history.jsonl(D3)→ Task A2

**跨 task:** A2/A3 都 consume A1;A3 是收口(讓自學真的餵回 L0)。**A2 是 security 承重,registry gating 接錯整個 Part A 無效 —— reviewer 要特別驗 external_public/純反思回合 LearnName 結構性不存在。**

---

## 執行銜接

`superpowers:subagent-driven-development`,每 task fresh implementer + reviewer(sonnet),whole-branch review 用 **opus**(ultracode:嚴查 A2 的 registry gating 真的擋掉陌生寫入面、A3 的 seed guard、match 硬化 teeth)。worktree `.worktrees/pa-aliases/` on branch `pa-aliases`。驗收 = spec §9 的單元 + live-smoke 對照。
