# Self-Learned Name Aliases + `owner_guild_only` — Design Proposal

**Status: PROPOSAL — awaiting user approval (design-before-code, CLAUDE.md「Specs before code」).**
**Date: 2026-07-06**
**Scope: `mind/attention.py`, `mind/` (new alias store module), `tools.py`, `kernel.py`, `config.py`, `discord_bridge/{controller,client,__main__}.py`, `character.py` (read-only)**

這份 spec 一次涵蓋兩個相關但獨立的變更：

- **Part A** — Doll 自己學會別人怎麼叫她（`name_aliases` 由靜態 config 改成 self-learned + 中毒防護），user 核心意圖:「name_aliases 應該由 doll 自行學會」。
- **Part B** — bridge 端新增 `owner_guild_only` 開關，取代已死的 `channel_allowlist` 角色。

兩者都建立在 P1c（forward-all + daemon-side `AttentionGate`）與 P1e（`origin_tier` 中毒隔離）之上，並**重用既有 substrate、不重造**。

---

## R1 review convergence（2026-07-06，兩份對抗性審查合流）

兩份獨立審查（security/poisoning 與 soundness/YAGNI）**各自對照真實 code 逐條驗證**後，指向同一個致命的結構性缺陷。本版把它修掉，並沿著兩份審查一致認同的方向（owner-context-only 嚴格更穩健）重寫 Part A 的學習機制。

- **C1（CRITICAL，已修）— origin-gate 是死碼，原設計每個學到的 alias 都落 `active`。** 原 §3.3 讓 `LearnName` 仿 `PinSelf` 只在 reflection turn 出現。但 reflection turn 由 `ReflectionObserver` 發的 `ReflectionMoment` 觸發，它**沒有 `channel_id`**（`reflection_observer.py:46`）→ 落 `perception_queue.py:87` 的 origin-less 桶，永不與 `ChannelMessage` 同桶 → `_derive_origin_tier`（`mind_loop.py:316-321`）恆回 `"internal"`。所以 `LearnName` 寫入回合的 `ctx.origin_tier` **恆為 internal**，`external_public → pending` 分支永不執行：陌生人在公開頻道用的字，Doll 事後一反思寫下去就是 `active`，完全重現 P1c「亂回」。原 §6 宣稱的「關鍵不變式」為偽。（順帶證實：既有 `PinSelf`-on-external-reflection 分支 `mind_loop.py:810` 同理不可達。）
- **C2（CRITICAL，已修）— 設計裡沒有 owner-gate，只有「Doll 自我採納」。** 原 pending→active 靠 Doll 在 internal 反思回合 `op=adopt`，而反思恆 internal → 這是**純自我認可、無人在迴路**，牴觸 MEMORY `ref_intrinsic-reflection-is-net-negative`（無外部 grounding 的反思淨負）。真正的 owner DM 回合（`external_dm`）拿的是 `EXTERNAL_TOOLS`（`tools.py:1043`，不含 `LearnName`），adopt 永不可能發生在真的 owner DM 裡。

**修法（本版採用）：** 把 alias 學習**綁在真正第一手聽到名字的那個 live turn**，信任邊界由 **tool registry 可用性**強制——呼叫當下的 `origin_tier` 是真的來源，不是事後反思讀到的洗白值。`LearnName` 只在 **owner 情境**（`external_dm` owner DM，或帶 `UserSpoke` 的 `internal` 本機對話）出現；`external_public` 與純反思回合**根本不提供這個工具**。陌生人不是「被 gate 擋掉」，是**結構上無法呼叫**——攻擊面歸零，不是靠一個不會 fire 的 gate 假擋。這**不是為了簡化而弱化**：移除寫入面比擋寫入面嚴格更強（兩份審查一致結論）。原本承重卻失效的 pending/adopt/A4 浮現塔從 base design 移除，降為「若未來要讓 Doll 撿社群暱稱」的選用擴充（需正確接線：stranger→pending 在**真實 external_public 回合**寫入 + owner 在 DM **顯式**核可，絕非反思自採）——列為 open decision **D6**。

其餘折入的發現：seed/pack alias 也必須過同一套機械 guard 與比對硬化（I3，§3.4/§3.5）；`is_owner_in_guild` 非-`NotFound` 失敗模式與 fail 方向（I1，§4.2）；不安全 default（I2 → D7）；backfill scope 流失 + reconnect 迴圈放大（I2 → §4.3/D5）；provider `stat()` 失敗 fail-closed（M1，§3.5）；中毒 alias 開整段 session 的放大半徑（M5，§6）；行號校正（M2，§5.2）。逐條對應見各節與 §6、§7、§8。

---

## 1. Goal

1. **cold-start 就能醒**：Doll 一啟動就把角色包正名（`doll.toml [meta] name`）當成永久 alias，還沒學任何東西之前就能被叫醒。
2. **她自己學暱稱（從可信來源）**：owner 在對話（本機 chat 或 owner DM）裡叫她某個暱稱，她在**當下那一回合**用 `LearnName` 把它記成「owner 這樣叫我」，之後這暱稱也能叫醒她。學習只發生在第一手聽到的 live 回合，不 defer 到事後反思（否則來源脈絡會被洗成 internal，見 R1 convergence C1）。
3. **陌生人不能教壞她**（CRITICAL）：陌生人不能讓 Doll 相信「hey / everyone / 你 / 某常見字」是她的名字——否則她會對所有訊息醒過來，正是 P1c 修掉的「亂回」失敗。本版用**結構性移除寫入面**達成：陌生（`external_public`）回合根本不提供 `LearnName` 工具，不是靠一個事後才判斷的 gate。
4. **L0 讀到活的 alias 集合**：`AttentionGate` 的名字比對在 turn time 讀 `{pack 正名} ∪ {已學+已認可的 alias}`，且**留在 daemon 端**（report 1 證實餵 L0 的是 daemon 的 `AttentionSettings`，不是 bridge）。
5. **owner_guild_only**：bridge 只轉發「owner 有加入的 guild」的訊息（+ owner DM 永遠轉），關掉時維持現狀 forward-all；用不新增 intent 的 `fetch_member` 偵測；順手清掉已死的 `channel_allowlist`。

## 2. Scope

