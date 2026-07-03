# P1a Backbone I/O Typing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Backbone that types every I/O channel `internal|external` and routes each turn's reply to the channel that woke it — replacing the single-conversation "most-recent sink" assumption with per-origin turn segmentation.

**Architecture:** A `ChannelRegistry` on the daemon holds `{channel_id → locus/kind}`. `PerceptionQueue.drain()` gains an origin-grouping mode so a batch with mixed origins yields one bucket per origin channel; `MindLoop` runs one cascade + resolves one sink per bucket. `SinkResolver` gains locus/origin-aware resolution so a connected external bridge never steals internal output. A timed batch accumulator coalesces same-channel messages before they enqueue. This is the foundation for P1b (discord-bridge) — no Discord code here.

**Tech Stack:** Python 3.12, asyncio, pytest (`uv run pytest`). Baseline: 1098 tests green on main.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-dollos-mvp-discord-presence-design.md` §3.1; R2 arch findings in `2026-07-03-mvp-r2-findings.md` (P1f trace deferred, but this plan must not close the door on per-origin trace).
- **Perception carries origin.** Every `Perception` may carry `data["channel_id"]` (str) identifying its origin channel; absence = internal/origin-less (existing behavior preserved).
- **UserSpoke stays internal.** This plan does NOT add the `ChannelMessage` kind or `external_ctx` wiring (that is P1b/P1e). It only makes routing *origin-aware* so P1b can drop in. Existing `UserSpoke`/`TextInput` flow is byte-unchanged when no origin is set.
- **No behavior change for existing internal clients.** All 1098 existing tests stay green. CLI/voice (single internal sink) must route exactly as today.
- **No-fallback / friendly-error house rules.** No silent degradation.
- **Worktree:** `.worktrees/p1a-backbone/`, branch `p1a-backbone`. Commit-before check: `git branch --show-current` must print `p1a-backbone`.

---

### Task 1: `ChannelRegistry`

**Files:**
- Create: `src/dollos/ipc/channel_registry.py`
- Test: `tests/test_channel_registry.py`

**Interfaces:**
- Produces: `ChannelRegistry.register(channel_id: str, *, locus: str, kind: str) -> None`; `.get(channel_id) -> ChannelInfo | None`; `.locus_of(channel_id) -> str` (returns `"internal"` for unknown — origin-less defaults internal); `.unregister(channel_id) -> None`. `ChannelInfo` = frozen dataclass `{channel_id, locus, kind}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_channel_registry.py
"""ChannelRegistry — channel locus/kind lookup (spec §3.1)."""
import pytest

from dollos.ipc.channel_registry import ChannelRegistry, ChannelInfo


def test_register_and_get():
    r = ChannelRegistry()
    r.register("disc:123", locus="external", kind="discord")
    assert r.get("disc:123") == ChannelInfo("disc:123", "external", "discord")


def test_locus_of_unknown_defaults_internal():
    r = ChannelRegistry()
    assert r.locus_of("nope") == "internal"     # origin-less = internal
    r.register("disc:1", locus="external", kind="discord")
    assert r.locus_of("disc:1") == "external"


def test_unregister():
    r = ChannelRegistry()
    r.register("c", locus="internal", kind="text")
    r.unregister("c")
    assert r.get("c") is None


def test_register_rejects_bad_locus():
    r = ChannelRegistry()
    with pytest.raises(ValueError):
        r.register("c", locus="sideways", kind="text")
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/test_channel_registry.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/dollos/ipc/channel_registry.py
"""ChannelRegistry — daemon-side registry typing each I/O channel by locus
(internal=body organ / external=world) and kind (spec 2026-07-03 §3.1)."""
from __future__ import annotations

from dataclasses import dataclass

_LOCI = frozenset({"internal", "external"})


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    locus: str
    kind: str


class ChannelRegistry:
    """channel_id → ChannelInfo. Unknown channel_id resolves to locus
    'internal' (origin-less events are the existing internal path)."""

    def __init__(self) -> None:
        self._by_id: dict[str, ChannelInfo] = {}

    def register(self, channel_id: str, *, locus: str, kind: str) -> None:
        if locus not in _LOCI:
            raise ValueError(f"locus must be one of {_LOCI}, got {locus!r}")
        self._by_id[channel_id] = ChannelInfo(channel_id, locus, kind)

    def get(self, channel_id: str) -> ChannelInfo | None:
        return self._by_id.get(channel_id)

    def locus_of(self, channel_id: str | None) -> str:
        info = self._by_id.get(channel_id) if channel_id else None
        return info.locus if info is not None else "internal"

    def unregister(self, channel_id: str) -> None:
        self._by_id.pop(channel_id, None)
