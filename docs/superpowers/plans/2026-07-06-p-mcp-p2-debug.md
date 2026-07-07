# DollOS MCP Server — Phase P2 (Debug Mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secret-gated **debug mode** to the `dollos-mcp` connector: a debug connection gets read-only introspection (`get_state` / `get_recent`) and a reliability nudge on `talk`, without ever unlocking Shell or leaking owner-private data.

**Architecture:** P1 (peer talk, merged) already ships the `dollos-mcp` connector (FastMCP + daemon WS client) and the external-safe peer pipeline. P2 adds: (1) a minimal authenticated **IPC query protocol** (`QueryState`/`QueryRecent` → `QueryResult`, each query carrying a REQUIRED daemon-known `query_token`, fail-closed) so the daemon can answer read-only state snapshots; (2) a connector **secret gate** (`debug_secret` in `mcp.toml`) that exposes `get_state`/`get_recent` tools and flags `talk` as `debug_reliable` only for authenticated connections; (3) a daemon-side **reliability nudge** (prompt-level situational hint when `debug_reliable=True`, NOT a tier change); (4) docs. Debug is "read-only observation + reliable conversation", never remote control, never owner.

**Tech Stack:** Python 3.13 / asyncio / pydantic v2 / `mcp` (FastMCP, already a dependency from P1) / pytest. All in `src/dollos/`.

**Spec:** `docs/superpowers/specs/2026-07-06-mcp-server-design.md` — §C.1 (secret gate), §C.2 (reliable nudge), §C.3 (IPC query protocol — the authoritative message shapes + security), §C.4 (v1 scope), §H (open decisions, all resolved). Read the relevant §C section before each task.

## Global Constraints

