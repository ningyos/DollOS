# LLM Provider / Template 解耦 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Plan 1's `LlamaCppAdapter` into two orthogonal abstractions — `Provider` (HTTP transport) and `PromptTemplate` (model-specific formatting) — without changing daemon's external behavior.

**Architecture:** Three new files under `src/dollos/llm/` introduce ABCs (`Provider`, `PromptTemplate`) plus their first concrete impls (`LlamaCppProvider`, `Qwen3ThinkingTemplate`) and a `ComposedLLMAdapter` that combines them to satisfy the existing `LLMAdapter` interface. `LLMConfig` schema renames `backend` → `provider` and adds `template`. Existing `LlamaCppAdapter` deleted at the end. Each task ends with a green test suite.

**Tech Stack:**
- Python 3.13+, existing project (no new deps)
- pydantic v2 for config (already in)
- httpx + websockets + sqlite-vec already in `pyproject.toml`
- pytest + respx for tests

**Spec reference:** `docs/superpowers/specs/2026-05-02-llm-provider-template-design.md`

---

## File Structure

After this plan, `src/dollos/llm/` looks like:

```
src/dollos/llm/
├── __init__.py        # MODIFY: drop LlamaCppAdapter export, add new symbols
├── adapter.py         # UNCHANGED: LLMAdapter ABC + StreamChunk
├── transport.py       # NEW: Provider ABC + LlamaCppProvider
├── templates.py       # NEW: PromptTemplate ABC + Qwen3ThinkingTemplate
├── composed.py        # NEW: ComposedLLMAdapter
└── llamacpp.py        # DELETED at the end
```

Tests:

```
tests/
├── test_llm_transport.py      # NEW: LlamaCppProvider HTTP/SSE behavior
├── test_llm_templates.py      # NEW: Qwen3ThinkingTemplate render output
├── test_llm_composed.py       # NEW: ComposedLLMAdapter wiring
├── test_llm_llamacpp.py       # DELETED at the end
├── test_config.py             # MODIFY: existing tests use new field names
└── test_e2e.py                # MODIFY: LLMConfig construction uses new field names
```

Other files modified along the way:
- `src/dollos/config.py` — `LLMConfig` schema changes (`backend` → `provider`, add `template`)
- `src/dollos/daemon.py` — `build_adapter()` returns `ComposedLLMAdapter`
- `config.example.toml` — example uses new field names

---

## Worktree Setup

Before any task. Run from main repo root.

```bash
cd /home/progcat/Projects/DollOS
git worktree add .worktrees/llm-provider-template -b feature/llm-provider-template
cd .worktrees/llm-provider-template
uv sync
uv run pytest -v 2>&1 | tail -3
```

Expected baseline: **56 passed** (15 from Plan 1 + 41 from Plan 2).

All subsequent task commands run in `/home/progcat/Projects/DollOS/.worktrees/llm-provider-template/` unless otherwise noted.

---

## Task 1: Provider ABC + LlamaCppProvider

**Files:**
- Create: `src/dollos/llm/transport.py`
- Create: `tests/test_llm_transport.py`

