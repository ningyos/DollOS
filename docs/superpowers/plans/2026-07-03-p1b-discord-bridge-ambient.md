# P1b discord-bridge + ambient log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A `discord-bridge` process that connects to the daemon as a WS client, forwards every allowlisted-channel message into the daemon (full-capture ambient log), routes wake-worthy ones as `ChannelMessage` perceptions, and delivers her `AddressedText` replies back to the right Discord channel.

**Architecture:** `src/dollos/discord_bridge/` mirrors `src/dollos/voice/bridge/` (separate process, argparse `--daemon ws://…`, websockets client). Discord I/O sits behind a mockable `DiscordClient` protocol so all bridge logic is unit-testable without py-cord/real Discord (real Discord is the live smoke). Daemon side: kernel `_handle_message` gains `ChannelRegister` (dual-register into ChannelRegistry + SinkResolver — carry-note I-1) and `ChannelEvent` (→ `ChannelMessage` perception) branches, mirroring the existing `TextInput`→`UserSpoke` path but WITHOUT cancel/preempt for strangers. Consumes P1a's ChannelRegistry / SinkResolver locus / AddressedText / BatchAccumulator.

**Tech Stack:** Python 3.12, py-cord (NEW dep), websockets, asyncio, pytest. Baseline: 1125 tests green on main.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-dollos-mvp-discord-presence-design.md` §3.2/§3.3 + §3.1 (backbone, merged). R2 scope findings (`2026-07-03-mvp-r2-findings.md`): Discord 429 rate-limit handling, reconnect gap msg_id dedup.
- **P1a carry-notes (binding):** I-1 register external channel in BOTH `ChannelRegistry` AND `SinkResolver` atomically on ChannelRegister, unregister from both on disconnect. I-2 cross-bucket at-least-once → **msg_id idempotency** required (a replayed bucket must not re-send a Discord reply / re-log an ambient line). M-2 reconcile `AddressedText` (P1a omitted spec's `target:{guild,channel}` — bridge derives target from channel_id, keep as-is + note). M-3 harden BatchAccumulator timer if wired here.
- **Self-filter (spec §3.3 C3):** the bot's OWN messages (`author_id == bot.user.id`) write to ambient log but are HARD-filtered before L0/L1/L2 — never a perception (prevents day-1 self-echo loop).
- **Stranger ≠ UserSpoke:** `ChannelEvent` → `ChannelMessage` perception. Do NOT cancel consolidation/evolution or preempt for strangers. ONLY `author_is_owner` (owner_discord_id match, numeric author_id) gets the TextInput-style cancel+preempt.
- **external_ctx / conservative toolset / L1 attention are NOT P1b** (external_ctx=P1e, attention L1/L2=P1c). P1b ships L0 hard rules only (DM / mention / name_aliases / always_wake_channels → wake); everything else → ambient-log-only. ChannelMessage kind IS added here (P1e/P1c build on it).
- **py-cord behind a protocol.** No test imports py-cord; the real client is a thin adapter. `pyproject.toml` adds `py-cord` as a dep but tests use `FakeDiscordClient`.
- No-fallback / friendly-error house rules. Existing 1125 tests stay green.
- **Worktree:** `.worktrees/p1b-discord/`, branch `p1b-discord`. Commit-check: `git branch --show-current`==`p1b-discord`.

---

### Task 1: `ChannelMessage` perception kind

**Files:**
- Modify: `src/dollos/mind/mind_state.py` (the `Perception.kind` Literal ~line 74)
- Modify: `src/dollos/mind/mind_prompt.py` (`_percep_body` — render a ChannelMessage into her prompt)
- Test: `tests/test_channel_message_perception.py`

**Interfaces:**
- Produces: `Perception(kind="ChannelMessage", data={channel_id, guild, channel, author, author_id, content, mentioned, is_dm, msg_id, author_is_owner})` is a valid perception; `_percep_body` renders it as a readable line (P1b: minimal neutral rendering; §3.5 situated rendering is P1d).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_channel_message_perception.py
"""ChannelMessage perception kind + minimal rendering (spec §3.2)."""
import time

from dollos.mind.mind_state import Perception
from dollos.mind.mind_prompt import _percep_body


def _cm(**data):
    base = dict(channel_id="disc:g1:c1", guild="g1", channel="general",
                author="stranger", author_id="42", content="hello there",
                mentioned=False, is_dm=False, msg_id="m1", author_is_owner=False)
    base.update(data)
    return Perception(kind="ChannelMessage", t=time.time(), data=base)


def test_channel_message_is_valid_kind():
    p = _cm()
    assert p.kind == "ChannelMessage"          # no Literal ValidationError


def test_percep_body_renders_content_and_author():
    body = _percep_body(_cm(author="alice", content="ping"))
    assert "alice" in body and "ping" in body and "general" in body


def test_percep_body_dm_marks_dm():
    body = _percep_body(_cm(is_dm=True, channel="DM", content="hi"))
    assert "私訊" in body or "DM" in body
```