```

- [ ] **Step 4: Run** → 4 PASS.
- [ ] **Step 5: Commit** — `feat(ipc): ChannelRegistry — locus/kind typing per channel (P1a §3.1)`

---

### Task 2: origin-grouping in `PerceptionQueue.drain`

**Files:**
- Modify: `src/dollos/mind/perception_queue.py` (add `drain_grouped`, keep `drain` intact)
- Test: `tests/test_perception_queue_grouped.py`

**Interfaces:**
- Consumes: `Perception.data.get("channel_id")` as origin key (None → the shared internal bucket key `""`).
- Produces: `PerceptionQueue.drain_grouped(timeout_s=None) -> list[list[Perception]]` — the same perceptions `drain()` would return, partitioned into origin buckets **preserving arrival order within each bucket**; internal/origin-less perceptions share one bucket keyed `""`. Empty list on shutdown (mirrors `drain`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_perception_queue_grouped.py
"""drain_grouped — per-origin turn segmentation (spec §3.1 R2-C1)."""
import time

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue


def _p(kind, **data):
    return Perception(kind=kind, t=time.time(), data=data)


@pytest.mark.asyncio
async def test_groups_by_channel_preserving_order():
    q = PerceptionQueue()
    q.put(_p("ChannelMessage", channel_id="A", content="a1"))
    q.put(_p("ChannelMessage", channel_id="B", content="b1"))
    q.put(_p("ChannelMessage", channel_id="A", content="a2"))
    buckets = await q.drain_grouped()
    # two buckets; A keeps [a1,a2] order; B is [b1]
    got = {b[0].data["channel_id"]: [p.data["content"] for p in b] for b in buckets}
    assert got == {"A": ["a1", "a2"], "B": ["b1"]}


@pytest.mark.asyncio
async def test_originless_share_one_internal_bucket():
    q = PerceptionQueue()
    q.put(_p("UserSpoke", text="hi"))          # no channel_id
    q.put(_p("ReflectionMoment"))              # no channel_id
    buckets = await q.drain_grouped()
    assert len(buckets) == 1 and len(buckets[0]) == 2


@pytest.mark.asyncio
async def test_shutdown_returns_empty():
    q = PerceptionQueue()
    q.shutdown()
    assert await q.drain_grouped() == []
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: drain_grouped`).

- [ ] **Step 3: Implement** — append to `PerceptionQueue`:

```python
    async def drain_grouped(self, timeout_s: float | None = None) -> list[list[Perception]]:
        """Like drain(), but partition the batch by origin channel so a mixed
        batch yields one bucket per channel (spec §3.1 R2-C1: drain is
        origin-blind; per-origin turn segmentation avoids crosstalk).
        Origin-less perceptions (no data['channel_id']) share one bucket.
        Insertion order within each bucket is preserved; bucket order follows
        first-seen channel."""
        flat = await self.drain(timeout_s)
        if not flat:
            return []
        buckets: dict[str, list[Perception]] = {}
        for p in flat:
            key = (p.data or {}).get("channel_id") or ""
            buckets.setdefault(key, []).append(p)
        return list(buckets.values())
```

- [ ] **Step 4: Run** → 3 PASS. Also `uv run pytest tests/test_perception_queue.py -v` (existing `drain` untouched) → green.
- [ ] **Step 5: Commit** — `feat(mind): drain_grouped — per-origin turn segmentation (P1a R2-C1)`

---

### Task 3: locus/origin-aware `SinkResolver`

**Files:**
- Modify: `src/dollos/mind/sink_resolver.py`
- Test: `tests/test_sink_resolver_locus.py`

**Interfaces:**
- Produces: `register(sink, *, locus="internal", channel_id=None) -> int` (kwargs optional → back-compat: `register(sink)` still works, defaults internal/None); `__call__(origin=None) -> _SinkLike`. Resolution: if `origin` (channel_id) matches a registered sink's `channel_id` → that sink; else → most-recent **internal** sink; else DummySink. **Never** returns an external sink for an origin-less/internal turn (R1-arch I2: prevents a connected bridge from stealing internal output).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sink_resolver_locus.py
"""SinkResolver locus/origin routing (spec §3.1 R1-arch I2)."""
from dollos.mind.sink_resolver import SinkResolver, DummySink


class _Sink:
    def __init__(self): self.items = []
    def put_nowait(self, item): self.items.append(item)


def test_backcompat_register_no_kwargs_is_internal():
    r = SinkResolver()
    s = _Sink()
    r.register(s)                       # old signature
    assert r() is s                     # origin-less → internal sink