### In
- 新增 daemon-side self-learned alias store（`{memory_root}/name_aliases.json`）+ 標準庫 module `mind/name_aliases.py`（結構仿 `self_profile.py`；base API 只有 `add/remove`，無 pending/adopt）。
- 新增 `LearnName` pydantic tool，**只在 owner-present 的 live 回合**（`external_dm` owner DM，或帶 `UserSpoke` 的 `internal` 本機對話）進入工具集——由 registry 可用性強制信任邊界。
- `AttentionGate` 由「建構時凍結 list」改為「turn time 讀 alias provider」（provider `stat()` 失敗 fail-closed）。
- alias 中毒防護:**結構性移除陌生寫入面**（`external_public`/純反思回合不提供工具）+ 機械 guard（min length / denylist，寫入時）+ L0 比對規則硬化（讀取時，seed/config/learned 一律適用）+ seed 不可污染 floor。**不用**原設計那套（已證實失效的）`origin_tier` post-hoc active/pending 路由。
- bridge:移除已死的 `name_aliases`（report 1）與 `channel_allowlist`;`__main__` 的 pre-register / `reconnect_backfill` 改用 guild channel 列舉;新增 `owner_guild_only` gate + 兩個新的 `DiscordClient` Protocol method。

### Out（明確不做）
- **不動** L0/L1 engagement session 邏輯（`Session`、`window_for`、`note_reply`,`attention.py:99-206`）——只換 `_l0_signal` 的 alias 來源。
- **不做**完整 skeptic gauntlet 於 alias（名字不是身分改寫）。base design **也不做** pending/adopt ratification-lite——因為原本靠它擋陌生人的 origin-gate 已證實失效(R1 C1),改用結構性移除陌生寫入面(§3.3)。pending/adopt 僅在 D6 選用擴充才回來,且屆時 ratification **必須有 owner 在迴路**(§6.3),不是 Doll 自採。
- **不動** voice bridge、UI、phone。
- **不改** `self_profile.md` 的內容/section 結構（learned alias 走**獨立** store,不塞進 self_profile 的自由文 bullet——原因見 §3.2）。
- **不新增** Discord privileged intent（`members`）——用 REST `fetch_member`（report 3 §3）。

---

## Part A — Self-Learned Name Aliases

### 3.1 現況（三份 report 的合流結論）

- L0 名字比對在 daemon:`AttentionGate._l0_signal`(`attention.py:91`)做 **case-sensitive 子字串** `any(alias in content ...)`,aliases 由 `__init__` kwarg 凍結成 `self._name_aliases`(`attention.py:74`)。
- 來源是 daemon config `AttentionSettings.name_aliases`(`config.py:237`, default `[]`)→ `kernel.py:287`。但**沒有任何 daemon toml 宣告 `[attention]`**,所以現況恆為 `[]`,`l0_name` 從不觸發(report 1 §4)。
- `bridge.toml:42` 的 `name_aliases=["Gura","gura","古拉"]` 載進 `BridgeConfig.name_aliases`(`controller.py:83`)但**零讀取點**——forward-all 刪掉 `wake.py` 之後就是死 config(report 1 §3)。
- 角色包只有單一正名:`character_packs/gura/doll.toml:3` `name = "Gura"`,由 `DollPack.load`(`character.py:63`)讀進 `kernel.py:262` `self._doll_pack`,**早於** AttentionGate 建構(`kernel.py:286`)——正名在建 gate 時已在手,可直接當 seed。
- self-learning substrate 現成兩層(report 2):`self_profile`(`PinSelf`→`self_profile.apply`,always-inject,id-tagged,provenance-tagged,不索引,**無 ratification**)與 `current_self`(single-slot pending → skeptic → Doll `SelfRevision` adopt → `evo_adopt` log 即真相)。`origin_tier` 路由(P1e,`tools.py:56` `_ORIGIN_DIR`)把 stranger 寫入隔離到 `external_public/`。

### 3.2 設計:獨立 alias store（仿 self_profile 的 pattern，不共用 self_profile.md）

**決策:新增獨立標準庫 module `src/dollos/mind/name_aliases.py`**,結構複製 `self_profile.py`(純 read-modify-write、id-tagged、provenance-tagged、atomic write、永不 raise、可單元測試),但**不重用 `self_profile.md` 檔本身**。理由:

1. `self_profile.md` 是自由文 markdown bullet(`self_profile.py:58-68`),不是機器可解析的乾淨 token 集;把喚醒觸發字混進 Doll 會自由 prune 的散文裡,對一個**安全敏感**的 wake trigger 太脆弱。
2. wake gate 需要 per-alias 的 `state`(active/pending)、`origin`(seed/owner/stranger)、provenance——`PinSelf` 的散文 bullet 給不了。
3. `self_profile.md`「絕不索引、always-inject」是好 pattern(值得結構性複製),但 alias store 的**消費者是 `AttentionGate`**(wake 判斷),不是 prompt。

> 這正是 report 2 §5 的建議:「structurally clone self_profile … but gate wake-eligible aliases」。我們複製 pattern,分出獨立 store。

**檔案:`{memory_root}/name_aliases.json`**（`memory_root = settings.data.root / "memory"`,`kernel.py:135`)。格式:

```json
{
  "aliases": [
    {"id": "a1", "token": "小鯊", "state": "active", "origin": "owner", "date": "2026-07-06", "turn": 42, "external_ctx": false},
    {"id": "a2", "token": "shork", "state": "active", "origin": "owner", "date": "2026-07-06", "turn": 58, "external_ctx": true}
  ]
}
```

- **seed/正名不存進檔案**:pack `meta.name`(可能多個,見 open decision D1)在讀取時 union 進來(§3.5),所以它**永遠不可被 prune / 中毒 / 刪除**,即使整個 json 被刪也還在——這就是 cold-start 保證。**但 seed 也不是無條件安全**:它照樣要過 L0 比對硬化與機械 guard(I3,§3.4)——「不可刪」不等於「不可危險」。
- `state`:base design 恆為 `active`(=wake-eligible,進 L0 集合)。`external_ctx=true` 的 `a2` 是 owner **在 owner DM 裡**(external_dm 仍屬外部脈絡但為 owner 本人),仍是可信寫入。**保留 `state` 欄位**是為了 D6 選用擴充(若未來加 stranger→pending);base design 永不寫 `pending`,因為沒有 stranger 寫入路徑。
- `origin`:base design 恆為 `owner`(external_dm/internal-UserSpoke 都是 owner);`seed`(正名)只存在於 union 層,不落檔。
- module API 仿 `self_profile.apply`:base 為 `add / remove`(`adopt` 僅 D6 選用擴充才需要),回人類可讀字串或 friendly-error(永不 raise),provenance 事件寫進 `{memory_root}/self_history.jsonl`(或平行的 `aliases_history.jsonl`,見 D3)。