- [ ] **Step 2: Run** → FAIL (Literal ValidationError / `_percep_body` KeyError).

- [ ] **Step 3: Implement**
  - `mind_state.py`: add `"ChannelMessage"` to the `Perception.kind` `Literal[...]`.
  - `mind_prompt.py` `_percep_body`: add a branch for `kind == "ChannelMessage"`. Read the existing branches first (`grep -n "kind ==" src/dollos/mind/mind_prompt.py`) and match their return style. Minimal rendering:
    ```python
    if p.kind == "ChannelMessage":
        d = p.data
        where = "私訊" if d.get("is_dm") else f"#{d.get('channel','?')}"
        return f"[{where}] {d.get('author','?')}:{d.get('content','')}"
    ```
  Place it consistently with the other kind branches.

- [ ] **Step 4: Run** → 3 PASS; `uv run pytest tests/test_mind_prompt.py -q` (or the mind_prompt test file) green.
- [ ] **Step 5: Commit** — `feat(mind): ChannelMessage perception kind + minimal render (P1b §3.2)`

---

### Task 2: kernel dispatch — ChannelRegister (dual-register) + ChannelEvent → perception

**Files:**
- Modify: `src/dollos/kernel.py` (`_handle_message` ~line 447; ctor `ChannelRegistry` already built in P1a at :257)
- Test: `tests/test_kernel_channel_dispatch.py`

**Interfaces:**
- Consumes: `ChannelRegister(channel_id, locus, kind)` + `ChannelEvent(channel_id, payload: dict)` (P1a wire-schema); `ChannelRegistry.register`, `SinkResolver.register(sink, locus=, channel_id=)` (P1a).
- Produces: on `ChannelRegister` → `ChannelRegistry.register(channel_id, locus, kind)` AND (if external) re-register the connection's sink with `locus="external", channel_id=…` (carry I-1); on `ChannelEvent` → build `ChannelMessage` perception from payload + queue it; owner (payload `author_is_owner`) additionally does the TextInput-style `_cancel_consolidation()/_cancel_evolution()/_maybe_preempt_for_new_input(sink)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_kernel_channel_dispatch.py
"""kernel ChannelRegister/ChannelEvent dispatch (spec §3.2 + carry I-1)."""
import asyncio

import pytest

