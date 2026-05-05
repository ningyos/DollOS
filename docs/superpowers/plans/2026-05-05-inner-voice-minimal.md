# Inner Voice (minimal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `Instinct` ABC + `SmallModelInstinct` so each event is preprocessed by the small model into a rolling natural-language summary, which is injected into the big model prefill as a `STATE:` block before `RECALL:`. Externally observable behavior changes only by addition of the STATE block; everything else (IPC sequence, recall logic, big-model streaming) stays identical to step 4.

**Architecture:** New `dollos.instinct` module defines `Instinct` ABC with one method `process(event: DollEvent) -> str` and a concrete `SmallModelInstinct` that calls a small-model `LLMAdapter` non-streaming and maintains in-memory `_last_summary`. New jinja template `iv_summary.jinja` (system + user blocks) instructs the small model to update the summary. `EventDispatcher` constructor takes `instinct: Instinct`; `_handle` calls `summary = await self._instinct.process(doll_event)` between perceive and respond; `_respond` prepends `STATE:\n{summary}\n\n` to the prefill iff summary is non-empty. `Kernel` adds a `build_instinct()` factory that reuses the existing small-model `LlamaCppProvider` + `Qwen3PlainTemplate` pair already used by `InnerVoice`.

**Tech Stack:** Python 3.12+, `asyncio`, `jinja2`, `pytest` + `pytest-asyncio`. No new external deps.

**Spec:** `docs/superpowers/specs/2026-05-05-inner-voice-minimal-design.md`

---

## Task 1: `dollos/instinct.py` — Instinct ABC + SmallModelInstinct

**Files:**
- Create: `src/dollos/instinct.py`
- Create: `tests/test_instinct.py`

Pure logic. No dispatcher / kernel touch yet.

### Step 1: Write tests first (RED)

- [ ] Create `tests/test_instinct.py` with these cases:

```python
"""Tests for Instinct ABC + SmallModelInstinct."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from dollos.events import DollEvent, UserTextEvent
from dollos.instinct import Instinct, SmallModelInstinct
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer


@dataclass
class _FakeAdapter(LLMAdapter):
    """Fake LLMAdapter — yields configured chunks; captures call args."""

    chunks: list[StreamChunk] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "user": user, "prefill": prefill})
        for c in self.chunks:
            yield c


def _make_doll_event(text: str) -> DollEvent:
    raw = UserTextEvent(text=text, response_sink=asyncio.Queue())
    return DollEvent(perception=text, raw=raw)


def test_instinct_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        Instinct()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_small_model_instinct_first_call_uses_empty_prev_summary():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="主人說了 hi。", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    event = _make_doll_event("hi")

    summary = await inst.process(event)

    assert summary == "主人說了 hi。"
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert "(none — this is the first event)" in call["user"]
    assert "hi" in call["user"]
    assert call["prefill"] == ""


@pytest.mark.asyncio
async def test_small_model_instinct_persists_summary_across_calls():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="第一次摘要", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())

    s1 = await inst.process(_make_doll_event("first"))
    assert s1 == "第一次摘要"

    # Replace chunks for second call
    adapter.chunks = [
        StreamChunk(text="第二次摘要", done=False),
        StreamChunk(text="", done=True),
    ]
    s2 = await inst.process(_make_doll_event("second"))
    assert s2 == "第二次摘要"

    second_user = adapter.calls[1]["user"]
    assert "第一次摘要" in second_user
    assert "second" in second_user


@pytest.mark.asyncio
async def test_small_model_instinct_strips_whitespace():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="  trimmed  \n", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    summary = await inst.process(_make_doll_event("x"))
    assert summary == "trimmed"


@pytest.mark.asyncio
async def test_small_model_instinct_empty_output_is_empty_summary():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="", done=True)]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    summary = await inst.process(_make_doll_event("x"))
    assert summary == ""

    # Second call should now use "(none — this is the first event)" again
    # because _last_summary is empty (treated as no prev).
    adapter.chunks = [
        StreamChunk(text="recovered", done=False),
        StreamChunk(text="", done=True),
    ]
    await inst.process(_make_doll_event("y"))
    second_user = adapter.calls[1]["user"]
    assert "(none — this is the first event)" in second_user
```