### 3.3 學習機制:`LearnName` 工具（owner-context-only，live-turn，不 defer 到反思）

**信任邊界由「這個工具在哪些回合出現在工具集」強制,而不是由事後讀 `ctx.origin_tier` 判斷。** 原設計(仿 `PinSelf` 只在反思回合出現)已被 R1 convergence C1 證實失效:反思回合恆 `internal`,`origin_tier` 到 `LearnName.run` 時已被洗白,`external_public → pending` 分支永不 fire。本版改由 **registry 可用性**在呼叫當下就框死信任面——那一刻的 `origin_tier` 才是真的來源。

新增 pydantic tool `LearnName`(`tools.py`)。它**只在以下 live、owner-present 回合進入工具集**(在 `mind_loop._active_tool_registry` `mind_loop.py:778-820` 加一條 branch):

- **`external_dm` 回合**(owner DM)。`_derive_origin_tier`(`mind_loop.py:316-321`)只在 `author_is_owner AND is_dm` 才回此 tier,故 `external_dm ≡ owner 本人`。此回合的 `EXTERNAL_TOOLS` 子集需**額外加入 `LearnName`**(對照 line 810-811 加 `PinSelf` 的 pattern,但條件是 `origin_tier == "external_dm"`,不是 `_is_reflection`)。
- **帶 `UserSpoke` 的 `internal` 回合**(本機語音/文字對話 = 坐在電腦前的 owner)。

**明確不提供**的回合:
- **`external_public`**(任何公開頻道,含 owner 在公開頻道發言)——陌生人**結構上無法呼叫** `LearnName`,不是被 gate 事後擋掉。
- **純反思回合**(`ReflectionMoment`,無第一手 utterance;`internal` 但無 `UserSpoke`)——這正是 C1 的洗白路徑,移除之。名字絕不在「事後回想」時被寫入,只在**當下第一手聽到**時寫入。這也一併解掉 review I1 的疑慮:證據就是**當前 perception**(她正在讀的 owner 訊息),沒有「反思時 utterance 已滾出 context」的問題。

欄位(概念):
- `op: Literal["add","remove"]`(base design **不含** `adopt`——沒有 pending 狀態要升級;見 §6 與 D6)。
- `token: str` — 暱稱本身。
- (可選)`note: str` — 誰、什麼情境這樣叫她(稽核/自我敘事用)。

`LearnName.run(ctx)` 行為:
1. **機械 guard(L3,不論來源一律套用,連 owner 情境也擋)**:`token` 太短(< 門檻)、落 denylist(常見字/代名詞)、或違反 L0 比對規則 → 回 friendly-error,不寫檔。這是唯一能擋「owner 自己教壞字」(如把「早安」設成 alias)的閘。
2. tool 只在 owner 情境出現(見上),故成功寫入一律 `state=active`、`origin="owner"`。**沒有 stranger 寫入路徑,故 base design 無 `pending` 狀態、無 origin 路由分支。** 防禦不靠一個會不會 fire 都存疑的 `origin_tier` 判斷,靠「陌生回合工具不存在」這個結構事實。
3. `op=remove`:任何 owner 情境回合皆可(她淘汰不再用的暱稱)。
4. provenance:每次成功寫入把 `external_ctx`(`ctx.external_ctx`,`mind_loop.py:377`)與 `origin_tier` 一起記進 history,與 `PinSelf` 一致,稽核可追。

**為什麼這比原設計嚴格更穩健(不是為了簡化而弱化)**:原設計宣稱用 `origin_tier` 兩道 code gate 擋陌生人,但那 gate(C1)根本不會 fire、且 adopt 是純自我認可(C2)。本版直接**移除陌生人的寫入面**——不是「擋」,是「不存在」。滿足 user 核心意圖「name_aliases 由 doll 自行學會」(她確實自學,從可信來源學),同時陌生中毒面歸零。**若**未來想讓 Doll 撿社群裡陌生人用的暱稱,那需要一條正確接線的 pending 路徑(見 §6 尾與 D6),不是把失效的舊塔留著。

### 3.4 L0 比對規則硬化（把「亂回」升級成 code gate）

現況 `alias in content`(case-sensitive 子字串,`attention.py:91`)本身就危險:`"Gura"` 會命中 `"Gurapp"`,2 字 alias 命中一切。因為這是安全敏感 wake trigger,**prompt 管不住的語意要升級成 code 閘**(MEMORY `ref_weak_model_soft_mechanism_playbook`)。硬化規則(§5 詳述,D2 定案):

- ASCII token:lowercased **word-boundary** 比對(`\bshork\b`),不再裸子字串。
- CJK token(無空白可切):維持子字串,但強制 **min length ≥ 2 CJK 字**,並過 denylist。
- **混合 token**(如 `Gura醬`):歸類與門檻由 D2 定案(見 §7)——動手前先釘死走 ASCII 界規則還是 CJK 規則,避免灰區。
- 機械 guard(§3.3 step 1)在 **寫入時**擋掉危險 token;比對規則在 **讀取/match 時**縮小炸半徑——兩道獨立防線。

**seed / config floor 也一律過同一套硬化(I3,關鍵)**:pack `meta.name`、D1(b) 的 `[meta] aliases`、以及 daemon `[attention] name_aliases` admin floor,雖然不經 `LearnName` 的寫入 guard(它們不落 `name_aliases.json`),但在 **match 時**照樣套 L4 比對規則,且在 **kernel 建 provider 時**過一次 load-time guard:
- 一個 denylisted 或**低於 min-length** 的 seed/floor(如 pack `name = "你"` 或單字 CJK 名)→ **load 時 `logger.warning`**,且**不納入 wake-eligible 集合**。
- **誠實邊界(對照 review2 M1)**:這代表若某 pack 的正名本身太短/太常見,它的 cold-start-by-name 就**不會生效**(Doll 不會被那個名字叫醒)——這是刻意的:寧可那個 pack 的 name-wake 失效並在 log 明講,也不讓「不可刪的 seed」變成「不可刪的亂回來源」。1 字 CJK 的 pack 需在 pack 端補一個 ≥2 字的正名或用 D1(b) 補 seed。**匯入/分享來的 pack 一律視為 untrusted**(願景明寫多-doll/doll 社交),同一套 guard 保護。

### 3.5 餵回 L0:alias provider（daemon-side，turn time）