# Locate the kernel test harness that constructs a DollOS with a fake queue +
# calls _handle_message directly: `grep -rn "_handle_message\|def _kernel\|DollOS(" tests/`.
# Copy that harness. Assert:
#  (a) ChannelRegister(external) → ChannelRegistry.get(cid).locus == "external"
#      AND the sink is resolvable by origin (SinkResolver(cid) returns that sink).
#  (b) ChannelEvent(stranger) → a ChannelMessage perception is queued AND
#      consolidation/evolution are NOT cancelled (spy the cancel methods).
#  (c) ChannelEvent(author_is_owner=True) → perception queued AND cancel fired.
# Stub the cancel methods with a recorder; use a real PerceptionQueue.
```

Note: the exact harness is project-specific — read `tests/test_kernel.py` for the DollOS construction + `_handle_message` invocation pattern and copy it. The three behavioral assertions above are the contract to pin.

- [ ] **Step 2: Run** → FAIL (`ChannelRegister` hits the `else` no-op branch; no perception queued).

- [ ] **Step 3: Implement** — in `_handle_message`, before the `else`:

```python
        elif isinstance(msg, ChannelRegister):
            self._channel_registry.register(msg.channel_id, locus=msg.locus, kind=msg.kind)
            if msg.locus == "external":
                # carry I-1: the connection's sink must be addressable by this
                # channel_id, registered atomically with the registry entry.
                self._register_external_sink(sink, msg.channel_id)
        elif isinstance(msg, ChannelEvent):
            d = msg.payload
            if d.get("author_is_owner"):
                # owner speaking from Discord = TextInput-equivalent cancel/preempt
                await self._maybe_preempt_for_new_input(sink)
                self._cancel_consolidation()
                self._cancel_evolution()
            self._perception_queue.put(Perception(
                kind="ChannelMessage", t=time.time(),
                data={"channel_id": msg.channel_id, **d}))