This task pulls the HTTP/SSE half of `LlamaCppAdapter` into a new `Provider` abstraction. `LlamaCppAdapter` itself is left untouched (will be deleted in Task 7 after we've migrated the world).

- [ ] **Step 1: Write the failing test `tests/test_llm_transport.py`**

```python
"""Tests for Provider ABC + LlamaCppProvider."""

import json

import httpx
import pytest
import respx

from dollos.llm.transport import LlamaCppProvider, Provider


def test_provider_is_abstract():
    with pytest.raises(TypeError):
        Provider()  # type: ignore[abstract]


def test_llamacpp_provider_supports_prefill_is_true():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)
    assert provider.supports_prefill is True


@pytest.mark.asyncio
async def test_llamacpp_provider_streams_chunks_until_done():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    sse_body = (
        'data: {"content": "Hello", "stop": false}\n\n'
        'data: {"content": " world", "stop": false}\n\n'
        'data: {"content": "", "stop": true}\n\n'
    )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body,
            )
        )

        chunks = []
        async for chunk in provider.stream(
            prompt="hello prompt",
            stop=None,
            max_tokens=128,
        ):
            chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].text == "Hello"
    assert chunks[0].done is False
    assert chunks[1].text == " world"
    assert chunks[1].done is False
    assert chunks[2].done is True


@pytest.mark.asyncio
async def test_llamacpp_provider_forwards_prompt_verbatim():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(
            prompt="THE EXACT PROMPT STRING",
            stop=None,
            max_tokens=128,
        ):
            pass

    # Provider should NOT mutate the prompt string at all.
    assert captured["body"]["prompt"] == "THE EXACT PROMPT STRING"


@pytest.mark.asyncio
async def test_llamacpp_provider_forwards_stop_and_max_tokens():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(
            prompt="x",
            stop=["<|im_end|>"],
            max_tokens=512,
        ):
            pass

    assert captured["body"]["stop"] == ["<|im_end|>"]
    assert captured["body"]["n_predict"] == 512
    assert captured["body"]["stream"] is True
    assert captured["body"]["cache_prompt"] is True


@pytest.mark.asyncio
async def test_llamacpp_provider_default_stop_when_none_passed():
    provider = LlamaCppProvider(base_url="http://test.local:8001", timeout_s=5.0)

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"content": "", "stop": true}\n\n',
        )

    with respx.mock(base_url="http://test.local:8001") as m:
        m.post("/completion").mock(side_effect=capture)

        async for _ in provider.stream(prompt="x", stop=None, max_tokens=128):
            pass

    # Acknowledged tech-debt (spec §10 Open Questions): default stop is
    # ChatML-flavored `<|im_end|>` even though stop is conceptually
    # template's concern. Future plans will revisit.
    assert captured["body"]["stop"] == ["<|im_end|>"]
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_transport.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.llm.transport'`.

- [ ] **Step 3: Write `src/dollos/llm/transport.py`**

```python
"""LLM transport — HTTP / endpoint conventions / response parsing.

A Provider talks to a specific LLM server (llama.cpp, vLLM, OpenAI-compat,
Anthropic, ...) and yields StreamChunk objects. It takes a fully-rendered
prompt string; prompt formatting is PromptTemplate's job.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from dollos.llm.adapter import StreamChunk

logger = logging.getLogger(__name__)


class Provider(ABC):
    """Abstract LLM transport."""

    @property
    @abstractmethod
    def supports_prefill(self) -> bool:
        """True iff this provider's endpoint can take an open assistant
        turn (i.e. the caller can give a partial assistant message and have
        the model continue from there). Critical for VoM."""

    @abstractmethod
    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        """Stream tokens. Caller owns prompt formatting."""
        ...


class LlamaCppProvider(Provider):
    """POST /completion to a llama-server with SSE streaming."""

    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def supports_prefill(self) -> bool:
        return True  # llama.cpp /completion always supports prefill via raw prompt

    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        body = {
            "prompt": prompt,
            "stream": True,
            "n_predict": max_tokens,
            # Default ChatML stop kept here for v1 (see spec §10 Open Question).
            # Will be moved to PromptTemplate when a non-ChatML template lands.
            "stop": stop if stop is not None else ["<|im_end|>"],
            "cache_prompt": True,
        }
        url = f"{self._base_url}/completion"
        timeout = httpx.Timeout(self._timeout_s, connect=5.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("non-JSON SSE line: %r", payload)
                        continue
                    yield StreamChunk(
                        text=data.get("content", ""),
                        done=bool(data.get("stop", False)),
                    )
                    if data.get("stop"):
                        return
```

- [ ] **Step 4: Run tests, verify all pass**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_transport.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 62 passed (56 prior + 6 new). The existing `tests/test_llm_llamacpp.py` is untouched and still passes.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/llm/transport.py tests/test_llm_transport.py
git commit -m "feat(llm): Provider ABC + LlamaCppProvider transport"
```

---

## Task 2: PromptTemplate ABC + Qwen3ThinkingTemplate

**Files:**
- Create: `src/dollos/llm/templates.py`
- Create: `tests/test_llm_templates.py`

- [ ] **Step 1: Write the failing test `tests/test_llm_templates.py`**

```python
"""Tests for PromptTemplate ABC + Qwen3ThinkingTemplate."""

import pytest

from dollos.llm.templates import PromptTemplate, Qwen3ThinkingTemplate


def test_template_is_abstract():
    with pytest.raises(TypeError):
        PromptTemplate()  # type: ignore[abstract]


def test_qwen3_thinking_renders_chatml_envelope():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="SYS", user="USR", prefill="")

    assert "<|im_start|>system\nSYS\n<|im_end|>" in out
    assert "<|im_start|>user\nUSR\n<|im_end|>" in out
    assert "<|im_start|>assistant\n<think>\n" in out