Run: `uv run pytest tests/test_instinct.py -q`
Expected: ImportError / collection error — `dollos.instinct` doesn't exist yet.

### Step 2: Implement (GREEN)

- [ ] Create `src/dollos/instinct.py`:

```python
"""Instinct — System 1 per-event preprocessing layer.

Step 5 minimal: only `process()` returning a rolling natural-language summary.
Future steps will extend with first_instinct (step 7 reflex), wake gating, etc.
The summary is injected into the big-model prefill as the STATE block.

Prompt content lives in `dollos/prompts/templates/iv_summary.jinja`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dollos.events import DollEvent
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


class Instinct(ABC):
    """Per-event small-model preprocessing layer (System 1)."""

    @abstractmethod
    async def process(self, event: DollEvent) -> str:
        """Return updated rolling summary for this event.

        Implementations may maintain in-memory state across calls.
        Empty string means "no STATE block" (caller skips injection).
        """


class SmallModelInstinct(Instinct):
    """Instinct backed by a small LLM that maintains a rolling summary."""

    def __init__(
        self,
        adapter: LLMAdapter,
        renderer: PromptRenderer,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._last_summary = ""

    async def process(self, event: DollEvent) -> str:
        prev = self._last_summary or "(none — this is the first event)"
        blocks = self._renderer.render_blocks(
            "iv_summary",
            prev_summary=prev,
            perception=event.perception,
        )

        chunks: list[str] = []
        async for chunk in self._adapter.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        self._last_summary = "".join(chunks).strip()
        return self._last_summary
```

- [ ] Create `src/dollos/prompts/templates/iv_summary.jinja`:

```jinja
{%- block system -%}
You are Doll's inner voice. Maintain a continuous summary of what is happening
in Doll's interaction. The summary is Doll's working memory across events.

Rules:
- Output ONLY the new summary as plain prose, 1–3 sentences.
- Carry forward relevant context from the previous summary.
- Drop details that are no longer load-bearing.
- Do NOT add commentary, headers, or bullets.
- Do NOT roleplay. You are not Doll; you are her working memory.
- If the new perception adds nothing meaningful, return the previous summary unchanged.
{%- endblock -%}

{%- block user -%}
Previous summary:
{{ prev_summary }}

New perception:
{{ perception }}
{%- endblock -%}
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_instinct.py -q`
- [ ] Expected: 5 passed.

### Step 4: Lint / typecheck

- [ ] Run: `uv run ruff check src/dollos/instinct.py tests/test_instinct.py`
- [ ] Run: `uv run mypy src/dollos/instinct.py` (only if mypy is part of project lint — skip if `mypy` not configured; check `pyproject.toml`)
- [ ] Expected: no errors. Fix any violations before commit.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/instinct.py src/dollos/prompts/templates/iv_summary.jinja tests/test_instinct.py
git commit -m "$(cat <<'EOF'
feat(instinct): Instinct ABC + SmallModelInstinct (rolling summary)

Step 5 minimal: per-event small-model call producing a rolling
natural-language summary. In-memory _last_summary; restart clears.
Prompt template iv_summary.jinja outputs 1–3 sentence prose.

No dispatcher / kernel wiring yet (next task).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire `Instinct` into `EventDispatcher`

**Files:**
- Modify: `src/dollos/dispatcher.py`
- Modify: `tests/test_dispatcher.py`

`EventDispatcher` takes a new `instinct` ctor arg, calls it between perceive and respond, and prepends a `STATE:` block to the prefill when summary is non-empty.

### Step 1: Write failing tests (RED)

- [ ] Open `tests/test_dispatcher.py`. Add a `_FakeInstinct` helper near the existing `_FakeInnerVoice`:

```python
class _FakeInstinct:
    """Fake Instinct.process — returns configurable summaries, captures calls."""

    def __init__(
        self,
        summaries: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._summaries = list(summaries) if summaries is not None else [""]
        self._raises = raises
        self.calls: list[str] = []   # perception strings seen

    async def process(self, event):  # type: ignore[no-untyped-def]
        self.calls.append(event.perception)
        if self._raises:
            raise self._raises
        if self._summaries:
            return self._summaries.pop(0)
        return ""
```

- [ ] Update **every** existing `EventDispatcher(...)` construction in `tests/test_dispatcher.py` to pass `instinct=_FakeInstinct()` (default empty summary preserves current prefill shape so existing assertions still pass). Search for `EventDispatcher(` and add the kwarg to each.

- [ ] Add new test cases:

```python
@pytest.mark.asyncio
async def test_dispatcher_calls_instinct_with_doll_event_perception():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="ok", done=False), StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=["主人剛打招呼。"])
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))

    # Drain
    while True:
        item = await sink.get()
        if item is None:
            break

    assert inst.calls == ["hi"]


@pytest.mark.asyncio
async def test_dispatcher_prepends_state_block_when_summary_nonempty():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="ok", done=False), StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=["主人剛打招呼。"])
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            break

    assert len(adapter.calls) == 1
    prefill = adapter.calls[0]["prefill"]
    assert prefill == "STATE:\n主人剛打招呼。\n\nRECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_dispatcher_skips_state_block_when_summary_empty():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="ok", done=False), StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(summaries=[""])
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        item = await sink.get()
        if item is None:
            break

    prefill = adapter.calls[0]["prefill"]
    assert "STATE:" not in prefill
    assert prefill == "RECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_dispatcher_instinct_error_surfaces_as_error_msg():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="ok", done=False), StreamChunk(text="", done=True)]
    )
    iv = _FakeInnerVoice(recall_text="RECALL:\n- foo\n")
    inst = _FakeInstinct(raises=RuntimeError("instinct boom"))
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        character_profile="You are Doll.",
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))

    items = []
    while True:
        item = await sink.get()
        if item is None:
            break
        items.append(item)

    assert any(isinstance(m, ErrorMsg) and "instinct boom" in m.message for m in items)
    assert len(adapter.calls) == 0   # adapter never reached
```