```

Add helper `_register_external_sink(self, sink, channel_id)`: re-register the sink with locus/channel and track the handle for disconnect cleanup. **Carry I-1 disconnect:** in the existing `_on_disconnect` (kernel.py ~:593), also `self._channel_registry.unregister(channel_id)` for any channels this sink registered — track `sink → set[channel_id]` in a dict alongside the existing sink-handle map (kernel.py:414). Import `ChannelRegister, ChannelEvent` from `dollos.ipc.messages`.

- [ ] **Step 4: Run** → 3 PASS; full suite green.
- [ ] **Step 5: Commit** — `feat(kernel): ChannelRegister dual-register + ChannelEvent→perception (P1b I-1)`

---

### Task 3: ambient log (full capture + self-filter + msg_id dedup + retention)

**Files:**
- Create: `src/dollos/discord_bridge/ambient_log.py`
- Test: `tests/test_ambient_log.py`

**Interfaces:**
- Produces: `AmbientLog(root: Path, retention_days: int)` with `append(guild_id, channel_id, event: dict) -> bool` (returns False if `event["msg_id"]` already logged for that channel/date — dedup for reconnect backfill, carry I-2) and `prune() -> None` (delete files older than retention_days). Path: `{root}/discord/{guild_id}/{channel_id}/{date}.jsonl`. Pure file I/O, no Discord.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ambient_log.py
"""AmbientLog — full capture + msg_id dedup + retention (spec §3.3 + carry I-2)."""
import json

from dollos.discord_bridge.ambient_log import AmbientLog


def test_append_writes_jsonl(tmp_path):
    log = AmbientLog(tmp_path, retention_days=30)
    assert log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is True
    p = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    assert json.loads(p.read_text().splitlines()[0])["msg_id"] == "m1"


def test_dedup_same_msg_id(tmp_path):
    log = AmbientLog(tmp_path, retention_days=30)
    log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"})
    assert log.append("g1", "c1", {"msg_id": "m1", "content": "hi", "date": "2026-07-03"}) is False
    p = tmp_path / "discord" / "g1" / "c1" / "2026-07-03.jsonl"
    assert len(p.read_text().splitlines()) == 1     # not duplicated


def test_prune_deletes_old(tmp_path):
    log = AmbientLog(tmp_path, retention_days=1)
    old = tmp_path / "discord" / "g1" / "c1" / "2020-01-01.jsonl"
    old.parent.mkdir(parents=True); old.write_text('{"msg_id":"x"}\n')
    log.prune()
    assert not old.exists()
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — `AmbientLog` with a per-(channel,date) in-memory `set` of seen msg_ids (loaded lazily from the file on first append to that date, so restart-safe dedup); `append` skips + returns False on a seen msg_id; `prune` walks `discord/*/*/*.jsonl`, parses the date stem, unlinks if older than `retention_days` from a `today` param (pass today in — do NOT call `date.today()` inside, keep it testable: `prune(today: str)` or accept an injected clock). Retention bound = the §5 S10 requirement.

- [ ] **Step 4: Run** → 3 PASS.
- [ ] **Step 5: Commit** — `feat(discord): AmbientLog — capture + msg_id dedup + retention (P1b §3.3)`

---

### Task 4: `DiscordClient` protocol + `FakeDiscordClient` + L0 wake rules + self-filter

**Files:**
- Create: `src/dollos/discord_bridge/client.py` (protocol + a real py-cord adapter stub), `src/dollos/discord_bridge/wake.py` (L0 rules + self-filter)
- Test: `tests/test_discord_wake.py`

**Interfaces:**
- Produces: `DiscordClient` Protocol (`on_message(cb)`, `send(channel_id, text)`, `me_id() -> str`, `run()`); `l0_wake(event: dict, *, bot_id, owner_id, name_aliases, always_wake_channels) -> bool` — returns False (drop from wake) if `author_id == bot_id` (self-filter, but caller still logs it); True if DM / bot mentioned / any name_alias substring in content / channel in always_wake; else False. Pure function.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_discord_wake.py
"""L0 wake rules + self-filter (spec §3.3/§3.4 L0 + C3)."""
from dollos.discord_bridge.wake import l0_wake


def _e(**kw):
    base = dict(author_id="42", is_dm=False, mentioned=False,
                content="just chatting", channel_id="c1")
    base.update(kw); return base


def _cfg(**kw):
    base = dict(bot_id="bot", owner_id="owner", name_aliases=["gura", "古拉"],
                always_wake_channels=set())
    base.update(kw); return base


def test_self_message_never_wakes():
    assert l0_wake(_e(author_id="bot", content="gura here"), **_cfg()) is False


def test_dm_wakes():
    assert l0_wake(_e(is_dm=True), **_cfg()) is True


def test_mention_wakes():
    assert l0_wake(_e(mentioned=True), **_cfg()) is True


def test_name_alias_substring_wakes():
    assert l0_wake(_e(content="hey 古拉 look"), **_cfg()) is True


def test_unrelated_public_chatter_does_not_wake():
    assert l0_wake(_e(content="anyone up for a game"), **_cfg()) is False


def test_always_wake_channel():
    assert l0_wake(_e(channel_id="vip"), **_cfg(always_wake_channels={"vip"})) is True
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — `wake.py` `l0_wake` per the rules (self-filter first → False; then DM/mention/alias/always → True; else False). `client.py`: a `DiscordClient` Protocol and a `PycordClient` adapter that imports py-cord lazily inside `run()` (so importing the module doesn't require py-cord — tests never call run()). Add `py-cord` to `pyproject.toml` dependencies.

- [ ] **Step 4: Run** → 6 PASS. `uv run pytest -q` full suite (no py-cord import at collection since it's lazy) green.
- [ ] **Step 5: Commit** — `feat(discord): DiscordClient protocol + L0 wake + self-filter (P1b §3.4 L0/C3)`

---

### Task 5: bridge controller — wire client↔daemon↔ambient (fake-Discord integration)

**Files:**
- Create: `src/dollos/discord_bridge/controller.py`, `src/dollos/discord_bridge/__main__.py`
- Test: `tests/test_discord_bridge_controller.py`

**Interfaces:**
- Consumes: everything above + AddressedText/ChannelEvent/ChannelRegister (P1a schema) + AmbientLog + l0_wake + BatchAccumulator (P1a).
- Produces: `BridgeController(discord: DiscordClient, daemon_send: Callable, ambient: AmbientLog, cfg)` with `async def on_discord_message(event)` (→ ALWAYS ambient.append; if l0_wake → daemon_send ChannelEvent) and `async def on_daemon_message(msg)` (AddressedText → discord.send(channel_id, text)). `__main__.py` mirrors voice bridge (argparse `--daemon`, websockets client, ChannelRegister on connect for each allowlisted channel).

- [ ] **Step 1: Write the failing test** — an end-to-end with `FakeDiscordClient` + a fake daemon socket:

```python
# tests/test_discord_bridge_controller.py
"""Bridge controller: message in → ambient+ChannelEvent; AddressedText → send."""
import pytest