def test_external_sink_never_steals_internal_output():
    r = SinkResolver()
    internal = _Sink(); external = _Sink()
    r.register(internal, locus="internal")
    r.register(external, locus="external", channel_id="disc:1")   # higher handle
    # origin-less internal turn must resolve to internal, NOT the newer external
    assert r() is internal
    assert r(None) is internal


def test_origin_routes_to_matching_external_sink():
    r = SinkResolver()
    internal = _Sink(); ext_a = _Sink(); ext_b = _Sink()
    r.register(internal, locus="internal")
    r.register(ext_a, locus="external", channel_id="A")
    r.register(ext_b, locus="external", channel_id="B")
    assert r("A") is ext_a
    assert r("B") is ext_b


def test_unknown_origin_falls_to_internal():
    r = SinkResolver()
    internal = _Sink()
    r.register(internal, locus="internal")
    r.register(_Sink(), locus="external", channel_id="A")
    assert r("ZZZ") is internal          # unknown origin → internal, not external


def test_empty_returns_dummy():
    r = SinkResolver()
    assert isinstance(r(), DummySink)
```

- [ ] **Step 2: Run** → FAIL (`register() takes 2 positional... unexpected keyword` / `__call__ takes 1 positional`).

- [ ] **Step 3: Implement** — replace the class body's `__init__`/`register`/`__call__`:

```python
    def __init__(self) -> None:
        self._sinks: dict[int, _SinkLike] = {}
        self._meta: dict[int, tuple[str, str | None]] = {}   # handle → (locus, channel_id)
        self._counter: int = 0
        self._dummy = DummySink()

    def register(self, sink: _SinkLike, *, locus: str = "internal",
                 channel_id: str | None = None) -> int:
        """Register a sink with its locus/channel. Bare register(sink) keeps
        the legacy internal-sink behavior (back-compat)."""
        handle = self._counter
        self._counter += 1
        self._sinks[handle] = sink
        self._meta[handle] = (locus, channel_id)
        return handle

    def __call__(self, origin: str | None = None) -> _SinkLike:
        """Resolve the sink for this turn's origin channel. External sink only
        when origin matches its channel_id; otherwise the most-recent INTERNAL
        sink (R1-arch I2: a connected external bridge must not steal internal
        output). DummySink when nothing suitable."""
        if origin is not None:
            for h in sorted(self._sinks, reverse=True):
                loc, cid = self._meta[h]
                if loc == "external" and cid == origin:
                    return self._sinks[h]
        # origin-less, or no external match → most-recent internal
        internal = [h for h in self._sinks if self._meta[h][0] == "internal"]
        if internal:
            return self._sinks[max(internal)]
        return self._dummy
```

Also update `unregister` to drop `_meta`: `self._meta.pop(handle, None)` alongside `self._sinks.pop(...)`.

- [ ] **Step 4: Run** → 5 PASS. Full suite spot: `uv run pytest tests/ -k "sink or kernel or mind_loop" -q` → green (bare `register(sink)` + zero-arg `r()` preserved).
- [ ] **Step 5: Commit** — `feat(mind): SinkResolver locus/origin routing — no external steal of internal output (P1a I2)`

---

### Task 4: MindLoop per-origin turn + origin-aware sink resolution

**Files:**
- Modify: `src/dollos/mind/mind_loop.py` (iterate: `drain`→`drain_grouped` loop; thread origin into sink resolution at :397/:612; `_flush_chunker` origin)
- Modify: `src/dollos/mind/mind_ctx.py` (+`current_origin: str | None = None`)
- Test: `tests/test_mind_loop_origin.py`

**Interfaces:**
- Consumes: `drain_grouped` (Task 2), `SinkResolver.__call__(origin)` (Task 3).
- Produces: `MindLoop.iterate` processes one origin bucket per cascade; `MindCtx.current_origin` set per bucket; all `sink_resolver()` calls in the streaming path become `sink_resolver(self._ctx.current_origin)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mind_loop_origin.py
"""MindLoop routes each origin bucket's output to that origin's sink (§3.1 C1)."""
import pytest