`AttentionGate` 目前建構時凍結 `self._name_aliases`(`attention.py:74`)。改為注入一個 **provider callable**:

- `AttentionGate.__init__` 收 `alias_provider: Callable[[], frozenset[str]]`(取代 `name_aliases: list[str]`)。
- `_l0_signal`(`attention.py:84-97`)在 match 時呼叫 `self._alias_provider()` 拿當前 wake-eligible 集合再跑比對迴圈——gate 維持 pure-logic,不知道 alias 從哪來。
- provider 由 **kernel** 建(`kernel.py:286` 附近),閉包 union 三個來源(每一個都先過 §3.4 的 load-time guard,denylisted/過短者 warn 並剔除):
  1. `self._doll_pack.meta.name`(+ D1(b) 選用 `[meta] aliases`)(正名 seed,`character.py:28`)——永久 floor。
  2. `settings.attention.name_aliases`(`config.py:237`)——保留為**選用的 admin 靜態 floor**(不再是主來源;可空)。
  3. `name_aliases.json` 裡 `state=="active"` 的 learned token。
- **效能**:provider 對 `name_aliases.json` 做 **mtime-gated cache**——只有檔案 mtime 變(owner 情境寫入才變,罕見)才重建 frozenset;正名 + config floor 是常數。避免每則訊息讀檔。
- **純度 / fail-closed(M1)**:`AttentionGate` docstring 宣稱 pure-logic/no-I/O,但 provider 閉包為了 mtime cache 會在每則訊息的 match 路徑做 `path.stat()`。這條 I/O **必須 fail-closed**:`stat()`(或讀檔/parse)拋例外 → 回**上一份 good frozenset**(若曾建過),否則回**只含 seed+config floor 的集合**——**永不 crash 整條 attention 路徑,也永不因錯誤而放寬**(絕不回全集/絕不 admit-all)。閉包自己吞例外,不讓它逃進 `_l0_signal`。

這條路徑**全在 daemon**(report 1 §4 結論:架構上 L0 讀 daemon `AttentionSettings`);bridge 完全不參與 alias。

### 3.6 bridge `name_aliases` 移除

report 1 §3 證實 bridge `name_aliases` 零讀取點(死 config)。移除:
- `BridgeConfig.name_aliases`(`controller.py:83`)欄位刪除。
- `_load_bridge_config` 的 `name_aliases=list(d.get("name_aliases", []))`(`__main__.py:84`)刪除。
- `bridge.toml` / `bridge.example.toml` 的 `name_aliases` 行 + 註解(`bridge.toml:42`,`bridge.example.toml:42`)刪除。
- 附帶:`always_wake_channels`(`bridge.toml:47`)在 bridge 端**同樣**是 forward-all 後的死 config(controller 只讀 `bot_id`/`owner_id`,report 1 §3)——建議一併移除(D4)。

---

## Part B — `owner_guild_only` Toggle

### 4.1 現況（report 3）

- `_capture_and_forward`(`controller.py:167-218`)只有兩個 return gate:dedup(`:185`)與 self-filter `author_id == bot_id`(`:188-189`);**forward-all,allowlist 不 gate**。
- 每則 event 已帶 guild id:`_to_event` 蓋 `is_dm`(`client.py:130`)與 `guild`(`client.py:135`,DM 為 `None`);`on_discord_message` 把 `None → "dm"`(`controller.py:117`)。
- 現有 intent(`Intents.default()` + `message_content`,`client.py:240-241`)下 `members` intent **OFF**,故 cache 版 `get_member` 不可用;但 REST `guild.fetch_member` 不看 intent 可用,non-member 丟 `discord.NotFound`(report 3 §3)。
- `channel_allowlist` 兩個殘留用途:seed `_registered`(`controller.py:112`)+ pre-register(`__main__.py:158`)——與 register-on-first-forward(`controller.py:204-210`)重複,可移除;真正會壞的是 `reconnect_backfill`(`__main__.py:182`)需要明確 channel 列表(report 3 §4)。
- `DiscordClient` Protocol(`client.py:46-83`)**無** guild/member/channel 列舉存取——兩個新需求各需新 Protocol method + Fake(report 3 §5)。

### 4.2 設計

**config**:`[discord]` 新增 `owner_guild_only: bool`(default `false` = 現狀)。

**語意**:
| 情境 | `owner_guild_only=false`(現狀) | `owner_guild_only=true` |
|---|---|---|
| owner DM | forward | forward(永遠) |
| stranger DM | forward | **drop** |
| owner 有加入的 guild 的訊息 | forward | forward |
| owner 沒加入的 guild 的訊息 | forward | **drop** |

**gate 位置**:在 `_capture_and_forward` 內,**ambient log 之後**(full-capture 永遠無條件——稽核/finetune 語料,`controller.py:11-13` §3.3)、**self-filter 之後**、register/forward **之前**加一個 `owner_guild_only` gate;drop = `return`(已 log,不轉發)。這維持「ambient log 無條件」不變。

```
full-capture (always)  →  self-filter  →  [NEW owner_guild gate]  →  register-on-first-forward  →  forward
```

**config-load guard(I1,先決)**:`owner_guild_only=true` 且 `owner_id` 為 None/空 → **config 載入即拒啟動**(raise，不 start)。一個「只信 owner」卻不知 owner 是誰的 gate 沒有安全語意,不能讓它以 drop-all 或 crash 的形式跑起來。

**偵測**:新 Protocol method
```python
async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool
```
`PycordClient` 實作:`bot.get_guild(int(guild_id))` + `await guild.fetch_member(int(owner_id))`(py-cord import 維持 lazy,同既有 pattern)。**無需新 intent**。**gate 呼叫端(`_capture_and_forward`)把整個 owner-guild 判斷包在 try**:任何未預期例外一律視為 **drop(fail-closed)** 並 log,**絕不讓例外逃進轉發路徑**(`controller.py:167`)把整條 forward 打掛。

**各失敗模式明定 fail 方向(I1；leak vs starve 的誠實取捨,安全側一律 fail-closed)**:

| 情況 | 處置 |
|---|---|
| `fetch_member` 回正常 member | `True`,寫入 cache |
| `fetch_member` raise `discord.NotFound`(確定非成員) | `False`(drop),寫入 cache |
| `bot.get_guild(int(gid))` 回 `None`(reconnect 後 guild cache 尚未 populate 的窗口) | 無定論 → **有 cache 舊值用舊值**;否則 **fail-closed drop**,**不 cache**(下一則同 guild 訊息重查)。**誠實邊界**:新 guild 在 cache populate 完成前會**短暫 starve**(丟掉 owner 的合法訊息),但安全側寧 starve 不 leak。 |
| `fetch_member` raise `Forbidden` / `HTTPException` / rate-limit(429) / timeout(transient) | **有 cache 舊值用舊值**;否則 **fail-closed drop**,**不 cache 失敗**(retry 會重查,不把一次抖動釘成永久 drop) |

