# VoM Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `InnerVoice.recall()` into the IPC handler so DollOS prefills the big model's `<think>` block with a memory-grounded RECALL block before responding to user input — completing roadmap step 3 (VoM).

**Architecture:** Rebased `plan-4-inner-voice` becomes the base for a new `vom-integration` worktree. PromptRenderer gains `render_blocks()` for multi-block jinja templates. `iv_recall.jinja` replaces the hardcoded `INNER_VOICE_SYSTEM_PROMPT` constant. `InnerVoice` takes a `PromptRenderer` dependency. Kernel constructs `Memory` + `InnerVoice` at startup, then on each `TextInput` calls `await inner_voice.recall(text)` and feeds the result into prefill `<think>\n{recall}GOAL: `.

**Tech Stack:** Python 3.12+, `asyncio`, `pytest` + `pytest-asyncio`, `jinja2`, `pydantic`, `sqlite-vec`, `httpx` (via existing LlamaCppProvider).

**Spec:** `docs/superpowers/specs/2026-05-03-vom-integration-design.md`

---

## Task 0: Rebase `plan-4-inner-voice` and create worktree

**Files:**
- Branch: `plan-4-inner-voice` (rebased onto current `main`)
- Worktree: `.worktrees/vom-integration` on new branch `vom-integration`

This is prep, not a numbered TDD task. Run from the main repo (not the worktree).

- [ ] **Step 1: Confirm clean main + identify divergence**

```bash
git status                           # must be clean
git fetch origin
git log --oneline plan-4-inner-voice ^main
```

Expected: 5 commits listed (`5ea5aaf`, `993df51`, `2a5586a`, `f57553f`, `1c6672a` — but SHAs may differ, the message prefixes are stable).

- [ ] **Step 2: Rebase plan-4 onto current main**

```bash
git checkout plan-4-inner-voice
git rebase main
```

Expected conflicts:
- `src/dollos/daemon.py` was renamed to `src/dollos/kernel.py` in step 2 — rebase will replay plan-4's `build_inner_voice` factory addition; resolve by adding it to `kernel.py` instead. Add the imports it needs (`InnerVoice`, `Qwen3PlainTemplate`).
- `src/dollos/config.py` — plan-4 added `[inner_voice]` field to `Settings`; step 2 added `[character]` field. Both should coexist after merge — keep both.
- `config.example.toml` — plan-4 added `[inner_voice]` section; step 2 added `[character]` section. Keep both.
- `src/dollos/__main__.py` references — likely none, but if `daemon.py` import was added, change to `kernel.py`.

- [ ] **Step 3: Verify pytest green after rebase**

```bash
uv run pytest -q
```

Expected: all green (existing plan-4 tests + step-2 tests). If any test references `daemon.py` directly (other than e2e which already imports `kernel`), update to `kernel`.

- [ ] **Step 4: Push rebased plan-4 (force, since rebased) — optional but tidy**

```bash
git push --force-with-lease origin plan-4-inner-voice
```