# Reuse the project's existing MindLoop test harness/factory. Find it:
#   grep -rn "_mindloop_factory\|def _make_mind" tests/ | head
# Construct two sinks registered with distinct external channel_ids A,B,
# enqueue a ChannelMessage from A and one from B in the same drain window,
# drive one iterate cycle per bucket, and assert A's reply reached sink A only
# and B's reached sink B only (no crosstalk). Stub the LLM adapter to emit a
# fixed sentence per turn (copy the stub pattern from the existing mind_loop
# streaming test).
```

Note to implementer: locate the existing MindLoop streaming test (`grep -rn "TextChunk\|_flush_chunker\|stream" tests/`), copy its adapter-stub + sink harness, and assert per-origin delivery. The behavioral claim to pin: two same-window external messages from channels A and B do not cross-deliver.

- [ ] **Step 2: Run** → FAIL (crosstalk: both land on max-handle sink).

- [ ] **Step 3: Implement**
  1. `mind_ctx.py`: add `current_origin: str | None = None` next to `current_turn`.
  2. `mind_loop.py` `iterate` (line ~216): replace the single `perceptions = await self._queue.drain()` + body with a loop over `drain_grouped()`:
     ```python
     buckets = await self._queue.drain_grouped()
     if not buckets:
         return
     for bucket in buckets:
         self._ctx.current_origin = (bucket[0].data or {}).get("channel_id") or None
         await self._run_one_turn(bucket)   # extract existing per-batch body into _run_one_turn(perceptions)
     ```
     Extract the current post-drain body (sync/render/llm/execute/persist) into `_run_one_turn(self, perceptions)`; it already reads `perceptions` as the batch.
  3. Sink resolution: line ~397 `self._ctx.sink_resolver().put_nowait(None)` → `self._ctx.sink_resolver(self._ctx.current_origin).put_nowait(None)`; line ~612 `sink = self._ctx.sink_resolver()` → `sink = self._ctx.sink_resolver(self._ctx.current_origin)`.
  4. `current_origin` resets each bucket (set at loop top); reset to `None` after the loop for cleanliness.

- [ ] **Step 4: Run** → new test PASS; **full suite** `uv run pytest` → 1098 + new green (3 pre-existing torch voice failures only; internal single-sink path unchanged because origin=None → internal sink).
- [ ] **Step 5: Commit** — `feat(mind): per-origin turn loop + origin-aware sink resolution (P1a C1)`

---

### Task 5: `AddressedText` IPC message + streaming emit for external origin

**Files:**
- Modify: `src/dollos/ipc/messages.py` (+`AddressedText` server→client), `+ChannelRegister`/`ChannelEvent` client→daemon stubs
- Modify: `src/dollos/mind/mind_loop.py` (`_flush_chunker`: emit `AddressedText` when origin is external, else `TextChunk`)
- Test: `tests/test_addressed_text.py`

**Interfaces:**
- Consumes: `ChannelRegistry.locus_of` (Task 1), `current_origin` (Task 4).
- Produces: `AddressedText(channel_id: str, text: str)` pydantic (in `ServerMessage` union + `encode_server_message`); MindLoop emits it on external-origin turns so the bridge knows where to send. Internal turns keep `TextChunk` (unchanged).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_addressed_text.py
"""AddressedText server message + external-origin emit (spec §3.1)."""
import json

from dollos.ipc.messages import AddressedText, encode_server_message


def test_addressed_text_encodes():
    raw = encode_server_message(AddressedText(channel_id="disc:1", text="hi"))
    d = json.loads(raw)
    assert d["type"] == "addressed_text" and d["channel_id"] == "disc:1" and d["text"] == "hi"
```

Plus a MindLoop-level test (extend Task 4's harness): an external-origin turn's streamed sentences arrive as `AddressedText(channel_id=origin)` on that origin's sink; an internal-origin turn still emits `TextChunk`. (Copy the adapter/sink stub from Task 4.)

- [ ] **Step 2: Run** → FAIL (`ImportError: AddressedText`).

- [ ] **Step 3: Implement**
  - `messages.py`: add pydantic `AddressedText(BaseModel)` with `channel_id: str`, `text: str`; a literal `type` tag `"addressed_text"` matching the file's existing tagging convention (mirror `TextChunk`'s pattern exactly — read `encode_server_message` and copy the discriminator style); add to the `ServerMessage` union. Add `ChannelRegister(channel_id, locus, kind)` + `ChannelEvent(channel_id, payload: dict)` to the client→daemon side + `decode_client_message` (mirror `TextInput`). These are wire-schema only; kernel wiring of ChannelEvent→Perception is P1b.
  - `mind_loop.py` `_flush_chunker` (line ~843): thread origin — resolve locus via a registry reference. **Wiring:** MindLoop needs the `ChannelRegistry` to know if `current_origin` is external. Add `channel_registry` to `MindCtx` (kernel passes `self._channel_registry`, a new `ChannelRegistry()` built in kernel `__init__` near SinkResolver at kernel.py:255). In `_flush_chunker`/`_handle_stream_event`, if `self._ctx.current_origin` and `self._ctx.channel_registry.locus_of(self._ctx.current_origin) == "external"`: `sink.put_nowait(AddressedText(channel_id=self._ctx.current_origin, text=sentence))`; else `sink.put_nowait(TextChunk(text=sentence))`.