from dollos.discord_bridge.controller import BridgeController
# Build with a FakeDiscordClient (records send()), a fake daemon_send (records
# ChannelEvent), a tmp AmbientLog, cfg with name_aliases. Assert:
#  (a) a stranger unrelated message → ambient.append called, NO ChannelEvent.
#  (b) a message mentioning her → ambient.append AND a ChannelEvent(payload
#      carries author_is_owner correctly derived from owner_id) sent to daemon.
#  (c) an AddressedText(channel_id, text) from daemon → discord.send(channel_id, text).
#  (d) her OWN message (author_id==bot_id) → ambient.append but NO ChannelEvent.
```

Reuse the FakeDiscordClient from Task 4's tests (or lift it into a shared conftest). The four assertions are the contract.

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError` / missing methods).

- [ ] **Step 3: Implement** `controller.py` (the two handlers + author_is_owner derivation `author_id == cfg.owner_id`) and `__main__.py` (argparse + websockets connect loop + ChannelRegister-per-channel on connect + reconnect with backfill that dedups via AmbientLog.append's False return — carry I-2). Keep `__main__` thin; logic in controller.

- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** — `feat(discord): BridgeController + __main__ — end-to-end wire (P1b)`

---

### Task 6: reconnect backfill dedup + 429 handling + integration

**Files:**
- Modify: `src/dollos/discord_bridge/controller.py` (backfill dedup + send retry on 429)
- Test: extend `tests/test_discord_bridge_controller.py`

**Interfaces:** consumes Task 5; adds `async def backfill(self, channel_id, recent_events)` (each through ambient.append; only non-dup ones that pass l0_wake become ChannelEvents) and 429-aware send (FakeDiscordClient can raise a `RateLimited` once then succeed).

- [ ] **Step 1: Tests** — (a) backfill of events where some msg_ids already logged → only new ones append + only new+wake-worthy become ChannelEvents (no double-send of a reply, no dup ambient line — carry I-2); (b) `discord.send` raising `RateLimited` once → controller retries after the indicated delay and succeeds (stub the sleep).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — backfill loops events through `ambient.append` (False = already seen, skip entirely); 429: catch a `RateLimited(retry_after)` from `discord.send`, `await asyncio.sleep(retry_after)` (injectable clock in tests), retry once. R2 scope-lens findings closed.
- [ ] **Step 4: Run** → PASS; **full suite** `uv run pytest -q` green (1125 + new).
- [ ] **Step 5: Commit** — `feat(discord): reconnect backfill dedup + 429 retry (P1b I-2 / R2)`

---

## Completion

After Task 6: full suite green. **Live smoke (P1b gate — first real Discord test,軟機制必 live smoke):** run discord-bridge against a real test server (bot token in local config), verify: (a) a message mentioning her → she receives it and replies to the right channel; (b) an unrelated message → ambient-logged, no perception (check trace/logs); (c) her own reply does NOT loop (self-filter); (d) reconnect after a kill → backfill dedups (no double-reply). This is the first time she's actually ON Discord — but attention is still L0-only (P1c refines), external_ctx/safety is P1e (do NOT expose her to a truly hostile server until P1e lands — use a private test server). Merge via `superpowers:finishing-a-development-branch`.

**Deferred to later plans (carry forward):** L1/L2 attention + engagement session (P1c); external_ctx + conservative toolset + memory scope (P1e — REQUIRED before any stranger-facing server); situated rendering + DiscordLookup (P1d); trace per-origin (P1f, ideally before heavy dogfood). **Do NOT deploy to a public/stranger server until P1e.**