def test_qwen3_thinking_appends_prefill_after_think_marker():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="s", user="u", prefill="RECALL: x\nGOAL: ")
    assert out.endswith("<think>\nRECALL: x\nGOAL: ")


def test_qwen3_thinking_empty_prefill_ends_with_newline_after_think():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="s", user="u", prefill="")
    # When prefill is empty the renderer appends nothing extra, so the
    # prompt ends with the assistant turn opener: "<think>\n"
    assert out.endswith("<think>\n")


def test_qwen3_thinking_preserves_special_chars_in_inputs():
    tpl = Qwen3ThinkingTemplate()
    out = tpl.render(system="line1\nline2", user="<tag>", prefill="")
    assert "line1\nline2" in out
    assert "<tag>" in out
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_templates.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.llm.templates'`.

- [ ] **Step 3: Write `src/dollos/llm/templates.py`**

```python
"""PromptTemplate — model-family-specific prompt rendering."""

from abc import ABC, abstractmethod


class PromptTemplate(ABC):
    """Render a (system, user, prefill) tuple into the single prompt string
    the model expects.

    For "server-applied" templates (e.g. Anthropic / OpenAI chat completions
    where the API takes messages instead of a raw prompt), a concrete
    PromptTemplate may be a no-op stub and the corresponding Provider would
    talk in messages directly. Plan 3 v1 doesn't ship such a Provider, but
    the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
    ) -> str:
        ...


class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens the <think> block inside the assistant turn so prefill content
    goes inside the thinking block. This matches the Plan 1 review decision
    to optimize for Qwen3.6-thinking models (see grammar_injection_techreport
    §2.3 for the prefill technique).
    """

    def render(self, *, system: str, user: str, prefill: str) -> str:
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
```

- [ ] **Step 4: Run tests, verify all pass**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_templates.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 67 passed (62 + 5).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_templates.py
git commit -m "feat(llm): PromptTemplate ABC + Qwen3ThinkingTemplate"
```

---

## Task 3: ComposedLLMAdapter

**Files:**
- Create: `src/dollos/llm/composed.py`
- Create: `tests/test_llm_composed.py`

- [ ] **Step 1: Write the failing test `tests/test_llm_composed.py`**

```python
"""Tests for ComposedLLMAdapter wiring."""

from collections.abc import AsyncIterator

import pytest

from dollos.llm.adapter import StreamChunk
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider


class _FakeTemplate(PromptTemplate):
    """Renders prompt as 'SYS={system}|USR={user}|PRE={prefill}' for assertion."""

    def render(self, *, system: str, user: str, prefill: str) -> str:
        return f"SYS={system}|USR={user}|PRE={prefill}"