- **query_token is REQUIRED and fail-closed (R-DECISION-4).** The daemon IPC server has NO connection auth (`server.py:77`), so any local process can open `ws://127.0.0.1:9876` and send a query. Therefore the daemon MUST reject any `QueryState`/`QueryRecent` whose `token` is missing or ≠ `settings.mcp.query_token`, returning `QueryResult(ok=false, payload={})` and logging — WITHOUT executing the query. If `settings.mcp.query_token` is unset/empty, the query surface is ENTIRELY DISABLED (every query → `ok=false`). Authorization-in-the-connector (whether the tool is exposed) is a UX layer, NOT the security boundary.
- **get_recent is tier-scoped to external_public, fail-closed.** It returns ONLY `external_public`-origin recent perceptions. It MUST NOT return owner/internal/external_dm content, private memory, or trace. **`recent_outputs` is EXCLUDED entirely** (its `OutputRecord` has no origin field — `mind_state.py:85-89` — so Doll's replies to the owner cannot be filtered out; excluding is the fail-closed choice; showing outputs would require adding origin to OutputRecord = future).
- **Debug ≠ owner.** A debug connection NEVER sets `author_is_owner=True` and NEVER impersonates the owner's Discord `author_id`. Its `origin_tier` stays `external_public` (no Shell, no owner private memory). Debug adds only introspection + a reply nudge.
- **Reliable response is a SOFT nudge, not a guarantee (R-DECISION-2).** `debug_reliable=True` adds a prompt-level situational hint; it does NOT change `origin_tier` or the tool registry (still `EXTERNAL_TOOLS`, still no Shell). Wording is "strongly nudged / best-effort", never "guaranteed".
- **Read snapshot is synchronous.** The daemon query handler builds the `QueryResult` payload with NO `await` between reading `mood`/`current_self`/`recent_perceptions` and the `put_nowait` — copy first (`list(...)`), so it never observes a half-mutated deque or a mid-ratification `current_self`.
- **debug_secret + query_token are owner-sensitivity secrets** — they live in `mcp.toml` (gitignored, added in P1), never committed.
- **No fallback / degradation** (project hard rule). YAGNI: v1 introspection = `get_state` + `get_recent` only; NO trace dump, NO perception injection, NO triggering reflection, NO write-type introspection.
- **One concept per task, TDD, frequent commits.**

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/dollos/ipc/messages.py` | Modify | Add `QueryState`/`QueryRecent` (ClientMessage) + `QueryResult` (ServerMessage) + unions |
| `src/dollos/config.py` | Modify | `McpConfig` gains `query_token: str \| None = None` |
| `src/dollos/kernel.py` | Modify | `_handle_message` gains 2 read-only query branches (token gate + snapshot + external_public scope) |
| `src/dollos/mind/mind_prompt.py` | Modify | Reliability nudge in the situational render when a perception carries `debug_reliable` |
| `src/dollos/mcp_server/daemon_link.py` | Modify | Query round-trip (send Query*, correlate `query_id`, await `QueryResult`); `debug_reliable` on debug `talk` |
| `src/dollos/mcp_server/__main__.py` | Modify | Secret gate: expose `get_state`/`get_recent` tools + flag debug only for authenticated connections |
| `mcp.example.toml` / `config.example.toml` / `.gitignore` / `CLAUDE.md` / `docs/roadmap.md` / `docs/dollosctl-smoke.md` | Modify | Docs: `debug_secret`/`query_token` fields + arch note + F8 debug checklist |

Tasks map to spec §G phase-P2 Tasks 6→9 (renumbered 1→4 here).

---

### Task 1: IPC query protocol (daemon side) — messages + config + kernel handler

**Files:**
- Modify: `src/dollos/ipc/messages.py` (add `QueryState`/`QueryRecent` near the ClientMessage block ~50-83; `QueryResult` near ServerMessage ~147-160; both unions)
- Modify: `src/dollos/config.py` (`McpConfig` +`query_token`, ~165-188)
- Modify: `src/dollos/kernel.py` (`_handle_message`, after the `ChannelEvent` branch ~776)
- Test: `tests/test_ipc_query_messages.py` (new), `tests/test_config_mcp.py` (extend), `tests/test_kernel_query_handler.py` (new)

**Interfaces:**
- Consumes: `settings.mcp` (`McpConfig`, P1), `self._mind_state` (kernel holds it; `.mood`, `.current_self`, `.recent_perceptions`), the per-connection sink (kernel resolves it in `_handle_message`).
- Produces: `QueryState(type="query_state", query_id: str, token: str)`, `QueryRecent(type="query_recent", query_id: str, token: str, n: int = 20)`, `QueryResult(type="query_result", query_id: str, ok: bool, payload: dict)`; `McpConfig.query_token: str | None`.

- [ ] **Step 1: Write failing tests — message round-trip + config**

`tests/test_ipc_query_messages.py`:
```python
import json
from pydantic import TypeAdapter
from dollos.ipc.messages import (
    QueryState, QueryRecent, QueryResult, ClientMessage, ServerMessage,
    decode_client_message, encode_server_message,
)


def test_query_state_round_trip():
    m = QueryState(query_id="q1", token="s3cr3t")
    back = decode_client_message(m.model_dump_json())
    assert isinstance(back, QueryState) and back.query_id == "q1" and back.token == "s3cr3t"


def test_query_recent_defaults_n_20():
    m = decode_client_message(json.dumps({"type": "query_recent", "query_id": "q2", "token": "s"}))
    assert isinstance(m, QueryRecent) and m.n == 20


def test_query_result_round_trip():
    m = QueryResult(query_id="q1", ok=True, payload={"mood": "calm"})
    back = TypeAdapter(ServerMessage).validate_json(encode_server_message(m))
    assert isinstance(back, QueryResult) and back.ok is True and back.payload == {"mood": "calm"}


def test_query_types_unique_discriminators():
    # query_state / query_recent are ClientMessages; query_result is a ServerMessage.
    assert decode_client_message(json.dumps({"type": "query_state", "query_id": "x", "token": "t"})).type == "query_state"
    assert TypeAdapter(ServerMessage).validate_json('{"type":"query_result","query_id":"x","ok":false,"payload":{}}').type == "query_result"
```

`tests/test_config_mcp.py` (add):
```python
def test_mcp_query_token_defaults_none():
    from dollos.config import McpConfig
    assert McpConfig().query_token is None


def test_mcp_query_token_accepted():
    from dollos.config import McpConfig
    assert McpConfig(enabled=True, config="mcp.toml", query_token="s3cr3t").query_token == "s3cr3t"
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/test_ipc_query_messages.py tests/test_config_mcp.py -k "query" -v`
Expected: FAIL (`ImportError: cannot import name 'QueryState'` / `query_token` unknown).

- [ ] **Step 3: Add the messages** (`messages.py`)

In the Client→Server section (near `ChannelEvent`):
```python
class QueryState(BaseModel):
    """Debug-only read query: snapshot Doll's self-state. Carries a REQUIRED
    daemon token — the daemon fail-closes any query whose token ≠ settings.mcp.query_token
    (the IPC server has no connection auth; see spec §C.3 R-DECISION-4)."""
    type: Literal["query_state"] = "query_state"
    query_id: str
    token: str


class QueryRecent(BaseModel):
    """Debug-only read query: recent EXTERNAL_PUBLIC-origin interactions (n clamped
    by the daemon). REQUIRED daemon token, same fail-closed rule as QueryState."""
    type: Literal["query_recent"] = "query_recent"
    query_id: str
    token: str
    n: int = 20
```
Add both to `ClientMessage = Annotated[TextInput | ... | ChannelEvent | QueryState | QueryRecent, Field(discriminator="type")]`.

In the Server→Client section (near `AddressedText`):
```python
class QueryResult(BaseModel):
    """Response to a QueryState/QueryRecent, correlated by query_id. ok=false means
    the token was missing/wrong or the query surface is disabled — payload is empty,
    no data returned (fail-closed)."""
    type: Literal["query_result"] = "query_result"
    query_id: str
    ok: bool
    payload: dict
```
Add to `ServerMessage = Annotated[TextChunk | ... | AddressedText | TurnEndAddressed | QueryResult, Field(discriminator="type")]`.

- [ ] **Step 4: Add `query_token` to `McpConfig`** (`config.py`, in the class body after `config`)
```python
    query_token: str | None = None   # non-empty enables the debug read-query surface (spec §C.3)
```

- [ ] **Step 5: Run GREEN (messages + config)**

Run: `uv run pytest tests/test_ipc_query_messages.py tests/test_config_mcp.py -q`
Expected: PASS.

- [ ] **Step 6: Write failing tests — kernel query handler (token gate + scope)**

`tests/test_kernel_query_handler.py`. Build a minimal kernel/mind_state (mirror `tests/test_kernel_query_*`/`test_kernel_channel_dispatch.py` for how `_handle_message` is driven with a fake sink + how `self._mind_state` is populated). Assert:
```python
# 1. token gate fail-closed
async def test_query_rejected_when_token_missing_or_wrong():
    # settings.mcp.query_token = "right"; send QueryState(token="wrong") → QueryResult(ok=False, payload={})
    # and QueryState(token="right") with query_token unset → also ok=False (surface disabled)
    ...

# 2. get_state snapshot — seed a REAL Mood + a REAL self_history.jsonl (do NOT set an
#    ad-hoc current_self attr on a bare MindState — that passes in-test but crashes in prod
#    because MindState has no current_self field; C1).
async def test_query_state_returns_mood_and_current_self(tmp_path):
    # mind_state.mood = Mood(emotion="平靜", reason=...); write an evo_adopt line to
    # {data_root}/memory/self_history.jsonl so self_history.sanctioned_text returns known prose.
    # QueryState(token="right") → ok=True, payload["mood"]=="平靜" (the .emotion, not "Mood(...)"),
    # payload["current_self"]==<the ratified prose>; NO "energy" key. Also assert current_self=="" when
    # no self_history.jsonl / no adoption exists.
    ...

# 3. get_recent tier scope — the security test
async def test_query_recent_only_external_public_perceptions():
    # recent_perceptions has: an owner ChannelMessage (author_is_owner=True),
    # a UserSpoke (owner local chat), an mcp peer ChannelMessage (author_is_owner=False, channel_kind="mcp"),
    # an Awoke. QueryRecent(token="right", n=20) → items contain ONLY the peer ChannelMessage's text;
    # owner ChannelMessage text, UserSpoke text, Awoke are ABSENT.
    ...

# 4. recent_outputs excluded
async def test_query_recent_excludes_outputs():
    # recent_outputs populated (owner replies); items contain NO OutputRecord summary/text.
    ...

# 5. n clamp + n=0 edge
async def test_query_recent_clamps_n():
    # QueryRecent(n=9999) → daemon clamps to <= 100 (no crash, bounded).
    # QueryRecent(n=0) → items == [] (NOT the whole list — items[-0:] would return all; M1).
    ...

# 6. read-only: no perception enqueued, no cascade
async def test_query_does_not_enqueue_perception():
    # after a query, the perception queue is unchanged (query is a pure read side-channel).
    ...
```

- [ ] **Step 7: Run RED (kernel handler)**

Run: `uv run pytest tests/test_kernel_query_handler.py -v`
Expected: FAIL (no query handling in `_handle_message`).

- [ ] **Step 8: Implement the kernel query branches** (`kernel.py`, in `_handle_message`, after the `ChannelEvent` branch ~776)

```python
        elif isinstance(msg, (QueryState, QueryRecent)):
            # Debug-only read side-channel (spec §C.3). FAIL-CLOSED: the IPC server
            # has no connection auth, so authorize by a daemon-known token here.
            expected = self.settings.mcp.query_token
            if not expected or msg.token != expected:
                logger.warning("rejected %s: missing/invalid query_token", msg.type)
                sink.put_nowait(QueryResult(query_id=msg.query_id, ok=False, payload={}))
                return
            if isinstance(msg, QueryState):
                # synchronous snapshot — no await between read and put_nowait.
                # current_self is NOT a MindState field: it is the ratified prose from
                # self_history.jsonl's latest evo_adopt (mirror mind_loop.py:242-245).
                # self_history.sanctioned_text is a sync file read → snapshot-safe (no await).
                from dollos.mind import self_history
                cs = self_history.sanctioned_text(
                    self.settings.data.root / "memory" / "self_history.jsonl"
                ) or ""
                payload = {
                    "mood": self._mind_state.mood.emotion,   # Mood is a dataclass; take the display field, NOT str(Mood)
                    "current_self": cs,
                }
            else:  # QueryRecent
                n = max(0, min(msg.n, 100))                     # clamp (YAGNI)
                # REUSE the canonical external-safe filter (do NOT reinvent — a security
                # filter with two definitions drifts). _public_safe_perceptions allowlists
                # kind=="ChannelMessage" AND not author_is_owner; a future sensitive kind is
                # excluded by default (fail-closed on the KIND allowlist).
                from dollos.mind.mind_prompt import _public_safe_perceptions
                items = [
                    {"kind": p.kind, "text": _perception_text(p)[:500], "ts": p.t}
                    for p in _public_safe_perceptions(list(self._mind_state.recent_perceptions))
                ]
                payload = {"items": (items[-n:] if n else [])}   # n==0 → [] (items[-0:] would return ALL)
            sink.put_nowait(QueryResult(query_id=msg.query_id, ok=True, payload=payload))
            return
```
Add ONE module-level helper in `kernel.py` (the filter is reused from `mind_prompt._public_safe_perceptions`, only the text extractor is local):
```python
def _perception_text(p) -> str:
    d = p.data or {}
    return str(d.get("content") or d.get("text") or "")
```
> Grounded facts (verified in review): `MindState` has NO `current_self` field (`mind_state.py:117-171`) — read it via `self_history.sanctioned_text(...)`. `Mood` is a dataclass (`mind_state.py:40-45`) — serialize `.emotion` (not `str(Mood)`). `_public_safe_perceptions` exists at `mind_prompt.py:210-224` (same allowlist, already reviewed+shipped) — reuse it. The message body key is `content` (`mind_prompt.py:432,441`; `controller.py`; `daemon_link.py:104`). Confirm `self._mind_state` (kernel:400) + `self.settings.data.root` (kernel:377) attribute names.

- [ ] **Step 9: Run GREEN + regression**

Run: `uv run pytest tests/test_kernel_query_handler.py tests/test_ipc_query_messages.py -v` then `uv run pytest -q`
Expected: PASS; full suite green (baseline 1498 passed).

- [ ] **Step 10: Commit**
```bash
git add src/dollos/ipc/messages.py src/dollos/config.py src/dollos/kernel.py tests/test_ipc_query_messages.py tests/test_config_mcp.py tests/test_kernel_query_handler.py
git commit -m "feat(mcp): authenticated IPC query protocol (get_state/get_recent, token fail-closed, external_public-scoped) [P2 Task 1]"
```

---

### Task 2: connector debug mode — secret gate + query round-trip + debug_reliable

**Files:**
- Modify: `src/dollos/mcp_server/daemon_link.py` (query send/correlate; `debug_reliable` in ChannelEvent when debug)
- Modify: `src/dollos/mcp_server/__main__.py` (read `debug_secret`/`query_token` from `mcp.toml`; secret gate exposing `get_state`/`get_recent` tools; mark debug connections)
- Test: `tests/test_mcp_debug_mode.py` (new)

**Interfaces:**
- Consumes: `QueryState`/`QueryRecent`/`QueryResult` (Task 1), `mcp.toml` `debug_secret` + `query_token`, the P1 `DaemonLink` (`daemon_link.py`) + its WS dispatch loop.
- Produces: on a debug connection, MCP tools `get_state()` / `get_recent(n)` returning the daemon's `QueryResult.payload`; `talk` in debug mode sends `ChannelEvent` with `debug_reliable=True`.

- [ ] **Step 1: Read spec §C.1 + §C.3** (secret gate = registry availability, not post-hoc; query carries `query_token`; correlate by `query_id`).

- [ ] **Step 2: Write failing tests** (`tests/test_mcp_debug_mode.py`, using the P1 fake-WS harness from `tests/test_mcp_daemon_link.py`)
```python
# 1. query round-trip: DaemonLink.query_state() sends QueryState(token=<query_token>, query_id=X),
#    routes the matching QueryResult back by query_id, returns its payload.
# 2. two concurrent queries get their own results (query_id demux, no cross-delivery).
# 3. ok=False QueryResult surfaces as an error/empty (not a hang, not fabricated data).
# 4. secret gate: with debug_secret set, a connection presenting the right secret is flagged debug;
#    wrong/absent secret → NOT debug (get_state/get_recent absent from its tool set — registry availability).
# 5. debug talk() sends ChannelEvent payload with debug_reliable=True; non-debug talk() omits it (or False).
```
Mirror `test_mcp_daemon_link.py`'s fake-WS record/inject pattern; the tool-exposure gate can be unit-tested at the "which tools does this connection register" seam without a real MCP client.

- [ ] **Step 3: Run RED** → `uv run pytest tests/test_mcp_debug_mode.py -v` → FAIL.

- [ ] **Step 4: Implement query round-trip in `daemon_link.py`**

Add a `query_id`-keyed collector map (mirroring the P1 per-call `AddressedText` collector), a `query(msg) -> dict` coroutine that sends the `QueryState`/`QueryRecent`, awaits the `QueryResult` with the matching `query_id` (timeout → error), and returns `payload` (or raises/returns empty on `ok=False`). Extend the WS dispatch loop to route `query_result` frames to the query collector (in addition to the P1 `addressed_text`/`turn_end_addressed` routing; §B.6 ignore-others still holds for everything else). Add `debug_reliable: bool` to the ChannelEvent-building path so debug `talk()` stamps it.
> Show the actual code mirroring the P1 `_Collector`/`dispatch` structure in `daemon_link.py` — reuse the same demux discipline, keyed by `query_id` for queries and `channel_id` for talk replies.

- [ ] **Step 5: Implement secret gate in `__main__.py`** (per-session authed flag — FastMCP has no per-connection tool-set)

**Grounded correction (review I1): FastMCP (mcp 1.28.1) exposes a GLOBAL tool set — it CANNOT show different tools per connection.** So spec §C.1's "the tool doesn't exist for a non-auth connection" is infeasible. The mechanism is: `get_state`/`get_recent` are always-registered tools whose BODIES enforce a per-session authenticated flag (mirrors P1's per-connection state keyed on `id(ctx.session)`, `__main__.py`).

Read `debug_secret` + `query_token` from `mcp.toml` (alongside the P1 `bind_host`/`bind_port`; unknown keys already ignored by `tomllib`). Implement:
- A module-level `_authed: set[int] = set()` (session-id set), like P1's `_conn_ids`.
- An `@mcp.tool() authenticate(secret: str, ctx: Context) -> dict` that compares `secret` to `mcp.toml`'s `debug_secret` **in the mcp process** (fail-closed: `debug_secret` empty/unset → NEVER authenticates); on match, `_authed.add(id(ctx.session))` and return `{"debug": True}`; else return `{"debug": False}` (no exception, no leak of whether a secret exists).
- `get_state()` / `get_recent(n)` bodies: **hard-check** `if id(ctx.session) not in _authed: raise <error>` (or return an explicit "not authenticated" error) BEFORE calling `DaemonLink.query(...)`. This per-session check is a REAL enforcement, not cosmetic — with a global tool set it is the only thing stopping any local MCP client from reading `mood`/`current_self`/external_public recent items (that data is non-owner-private by design, and the daemon `query_token` is defense-in-depth, but the `debug_secret` gate must genuinely enforce per session).
- `get_state()`/`get_recent(n)` (once authed) call `DaemonLink.query(...)` with `token=<mcp.toml query_token>`.
- `talk()` stamps `debug_reliable=True` on its `ChannelEvent` payload only when `id(ctx.session) in _authed`.
> Note the boundary honestly: the daemon `query_token` (Task 1) gates OTHER local processes hitting the daemon IPC directly; it does NOT distinguish authed-vs-unauthed MCP clients (the connector always injects its `query_token`). So the connector's `debug_secret` per-session check IS the access control for MCP clients — it must be a hard check, not UX decoration.

- [ ] **Step 6: Run GREEN + regression** → `uv run pytest tests/test_mcp_debug_mode.py -v` then `uv run pytest -q` (baseline green).

- [ ] **Step 7: Commit**
```bash
git add src/dollos/mcp_server/ tests/test_mcp_debug_mode.py
git commit -m "feat(mcp): connector debug mode — secret gate + get_state/get_recent query round-trip + debug_reliable [P2 Task 2]"
```

---

### Task 3: reliability nudge (daemon side) — prompt-level, no tier change

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py` (situational render for a ChannelMessage perception carrying `debug_reliable`)
- Test: `tests/test_mind_prompt_debug_reliable.py` (new)

**Interfaces:**
- Consumes: the `debug_reliable` flag in a ChannelMessage perception's `data` (set by Task 2's debug `talk`).
- Produces: when rendering that perception, the prompt includes a situational nudge line; `origin_tier` and the tool registry are UNCHANGED.