(Skip if you don't push branches to remote; this is local-only OK.)

- [ ] **Step 5: Create worktree from rebased plan-4**

```bash
git worktree add .worktrees/vom-integration -b vom-integration plan-4-inner-voice
cd .worktrees/vom-integration
uv sync
uv run pytest -q                     # confirm green in worktree too
```

Expected: green.

All subsequent tasks happen in `.worktrees/vom-integration`.

---

## Task 1: `PromptRenderer.render_blocks()`

**Files:**
- Modify: `src/dollos/prompts/renderer.py`
- Test: `tests/test_prompt_renderer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompt_renderer.py`:

```python
def test_render_blocks_returns_dict_with_each_block():
    """A template with multiple {% block %} sections returns a dict keyed by name."""
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="hi", item="apple")
    assert set(blocks.keys()) == {"system", "user"}
    assert blocks["system"] == "hi from system"
    assert blocks["user"] == "user wants apple"


def test_render_blocks_strips_per_block_whitespace():
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="hi", item="apple")
    # No leading/trailing whitespace on any block value
    for v in blocks.values():
        assert v == v.strip()


def test_render_blocks_substitutes_ctx_into_each_block():
    renderer = PromptRenderer()
    blocks = renderer.render_blocks("_test_blocks_fixture", greeting="yo", item="banana")
    assert "yo" in blocks["system"]
    assert "banana" in blocks["user"]


def test_render_blocks_unknown_template_raises():
    renderer = PromptRenderer()
    with pytest.raises(TemplateNotFound):
        renderer.render_blocks("does_not_exist")
```

Also create the fixture template: `src/dollos/prompts/templates/_test_blocks_fixture.jinja`:

```jinja
{%- block system -%}
{{ greeting }} from system
{%- endblock -%}

{%- block user -%}
user wants {{ item }}
{%- endblock -%}
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_prompt_renderer.py -v
```

Expected: 4 new tests fail with `AttributeError: 'PromptRenderer' object has no attribute 'render_blocks'`.

- [ ] **Step 3: Implement `render_blocks()`**

In `src/dollos/prompts/renderer.py`, add to the `PromptRenderer` class:

```python
    def render_blocks(self, template_name: str, **ctx: object) -> dict[str, str]:
        """Render every `{% block %}` section in the template, return as dict.

        Each block is rendered with the same ctx; result keyed by block name.
        Per-block trailing/leading whitespace is stripped. Useful when one
        template defines multiple related prompt segments (e.g. system + user)
        that should evolve together.

        Raises jinja2.TemplateNotFound if the template isn't found.
        """
        template = self._env.get_template(f"{template_name}.jinja")
        ctx_obj = template.new_context(ctx)
        return {
            name: "".join(block(ctx_obj)).strip()
            for name, block in template.blocks.items()
        }
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_prompt_renderer.py -v
```

Expected: all 9 tests pass (5 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/prompts/renderer.py \
        src/dollos/prompts/templates/_test_blocks_fixture.jinja \
        tests/test_prompt_renderer.py
git commit -m "feat(prompts): PromptRenderer.render_blocks() for multi-block templates"
```

---

## Task 2: `iv_recall.jinja` template

**Files:**
- Create: `src/dollos/prompts/templates/iv_recall.jinja`
- Test: covered indirectly by Task 3's InnerVoice tests; no direct assertion on file contents.

- [ ] **Step 1: Create the template**

`src/dollos/prompts/templates/iv_recall.jinja`:

```jinja
{%- block system -%}
You are Doll's memory recall helper. Read the query and candidate facts from memory, output ONLY the facts relevant to the query as bullets.

Rules:
- One bullet per relevant fact: "- <fact in concise prose>"
- If a candidate is irrelevant, skip it
- Do NOT add facts not in candidates
- Do NOT speculate or fill gaps
- Output bullets only. Don't repeat the query, don't add header.
- If no candidates are relevant, output a single line: (no relevant memories)
{%- endblock -%}

{%- block user -%}
Query: {{ query }}

Candidates:
{{ candidates }}
{%- endblock -%}
```

- [ ] **Step 2: Smoke-check the template loads**

Verify it parses by running the existing renderer test suite (will load PackageLoader templates):

```bash
uv run pytest tests/test_prompt_renderer.py -q
```

Expected: green (no syntax errors in the new template would cause a `TemplateSyntaxError` somewhere).

- [ ] **Step 3: Commit**

```bash
git add src/dollos/prompts/templates/iv_recall.jinja
git commit -m "feat(prompts): iv_recall.jinja for InnerVoice recall prompt"
```

---

## Task 3: Migrate `InnerVoice` to use `PromptRenderer`

**Files:**
- Modify: `src/dollos/inner_voice.py`
- Modify: `tests/test_inner_voice.py`

- [ ] **Step 1: Update tests to reflect new InnerVoice contract**

Replace the contents of `tests/test_inner_voice.py` with:

```python
"""Tests for InnerVoice.recall() behavior."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dollos.inner_voice import InnerVoice
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.memory.embedder import StubEmbedder
from dollos.memory.store import Memory
from dollos.prompts import PromptRenderer


class _FakeLLMAdapter(LLMAdapter):
    """Yield canned chunks. Captures last call args for assertions."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.last_prefill: str | None = None
        self.call_count = 0

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        self.last_system = system
        self.last_user = user
        self.last_prefill = prefill
        self.call_count += 1
        if self._response:
            yield StreamChunk(text=self._response, done=False)
        yield StreamChunk(text="", done=True)


def _make_iv(memory: Memory, llm: LLMAdapter) -> InnerVoice:
    return InnerVoice(memory=memory, llm=llm, renderer=PromptRenderer())


@pytest.mark.asyncio
async def test_recall_with_facts_returns_recall_block(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("the sky is blue")
    await mem.write("user likes coffee")

    fake_llm = _FakeLLMAdapter(response="- user likes coffee\n- the sky is blue")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("what about coffee")

    assert block.startswith("RECALL:\n")
    assert "user likes coffee" in block
    assert block.endswith("\n")
    await mem.close()


@pytest.mark.asyncio
async def test_recall_system_prompt_comes_from_iv_recall_template(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("a fact")

    fake_llm = _FakeLLMAdapter(response="- a fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("query")

    # System prompt is rendered from iv_recall.jinja's `system` block.
    # We assert on a stable substring of the prompt rather than full equality
    # so prompt copy-edits don't break the test.
    assert fake_llm.last_system is not None
    assert "memory recall helper" in fake_llm.last_system
    assert "(no relevant memories)" in fake_llm.last_system
    assert fake_llm.call_count == 1
    await mem.close()


@pytest.mark.asyncio
async def test_recall_user_block_includes_query_and_candidates(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("fact alpha")
    await mem.write("fact beta")

    fake_llm = _FakeLLMAdapter(response="- fact alpha")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("alpha")

    user_block = fake_llm.last_user
    assert user_block is not None
    assert "Query: alpha" in user_block
    assert "Candidates:" in user_block
    assert "1." in user_block
    assert "2." in user_block
    assert "fact alpha" in user_block
    assert "fact beta" in user_block
    await mem.close()


@pytest.mark.asyncio
async def test_recall_uses_empty_prefill(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("fact")

    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("q")

    assert fake_llm.last_prefill == ""
    await mem.close()


@pytest.mark.asyncio
async def test_recall_empty_memory_returns_no_relevant_block(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fake_llm = _FakeLLMAdapter(response="should not be called")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("anything")

    assert block == "RECALL:\n(no relevant memories)\n"
    assert fake_llm.call_count == 0
    await mem.close()


@pytest.mark.asyncio
async def test_recall_strips_whitespace_from_model_output(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("fact")

    fake_llm = _FakeLLMAdapter(response="  \n- fact\n  \n")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("q")

    assert block == "RECALL:\n- fact\n"
    await mem.close()


@pytest.mark.asyncio
async def test_recall_passes_character_id_to_memory(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    await mem.write("shared fact")
    await mem.write("private gura fact", character_id="gura")

    fake_llm = _FakeLLMAdapter(response="- private gura fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("anything", character_id="gura")

    user_block = fake_llm.last_user
    assert user_block is not None
    # Both shared and gura facts should appear since gura scope unions shared.
    assert "shared fact" in user_block
    assert "private gura fact" in user_block
    await mem.close()
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_inner_voice.py -v
```

Expected: most tests fail with `TypeError: __init__() got an unexpected keyword argument 'renderer'` (existing InnerVoice doesn't take `renderer`). The empty-memory test still passes (early return), but most others fail.

- [ ] **Step 3: Update `InnerVoice` to take `PromptRenderer`**

Replace `src/dollos/inner_voice.py` with:

```python
"""InnerVoice — small-model VoM RECALL block synthesizer.

InnerVoice is the entry point to DollOS's signature feature: read a query,
pull relevant facts from Memory, and synthesize a concise RECALL block via
a small LLM. Caller (kernel) embeds the resulting string into the prefill
for Doll's turn.

This module is pure utility — it doesn't process events, write memory,
manage state, or know about characters beyond passing character_id through
to Memory.search().

Prompt content lives in `dollos/prompts/templates/iv_recall.jinja` (system
+ user blocks).
"""

from dollos.llm.adapter import LLMAdapter
from dollos.memory.store import Memory
from dollos.prompts import PromptRenderer


class InnerVoice:
    """Synthesize VoM RECALL blocks from memory using a small LLM."""

    def __init__(
        self,
        memory: Memory,
        llm: LLMAdapter,
        renderer: PromptRenderer,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._renderer = renderer

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
    ) -> str:
        """Return a RECALL block string for the given query.

        Always starts with "RECALL:\\n" so the caller can embed verbatim
        into a Doll prefill.

        If memory has no candidates, returns
        "RECALL:\\n(no relevant memories)\\n" without invoking the LLM.
        """
        results = await self._memory.search(
            query,
            character_id=character_id,
            top_k=top_k,
            mode="hybrid",
        )
        if not results:
            return "RECALL:\n(no relevant memories)\n"

        candidates = "\n".join(
            f"{i + 1}. {r.fact.text}" for i, r in enumerate(results)
        )
        blocks = self._renderer.render_blocks(
            "iv_recall",
            query=query,
            candidates=candidates,
        )

        chunks: list[str] = []
        async for chunk in self._llm.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        body = "".join(chunks).strip()
        return f"RECALL:\n{body}\n"
```

The `INNER_VOICE_SYSTEM_PROMPT` module-level constant is gone.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_inner_voice.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest -q
```

Expected: all green. If anything else imported `INNER_VOICE_SYSTEM_PROMPT`, fix the import (likely nothing — it was internal).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/inner_voice.py tests/test_inner_voice.py
git commit -m "refactor(inner_voice): render system+user prompts via iv_recall.jinja"
```

---

## Task 4: Wire `Memory` + `InnerVoice` into kernel and call recall before big LLM

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_e2e.py`
- Test: extend e2e to assert prefill format

This task does several wiring changes together because they form one coherent unit (kernel can't construct InnerVoice without Memory; can't update factory signature without updating callers; e2e test exercises the full chain).

- [ ] **Step 1: Update `build_inner_voice` signature in kernel and add `build_memory`**

In `src/dollos/kernel.py`:

a. Update imports — add:

```python
from dollos.inner_voice import InnerVoice
from dollos.llm.templates import Qwen3PlainTemplate
from dollos.memory.embedder_llamacpp import LlamaCppEmbedder
from dollos.memory.store import Memory
```

(Some of these already exist from the rebased plan-4 work. Keep existing imports; add only those missing.)

b. Add `build_memory()` factory near the other `build_*` factories:

```python
def build_memory(settings: Settings) -> Memory:
    if settings.embedder.backend == "llamacpp":
        embedder = LlamaCppEmbedder(
            base_url=settings.embedder.base_url,
            model_id=settings.embedder.model_id,
            timeout_s=settings.embedder.timeout_s,
        )
    else:
        raise ValueError(f"unknown embedder backend: {settings.embedder.backend}")
    return Memory(db_path=settings.memory.db_path, embedder=embedder)
```

c. Update `build_inner_voice()` to take a `PromptRenderer`:

```python
def build_inner_voice(
    settings: Settings, memory: Memory, renderer: PromptRenderer
) -> InnerVoice:
    """Construct InnerVoice wired to a small llama.cpp model."""
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    template = Qwen3PlainTemplate()
    llm = ComposedLLMAdapter(provider=provider, template=template)
    return InnerVoice(memory=memory, llm=llm, renderer=renderer)
```

- [ ] **Step 2: Wire kernel constructor to use `Memory` + `InnerVoice` and update handler**

Replace the `DollOS` class body with:

```python
class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memory = build_memory(settings)
        self.inner_voice = build_inner_voice(settings, self.memory, self.renderer)
        self._character_profile = settings.character.profile_path.read_text()
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            system = self.renderer.render(
                "scaffolding",
                character=self._character_profile,
            )
            recall = await self.inner_voice.recall(msg.text)
            prefill = f"<think>\n{recall}GOAL: "
            async for chunk in self.adapter.stream_completion(
                system=system,
                user=msg.text,
                prefill=prefill,
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")

    async def run(self) -> None:
        await self.memory.initialize()
        await self.server.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)
        try:
            await self._shutdown.wait()
        finally:
            await self.server.stop()
            await self.memory.close()
```

Note `run()` now `await self.memory.initialize()` before starting the server, and `await self.memory.close()` on shutdown. This is required — Memory needs to open SQLite + load sqlite-vec before any recall can run.

- [ ] **Step 3: Update `tests/test_e2e.py` to mock recall and verify prefill format**

The existing e2e test (`test_full_round_trip_with_mocked_llamacpp`) already captures the big-model request payload via `captured_requests`. Updates needed:
1. Add `InnerVoiceConfig` to imports + Settings construction.
2. Switch memory db path to `tmp_path` for isolation.
3. Monkeypatch `InnerVoice.recall` to a fixed return (avoid wiring up a third llama-server mock; recall behavior is already covered by `test_inner_voice.py`).
4. Monkeypatch `Memory.initialize` and `Memory.close` to no-ops (the LlamaCpp embedder would otherwise try a real probe call on `initialize`).
5. Add an assertion that the captured `prompt` includes the new prefill substring.

Replace `tests/test_e2e.py` entirely with:

```python
"""End-to-end test: WebSocket client → daemon → mocked llama.cpp → response."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
import websockets

from dollos.config import (
    CharacterConfig,
    EmbedderConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)
from dollos.kernel import DollOS


@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    character_path = tmp_path / "test_character.jinja"
    character_path.write_text("You are Gura, a 9000-year-old shark.")

    settings = Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="mock",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        memory=MemoryConfig(db_path=tmp_path / "memory.db"),
        embedder=EmbedderConfig(
            backend="llamacpp",
            base_url="http://test.local:8002",
            model_id="test-emb",
        ),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=5.0,
        ),
        character=CharacterConfig(
            profile_path=character_path,
        ),
    )

    # Stub InnerVoice.recall — recall behavior is covered by test_inner_voice.py;
    # here we only care that kernel feeds its output into prefill correctly.
    async def _stub_recall(self, query, **kwargs):
        return "RECALL:\n- user likes coffee\n"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)

    # Skip Memory.initialize/close — no real embedder endpoint in this test
    # (LlamaCppEmbedder.initialize would do a real /embedding probe call).
    async def _noop(self):
        return None

    monkeypatch.setattr("dollos.memory.store.Memory.initialize", _noop)
    monkeypatch.setattr("dollos.memory.store.Memory.close", _noop)

    dollos = DollOS(settings)

    sse_body = (
        'data: {"content": "Hi", "stop": false}\n\n'
        'data: {"content": " there", "stop": false}\n\n'
        'data: {"content": "", "stop": true}\n\n'
    )

    captured_requests: list[dict] = []

    def _capture_and_respond(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=_capture_and_respond)

        await dollos.server.start()
        try:
            port = dollos.server.port
            assert port is not None

            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"type": "text_input", "text": "Hello"}))

                received: list[dict] = []
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    parsed = json.loads(raw)
                    received.append(parsed)
                    if parsed["type"] == "turn_end":
                        break

            text_chunks = [msg for msg in received if msg["type"] == "text_chunk"]
            assert "".join(c["text"] for c in text_chunks) == "Hi there"
            assert received[-1]["type"] == "turn_end"
            assert len(captured_requests) == 1
            prompt = captured_requests[0]["prompt"]
            assert "You are Gura, a 9000-year-old shark." in prompt
            # New: VoM prefill must reach the big model
            assert "<think>\nRECALL:\n- user likes coffee\nGOAL: " in prompt
        finally:
            await dollos.server.stop()
```

Note: the e2e test does NOT call `dollos.run()`, so it never hits `await self.memory.initialize()` directly — but `DollOS(settings)` constructor doesn't call `initialize` either. The monkeypatch on `Memory.initialize`/`close` is defensive in case future refactors move initialization into the constructor.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest -q
```

Expected: all green. Common failures + fixes:
- `pydantic.ValidationError: extra fields not permitted` on Settings — confirm rebased plan-4 added `inner_voice` field to `Settings`. If missing, the rebase resolution dropped it; re-add per `2026-05-02-inner-voice-utility-design.md` §7.
- `Memory not initialized` in some non-e2e test — check if any other test constructs `DollOS()` without monkeypatching `initialize`. Only e2e should be affected.

- [ ] **Step 5: Commit**

```bash
git add src/dollos/kernel.py tests/test_e2e.py
git commit -m "feat(kernel): call inner_voice.recall before big LLM, prefill <think>...GOAL:"
```

---

## Task 5: Manual smoke test + roadmap update

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

No automated tests in this task — it's a real-environment validation that the wiring actually produces a coherent VoM response. The user will run this; document the steps so it's reproducible.

- [ ] **Step 1: Document smoke-test procedure inline in this plan (already below) and run it**

Procedure:

1. Start the embedding server (small embedding model):
   ```bash
   ./llama.cpp/llama-server -hf <embed-model-gguf> --embedding --port 8002 --host 0.0.0.0
   ```
2. Start the small Inner Voice model server:
   ```bash
   ./llama.cpp/llama-server -hf unsloth/Qwen3-1.7B-Instruct-GGUF:Q5_K_M --port 8003 --host 0.0.0.0
   ```
3. Start the big Doll model server (per CLAUDE.md "Self-host llama.cpp big model" command, port 8001).
4. Configure `config.toml` with `[memory]`, `[embedder]`, `[inner_voice]`, `[llm]`, `[character]` sections all pointing at the right ports.
5. Start DollOS: `uv run python -m dollos --config config.toml`.
6. Use a small WS client script (or `experiments/`) to:
   a. First write a fact directly via `Memory` (one-off Python REPL or a tiny script):
      ```python
      from dollos.config import load_settings
      from dollos.kernel import build_memory
      import asyncio

      async def main():
          s = load_settings(Path("config.toml"))
          m = build_memory(s)
          await m.initialize()
          await m.write("the user's favorite drink is oat milk latte")
          await m.close()

      asyncio.run(main())
      ```
   b. Send a `TextInput` message via WS asking "what does the user like to drink?" — observe Doll's response references oat milk latte.
7. Inspect daemon logs to confirm: `recall` was called, returned a non-empty `RECALL:` block, and the prefill into the big model contained `<think>\nRECALL:\n- ...\nGOAL: `.

Acceptance:
- [ ] Doll's response references the planted fact (or related concept) in its first 2 sentences.
- [ ] Log shows `inner_voice.recall` activity (you may need to add a single `logger.debug("recall returned: %s", recall[:100])` in `_handle_text_input` for visibility — fine to leave it in).
- [ ] No exceptions in the daemon log during the round-trip.

If any step fails, debug. Don't proceed to roadmap update until this passes.

- [ ] **Step 2: Update `docs/roadmap.md`**

Mark step 3 done. Replace the "已完成" table to add a new row:

```markdown
| Roadmap step 3 — VoM | Merged |
```

And update the "下一個" section to point at step 4 (event loop):

```markdown
### 下一個

**Roadmap step 4 — 跑通 event loop**：Event ABC + UserTextEvent + asyncio.Queue + DollLoop 主迴圈。IPC handler push event 進 queue，DollLoop pop event 跑 recall + LLM call + stream。完整 roadmap：`docs/roadmap.md`。
```

(Adjust wording to match the existing tone of the file.)

- [ ] **Step 3: Update `CLAUDE.md`**

In the "已完成" section under Implementation Plans, add:

```markdown
| Roadmap step 3 — VoM | Merged |
```

In the "下一個" section, replace step 3 description with step 4:

```markdown
**Roadmap step 4 — Event Loop**：Event ABC + asyncio.Queue + DollLoop 主迴圈，把 IPC handler 的同步路徑改成 event-driven。完整 roadmap：`docs/roadmap.md`。
```

- [ ] **Step 4: Commit roadmap + CLAUDE.md updates**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: mark roadmap step 3 (VoM) merged, point to step 4 event loop next"
```

- [ ] **Step 5: Hand off to `superpowers:finishing-a-development-branch`**

The branch `vom-integration` (built on rebased `plan-4-inner-voice`) is ready to merge to main. Run the finishing skill from the worktree to finalize:

```bash
# from .worktrees/vom-integration
git log --oneline main..HEAD             # review the chain (5 plan-4 commits + ~5 step-3 commits)
uv run pytest -q                         # final green check
```

Then invoke `superpowers:finishing-a-development-branch`.

---

## Summary of commits expected

After Task 0 (rebase, no new commits) and Tasks 1–5, the `vom-integration` branch should look like:

```
[plan-4 rebased commits, 5 of them]
feat(prompts): PromptRenderer.render_blocks() for multi-block templates
feat(prompts): iv_recall.jinja for InnerVoice recall prompt
refactor(inner_voice): render system+user prompts via iv_recall.jinja
feat(kernel): call inner_voice.recall before big LLM, prefill <think>...GOAL:
docs: mark roadmap step 3 (VoM) merged, point to step 4 event loop next
```

Total: 5 new commits on top of 5 rebased plan-4 commits = 10 commits going into main.