class _FakeProvider(Provider):
    """Captures the prompt it receives and yields canned chunks."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.last_prompt: str | None = None
        self.last_stop: list[str] | None = None
        self.last_max_tokens: int | None = None

    @property
    def supports_prefill(self) -> bool:
        return True

    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        self.last_prompt = prompt
        self.last_stop = stop
        self.last_max_tokens = max_tokens
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_composed_calls_template_then_provider():
    fake_chunks = [
        StreamChunk(text="ok", done=False),
        StreamChunk(text="", done=True),
    ]
    provider = _FakeProvider(fake_chunks)
    template = _FakeTemplate()
    adapter = ComposedLLMAdapter(provider=provider, template=template)

    out = []
    async for chunk in adapter.stream_completion(
        system="S",
        user="U",
        prefill="P",
        stop=["<|end|>"],
        max_tokens=42,
    ):
        out.append(chunk)

    # Template was applied and result handed to provider verbatim.
    assert provider.last_prompt == "SYS=S|USR=U|PRE=P"
    # Stop and max_tokens forwarded through.
    assert provider.last_stop == ["<|end|>"]
    assert provider.last_max_tokens == 42
    # Yielded chunks come straight from the provider.
    assert out == fake_chunks


@pytest.mark.asyncio
async def test_composed_default_prefill_is_empty():
    provider = _FakeProvider([StreamChunk(text="", done=True)])
    adapter = ComposedLLMAdapter(provider=provider, template=_FakeTemplate())

    async for _ in adapter.stream_completion(system="S", user="U"):
        pass

    # Default prefill is "".
    assert provider.last_prompt == "SYS=S|USR=U|PRE="


@pytest.mark.asyncio
async def test_composed_default_stop_and_max_tokens():
    provider = _FakeProvider([StreamChunk(text="", done=True)])
    adapter = ComposedLLMAdapter(provider=provider, template=_FakeTemplate())

    async for _ in adapter.stream_completion(system="S", user="U", prefill=""):
        pass

    # Defaults: stop=None, max_tokens=1024 forwarded through.
    assert provider.last_stop is None
    assert provider.last_max_tokens == 1024
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_composed.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.llm.composed'`.

- [ ] **Step 3: Write `src/dollos/llm/composed.py`**

```python
"""ComposedLLMAdapter — combines a Provider with a PromptTemplate."""

from collections.abc import AsyncIterator

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider


class ComposedLLMAdapter(LLMAdapter):
    """Combine a Provider with a PromptTemplate to satisfy LLMAdapter.

    The template formats (system, user, prefill) into a single prompt string;
    the provider sends that string to its backend and streams the response.
    """

    def __init__(self, provider: Provider, template: PromptTemplate) -> None:
        self._provider = provider
        self._template = template

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(system=system, user=user, prefill=prefill)
        async for chunk in self._provider.stream(
            prompt=prompt, stop=stop, max_tokens=max_tokens
        ):
            yield chunk
```

- [ ] **Step 4: Run tests, verify all pass**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_llm_composed.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 70 passed (67 + 3).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/llm/composed.py tests/test_llm_composed.py
git commit -m "feat(llm): ComposedLLMAdapter combines provider + template"
```

---

## Task 4: Coupled Switch — Config Schema + daemon.py + tests

**Files (all in one commit, can't split):**
- Modify: `src/dollos/config.py`
- Modify: `src/dollos/daemon.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_e2e.py`

This is the only multi-file commit in the plan. Reason: renaming `LLMConfig.backend` → `provider` and adding required `template` simultaneously breaks every `LLMConfig(...)` constructor call in the codebase. Doing it gradually leaves the build red between commits. So we change config schema, daemon's `build_adapter()`, and both call sites in tests in one atomic change.

- [ ] **Step 1: Update `src/dollos/config.py`**

Find the existing `LLMConfig` class:

```python
class LLMConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0
```

Replace with:

```python
class LLMConfig(BaseModel):
    provider: Literal["llamacpp"] = "llamacpp"
    template: Literal["qwen3-thinking"] = "qwen3-thinking"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0
```

(Only the first two fields change: `backend` → `provider`, and a new `template` field is inserted between them. `base_url`, `model_alias`, `timeout_s` are unchanged.)

- [ ] **Step 2: Update `src/dollos/daemon.py`**

Find the existing imports near the top:

```python
from dollos.llm.adapter import LLMAdapter
from dollos.llm.llamacpp import LlamaCppAdapter
```

Replace with:

```python
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
```

Find the existing `build_adapter` function:

```python
def build_adapter(settings: Settings) -> LLMAdapter:
    if settings.llm.backend == "llamacpp":
        return LlamaCppAdapter(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
        )
    raise ValueError(f"unknown LLM backend: {settings.llm.backend}")
```

Replace with:

```python
def build_adapter(settings: Settings) -> LLMAdapter:
    provider = _build_provider(settings)
    template = _build_template(settings)
    return ComposedLLMAdapter(provider=provider, template=template)


def _build_provider(settings: Settings) -> LlamaCppProvider:
    if settings.llm.provider == "llamacpp":
        return LlamaCppProvider(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
        )
    raise ValueError(f"unknown provider: {settings.llm.provider}")


def _build_template(settings: Settings) -> Qwen3ThinkingTemplate:
    if settings.llm.template == "qwen3-thinking":
        return Qwen3ThinkingTemplate()
    raise ValueError(f"unknown template: {settings.llm.template}")
```

(Note: the helper return type annotations use the concrete classes since v1 only has one of each; future plans may widen these to `Provider` / `PromptTemplate`.)

- [ ] **Step 3: Update `tests/test_config.py`**

Find the three existing tests that construct TOML with `backend = "llamacpp"` and update them to use `provider = "llamacpp"` and add `template = "qwen3-thinking"`. Replace the entire content of `tests/test_config.py` with:

```python
"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.config import Settings, load_settings


def test_load_settings_from_toml(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"
"""
    )

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.llm.provider == "llamacpp"
    assert settings.llm.template == "qwen3-thinking"
    assert settings.llm.base_url == "http://127.0.0.1:8001"
    assert settings.llm.model_alias == "test-model"
    assert settings.ipc.host == "127.0.0.1"
    assert settings.ipc.port == 9876
    assert settings.log.level == "INFO"