- [ ] **Step 1: Read spec §C.2** (soft nudge, best-effort, NOT a tier change; the registry still hard-blocks capability).

- [ ] **Step 2: Write failing tests** (`tests/test_mind_prompt_debug_reliable.py`)
```python
# 1. a ChannelMessage perception with data["debug_reliable"] is True → _percep_body (or the situational
#    render) includes a nudge substring (e.g. "除錯" / "務必" / a stable marker) AND still renders as the
#    mcp AI-peer framing from P1.
# 2. the same perception WITHOUT debug_reliable → NO nudge substring.
# 3. debug_reliable does NOT change origin_tier: _derive_origin_tier on a debug_reliable=True,
#    author_is_owner=False payload → still "external_public" (mirror the P1 F2 test).
```
Find where the P1 mcp AI-peer render lives (`_percep_body`, mind_prompt.py) and add the nudge there (or in the situational block the turn builds for that origin — read how a per-origin situational prompt is assembled and inject the nudge for that turn).

- [ ] **Step 3: Run RED** → FAIL.

- [ ] **Step 4: Implement the nudge** — in the mcp branch of `_percep_body` (`mind_prompt.py:425-433`). NOTE: that branch currently builds an f-string and `return`s it directly (there is no `body` variable). Refactor to build-into-a-local-then-append-then-return so the nudge can be appended when `d.get("debug_reliable")` is truthy:
```python
        # (inside the mcp AI-peer branch of _percep_body)
        body = f"...<the existing mcp AI-peer framing string>..."
        if d.get("debug_reliable"):
            body += "（這是除錯通道，請務必給出實質回覆，別已讀不回。）"
        return body
```
> Do NOT touch `_derive_origin_tier` or `_active_tool_registry` — the tier stays `external_public` and the registry stays `EXTERNAL_TOOLS`. The nudge is text only (soft, best-effort — §C.2). The triggering perception is on the prompt path: it is appended to `recent_perceptions` before render (`mind_loop.py:342`) and a debug `talk` (author_is_owner=False) is allowlisted by `_public_safe_perceptions` on its external_public turn.