Run: `uv run pytest tests/test_dispatcher.py -q`
Expected: new tests fail with `TypeError: EventDispatcher.__init__() got an unexpected keyword argument 'instinct'` (or similar — ctor doesn't accept it yet). Existing tests should still pass after the kwarg-add.

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/dispatcher.py`. Add import:

```python
from dollos.instinct import Instinct
```

- [ ] Update `EventDispatcher.__init__` signature and body to accept `instinct`:

```python
def __init__(
    self,
    *,
    adapter: LLMAdapter,
    inner_voice: InnerVoice,
    instinct: Instinct,
    renderer: PromptRenderer,
    character_profile: str,
) -> None:
    self._adapter = adapter
    self._inner_voice = inner_voice
    self._instinct = instinct
    self._renderer = renderer
    self._character_profile = character_profile
    self._tasks: set[asyncio.Task[None]] = set()
    self._stopping = False
```

- [ ] Update `_handle` to call `instinct.process` between perceive and respond, and pass summary into `_respond`:

```python
async def _handle(self, raw: RawEvent) -> None:
    try:
        sink = self._sink_of(raw)
    except TypeError:
        logger.exception("no sink for raw event %r", type(raw).__name__)
        return

    try:
        doll_event = await self._perceive(raw)
        summary = await self._instinct.process(doll_event)
        await self._respond(doll_event, summary, sink)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("dispatcher _handle error")
        sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
    finally:
        sink.put_nowait(None)
```

- [ ] Update `_respond` signature and prefill construction:

```python
async def _respond(
    self,
    doll_event: DollEvent,
    summary: str,
    sink: asyncio.Queue[ServerMessage | None],
) -> None:
    recall = await self._inner_voice.recall(doll_event.perception)
    system = self._renderer.render(
        "scaffolding", character=self._character_profile
    )
    state_block = f"STATE:\n{summary}\n\n" if summary else ""
    prefill = f"{state_block}{recall}DECISION: "
    async for chunk in self._adapter.stream_completion(
        system=system,
        user=doll_event.perception,
        prefill=prefill,
    ):
        if chunk.text:
            sink.put_nowait(TextChunk(text=chunk.text))
        if chunk.done:
            break
    sink.put_nowait(TurnEnd())
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_dispatcher.py -q`
- [ ] Expected: all dispatcher tests pass (existing + 4 new).
- [ ] Run full suite to catch fallout: `uv run pytest -q`
- [ ] Expected: only `tests/test_kernel.py` and `tests/test_e2e.py` may fail (Kernel constructs `EventDispatcher` without `instinct`; fixed in Task 3).

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/dispatcher.py tests/test_dispatcher.py`
- [ ] Expected: no errors.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/dispatcher.py tests/test_dispatcher.py
git commit -m "$(cat <<'EOF'
feat(dispatcher): wire Instinct into _handle, inject STATE block into prefill

Dispatcher now requires `instinct: Instinct` at construction. Each event
calls instinct.process(doll_event) between perceive and respond; non-empty
summary is prepended to the prefill as `STATE:\n{summary}\n\n` before the
existing RECALL block. Empty summary preserves step-4 prefill shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Kernel wiring — `build_instinct()` factory

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_kernel.py`
- Modify: `tests/test_kernel_factories.py`
- Modify: `tests/test_e2e.py`

Reuse the existing small-model adapter pair (already used by `InnerVoice`) to construct `SmallModelInstinct`.

### Step 1: Inspect existing kernel tests (RED)

- [ ] Read `tests/test_kernel.py` and `tests/test_kernel_factories.py` to understand current `EventDispatcher` construction patterns. The kernel tests likely instantiate `DollOS(settings)` and probably mock or stub the network.

- [ ] Run: `uv run pytest tests/test_kernel.py tests/test_kernel_factories.py tests/test_e2e.py -q`
- [ ] Expected: failures from `EventDispatcher.__init__()` missing `instinct` kwarg.

### Step 2: Add `build_instinct` factory (GREEN)

- [ ] Edit `src/dollos/kernel.py`. Add import:

```python
from dollos.instinct import Instinct, SmallModelInstinct
```

- [ ] Add factory function near `build_inner_voice` (uses the same small-model provider config; do NOT instantiate a second `LlamaCppProvider` — re-use the one constructed for InnerVoice if structurally simple, otherwise construct a fresh one with the same settings):

```python
def build_instinct(
    settings: Settings, renderer: PromptRenderer
) -> Instinct:
    """Construct SmallModelInstinct wired to the small llama.cpp model.

    Uses the same `inner_voice` config block as InnerVoice — both are
    small-model utilities. v1 hardcodes (LlamaCppProvider, Qwen3PlainTemplate).
    """
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    adapter = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return SmallModelInstinct(adapter=adapter, renderer=renderer)
```

- [ ] Update `DollOS.__init__` to construct instinct and pass into dispatcher:

```python
def __init__(self, settings: Settings) -> None:
    self.settings = settings
    self.adapter = build_adapter(settings)
    self.renderer = PromptRenderer()
    self.memsearch = build_memsearch(settings)
    self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
    self.instinct = build_instinct(settings, self.renderer)
    self._character_profile = settings.character.profile_path.read_text()
    self.dispatcher = EventDispatcher(
        adapter=self.adapter,
        inner_voice=self.inner_voice,
        instinct=self.instinct,
        renderer=self.renderer,
        character_profile=self._character_profile,
    )
    self.server = WebSocketServer(
        host=settings.ipc.host,
        port=settings.ipc.port,
        handler=self._handle_text_input,
    )
    self._shutdown = asyncio.Event()
```

### Step 3: Update kernel tests for new wiring

- [ ] Open `tests/test_kernel.py`. For any test that constructs `EventDispatcher` directly, add `instinct=_FakeInstinct()` (or import the same fake from test_dispatcher; if not exposed, copy the small fake locally).

- [ ] Open `tests/test_e2e.py`. The existing assertions about prefill content need updating — step-4 `"RECALL:\n- user likes coffee\nDECISION: "` becomes either:
  - `"STATE:\n{whatever-fake-summary}\n\nRECALL:\n- user likes coffee\nDECISION: "` if the e2e fake instinct returns a non-empty string, OR
  - keep as-is if the e2e test uses an instinct fake returning `""`
  
  Decide by reading the test: pick whichever requires fewer assertion edits. Recommended: use `_FakeInstinct(summaries=[""])` so prefill stays identical and only the `instinct` ctor wire is new.

- [ ] Open `tests/test_kernel_factories.py`. Add a `test_build_instinct_returns_small_model_instinct`:

```python
def test_build_instinct_returns_small_model_instinct(tmp_path):
    settings = _make_settings(tmp_path)   # reuse existing helper
    renderer = PromptRenderer()
    inst = build_instinct(settings, renderer)
    assert isinstance(inst, SmallModelInstinct)
```

(Adapt `_make_settings` reference to whatever factory the file already uses.)

### Step 4: Run tests (GREEN)

- [ ] Run: `uv run pytest -q`
- [ ] Expected: all green. If e2e fails because of prefill string mismatch, adjust the assertion per the chosen approach above.

### Step 5: Lint

- [ ] Run: `uv run ruff check src/dollos/kernel.py tests/test_kernel.py tests/test_kernel_factories.py tests/test_e2e.py`
- [ ] Expected: clean.

### Step 6: Commit

- [ ] Run:

```bash
git add src/dollos/kernel.py tests/test_kernel.py tests/test_kernel_factories.py tests/test_e2e.py
git commit -m "$(cat <<'EOF'
feat(kernel): build SmallModelInstinct and wire into EventDispatcher

Adds build_instinct() factory reusing inner_voice small-model config
(LlamaCppProvider + Qwen3PlainTemplate). DollOS.__init__ constructs
self.instinct and passes it to EventDispatcher.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Manual smoke test (real models, end-to-end)

**Files:**
- No code changes
- Optional: `experiments/ws_client.py` (already exists from step 4 — reuse)

Validate STATE block appears in real big-model prompt and rolling summary works across multiple turns. This is **not** automated; it requires real llama.cpp servers running per CLAUDE.md launch instructions.

### Step 1: Start servers

- [ ] Big-model server (per CLAUDE.md):

```bash
./llama.cpp/llama-server \
    -hf unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL \
    --alias "unsloth/Qwen3.6" \
    --jinja --reasoning-format none \
    --chat-template-kwargs '{"enable_thinking": true}' \
    --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
    --ctx-size 131072 --fit on \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --flash-attn on --cont-batching --parallel 2 \
    -ngl 99 --tensor-split 1,1 \
    --batch-size 2048 --ubatch-size 512 \
    --threads 8 --keep -1 \
    --port 8001 --host 0.0.0.0 \
    > /tmp/llama-big.log 2>&1 &
```

- [ ] Small-model server (CPU, Qwen3.5-0.8B per step 4 IV revert decision):

```bash
./llama.cpp/llama-server \
    -hf unsloth/Qwen3.5-0.8B-GGUF:UD-Q4_K_XL \
    --alias "unsloth/Qwen3.5-0.8B" \
    --jinja --reasoning-format none \
    --chat-template-kwargs '{"enable_thinking": false}' \
    --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
    --ctx-size 8192 --fit on \
    --threads 8 --keep -1 \
    --port 8002 --host 0.0.0.0 \
    > /tmp/llama-small.log 2>&1 &
```

(If your `config.toml` already points at `port 8002` for `inner_voice.base_url`, this works for both InnerVoice.recall and SmallModelInstinct.)

- [ ] Verify both servers respond: `curl -s http://localhost:8001/health` and `curl -s http://localhost:8002/health`.

### Step 2: Seed memory with test fact

- [ ] Ensure `data/memory/shared/` has at least one markdown file with a clear fact, e.g. `data/memory/shared/2026-05-04.md`:

```markdown
# 2026-05-04

- 主人喜歡喝美式咖啡，不加糖。
```

### Step 3: Start DollOS

- [ ] Run from worktree root:

```bash
uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &
```

- [ ] Tail log briefly to confirm "memsearch indexed" + "ipc server listening". (Read file, do **not** pipe to `tail -f` per CLAUDE.md.)

### Step 4: Drive a 3-turn conversation via `experiments/ws_client.py`

- [ ] Run interactive mode:

```bash
uv run python experiments/ws_client.py --interactive
```

- [ ] Send these turns one by one:
  1. `我等等想喝咖啡`
  2. `那我先去燒水`
  3. `對了你還記得我喜歡什麼咖啡嗎？`

### Step 5: Inspect logs

- [ ] In `/tmp/dollos.log`, find the prefill string for each big-model call. Expected pattern (turn 1, summary still empty so no STATE):

```
prefill: "RECALL:\n- 主人喜歡喝美式咖啡...\nDECISION: "
```

- [ ] Turn 2 expected:

```
prefill: "STATE:\n主人提到等等想喝咖啡。\n\nRECALL:\n...\nDECISION: "
```

(Exact wording varies; key check: `STATE:` block present and reflects turn 1.)

- [ ] Turn 3 expected: STATE block has rolled — should mention coffee + boiling water continuity.

- [ ] Big model's response in turn 3 should reference the coffee preference from RECALL **and** the conversational continuity from STATE.

### Step 6: Document outcomes

- [ ] If summary is good → record observation, move to Task 5.
- [ ] If summary is wrong (e.g. small model roleplays as Doll, outputs bullets, returns empty repeatedly) → tighten `iv_summary.jinja` prompt, restart DollOS, re-test. Do not loosen the test suite to match a broken summary.
- [ ] If small-model latency is unacceptable (>5s on CPU per turn) → note for follow-up; do not switch models in this plan.

### Step 7: Stop servers

- [ ] `pkill -f llama-server`
- [ ] `pkill -f "python -m dollos"`

### Step 8: Commit smoke notes (optional)

- [ ] If you adjusted `iv_summary.jinja` based on real output, commit:

```bash
git add src/dollos/prompts/templates/iv_summary.jinja
git commit -m "tune(iv_summary): tighten prompt based on smoke test"
```

---

## Task 5: Roadmap + spec sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

### Step 1: Update `docs/roadmap.md`

- [ ] In the `## 已完成` table, add a row:

```markdown
| Roadmap step 5 — Inner Voice (minimal, summary-only) | Merged |
```

- [ ] In the Roadmap section, mark step 5 as Merged (similar to step 4) and update the "下一個" pointer to step 6 (Tool calling).

### Step 2: Update `CLAUDE.md`

- [ ] In the "已完成" plan table, add:

```markdown
| Roadmap step 5 — Inner Voice (minimal, summary-only) | Merged |
```

- [ ] Replace the "下一個" paragraph with a step-6 brief (Tool calling — Tool ABC + ToolRegistry + first tools say/note_memory/recall).

### Step 3: Commit

- [ ] Run:

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: mark roadmap step 5 (Inner Voice minimal) merged, point to step 6

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: Final verification

- [ ] Run full suite once more: `uv run pytest -q`
- [ ] Expected: all green.
- [ ] Run lint: `uv run ruff check`
- [ ] Expected: clean.

---

## Done definition

- [ ] All four tasks committed on branch `inner-voice-minimal`.
- [ ] `uv run pytest -q` green.
- [ ] `uv run ruff check` clean.
- [ ] Manual smoke test (Task 4) shows STATE block appears in real big-model prefill and rolls across turns.
- [ ] Roadmap + CLAUDE.md updated.
- [ ] Ready for `superpowers:finishing-a-development-branch` to merge to `main`.