- [ ] **Step 4: Run** → PASS; full suite green (internal path: origin None → locus internal → TextChunk, unchanged).
- [ ] **Step 5: Commit** — `feat(ipc): AddressedText + ChannelRegister/Event schema + external-origin emit (P1a)`

---

### Task 6: timed batch accumulator (same-channel coalescing)

**Files:**
- Create: `src/dollos/ipc/batch_accumulator.py`
- Test: `tests/test_batch_accumulator.py`

**Interfaces:**
- Produces: `BatchAccumulator(enqueue: Callable[[list[dict]], None], window_s: float)` with `async def add(self, channel_id: str, item: dict) -> None` and `async def flush_all(self) -> None`. Holds items per channel for `window_s` after the FIRST item, then calls `enqueue(items)` once per channel (this is the layer §3.1 I1 says `drain` lacks). Pure timing utility — the daemon/bridge decides what `enqueue` does (P1b turns a flushed batch into one perception). No LLM, no Discord.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_batch_accumulator.py
"""BatchAccumulator — same-channel coalescing window (spec §3.1 I1)."""
import asyncio

import pytest

from dollos.ipc.batch_accumulator import BatchAccumulator


@pytest.mark.asyncio
async def test_coalesces_same_channel_within_window():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=0.05)
    await acc.add("A", {"n": 1})
    await acc.add("A", {"n": 2})       # within window → same batch
    await asyncio.sleep(0.08)
    assert flushed == [[{"n": 1}, {"n": 2}]]


@pytest.mark.asyncio
async def test_separate_channels_separate_batches():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=0.05)
    await acc.add("A", {"n": 1})
    await acc.add("B", {"n": 9})
    await asyncio.sleep(0.08)
    assert [{"n": 1}] in flushed and [{"n": 9}] in flushed and len(flushed) == 2


@pytest.mark.asyncio
async def test_flush_all_drains_immediately():
    flushed = []
    acc = BatchAccumulator(enqueue=lambda items: flushed.append(items), window_s=10.0)
    await acc.add("A", {"n": 1})
    await acc.flush_all()              # e.g. shutdown
    assert flushed == [[{"n": 1}]]
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# src/dollos/ipc/batch_accumulator.py
"""BatchAccumulator — coalesce same-channel items within a time window before
enqueue (spec 2026-07-03 §3.1 I1: PerceptionQueue.drain has no post-first
accumulation window). Pure asyncio timing; caller supplies enqueue()."""
from __future__ import annotations

import asyncio
from typing import Callable


class BatchAccumulator:
    def __init__(self, enqueue: Callable[[list[dict]], None], window_s: float) -> None:
        self._enqueue = enqueue
        self._window_s = window_s
        self._pending: dict[str, list[dict]] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, channel_id: str, item: dict) -> None:
        if channel_id not in self._pending:
            self._pending[channel_id] = []
            self._timers[channel_id] = asyncio.ensure_future(self._fire_after(channel_id))
        self._pending[channel_id].append(item)

    async def _fire_after(self, channel_id: str) -> None:
        try:
            await asyncio.sleep(self._window_s)
        except asyncio.CancelledError:
            return
        self._flush(channel_id)

    def _flush(self, channel_id: str) -> None:
        items = self._pending.pop(channel_id, None)
        self._timers.pop(channel_id, None)
        if items:
            self._enqueue(items)

    async def flush_all(self) -> None:
        for t in list(self._timers.values()):
            t.cancel()
        for cid in list(self._pending):
            self._flush(cid)
```

- [ ] **Step 4: Run** → 3 PASS.
- [ ] **Step 5: Commit** — `feat(ipc): BatchAccumulator — same-channel coalescing window (P1a I1)`

---

## Completion

After Task 6: full suite green (1098 + ~19 new). This plan ships the backbone P1a needs — nothing Discord-specific, all existing internal clients unchanged. **No live smoke here** (no external channel exists yet; P1b's bridge is the first consumer and carries the first Discord live smoke). Merge via `superpowers:finishing-a-development-branch`. Next: **P1b** (discord-bridge + ambient log) consumes `ChannelRegistry`/`ChannelEvent`/`AddressedText`/`BatchAccumulator`.

**R2 arch findings deferred but noted for downstream plans:** trace per-pass origin (P1f), grammar-state capture (P1f), energy origin-aware (P1e), DiscordLookup RPC correlation (P1d) — none are P1a scope; P1a only ensures origin flows through so they can hook it.