原設計只 catch `NotFound` 是不完整的:未 catch 的 429/`Forbidden`/timeout 會炸進 `_capture_and_forward`;`get_guild` 回 `None` 會 `None.fetch_member` AttributeError。以上表把兩者都收斂到 fail-closed。

**caching + staleness(M2)**:controller 內持 `dict[str, bool]`,per `guild_id` 惰性填 + **短 TTL**(建議 **≤ 5–15 min,不是 1h**)過期重查;reconnect 時清空(fresh controller 每次 reconnect 重建,`controller.py:106-112` 的 per-session 註解已是此模型)。**owner 中途退群的 leak 窗上界 = TTL**,故取短。cache miss + error 一律 fail-closed(見上表)。DM(`guild_id == "dm"` / `is_dm`)**短路不查 REST**:`author_id == owner_id` → forward,否則 drop。

> **實作偏移記錄(Part B whole-branch review M2,2026-07-06 補記)**:目前
> `controller.py::_owner_in_guild_cached` 與上表的 fail-direction 表不完全
> 一致——它用單一 `try/except Exception` 把所有失敗模式(`NotFound`、
> `get_guild` 回 `None`、transient 429/`Forbidden`/timeout)一律收斂成同一
> 個 `False`,並**無條件寫入 cache,佔滿整個 `OWNER_GUILD_CACHE_TTL_S`**——
> 不像上表區分「confirmed `NotFound` 才長 cache;transient 有舊值用舊值,
> 否則 fail-closed 但不 cache 失敗」。
>
> 這是刻意選擇的安全側可用性取捨:對一個高流量、owner 不在的 guild,把
> transient 失敗也 cache `False` 能界定 REST 呼叫的放大上界(不 cache 的話
> 同一 guild 的每一則訊息都會重新打一次 REST);代價是若某個 guild 第一次
> 查詢就撞到 transient 失敗(當下無舊值可退回),TTL 內 owner 在該 guild 的
> 合法訊息會被誤丟(starve),即使那次失敗其實跟「owner 是否在這個 guild」
> 無關。這跟表格「transient 失敗不 cache,下一則重查」的語意不同——真正要
> 對齊表格需要先改變 B1 `is_owner_in_guild` 的 bool 回傳契約,留給日後的
> 設計變動處理,而非本輪順手改掉。
>
> **Follow-up(留待未來設計,尚未排期)**:tri-state cache 精修——
> `is_owner_in_guild` 改回傳三態(member / not-member / unknown),
> controller 端 confirmed-not-member 才寫入滿 TTL 的 cache,transient/
> unknown 短 cache 或不 cache,才能真正落實上表 leak-vs-starve 的取捨,而
> 不是現在被單一 bool 契約壓縮成一種安全側處理(見 `controller.py`
> `_owner_in_guild_cached` 的 `# NOTE:` 註解)。

### 4.3 `channel_allowlist` 移除 + backfill 改寫

- 刪 `BridgeConfig.channel_allowlist`(`controller.py:85`);`_registered` seed 改 `set()`(`controller.py:112`)。
- 刪 `__main__.py:158-166` 的 pre-register 迴圈(register-on-first-forward `controller.py:204-210` 已覆蓋,report 3 §4:無正確性損失,第一則 inbound 會在轉發前先 register)。
- 刪 `_load_bridge_config` 的 `channel_allowlist=list(d["channel_allowlist"])`(`__main__.py:86`)。
- **backfill 改寫**(真正會壞的一環):新 Protocol method
  ```python
  async def owner_guild_channels(self, owner_id: str | None) -> list[str]
  ```
  列舉 bot 所在 guild 的可讀 text channel:`[str(c.id) for g in bot.guilds for c in g.text_channels if c.permissions_for(g.me).read_message_history]`(guild/channel cache 在 default intent 下已 populated,report 3 §4)。當 `owner_guild_only=true` 時**只列 owner 有加入的 guild**(用 `is_owner_in_guild` 過濾),當 `false` 時列全部。
  `__main__.py:182-184` 的 `reconnect_backfill(discord.fetch_history, cfg.channel_allowlist)` 改成先 `channels = await discord.owner_guild_channels(...)` 再傳入。
- **成本 synergy / 警示(I2，加重)**:三個要一起看清楚,別只當「成本」——
  1. **reconnect 迴圈放大**:`run()` 的 reconnect loop 失敗時每 ~5s 重入(`__main__.py:110-124`),而 backfill 在**每次 reconnect** 跑。`false` 時列舉**每個 guild × 每個可讀 channel × `fetch_history(50)`** = N×M 個 REST call **每次 reconnect**,是 allowlist 以前框住的 rate-limit 風險;`owner_guild_only=true` 只收斂到 owner guild,channel 仍可能很多。
  2. **語意變化(不只成本)**:這改變了**哪些 channel 的歷史被 replay 進 daemon**——從一小撮 curated channel 變成所有可讀 channel。這是行為改變,不是純效能。
  3. 因此把 **backfill scope** 與 **wake** 解耦是合理的:見 D5——建議保留一個**選用** `backfill_channels`(純供 backfill,與 owner_guild wake 無關),或對列舉設上界。**pre-register 的移除本身沒問題**(register-on-first-forward `controller.py:204-210` 已覆蓋,已驗證);咬人的專指 backfill scope 這一環。

---

## 5. Config Schema Changes（before / after）

### 5.1 Daemon config（`config.toml` / `config.gura.toml`）— `[attention]`

現況三個 daemon toml 都**沒有** `[attention]`(report 1 §4),恆用 default。Part A 後 `name_aliases` 不再是主來源(改 self-learned),但保留為選用 admin floor。

**Before**(隱含 default,`config.py:237`):
```toml
# (無 [attention] 區塊 → name_aliases = [] → l0_name 從不觸發)
```
**After**(選用;seed 已由 pack 正名保證,這裡只放 admin 想硬釘的額外 floor):
```toml
[attention]
# 選用:pack 正名已是永久 alias;learned alias 由 Doll 自學。
# 這裡只放你想「不經學習就永久生效」的額外喚醒字(可留空)。
name_aliases = []
```
`AttentionSettings.name_aliases` 欄位**保留**(不破壞 schema);語意由「唯一來源」降為「選用 floor」。文件註記更新。