def test_load_settings_missing_required_field(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
# missing base_url and model_alias
"""
    )

    with pytest.raises(ValueError):
        load_settings(config_path)


def test_load_settings_default_log_level(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876
"""
    )

    settings = load_settings(config_path)

    assert settings.log.level == "INFO"


def test_load_settings_old_backend_field_raises(tmp_path: Path):
    """Pre-Plan-3 configs used `backend = "llamacpp"`. After the rename
    that field is unknown and pydantic should reject the missing
    required `provider`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876
"""
    )

    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_unknown_provider_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "vllm"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876
"""
    )

    with pytest.raises(ValidationError):
        load_settings(config_path)
```

(Note: this also adds a fifth test for unknown provider value, since `Literal["llamacpp"]` should reject other strings. Belt and braces.)

- [ ] **Step 4: Update `tests/test_e2e.py`**

Find the existing `Settings(...)` construction that uses `LLMConfig(backend="llamacpp", ...)` and update. Locate this block:

```python
settings = Settings(
    llm=LLMConfig(
        backend="llamacpp",
        base_url="http://test.local:8001",
        model_alias="mock",
    ),
    ipc=IPCConfig(host="127.0.0.1", port=0),
    log=LogConfig(level="WARNING"),
)
```

Replace with:

```python
settings = Settings(
    llm=LLMConfig(
        provider="llamacpp",
        template="qwen3-thinking",
        base_url="http://test.local:8001",
        model_alias="mock",
    ),
    ipc=IPCConfig(host="127.0.0.1", port=0),
    log=LogConfig(level="WARNING"),
)
```

(Only the LLMConfig kwargs change. The rest of the test is unchanged.)

- [ ] **Step 5: Run the changed test files first**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run pytest tests/test_config.py tests/test_e2e.py -v
```

Expected: 5 config tests + 1 e2e test = 6 pass. The e2e test now exercises the new `ComposedLLMAdapter → LlamaCppProvider → mocked /completion` round-trip with the same end-to-end behavior as before.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 71 passed (was 70 — config gained one test from 3→5; was 70 from 70 still since old `test_llm_llamacpp.py` still works because `llamacpp.py` itself is untouched).

Wait — let me recount. Before Task 4: 70 (56 baseline + 6 transport + 5 templates + 3 composed = 70). Task 4 adds 2 tests to test_config.py (the old 3 + 2 new = 5, net +2). So after Task 4: 72. Confirm with the run.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/config.py src/dollos/daemon.py tests/test_config.py tests/test_e2e.py
git commit -m "refactor(llm): switch daemon + tests to ComposedLLMAdapter

Renames LLMConfig.backend → provider and adds required template field.
daemon.py build_adapter() now returns ComposedLLMAdapter composed of
LlamaCppProvider + Qwen3ThinkingTemplate. test_config.py and test_e2e.py
constructors updated. Old LlamaCppAdapter still imported by no caller
in this commit; deletion comes in a later task."
```

---

## Task 5: config.example.toml

**Files:**
- Modify: `config.example.toml`

- [ ] **Step 1: Update `config.example.toml`**

Find the existing `[llm]` section (likely the first section in the file):

```toml
[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 60.0
```

Replace with:

```toml
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 60.0
```

Other sections (`[ipc]`, `[log]`, `[memory]`, `[embedder]`) are unchanged.

- [ ] **Step 2: Verify the example loads cleanly**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
uv run python -c "
from pathlib import Path
from dollos.config import load_settings
s = load_settings(Path('config.example.toml'))
print('provider:', s.llm.provider)
print('template:', s.llm.template)
"
```

Expected:
```
provider: llamacpp
template: qwen3-thinking
```

- [ ] **Step 3: Run full suite (no regression check)**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: same count as Task 4 (config.example.toml is data, no test impact).

- [ ] **Step 4: Commit**

```bash
git add config.example.toml
git commit -m "docs(config): rename backend → provider, add template in example"
```

---

## Task 6: __init__.py exports + grep for stragglers

**Files:**
- Modify: `src/dollos/llm/__init__.py`

- [ ] **Step 1: Inspect current exports**

```bash
cat src/dollos/llm/__init__.py
```

Expected current content:

```python
"""LLM backend adapters."""

from dollos.llm.adapter import LLMAdapter, StreamChunk

__all__ = ["LLMAdapter", "StreamChunk"]
```

- [ ] **Step 2: Replace with new exports**

```python
"""LLM backend adapters."""

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import PromptTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider, Provider

__all__ = [
    "ComposedLLMAdapter",
    "LLMAdapter",
    "LlamaCppProvider",
    "PromptTemplate",
    "Provider",
    "Qwen3ThinkingTemplate",
    "StreamChunk",
]
```

(Alphabetical for stability. `LlamaCppAdapter` is intentionally NOT in this list — it still exists in `llamacpp.py` but no longer surfaced via the package.)

- [ ] **Step 3: Grep for any leftover LlamaCppAdapter imports**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
grep -rn "LlamaCppAdapter\|from dollos.llm.llamacpp\|from dollos.llm import LlamaCppAdapter" src/ tests/ 2>&1 | grep -v llamacpp.py | grep -v test_llm_llamacpp.py
```

Expected output: empty (only `llamacpp.py` and its dedicated test file should still reference `LlamaCppAdapter`, and those are deleted in Task 7).

If anything else shows up, replace those imports with the appropriate new path:
- `from dollos.llm.composed import ComposedLLMAdapter` for adapter usage
- `from dollos.llm.transport import LlamaCppProvider` for provider only
- `from dollos.llm.templates import Qwen3ThinkingTemplate` for template only

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: same count as Task 5 (only the package-level imports changed; tests haven't moved).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/__init__.py
git commit -m "refactor(llm): expose new abstractions via package exports"
```

---

## Task 7: Delete `llamacpp.py` + `test_llm_llamacpp.py`

**Files:**
- Delete: `src/dollos/llm/llamacpp.py`
- Delete: `tests/test_llm_llamacpp.py`

- [ ] **Step 1: Delete the two files**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/llm-provider-template
git rm src/dollos/llm/llamacpp.py tests/test_llm_llamacpp.py
```

- [ ] **Step 2: Run full suite**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 69 passed. Math: was 72 after Task 4 (and 5/6 added zero tests), minus 3 tests in `test_llm_llamacpp.py` = 69.

If the count differs, something's off — investigate before committing.

- [ ] **Step 3: Sanity-check the daemon still starts**

```bash
cp config.example.toml config.toml
timeout 2 uv run python -m dollos --config config.toml 2>&1 || true
```

Expected: log line `WebSocket server listening on 127.0.0.1:9876`, then exits cleanly on timeout.

(Note: the timeout exit code is 124; the `|| true` swallows it. The presence of the listening log line is what we're checking for.)

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(llm): remove legacy LlamaCppAdapter

All callers migrated to ComposedLLMAdapter via Tasks 4-6. The two-file
LlamaCppAdapter is now dead code; deleting it. Behavior unchanged for
end users."
```

---

## Done — What This Plan Produced

After all tasks complete you have:

- `Provider` ABC and `LlamaCppProvider` concrete in `src/dollos/llm/transport.py`
- `PromptTemplate` ABC and `Qwen3ThinkingTemplate` concrete in `src/dollos/llm/templates.py`
- `ComposedLLMAdapter` in `src/dollos/llm/composed.py` implementing the existing `LLMAdapter` contract
- `LLMConfig` schema with `provider` + `template` fields (was `backend`)
- `daemon.py` `build_adapter()` returns `ComposedLLMAdapter` via `_build_provider` + `_build_template` helpers
- 69 passing automated tests (down 1 net: dropped 3 tests from the deleted llamacpp test file, added 14 across new test files: 6 transport + 5 templates + 3 composed; plus +2 config tests for the rename = +14, -3 = +11; baseline was 56 + 11 + 2 = 69... let me recompute)

Final count math:

| | Tests |
|---|---|
| Baseline (Plan 1 + Plan 2 done) | 56 |
| Task 1: + transport tests | +6 → 62 |
| Task 2: + templates tests | +5 → 67 |
| Task 3: + composed tests | +3 → 70 |
| Task 4: + config tests (3→5) | +2 → 72 |
| Task 7: − llamacpp tests (3→0) | −3 → **69** |

Final: **69 tests passing**.

**What is NOT in this plan (deferred to later plans):**
- New providers (vLLM / OpenAI-compat / Anthropic) — Plan 5+ as needed
- New templates (Qwen3-plain, Llama, Gemma, server-applied) — Plan 4 brings Qwen3-plain
- Default stop sequence migration (Provider → PromptTemplate) — when a non-ChatML template lands
- Prefill capability runtime warnings — when a provider that doesn't support prefill is added

Next plan: **Inner Voice + VoM RECALL utility** (Plan 4).

---

## Self-Review

**Spec coverage check** (each spec section → which task implements it):
- §0 scope (one (provider, template) combo, no new shipped) → respected throughout
- §1 motivation (decouple HTTP from prompt format) → Tasks 1+2 split the responsibilities
- §2 three-layer architecture (LLMAdapter / ComposedLLMAdapter / Provider × PromptTemplate) → Tasks 1, 2, 3
- §3.1 Provider ABC → Task 1, Step 3
- §3.2 PromptTemplate ABC → Task 2, Step 3
- §3.3 ComposedLLMAdapter → Task 3, Step 3
- §4.1 LlamaCppProvider concrete → Task 1, Step 3
- §4.2 Qwen3ThinkingTemplate concrete → Task 2, Step 3
- §5.1 LLMConfig schema (`provider` + `template`) → Task 4, Step 1
- §5.2 config.example.toml update → Task 5
- §5.3 no backwards compat → covered by Task 4 Step 3 (test asserts old `backend` field raises)
- §5.4 build_adapter factory → Task 4, Step 2
- §6 file structure (transport.py / templates.py / composed.py / __init__.py / llamacpp.py deleted) → Tasks 1, 2, 3, 6, 7
- §7 testing strategy → Tasks 1-3 add new test files; Task 4 updates test_config.py + test_e2e.py; Task 7 deletes test_llm_llamacpp.py
- §8 migration steps (7 commits, intermediate green) → Tasks 1-7 each end with full suite run
- §9 Non-goals → not implemented (correctly)
- §10 Open Questions → §10 issue #1 (Provider stop default) acknowledged as tech debt in Task 1's `test_llamacpp_provider_default_stop_when_none_passed` comment
- §11 Plan task count (7) → 7 tasks here

**Placeholder scan:** searched for "TBD", "TODO", "implement later", "fill in details", "Add appropriate", "Similar to Task" — none present. All code blocks are complete.

**Type consistency check:**
- `Provider.stream(prompt: str, stop: list[str] | None = None, max_tokens: int = 1024) -> AsyncIterator[StreamChunk]` consistent across ABC (transport.py) and concrete (LlamaCppProvider) and consumer (ComposedLLMAdapter)
- `PromptTemplate.render(*, system: str, user: str, prefill: str) -> str` consistent across ABC and Qwen3ThinkingTemplate and consumer ComposedLLMAdapter
- `StreamChunk(text: str, done: bool = False)` (frozen dataclass from Plan 1's `adapter.py`) reused unchanged
- `LLMConfig(provider, template, base_url, model_alias, timeout_s)` consistent in config.py, test_config.py, test_e2e.py, config.example.toml, daemon.py's `_build_provider` / `_build_template`
- `ComposedLLMAdapter.__init__(provider, template)` keyword-or-positional; consumers (daemon.py, tests) use keyword form consistently

No inconsistencies found.