- [ ] **Step 5: Run GREEN + regression** → PASS; full suite green.

- [ ] **Step 6: Commit**
```bash
git add src/dollos/mind/mind_prompt.py tests/test_mind_prompt_debug_reliable.py
git commit -m "feat(mcp): debug reliability nudge (prompt-level, tier unchanged) [P2 Task 3]"
```

---

### Task 4: debug docs finish

**Files:**
- Modify: `mcp.example.toml` (add `debug_secret` + `query_token` with comments), `config.example.toml` (`[mcp].query_token` line), `.gitignore` (confirm `mcp.toml` covered — added P1), `CLAUDE.md` (architecture note), `docs/roadmap.md` (this step), `docs/dollosctl-smoke.md` (F8 debug checklist)

**Interfaces:** (pure docs)

- [ ] **Step 1: `mcp.example.toml`** — add under `[server]` (or the appropriate table):
```toml
# Debug mode (optional). Leave both EMPTY to keep the endpoint peer-only (no introspection).
# Treat these as OWNER-SENSITIVE secrets — mcp.toml is gitignored, never commit real values.
debug_secret = ""   # a connection presenting this secret gets get_state/get_recent + reliable talk
query_token  = ""   # MUST equal config.toml's [mcp].query_token; the daemon rejects queries without it
```