### 5.2 Bridge config（`bridge.toml` / `bridge.example.toml`）— `[discord]`

**Before**（實際行號:`owner_discord_id` :27、`channel_allowlist` :35、`name_aliases` :42、`always_wake_channels` :47，M2 校正）:
```toml
[discord]
token = "..."
owner_discord_id = "123..."                  # :27
channel_allowlist = ["111", "222"]           # :35  只 seed/pre-register/backfill
name_aliases = ["Gura", "gura", "古拉"]     # :42  死 config(零讀取點)
always_wake_channels = []                    # :47  死 config(forward-all 後)
```
**After**:
```toml
[discord]
token = "..."
owner_discord_id = "123..."
owner_guild_only = false                     # NEW: true=只轉 owner 所在 guild(+owner DM)
# name_aliases 移除:Doll 自學(daemon-side self-learned aliases)
# channel_allowlist 移除:register-on-first-forward + owner_guild_channels 取代
# always_wake_channels 移除:forward-all 後 bridge 端不再用(D4)
```

### 5.3 新增 `DiscordClient` Protocol method（`client.py:46-83`）

```python
async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool: ...
async def owner_guild_channels(self, owner_id: str | None) -> list[str]: ...
```
各配一個 `FakeDiscordClient` 對應(`tests/test_discord_bridge_controller.py:36`、`tests/test_discord_forward_all.py:31`),讓 owner_guild gate 與 backfill 維持可單元測試(report 3 §5)。

### 5.4 `AttentionGate.__init__` 簽章

**Before**:`name_aliases: list[str]`(`attention.py:65`)。
**After**:`alias_provider: Callable[[], frozenset[str]]`。`kernel.py:286` 傳 kernel-side 閉包(§3.5);`tests/test_attention*.py`(`test_attention.py:21`、`test_attention_engagement.py:27`)改傳 `alias_provider=lambda: frozenset({...})`。

---

## 6. Security / Poisoning Analysis

**威脅**:陌生人在公開頻道讓 Doll 相信某個常見字是她的名字 → learned alias 流進 `alias_provider()` → L0 對一切訊息命中 `l0_name` → Doll 對所有流量醒過來 = P1c 修掉的「亂回」重現。**放大半徑(M5)**:`l0_name` 命中不是「醒一次」——`attention.py:120-139` 任何 L0 signal 會**reset engagement window + 把 author 併入 participants**,之後該 author 的**無標記後續訊息**走 L1 continuation 持續被 admit。一個中毒 alias = 對那個陌生人開整段 session。所以這個防線必須是硬的。

### 6.1 為什麼原設計的塔是假的（R1 convergence，已修）

原 §6 宣稱 L1(origin-gate)+ L2(ratification-lite)兩道 code gate 強制「stranger alias 永不進 frozenset」。**兩者皆偽**:`LearnName` 綁反思回合、反思回合恆 `internal` tier(`reflection_observer.py:46` 無 channel_id → `perception_queue.py:87` origin-less 桶 → `mind_loop.py:316-321` internal),所以 L1 的 `external_public → pending` 分支**永不執行**、每個 add 落 `active`;L2 的 adopt 只發生在 internal 反思裡 = **純自我認可、無人在迴路**。詳見檔頭 R1 convergence C1/C2。**不修 C1/C2,整個威脅模型不成立**,故本版重寫防線。

### 6.2 修正後的防線（結構性,不靠會不會 fire 都存疑的 gate）

| 層 | 機制 | 擋什麼 |
|---|---|---|
| **L1 結構性移除寫入面** | `LearnName` **只在 owner 情境**(`external_dm` owner DM / 帶 `UserSpoke` 的 `internal`)進入工具集;`external_public` 與純反思回合**根本不提供這個工具**(registry 可用性,`mind_loop._active_tool_registry`) | 陌生人**無法呼叫** `LearnName`,不是被 gate 事後擋;反思回合也無法把陌生證據洗成寫入 |
| **L2 寫入時機械 guard** | min length + denylist(常見字/代名詞)——即使 owner 情境也擋(`ref_weak_model_soft_mechanism_playbook`) | 「你/hey/the/2 字」永不成為喚醒字,擋 owner 自傷 |
| **L3 match 硬化(讀取時,seed/config/learned 一律)** | ASCII word-boundary、CJK min-length + denylist,取代裸子字串;seed/floor 同樣過(I3) | 縮小子字串炸半徑(`Gura`≠`Gurapp`);denylisted/過短 seed 不成為喚醒字 |
| **L4 seed 不可污染 floor** | pack 正名只在 union 層,不落檔、不可 prune/remove(但仍過 L3) | 中毒/刪檔都動不了 cold-start 喚醒 |
| **L5 provider fail-closed** | `stat()`/讀檔失敗 → 回上一份 good set 或 seed-only,**絕不放寬/crash** | I/O 抖動不會意外放大 wake 集合 |

**關鍵不變式(修正後,為真)**:一個 stranger-proposed alias **永遠不會**出現在 `alias_provider()` 的 frozenset 裡——因為**陌生回合根本不存在 `LearnName` 這個工具**,寫入面在結構上不存在,不是靠一道 runtime gate 判斷。這比原設計的「擋」嚴格更強(移除 vs 攔截)。

**殘留風險 / 誠實邊界**(CLAUDE.md「State boundaries clearly」):
- owner 自己在可信情境教一個壞字(如把「早安」設成 alias)仍會嘗試——但被 L2 denylist / min-length 擋;denylist 只能擋已知字,擋不了 owner 刻意的自傷(如教一個獨特但常出現的字)。可接受:owner 對自己的 Doll 有權這麼做,L3 word-boundary/min-length 仍縮半徑。
- L3 對 CJK 只能靠 min-length + denylist(無詞界);2 字 CJK alias 仍可能過度命中——D2 需定 CJK 門檻與混合 token 歸類。
- **daemon 端 `l0_dm` 對 DM 無條件喚醒**(`attention.py:87-88`,不看 author)——這是 P1c 既有狀態,本 spec 的 owner_guild_only(Part B)在 bridge 端把陌生 DM drop 掉是唯一的閘;若 `owner_guild_only=false`(現 default),陌生 DM 仍能喚醒 Doll。列為 D7。把 daemon `l0_dm` 本身改成 owner-aware 是**另一條軸**(喚醒 vs 轉發),超出本 spec 範圍,此處誠實標記。

