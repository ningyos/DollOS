# Part B — `owner_guild_only` Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use `- [ ]` syntax.

**Goal:** discord-bridge 加 `[discord] owner_guild_only` 開關取代 `channel_allowlist`:`true`(**預設**,D7)= 只轉發「owner 是成員的 guild」的訊息(+ owner DM 一律轉、陌生 DM drop);`false` = forward-all(現狀)。owner-guild 偵測用 `fetch_member`(**不需新 intent**),全失敗模式 **fail-closed**(查不到就不轉、不 crash)。

**Architecture:** 新 `DiscordClient` Protocol method `is_owner_in_guild`(`bot.get_guild` + REST `fetch_member`,catch `NotFound`→False、`get_guild` None→fail-closed);`_capture_and_forward` 在 self-filter 後加 gate(DM 短路 + per-guild **短 TTL** cache);移除 `channel_allowlist`,backfill 改用新 `owner_guild_channels` 列舉,並加**選用** `backfill_channels`(D5:與 wake 解耦,擋 reconnect 迴圈 REST 放大)。依據 spec `docs/superpowers/specs/2026-07-06-self-learned-aliases-owner-guild-design.md` §4/§8 Part B(D 值定案 §7)。

**Tech Stack:** Python 3.13、py-cord(behind mockable `DiscordClient` Protocol,lazy import)、既有 bridge。無新第三方依賴、**無新 Discord intent**。

## Global Constraints

- **No fallback / fail-closed**:owner-guild 偵測任何不確定(guild 未 cache、member fetch transient error、owner 中途離開)→ **不轉發**(寧漏轉不誤轉);`is_owner_in_guild` 例外**呼叫端包 try 不讓逃**。ambient log **無條件**寫(語料完整不受 gate 影響)。
- **不需新 intent**:`fetch_member` 是 REST,現有 `Intents.default()+message_content` 就夠;**不**改用需 privileged Server Members Intent 的 `get_member`-only 路徑。
- **config-load guard(spec I1)**:`owner_guild_only=true` 且 `owner_id` 空 → **拒啟動**(明確錯誤,非靜默)。
- **DM 短路**:owner DM(`author_is_owner` 且 `is_dm`)**一律轉**;陌生 DM 在 `owner_guild_only=true` 時 **drop**(guild 概念不適用 DM,陌生人私訊不算 owner context)。
- **D 值(定案)**:D4=一併移除 bridge `always_wake_channels`;D5=(ii) 選用 `backfill_channels`(純供 backfill,與 owner_guild wake 解耦);D7=(a) `owner_guild_only` 預設 `true`。
- **測試走 Fake**:`DiscordClient` Protocol 的 test Fake 要能模擬 `is_owner_in_guild` 的 True/False/`NotFound`/transient/`get_guild`-None,不碰 py-cord/網路。
- **獨立於 Part A**:本 plan 不碰 name_aliases(那是 Part A)。**注意**:Part A 的 A5 也動 bridge config(移除 `name_aliases`);兩 plan 若並行,merge 時留意 `bridge.toml`/`__main__.py` 同檔的 config-schema 改動可能撞——後 merge 者 rebase。

## 範圍界定

**本 plan 只加一個概念:owner_guild_only 轉發 scope 閘。** 蓋 spec §4 全部 + §8 Part B(B1/B2/B3)。name_aliases 自學 = Part A。

---

## File Structure

- **Modify** `src/dollos/discord_bridge/client.py`(`DiscordClient` Protocol + `PycordClient` 新 method)、`src/dollos/discord_bridge/controller.py`(gate + cache)、`src/dollos/discord_bridge/__main__.py`(config 欄位 + backfill 改寫)、`bridge.toml`/`bridge.example.toml`(schema)。
- **Test**: `tests/test_discord_bridge_client.py`(is_owner_in_guild Fake + 失敗模式)、`tests/test_discord_forward_all.py` / 新 `tests/test_owner_guild_gate.py`(gate + DM 短路 + fail-closed)、config-load guard 測試。