- [ ] **Step 2: `config.example.toml`** — add to the `[mcp]` block:
```toml
# query_token = "change-me"   # enables the debug read-query surface; MUST match mcp.toml's query_token. Empty/unset = query surface disabled (fail-closed).
```

- [ ] **Step 3: `.gitignore`** — confirm `mcp.toml` is already listed (added in P1 Task 5). No change if present; add if missing.

- [ ] **Step 4: `CLAUDE.md`** — extend the MCP architecture note: "debug mode = secret-gated (`mcp.toml` `debug_secret`) read-only introspection (`get_state`/`get_recent` via an authenticated IPC query protocol — daemon `query_token` fail-closed, `get_recent` scoped to external_public) + a best-effort reliability nudge; still no Shell, still not owner."

- [ ] **Step 5: `docs/roadmap.md`** — add the P2 step (MCP debug mode), matching the existing entry format.

- [ ] **Step 6: `docs/dollosctl-smoke.md`** — add the F8 debug live-smoke checklist: set `debug_secret`+`query_token` (matching in both files), restart daemon, connect a debug MCP client, verify `get_state` returns mood/current_self, `get_recent` returns only peer interactions (NOT owner DMs), a wrong `query_token` → `ok=false`, and debug `talk` gets a substantive reply.

- [ ] **Step 7: Commit**
```bash
git add mcp.example.toml config.example.toml .gitignore CLAUDE.md docs/roadmap.md docs/dollosctl-smoke.md
git commit -m "docs(mcp): debug mode (secret/query_token, F8 debug checklist, arch note) [P2 Task 4]"
```