### 6.3 選用擴充:若未來要讓 Doll 撿陌生人用的暱稱（D6，正確接線）

base design 刻意**不**讓陌生人的字有任何寫入面。若日後 user 要這功能(社群裡大家都叫某暱稱、多-doll 分享),**不可**把失效的舊塔接回去,必須這樣接:
1. **stranger→pending 的寫入發生在真實 `external_public` 回合**(把 `LearnName op=add` 加進 `EXTERNAL_TOOLS`,在 external_public 回合落 `state=pending`、`origin="stranger"`)——這樣 `origin_tier` 在呼叫當下是真的,不經反思洗白。
2. **pending→active 的升級必須有 owner 在迴路**:只認 **owner 在 owner DM(`external_dm`)的顯式核可**,不是 Doll 反思自採。做法:把 pending alias 浮現給 owner(`[有人這樣叫你]` 區塊),owner 一句話核可才升級。
3. **浮現/surfaced-gate 是承重牆,必做**:對照 `SelfRevision` 的 `evolution_candidate_surfaced` 閘(`tools.py:903`「這個候選這一輪還沒呈現給妳看」就拒 adopt)——沒有這個閘,會 adopt 一個這回合根本沒看到的 pending。
這條路徑是**額外**的攻擊面,故列為 open decision D6 由 user 決定要不要開,而非 base 預設。

**owner_guild_only 的安全面**:`owner_guild_only=true` 額外把「Doll 只出現在 owner 的圈子」變成可設定邊界——陌生 DM 與陌生 guild 直接在 bridge drop(仍全量 ambient log,稽核不損)。與 alias 中毒防護正交但互補:即使某個(D6 選用)pending alias 存在,`owner_guild_only=true` 也不會讓陌生 guild 的訊息進到 daemon 讓它有機會被比對。

---

## 7. Open Decisions（需 user 拍板）

> **✅ 定案 2026-07-06（user「LGTM」批准,全採建議值）:D1=(b) pack `[meta] aliases` 多正名 seed;D2=ASCII `\b` 界 / CJK≥2 / 混合含 ASCII 詞走 `\b` 界 / 硬編小 denylist(`你 妳 hey hi hello the everyone all 大家 各位` …)+ config 可加;D3=平行 `aliases_history.jsonl`;D4=移除 `always_wake_channels`;D5=(ii) 選用 `backfill_channels` 與 wake 解耦;D6=base 不開陌生暱稱學習(A4 不做);D7=(a) `owner_guild_only` 預設 `true`。以下為原始決策紀錄。**

- **D1 — pack 多正名? + 中文正名遷移(併入 M6)** 目前 `PackMeta.name` 是單一字串(`character.py:28`)。要不要讓 pack 宣告多個 seed alias(如 `Gura` + `古拉` + `小鯊`)當永久 floor?選項:(a) 維持單 `name`,其餘全靠自學;(b) `doll.toml [meta]` 加選用 `aliases = [...]` 當永久 seed。**建議 (b)**——`古拉` 這種明顯正名不該還要「學」。**遷移注意(M6)**:現 bridge `name_aliases=["Gura","gura","古拉"]` 已是死 config(daemon `attention.name_aliases=[]`,`l0_name` 從不觸發),移除它**無 live 損失**;但「古拉」不是 pack `meta.name` → cold-start 不會醒。A5 清理時應把中文正名 port 進 pack seed(D1b)或 daemon `[attention]` floor,別讓中文名喚醒被無聲丟掉。**注意 D1(b) 的 seed 也過 §3.4 guard**:一個分享/惡意 pack 塞 `aliases=["hey","你好"]` 會被 load-time guard warn 並剔除,不會重開亂回。
- **D2 — L0 比對規則 + guard 門檻**:ASCII word-boundary 定案?CJK min-length 取 2 還是 3?**混合 token(`Gura醬`)歸 ASCII 界規則還是 CJK 規則?**(M3——動手前必須釘死,否則灰區)。denylist 初始內容(代名詞/招呼語)由誰維護——硬編一小組 + 選用 config 擴充?**建議**:ASCII `\b` 界、CJK ≥2、混合 token 走「含任一 ASCII 詞則用 `\b` 界」規則、硬編小 denylist(`你/妳/hey/hi/hello/the/everyone/all/大家/各位` 等)+ config 可加。
- **D3 — provenance history 檔**:alias 事件寫進既有 `self_history.jsonl`(與 PinSelf 同檔,好處:單一稽核源)還是平行 `aliases_history.jsonl`(好處:不污染 self-evolution 稽核)?**建議**:平行檔,alias 與人格演化是不同 concern。
- **D4 — bridge `always_wake_channels`**:一併移除(forward-all 後 bridge 端已死)還是保留?**建議**:一併移除,與 `name_aliases` 同批清。
- **D5 — backfill scope(不只成本,I2 加重)**:`owner_guild_channels` 列舉會(a) 在**每次 reconnect**(~5s 迴圈)跑 N×M 個 `fetch_history` REST call、(b) 改變**哪些 channel 的歷史被 replay 進 daemon**(語意變化,非純成本)。選項:(i) 先接受(dogfood 規模小);(ii) 保留一個選用 `backfill_channels`(純供 backfill,與 owner_guild wake **解耦**)恢復原 allowlist 對 backfill scope 的框限;(iii) 對列舉設上界。**建議 (ii)**——scope 與 wake 是兩件事,解耦最乾淨且擋掉 reconnect 迴圈放大。
- **D6 — 要不要讓 Doll 撿陌生人用的暱稱?(新)** base design **不**開這功能(陌生寫入面歸零)。若要開,必須用 §6.3 的正確接線:stranger→pending 在**真實 external_public 回合**寫入 + pending→active **只認 owner 在 owner DM 的顯式核可** + 強制 surfaced-gate(仿 `evolution_candidate_surfaced` `tools.py:903`)。這是**額外攻擊面**。**建議**:base 不開;真有社群暱稱需求再開,且必照 §6.3 接線,不得把失效舊塔接回。
- **D7 — `owner_guild_only` 預設 + daemon `l0_dm` 無條件喚醒(新,I2)**:現 default `false`(維持現狀)代表**任何陌生人 DM 這個 bot 就能喚醒 Doll**(`attention.py:87-88` `l0_dm` 不看 author)。這是 P1c 既有狀態,但本 spec 引入了修它的開關卻不預設打開。選項:(a) 預設 `owner_guild_only=true`(安全 default,但改變現狀);(b) 維持 `false` 但在文件大聲標記風險;(c) 另做 daemon 端 `l0_dm` owner-aware(喚醒與轉發不同軸,超出本 spec)。**建議 (a)**——這份 spec 的整個目的就是 attention/poisoning 邊界,不該出貨一個不安全 default。至少 (b) 要明講。