---

## Task B1: `is_owner_in_guild` / `owner_guild_channels` Protocol method + Fake

**Files:** Modify `src/dollos/discord_bridge/client.py`;Test `tests/test_discord_bridge_client.py`

**Interfaces:**
- Produces(加進 `DiscordClient` Protocol,`client.py:46-83`):
  - `async def is_owner_in_guild(self, guild_id: str, owner_id: str) -> bool` — `PycordClient` 實作:`bot.get_guild(int(guild_id))` → None 則 **False**(fail-closed);否則 `await guild.fetch_member(int(owner_id))` → 成功 True、`discord.NotFound` → False、其他 transient 例外 → **False**(fail-closed,呼叫端另有 cache）。
  - `async def owner_guild_channels(self, owner_id: str) -> list[str]` — 列舉 owner 所在 guild 的 text channel id(供 backfill;`bot.guilds` 過濾 `is_owner_in_guild`,收集 `guild.text_channels`)。
- 兩個 test Fake(既有 `FakeDiscordClient` 擴充或新增):可設定每 (guild,owner) 的回傳 = True/False/raise NotFound/raise transient/get_guild-None。

- [ ] **Step 1: 失敗測試** — `tests/test_discord_bridge_client.py`:Fake `is_owner_in_guild` 回 True/False;模擬 `NotFound`→False;模擬 transient 例外 → False(不 raise);`get_guild` None → False。`owner_guild_channels` 回 owner-guild 的 channel 列表。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — Protocol 加兩 method;`PycordClient` REST 實作(lazy import discord,catch `discord.NotFound` + 泛 `Exception` → False + log);Fake 擴充。**無** gate wiring(B2 才接)。
- [ ] **Step 4-5: 跑綠 + 全套回歸**
- [ ] **Step 6: Commit** `feat(bridge): is_owner_in_guild / owner_guild_channels protocol method + fakes, all fail-closed (Part B / B1)` + trailers。

---

## Task B2: `owner_guild_only` gate + caching + config-load guard

**Files:** Modify `src/dollos/discord_bridge/controller.py`、`src/dollos/discord_bridge/__main__.py`;Test `tests/test_owner_guild_gate.py`

**Interfaces:**
- Consumes: B1 `is_owner_in_guild`。
- Produces:
  - `BridgeConfig` 加 `owner_guild_only: bool = True`(D7 預設);`_load_bridge_config` 讀 `d.get("owner_guild_only", True)`,且 **`owner_guild_only=True` 且 `owner_id` 空 → raise**(拒啟動,I1)。
  - `_capture_and_forward`(`controller.py`)在 self-filter(`author_id==bot_id`)**之後**、`_daemon_send` **之前**加 gate:
    ```
    if cfg.owner_guild_only:
        if event["is_dm"]:
            if not event.get("author_is_owner"): return   # 陌生 DM drop
            # owner DM 一律放行
        else:
            guild = event.get("guild")
            if guild is None: return
            if not await self._owner_in_guild_cached(guild): return  # fail-closed
    ```
    ambient.append 已在 gate **之前**(無條件,不受影響)。
  - `_owner_in_guild_cached(guild_id)` — per-guild **短 TTL** cache(dict[guild_id]→(bool, expiry));過期或未 cache → 呼 `is_owner_in_guild`(**try 包**,例外→False fail-closed)→ 寫 cache。TTL 短(如 300s)以撐 owner 中途離開/加入。

- [ ] **Step 1: 失敗測試** — `tests/test_owner_guild_gate.py`(Fake client):
  - `owner_guild_only=true`:owner-guild 訊息 → 轉;非-owner-guild → **不轉**;owner DM → 轉;陌生 DM → **不轉**;`is_owner_in_guild` raise → **不轉**(fail-closed);ambient.append 一律被呼叫(即使不轉)。
  - `owner_guild_only=false`:全轉(= 現狀 forward-all)。
  - cache:同 guild 短時間內第二則不重複 fetch(呼一次)。
  - config guard:`owner_guild_only=true` + 空 owner_id → `_load_bridge_config` raise。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作**(gate + TTL cache + config 欄位 + load guard)。gate 是 async(fetch_member async)—— 確認 `_capture_and_forward` 已是 async(是)。
- [ ] **Step 4-5: 跑綠 + 全套回歸**(既有 forward-all 測試在 `owner_guild_only=false` 下仍綠;有 default=true 的測試需設 false 或加 owner-guild)
- [ ] **Step 6: Commit** `feat(bridge): owner_guild_only forward gate (fail-closed) + TTL cache + config guard (Part B / B2)` + trailers。

---

## Task B3: `channel_allowlist` 移除 + backfill 改寫（D5 解耦）

**Files:** Modify `src/dollos/discord_bridge/{controller.py,__main__.py}`、`bridge.toml`/`bridge.example.toml`;Test 既有 backfill 測試更新

**背景**:`channel_allowlist` 現只用於 (a) seed `_registered`(reply-routing pre-register)、(b) `__main__` backfill 的 channel 列表。移除欄位;pre-register 靠 register-on-first-forward(P1c 已有);backfill 改用 **選用** `backfill_channels`(D5:與 owner_guild wake 解耦,擋 reconnect ~5s 迴圈的 N×M `fetch_history` REST 放大)。

- [ ] **Step 1: 失敗測試** — bridge config 移除 `channel_allowlist` 後 `_load_bridge_config` 不炸;`_registered` 初始空(不再 seed);backfill 只掃 `backfill_channels`(若設)否則不掃(或掃 owner_guild_channels——**選 D5(ii):預設不掃,只掃明列的 `backfill_channels`**,最省 + 最可預測);register-on-first-forward 仍讓新頻道註冊(既有行為不破)。
- [ ] **Step 2: 跑確認 fail**
- [ ] **Step 3: 實作** — 移除 `channel_allowlist`(`BridgeConfig`、`_load_bridge_config`、兩 toml);`_registered` 不 seed;`__main__` backfill 改讀 `backfill_channels`(`d.get("backfill_channels", [])`);reconnect_backfill 同法。文件註記「backfill_channels 純供重連補歷史,與 owner_guild wake scope 無關」。
- [ ] **Step 4-5: 跑綠 + 全套回歸**(既有 reconnect/backfill 測試改用 backfill_channels)
- [ ] **Step 6: Commit** `feat(bridge): remove channel_allowlist, decouple backfill via optional backfill_channels (Part B / B3)` + trailers。

---

## Self-Review（對 spec §4/§8 Part B 逐條核）

- [x] B1 `is_owner_in_guild`/`owner_guild_channels` + 全失敗模式 fail-closed + Fake → Task B1
- [x] B2 gate(guild/DM 短路)+ 短 TTL cache + config-load guard(空 owner 拒啟動,I1)→ Task B2
- [x] B3 channel_allowlist 移除 + backfill 解耦(D5 ii,擋 reconnect REST 放大 I2)→ Task B3
- [x] owner_guild_only 預設 true(D7,安全默認)→ Task B2
- [x] 不需新 intent(fetch_member REST)→ Task B1
- [x] ambient log 無條件(不受 gate 影響)→ Task B2

**跨 task:** B2 consume B1;B3 獨立可與 B2 並行但同動 bridge config,序列較穩。**與 Part A 的 A5 同動 bridge config —— 後 merge 者 rebase。**

---

## 執行銜接

`superpowers:subagent-driven-development`,每 task fresh implementer + reviewer(sonnet),whole-branch review 用 **opus**(ultracode:嚴查 fail-closed 每個失敗模式真的不 leak、DM 短路無洞、config guard、cache TTL 不會誤轉過期)。worktree `.worktrees/pb-ownerguild/` on branch `pb-ownerguild`。驗收 = spec §9 的 owner_guild live-smoke。