---

## Self-Review

**1. Spec coverage:**
- §C.1 secret gate → Task 2 ✓
- §C.2 reliable nudge (soft, no tier change) → Task 3 ✓
- §C.3 IPC query protocol (messages, token fail-closed, sync snapshot, external_public scope, n clamp, read-only bypass) → Task 1 ✓
- §C.3 connector query round-trip (query_id correlate, query_token) → Task 2 ✓
- §C.4 v1 scope (only get_state+get_recent; no trace/inject/reflection/write) → honored across Tasks 1-2 (nothing else added) ✓
- §H-4 query_token REQUIRED daemon-side → Task 1 Global Constraint ✓
- docs → Task 4 ✓

**2. Placeholder scan:** Task 1 Step 8 and Task 2 Steps 4-5 reference "mirror the P1 structure" for the daemon_link collector/dispatch and the kernel test harness — these point at concrete existing code (`daemon_link.py` `_Collector`/`dispatch`, `test_mcp_daemon_link.py`, `test_kernel_channel_dispatch.py`) the implementer reads, not vague TODOs. The `get_recent` text-key (`content` vs `text`) and the FastMCP secret-presentation mechanism are flagged as "confirm against real code / pick simplest that works" — genuine grounded-choice points, not placeholders.

**3. Type consistency:** `QueryState`/`QueryRecent` carry `query_id: str` + `token: str` (+ `n: int` for Recent); `QueryResult` carries `query_id: str` + `ok: bool` + `payload: dict` — consistent across messages.py (Task 1), the kernel handler (Task 1), and the connector round-trip (Task 2). `McpConfig.query_token: str | None` consistent (Task 1 config + kernel read + Task 4 docs). `debug_reliable` bool consistent (Task 2 connector sets it in ChannelEvent payload → Task 3 daemon reads `d.get("debug_reliable")`). `get_state` payload `{mood, current_self}` (no energy) + `get_recent` payload `{items:[{kind,text,ts}]}` consistent across Task 1 handler + tests + Task 4 docs.

**Dependency between tasks:** 1 → 2 → 3 → 4 (messages/handler → connector → nudge → docs). Task 1 (the daemon security boundary) and Task 2 (the connector) are the security-bearing tasks → opus review. Task 3 (nudge) and Task 4 (docs) → standard review. Whole-branch opus review + full suite before merge. P2 merges on top of P1; dogfood P1+P2 together after.