---

## 8. Task Decomposition（每個 single-concept,供後續 SDD）

每個 plan 一個新概念(CLAUDE.md「每個 plan 只加一個新概念」);Part A 與 Part B 獨立,可並行。

**Part A**
- **A1 — `mind/name_aliases.py` store module**:純 read-modify-write `name_aliases.json`,base API **`add/remove`** + id + provenance,永不 raise(`adopt` 僅 D6 選用擴充才加)。標準庫 module,單元測試齊(仿 `self_profile.py` 的測試組)。**無** wiring。C1 不影響此 task——store 本身與 origin 無關。
- **A2 — `LearnName` 工具 + owner-context registry gating + 機械 guard(承重 security piece)**:`tools.py` 新 pydantic tool(`op: add/remove`);`mind_loop._active_tool_registry`(`:778-820`)加 branch,**只在 `external_dm` 或帶 `UserSpoke` 的 `internal` 回合**入工具集,`external_public`/純反思回合**不提供**;成功寫入恆 `state=active`/`origin=owner`;寫入時 L2 guard(min length/denylist)。**取代**原 A2 的 origin_tier active/pending 路由(C1 證實失效)。單元測試需覆蓋:external_public 回合工具不存在、純反思回合工具不存在、owner DM 回合可寫、guard 擋短/denylist。
- **A3 — AttentionGate alias provider + L0 match 硬化 + seed guard**:`attention.py` 改 `alias_provider` 注入(fail-closed on `stat()`,L5)+ word-boundary/CJK min-length 比對(L3,seed/config/learned **一律**適用);`kernel.py` 建 union+mtime-cache 閉包(pack seed ∪ config floor ∪ active learned),**load-time guard warn 並剔除 denylisted/過短 seed**(I3);更新 `tests/test_attention*.py`(改傳 `alias_provider=lambda: frozenset({...})`)。
- **~~A4~~ — 移出 base**:原 pending alias 浮現 `[有人這樣叫你]` + `op=adopt`。base design 無 pending → **A4 的 candidate set 恆空、adopt 無物可批**(review I3),故**不在 base 計畫**。僅當 D6 選擇開啟陌生暱稱學習時才需要,且屆時它是**承重必做**(仿 `evolution_candidate_surfaced` `tools.py:903` 的 surfaced-gate,M4),不是可選收尾。
- **A5 — bridge alias config 清理 + 中文正名遷移**:移除 bridge `name_aliases`(+ D4 定案的 `always_wake_channels`)於 `controller.py`/`__main__.py`/`bridge.toml`/`bridge.example.toml`;把中文正名 port 進 pack seed(D1b)或 daemon `[attention]` floor(M6,別讓「古拉」喚醒無聲消失);更新 daemon `[attention]` 文件註記。

**Part B**
- **B1 — `is_owner_in_guild` Protocol method + Fake**:`client.py` Protocol + `PycordClient` REST 實作,**依 §4.2 表處理全部失敗模式**(`NotFound`→False、`get_guild` None→fail-closed、transient→cache-or-drop),**呼叫端包 try 不讓例外逃**。+ 兩個測試 Fake(含模擬 `NotFound`/transient/`None`)。**無** gate wiring。
- **B2 — `owner_guild_only` gate + caching + config-load guard**:`config`/`BridgeConfig`/`_load_bridge_config` 加開關 + **`owner_guild_only=true` 且 `owner_id` 空 → 拒啟動**(I1);`_capture_and_forward` 在 self-filter 後加 gate(DM 短路 + guild **短 TTL** cache,fail-closed);forward-all 路徑 default 依 D7 定案(建議 `true`,若維持 `false` 需文件標記)。
- **B3 — `channel_allowlist` 移除 + backfill 改寫(依 D5 定案)**:移除欄位/seed/pre-register;新 `owner_guild_channels` Protocol method + Fake;`__main__` backfill 改用列舉——**D5 若選 (ii) 則加選用 `backfill_channels` 與 wake 解耦**,避免 reconnect 迴圈放大(I2)。

**建議順序**:A1→A2→A3(A3 是 A 的收口,讓自學真的餵回 L0);**A2 是 security 承重,接錯(如誤用 origin_tier 路由)整個 Part A 無效——先確認 registry gating 對**。base **不含 A4**(僅 D6 開啟才做)。B1→B2、B1→B3 並行,B3 先折入 D5 定案。A、B 兩 track 互不依賴,依 CLAUDE.md 各開 worktree。

---

## 9. 驗證（後續 SDD 用）

- 單元:name_aliases store(A1)、LearnName **registry gating**+guard(A2:external_public/純反思回合工具不存在、owner DM 可寫、guard 擋短/denylist)、AttentionGate provider+match 硬化+seed guard+stat fail-closed(A3)、`is_owner_in_guild` 全失敗模式 + owner_guild gate + backfill 列舉(B1/B2/B3),皆走 Fake,不碰 py-cord/網路。
- Live-smoke(人工,對照 `docs/dollosctl-smoke.md`):
  1. cold-start:全新 data,叫 pack 正名 → 醒(seed floor 生效)。denylisted/過短的 pack 正名 → log warn + **不**因此亂醒。
  2. owner DM(或本機 chat)教暱稱 → 之後只用該暱稱叫 → 醒(active learned)。
  3. 陌生公開頻道宣稱「大家都是你的名字」→ **不醒**;且該字**沒進** `alias_provider()`;確認**該回合 Doll 的工具集裡根本沒有 `LearnName`**(結構性,非事後擋)。
  4. (僅 D6 開啟才測)陌生 pending + owner DM 顯式核可 → 升級生效;未核可 → 不生效;未 surfaced 的候選 adopt → 拒。
  5. `owner_guild_only=true`:陌生 guild 訊息不轉、陌生 DM 不轉、owner DM 轉、owner guild 轉;`get_guild` 回 None / transient error → **fail-closed drop**(不 leak、不 crash);reconnect backfill 只掃 owner guild channel(或 D5 的 `backfill_channels`)。`owner_id` 空 + `owner_guild_only=true` → **拒啟動**。
