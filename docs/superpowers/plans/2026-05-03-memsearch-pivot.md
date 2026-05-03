# memsearch Pivot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace DollOS's Plan 2 sqlite-vec + custom Memory module with the [memsearch](https://github.com/zilliztech/memsearch) package (Milvus Lite + ONNX bge-m3 + markdown SoT), while preserving the prompt-side work from the abandoned `vom-integration` branch (`render_blocks()`, `iv_recall.jinja`, `Qwen3PlainTemplate`) and re-implementing VoM (roadmap step 3) on top of memsearch.

**Architecture:** New worktree off `main` called `memsearch-pivot`. Cherry-pick three pure prompt-side commits from `vom-integration`. Then: drop `dollos.memory` module entirely, add `memsearch` dependency, introduce `[data]` + `[memsearch]` config sections, rewrite `InnerVoice` to call `memsearch.search()`, wire kernel to construct `MemSearch` and run `index()` at startup. End-state: VoM works exactly as in step 3, but backed by markdown daily summary files under `data/memory/shared/`, indexed in Milvus Lite via ONNX bge-m3 embeddings.

**Tech Stack:** Python 3.13+, `memsearch` (Milvus Lite, ONNX bge-m3 default), `pydantic`, `jinja2`, `pytest` + `pytest-asyncio` + `respx`.

**Spec:** `docs/superpowers/specs/2026-05-03-memsearch-pivot-design.md`

---

## Task 0: Open `memsearch-pivot` worktree and cherry-pick prompt-side commits

**Files:**
- Worktree: `.worktrees/memsearch-pivot` on new branch `memsearch-pivot`, based on `main`

This is prep, not a TDD task. Run from the main repo (not the worktree).

- [ ] **Step 1: Confirm clean main**

```bash
cd /home/progcat/Projects/DollOS
git status
```

Expected: working tree clean, branch `main`.

- [ ] **Step 2: Identify the three prompt-side commits to cherry-pick**

```bash
git log --oneline plan-4-inner-voice vom-integration ^main | head -20
```

The three commits to cherry-pick (by message prefix; SHAs may differ if anyone rebased):
- `feat(llm): Qwen3PlainTemplate for non-thinking small models` — from `plan-4-inner-voice`
- `feat(prompts): PromptRenderer.render_blocks() for multi-block templates` — from `vom-integration`
- `feat(prompts): iv_recall.jinja for InnerVoice recall prompt` — from `vom-integration`

Find their SHAs in the log output. Save them locally; you'll use them in Step 4.

- [ ] **Step 3: Create the worktree**

```bash
git worktree add .worktrees/memsearch-pivot -b memsearch-pivot main
cd .worktrees/memsearch-pivot
uv sync
uv run pytest -q
```

Expected: clean main has its current passing test count (around 95 tests pre-step-3).

- [ ] **Step 4: Cherry-pick three prompt-side commits**

```bash
# in .worktrees/memsearch-pivot
git cherry-pick <SHA-of-Qwen3PlainTemplate>
git cherry-pick <SHA-of-render_blocks>
git cherry-pick <SHA-of-iv_recall.jinja>
```

Expected behavior per cherry-pick:

1. **Qwen3PlainTemplate** — should apply cleanly to `src/dollos/llm/templates.py` and `tests/test_llm_templates.py`. No conflicts expected.

2. **render_blocks** — should apply cleanly to `src/dollos/prompts/renderer.py` and add `_test_blocks_fixture.jinja` + tests in `test_prompt_renderer.py`. No conflicts expected.

3. **iv_recall.jinja** — adds a new file. No conflicts expected.

If any cherry-pick has conflicts, abort with `git cherry-pick --abort` and report back; otherwise continue.

- [ ] **Step 5: Verify pytest still green after cherry-picks**

```bash
uv run pytest -q
```

Expected: green. New tests added by the cherry-picks should now be in the suite.

All subsequent tasks happen in `.worktrees/memsearch-pivot`.

---

## Task 1: Drop `dollos.memory` module + sqlite-vec dep + add memsearch dep

**Files:**
- Delete: `src/dollos/memory/` (entire directory: `__init__.py`, `embedder.py`, `embedder_llamacpp.py`, `schema.sql`, `scoring.py`, `store.py`)
- Delete: `tests/test_memory_e2e.py`, `tests/test_memory_embedder.py`, `tests/test_memory_embedder_llamacpp.py`, `tests/test_memory_scoring.py`, `tests/test_memory_store.py`
- Modify: `pyproject.toml` (drop `sqlite-vec`, add `memsearch`)
- Modify: `.gitignore` (add `data/`)
- Create: `data/.gitkeep`
- Modify: `src/dollos/kernel.py` (placeholder edits to keep it compiling — see Step 4)
- Modify: `src/dollos/inner_voice.py` (placeholder edits — see Step 4)

This is a destructive task. After it, the codebase will not run end-to-end (kernel + inner_voice will reference deleted symbols), but pytest can still pass on the surviving non-memory tests as long as we stub out the imports. Tasks 2-5 build the new surface back.

- [ ] **Step 1: Delete `src/dollos/memory/` directory**

```bash
git rm -r src/dollos/memory/
```

- [ ] **Step 2: Delete the 5 test_memory_*.py files**

```bash
git rm tests/test_memory_e2e.py \
       tests/test_memory_embedder.py \
       tests/test_memory_embedder_llamacpp.py \
       tests/test_memory_scoring.py \
       tests/test_memory_store.py
```

- [ ] **Step 3: Update `pyproject.toml`**

Replace `pyproject.toml` `[project] dependencies` block — drop `sqlite-vec`, add `memsearch`:

```toml
dependencies = [
    "httpx>=0.27",
    "jinja2>=3.1",
    "memsearch>=0.1",
    "pydantic>=2.6",
    "websockets>=12.0",
]
```

(Adjust the `memsearch` version constraint to whatever PyPI shows as latest at implementation time. If memsearch isn't on PyPI yet, use a git URL: `"memsearch @ git+https://github.com/zilliztech/memsearch.git"`. Verify with `pip search` or `pip install memsearch --dry-run` before committing.)

- [ ] **Step 4: Stub kernel.py and inner_voice.py imports to compile**

Tasks 2-5 will rewrite these properly. For now, replace their contents with minimal stubs that pytest can collect without ImportError. This avoids polluting later commits with intermediate imports.

`src/dollos/inner_voice.py`:

```python
"""InnerVoice — placeholder during memsearch pivot. Rewritten in Task 3."""

from typing import Any


class InnerVoice:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "InnerVoice is being rewritten for memsearch pivot. See Task 3."
        )
```

`src/dollos/kernel.py` — keep most of it, but the import-side and `DollOS.__init__` need surgery. Replace the entire file with this minimal stub for now:

```python
"""DollOS kernel — placeholder during memsearch pivot. Rewritten in Task 4."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from dollos.config import Settings
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer

logger = logging.getLogger(__name__)


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


class DollOS:
    """Placeholder during memsearch pivot — full impl in Task 4."""

    def __init__(self, settings: Settings) -> None:
        raise NotImplementedError(
            "DollOS is being rewritten for memsearch pivot. See Task 4."
        )
```

This deliberately removes `build_inner_voice`, `build_memory`, and the wired `DollOS.__init__`. They're rebuilt in later tasks.

- [ ] **Step 5: Delete `tests/test_inner_voice.py`, `tests/test_kernel_factories.py`, `tests/test_e2e.py`, `tests/test_config.py`**

These reference the deleted/stubbed code. They'll be rewritten in Tasks 2-5.

```bash
git rm tests/test_inner_voice.py \
       tests/test_kernel_factories.py \
       tests/test_e2e.py \
       tests/test_config.py
```

- [ ] **Step 6: Add `data/` to `.gitignore` and create `data/.gitkeep`**

Edit `.gitignore` — find the existing structure, append:

```
# DollOS-generated data (memsearch markdown, Milvus Lite cache, etc.)
data/
```

Create the placeholder file (in a way that bypasses .gitignore):

```bash
mkdir -p data
echo "# Placeholder so the data/ directory exists in the repo." > data/.gitkeep
git add -f data/.gitkeep
```

The `-f` is necessary because `data/` is in `.gitignore`.

- [ ] **Step 7: Run `uv sync` to install memsearch**

```bash
uv sync
```

Expected: memsearch installs cleanly. If it pulls a large transitive (e.g. `pymilvus`, `onnxruntime`), that's expected. ONNX model itself will only download on first `index()` call.

- [ ] **Step 8: Verify pytest still collects and passes the surviving tests**

```bash
uv run pytest -q
```

Expected: a subset of the previous test count passes (test_memory_*.py, test_inner_voice.py, test_kernel_factories.py, test_e2e.py, test_config.py are gone). Surviving tests: test_ipc_messages.py, test_ipc_server.py, test_llm_*.py, test_prompt_renderer.py. Should be roughly 50-60 tests passing, zero failing.

If any test fails because it imported from `dollos.memory` or referenced the deleted classes, find and update that import to remove the dependency, OR delete that test if it's solely about Memory.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock .gitignore data/.gitkeep \
        src/dollos/inner_voice.py src/dollos/kernel.py
git commit -m "refactor: drop dollos.memory module; add memsearch dep; stub kernel/inner_voice"
```

---

## Task 2: Replace `MemoryConfig` + `EmbedderConfig` with `DataConfig` + `MemsearchConfig`

**Files:**
- Modify: `src/dollos/config.py`
- Create: `tests/test_config.py` (was deleted in Task 1)
- Modify: `config.example.toml`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
"""Tests for config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.config import Settings, load_settings


_BASE_TOML = """
[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[character]
profile_path = "experiments/test_character.jinja"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""


def test_load_settings_minimal_uses_defaults_for_data_and_memsearch(tmp_path: Path):
    """[data] and [memsearch] are both optional with sensible defaults."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_BASE_TOML)

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.data.root == Path("data")
    assert settings.memsearch.top_k == 10


def test_load_settings_with_data_root_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[data]
root = "/var/lib/dollos"
"""
    )

    settings = load_settings(config_path)
    assert settings.data.root == Path("/var/lib/dollos")


def test_data_root_expands_user(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[data]
root = "~/my-dollos-data"
"""
    )
    settings = load_settings(config_path)
    assert "~" not in str(settings.data.root)
    assert str(settings.data.root).endswith("my-dollos-data")


def test_load_settings_with_memsearch_top_k_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memsearch]
top_k = 5
"""
    )
    settings = load_settings(config_path)
    assert settings.memsearch.top_k == 5


def test_load_settings_rejects_legacy_memory_section(tmp_path: Path):
    """Old [memory] section should produce a validation error (extra fields)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memory]
db_path = "/tmp/old.db"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_rejects_legacy_embedder_section(tmp_path: Path):
    """Old [embedder] section should produce a validation error (extra fields)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)


def test_load_settings_rejects_unknown_memsearch_field(tmp_path: Path):
    """Memsearch config has extra='forbid'; unknown fields rejected."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _BASE_TOML
        + """
[memsearch]
top_k = 10
embedding_provider = "openai"  # not exposed in v1
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

[character]
profile_path = "experiments/test_character.jinja"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""
    )
    with pytest.raises(ValidationError):
        load_settings(config_path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config.py -v
```

Expected: every test fails because `Settings` still has `MemoryConfig` + `EmbedderConfig` fields (and lacks `DataConfig` / `MemsearchConfig`). The error will be a pydantic ValidationError saying `memory` and `embedder` are required fields, since the test TOML doesn't provide them.

- [ ] **Step 3: Rewrite `src/dollos/config.py`**

Replace the entire file with:

```python
"""Configuration: TOML loading + pydantic validation."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["llamacpp"] = "llamacpp"
    template: Literal["qwen3-thinking"] = "qwen3-thinking"
    base_url: str
    model_alias: str
    timeout_s: float = 60.0


class IPCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 9876


class LogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DataConfig(BaseModel):
    """Root for all DollOS-generated data. data/ 不存在 = fresh launch."""
    model_config = ConfigDict(extra="forbid")

    root: Path = Path("data")

    @field_validator("root", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class MemsearchConfig(BaseModel):
    """memsearch knobs (paths derived from data.root in kernel.build_memsearch)."""
    model_config = ConfigDict(extra="forbid")

    top_k: int = 10


class CharacterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_path: Path

    @field_validator("profile_path", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class InnerVoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    timeout_s: float = 30.0


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    data: DataConfig = Field(default_factory=lambda: DataConfig())
    memsearch: MemsearchConfig = Field(default_factory=lambda: MemsearchConfig())
    character: CharacterConfig
    inner_voice: InnerVoiceConfig


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML config file."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)
```

Key design choices:
- `Settings` itself has `extra="forbid"` so old `[memory]` / `[embedder]` sections raise `ValidationError` (legacy-rejection tests rely on this).
- `data` and `memsearch` both default-construct with `Field(default_factory=...)`.
- `MemsearchConfig` has `extra="forbid"` so unknown fields like `embedding_provider` raise.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Update `config.example.toml`**

Replace the `[memory]` and `[embedder]` sections with `[data]` and `[memsearch]`:

```toml
# DollOS configuration template.
# Copy to config.toml and edit.

[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 60.0

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"

[data]
root = "data"   # repo-relative; data/ 不存在 = fresh launch. All system-generated data lives here.

[memsearch]
top_k = 10   # InnerVoice.recall default; memsearch uses ONNX bge-m3 by default.

[character]
profile_path = "experiments/test_character.jinja"   # development character profile (Gura mock)

[inner_voice]
base_url = "http://127.0.0.1:8003"   # separate llama-server with small Qwen3 instruct (e.g. Qwen3-0.6B-Instruct)
timeout_s = 30.0
```

- [ ] **Step 6: Run full suite**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/dollos/config.py tests/test_config.py config.example.toml
git commit -m "feat(config): replace [memory]/[embedder] with [data]/[memsearch]"
```

---

## Task 3: Rewrite `InnerVoice` to use `MemSearch`

**Files:**
- Modify: `src/dollos/inner_voice.py` (currently a stub from Task 1)
- Create: `tests/test_inner_voice.py` (was deleted in Task 1)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inner_voice.py`:

```python
"""Tests for InnerVoice.recall() against a fake MemSearch."""

from collections.abc import AsyncIterator

import pytest

from dollos.inner_voice import InnerVoice
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer


class _FakeMemSearch:
    """Stub: returns canned hits, captures last query / top_k."""

    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits
        self.last_query: str | None = None
        self.last_top_k: int | None = None
        self.call_count = 0

    async def search(self, query: str, top_k: int = 3) -> list[dict]:
        self.last_query = query
        self.last_top_k = top_k
        self.call_count += 1
        return self._hits


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


def _make_iv(memsearch, llm, default_top_k: int = 10) -> InnerVoice:
    return InnerVoice(
        memsearch=memsearch,
        llm=llm,
        renderer=PromptRenderer(),
        default_top_k=default_top_k,
    )


@pytest.mark.asyncio
async def test_recall_with_hits_returns_recall_block():
    mem = _FakeMemSearch(
        hits=[
            {"content": "the sky is blue", "score": 0.9, "source": "shared/2026-05-03.md"},
            {"content": "user likes coffee", "score": 0.8, "source": "shared/2026-05-03.md"},
        ]
    )
    fake_llm = _FakeLLMAdapter(response="- user likes coffee\n- the sky is blue")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("what about coffee")

    assert block.startswith("RECALL:\n")
    assert "user likes coffee" in block
    assert block.endswith("\n")


@pytest.mark.asyncio
async def test_recall_system_prompt_comes_from_iv_recall_template():
    mem = _FakeMemSearch(hits=[{"content": "a fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- a fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("query")

    assert fake_llm.last_system is not None
    assert "memory recall helper" in fake_llm.last_system
    assert "(no relevant memories)" in fake_llm.last_system
    assert fake_llm.call_count == 1


@pytest.mark.asyncio
async def test_recall_user_block_includes_query_and_candidates():
    mem = _FakeMemSearch(
        hits=[
            {"content": "fact alpha", "score": 0.9, "source": "x.md"},
            {"content": "fact beta", "score": 0.8, "source": "x.md"},
        ]
    )
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


@pytest.mark.asyncio
async def test_recall_uses_empty_prefill():
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("q")

    assert fake_llm.last_prefill == ""


@pytest.mark.asyncio
async def test_recall_empty_hits_returns_no_relevant_block():
    mem = _FakeMemSearch(hits=[])
    fake_llm = _FakeLLMAdapter(response="should not be called")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("anything")

    assert block == "RECALL:\n(no relevant memories)\n"
    assert fake_llm.call_count == 0


@pytest.mark.asyncio
async def test_recall_strips_whitespace_from_model_output():
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="  \n- fact\n  \n")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("q")

    assert block == "RECALL:\n- fact\n"


@pytest.mark.asyncio
async def test_recall_passes_top_k_to_memsearch():
    """default_top_k from settings is used; per-call override also works."""
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm, default_top_k=15)

    await iv.recall("q")
    assert mem.last_top_k == 15

    await iv.recall("q", top_k=3)
    assert mem.last_top_k == 3


@pytest.mark.asyncio
async def test_recall_ignores_character_id_in_step3():
    """character_id is reserved for step 10; step 3 just ignores it."""
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm)

    # Should not raise, should not affect the search call
    await iv.recall("q", character_id="gura")

    assert mem.last_query == "q"
    # No assertion about source_prefix / character_id propagating — it shouldn't
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_inner_voice.py -v
```

Expected: all tests fail with `NotImplementedError` (the Task 1 stub raises in `__init__`).

- [ ] **Step 3: Replace `src/dollos/inner_voice.py`**

```python
"""InnerVoice — small-model VoM RECALL block synthesizer.

Reads from memsearch (markdown SoT + Milvus shadow index) and uses a
small LLM to filter / synthesize a RECALL block. Pure utility — no
state, no event handling, no writes.

Prompt content lives in `dollos/prompts/templates/iv_recall.jinja`
(system + user blocks).
"""

from typing import Protocol

from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


class _MemSearchLike(Protocol):
    """Structural interface — anything with this `search` method works.

    memsearch.MemSearch satisfies this; the test fake also satisfies it.
    Avoids hard-importing memsearch at module level (test convenience).
    """
    async def search(self, query: str, top_k: int = ...) -> list[dict]: ...


class InnerVoice:
    """Synthesize VoM RECALL blocks from memsearch using a small LLM."""

    def __init__(
        self,
        memsearch: _MemSearchLike,
        llm: LLMAdapter,
        renderer: PromptRenderer,
        default_top_k: int = 10,
    ) -> None:
        self._memsearch = memsearch
        self._llm = llm
        self._renderer = renderer
        self._default_top_k = default_top_k

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,    # ignored in step 3; reserved for step 10
        top_k: int | None = None,
    ) -> str:
        """Return a RECALL block string for the given query.

        Always starts with "RECALL:\\n" so the caller can embed verbatim
        into a Doll prefill.

        If memsearch returns no hits, returns
        "RECALL:\\n(no relevant memories)\\n" without invoking the LLM.
        """
        k = top_k if top_k is not None else self._default_top_k
        hits = await self._memsearch.search(query, top_k=k)
        if not hits:
            return "RECALL:\n(no relevant memories)\n"

        candidates = "\n".join(
            f"{i + 1}. {h['content']}" for i, h in enumerate(hits)
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

Note: the `_MemSearchLike` Protocol is intentional — it lets tests pass a fake without depending on memsearch's actual class, AND it documents the minimal interface InnerVoice relies on. Real production code passes a real `memsearch.MemSearch` instance via the kernel factory in Task 4.

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_inner_voice.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/inner_voice.py tests/test_inner_voice.py
git commit -m "feat(inner_voice): rewrite InnerVoice to call memsearch.search()"
```

---

## Task 4: Wire kernel — `build_memsearch` + `build_inner_voice` + DollOS lifecycle

**Files:**
- Modify: `src/dollos/kernel.py` (currently a stub from Task 1)
- Create: `tests/test_kernel_factories.py` (was deleted in Task 1)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_kernel_factories.py`:

```python
"""Tests for kernel factory functions."""

from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.inner_voice import InnerVoice
from dollos.kernel import build_inner_voice, build_memsearch
from dollos.prompts import PromptRenderer


def _make_settings(tmp_path: Path) -> Settings:
    character_path = tmp_path / "character.jinja"
    character_path.write_text("You are Doll.")
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(profile_path=character_path),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=15.0,
        ),
    )


def test_build_memsearch_creates_shared_dir(tmp_path: Path):
    """build_memsearch should create data.root/memory/shared/ if missing."""
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "shared"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


def test_build_memsearch_returns_memsearch_instance(tmp_path: Path):
    settings = _make_settings(tmp_path)
    instance = build_memsearch(settings)
    # Don't assert on exact class to avoid hard-coupling the test to memsearch's
    # internal type name. It must at least have an async search() callable.
    assert hasattr(instance, "search")
    assert callable(instance.search)


def test_build_inner_voice_returns_innervoice_with_top_k_from_settings(tmp_path: Path):
    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert isinstance(iv, InnerVoice)
    assert iv._default_top_k == 7  # from MemsearchConfig.top_k


def test_build_inner_voice_uses_inner_voice_config_base_url(tmp_path: Path):
    """The factory must point InnerVoice's small-LLM provider at inner_voice.base_url,
    not the main LLM's base_url."""
    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert iv._llm._provider._base_url == "http://test.local:8003"
    assert iv._llm._provider._timeout_s == 15.0


def test_build_inner_voice_uses_qwen3_plain_template(tmp_path: Path):
    from dollos.llm.templates import Qwen3PlainTemplate

    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert isinstance(iv._llm._template, Qwen3PlainTemplate)
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_kernel_factories.py -v
```

Expected: ImportError or AttributeError because `build_memsearch` and `build_inner_voice` don't exist on `dollos.kernel` (the Task 1 stub removed them).

- [ ] **Step 3: Rewrite `src/dollos/kernel.py`**

Replace the entire file with:

```python
"""DollOS kernel: wires LLM adapter, memsearch, and IPC server together."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from memsearch import MemSearch

from dollos.config import Settings
from dollos.inner_voice import InnerVoice
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer

logger = logging.getLogger(__name__)


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


def build_memsearch(settings: Settings) -> MemSearch:
    """Construct memsearch rooted at data.root / memory / shared.

    step 10 will extend `paths` to include the active character's
    private directory (data.root/memory/<character_id>). v1 only has shared.
    """
    shared_path = settings.data.root / "memory" / "shared"
    shared_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(paths=[str(shared_path)], embedding_provider="onnx")


def build_inner_voice(
    settings: Settings, memsearch: MemSearch, renderer: PromptRenderer
) -> InnerVoice:
    """Construct InnerVoice wired to a small llama.cpp model + memsearch.

    v1 hardcodes (LlamaCppProvider, Qwen3PlainTemplate) for the small LLM.
    """
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    llm = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return InnerVoice(
        memsearch=memsearch,
        llm=llm,
        renderer=renderer,
        default_top_k=settings.memsearch.top_k,
    )


class DollOS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
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
                "scaffolding", character=self._character_profile
            )
            recall = await self.inner_voice.recall(msg.text)
            prefill = f"<think>\n{recall}GOAL: "
            async for chunk in self.adapter.stream_completion(
                system=system, user=msg.text, prefill=prefill,
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
        await self.memsearch.index()
        try:
            await self.server.start()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
        finally:
            pass   # memsearch has no close(); Milvus Lite is file-based
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_kernel_factories.py -v
```

Expected: all 5 tests pass. The `test_build_memsearch_returns_memsearch_instance` test will trigger memsearch's first construction; this should NOT download the ONNX model (download is deferred until `index()` or `search()` is called per memsearch docs).

If the construction itself triggers a download or network call, that's a memsearch behavior we need to handle in test_e2e via monkeypatch (Task 5). For factory tests it's fine to construct a real instance since it's the construction path being verified.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel_factories.py
git commit -m "feat(kernel): build_memsearch + DollOS wires memsearch + recall before big LLM"
```

---

## Task 5: Rewrite `tests/test_e2e.py` for memsearch wiring

**Files:**
- Create: `tests/test_e2e.py` (was deleted in Task 1)

- [ ] **Step 1: Write the e2e test**

Create `tests/test_e2e.py`:

```python
"""End-to-end test: WebSocket client → DollOS → mocked llama.cpp → response.

The full chain runs, but with two cheap stubs:
- InnerVoice.recall returns a fixed RECALL block (recall behavior is
  covered by tests/test_inner_voice.py).
- MemSearch.index is no-op'd (a real index() would download the
  ~558MB ONNX model on first run).
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
import websockets

from dollos.config import (
    CharacterConfig,
    DataConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
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
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(profile_path=character_path),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=5.0,
        ),
    )

    # Stub InnerVoice.recall — recall behavior is covered by test_inner_voice.py.
    async def _stub_recall(self, query, **kwargs):
        return "RECALL:\n- user likes coffee\n"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)

    # No-op memsearch.index() to avoid downloading the ONNX model in tests.
    async def _noop_index(self):
        return None

    monkeypatch.setattr("memsearch.MemSearch.index", _noop_index)

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

        # Use run-style lifecycle: index then start. We call them manually
        # because dollos.run() blocks on _shutdown.wait().
        await dollos.memsearch.index()  # no-op due to monkeypatch
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
            # VoM prefill must reach the big model
            assert "<think>\nRECALL:\n- user likes coffee\nGOAL: " in prompt
        finally:
            await dollos.server.stop()
```

- [ ] **Step 2: Run the e2e test**

```bash
uv run pytest tests/test_e2e.py -v
```

Expected: pass.

If memsearch's MemSearch constructor itself triggers any heavy work (e.g. eager Milvus Lite open), the test should still succeed because:
- `data.root` points at a fresh `tmp_path`, so memsearch initializes empty
- ONNX download is deferred per memsearch docs
- We monkeypatch `index()` to skip indexing

If the constructor unexpectedly does a network call or downloads, document it in the report and we'll add another monkeypatch.

- [ ] **Step 3: Run full suite**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(e2e): exercise memsearch wiring end-to-end"
```

---

## Task 6: Manual smoke test + roadmap / CLAUDE.md update

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

No automated tests in this task. The user will run the smoke test on a real environment; document the procedure inline.

- [ ] **Step 1: Run the manual smoke test (or document if deferred)**

Procedure (the user runs this; the implementer just confirms the procedure is captured here verbatim):

1. Start the small Inner Voice model server (Qwen3-1.7B-Instruct or 0.6B-Instruct):
   ```bash
   ./llama.cpp/llama-server \
       -hf unsloth/Qwen3-1.7B-Instruct-GGUF:Q5_K_M \
       --port 8003 --host 0.0.0.0
   ```

2. Start the big Doll model server (per CLAUDE.md "Self-host llama.cpp big model" command, port 8001).

3. Configure `config.toml` (copy from `config.example.toml`):
   - Verify `[data] root = "data"`
   - Verify `[memsearch] top_k = 10`
   - Verify `[inner_voice] base_url = "http://127.0.0.1:8003"`
   - Verify `[character] profile_path` points at a real character profile

4. Plant a test fact by creating `data/memory/shared/2026-05-03.md`:
   ```markdown
   # 2026-05-03

   <!-- session:smoke-test-001 -->

   The user's favorite drink is oat milk latte. They drink it every morning before starting work.
   ```

5. Start DollOS. First start will download ONNX bge-m3 (~558MB, one-time):
   ```bash
   uv run python -m dollos --config config.toml
   ```

   Wait for the log line indicating memsearch index complete and IPC server listening.

6. From a second terminal, send a TextInput via a small WS client (or `websocat`):
   ```bash
   echo '{"type":"text_input","text":"What does the user like to drink?"}' | \
       websocat ws://127.0.0.1:9876
   ```

7. Verify Doll's response references oat milk latte (or related concept) within the first 2 sentences.

8. Inspect daemon logs for evidence of the recall path. To make this easier, the implementer may temporarily add a single debug log in `_handle_text_input`:
   ```python
   logger.debug("recall returned: %s", recall[:120])
   ```
   (and run with `[log] level = "DEBUG"`). Remove the temporary log before committing the docs update, or keep it as a permanent observability hook — implementer's call.

Acceptance:
- [ ] Doll's response references the planted fact (or its concept).
- [ ] No exceptions in the daemon log during the round-trip.
- [ ] Second startup is faster than the first (ONNX cached, memsearch SHA-256 skips unchanged chunks).

If smoke test fails, debug. Don't proceed to Step 2 until it passes. If hardware constraints prevent running the smoke test in this session, mark this step as "deferred — to be run by user before merging" and continue.

- [ ] **Step 2: Update `docs/roadmap.md`**

Edit the "已完成" section — add a row marking step 3 as completed via memsearch:

```markdown
| Roadmap step 3 — VoM (memsearch-backed) | Merged |
```

Edit the "下一個" section — replace the step-3 description with step 4:

```markdown
### 下一個

**Roadmap step 4 — 跑通 event loop**：Event ABC + UserTextEvent + asyncio.Queue + DollLoop 主迴圈。IPC handler push event 進 queue，DollLoop pop event 跑 recall + LLM call + stream。完整 roadmap 詳見上方表格。
```

Match the existing tone / formatting of the file. Read the current `docs/roadmap.md` before editing to confirm exact section structure.

- [ ] **Step 3: Update `CLAUDE.md`**

In the "Implementation Plans → 已完成" table, add:

```markdown
| Roadmap step 3 — VoM (memsearch-backed) | Merged |
```

In "下一個", replace the step-3 description with step 4:

```markdown
**Roadmap step 4 — Event Loop**：Event ABC + asyncio.Queue + DollLoop 主迴圈，把 IPC handler 的同步路徑改成 event-driven。完整 roadmap：`docs/roadmap.md`。
```

Also update the **Memory SoT** bullet under "Key Architecture Decisions":

```markdown
- **Memory SoT**: memsearch (Milvus Lite + ONNX bge-m3 + markdown daily summary files). `data/memory/shared/` for shared facts, `data/memory/{character_id}/` for per-character private (step 10). Hybrid retrieval (dense + BM25 + RRF) provided by memsearch.
```

Read the current `CLAUDE.md` before editing to find the exact line; preserve everything else.

- [ ] **Step 4: Final pytest run**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: mark roadmap step 3 (VoM via memsearch) merged, point to step 4"
```

- [ ] **Step 6: Hand off to `superpowers:finishing-a-development-branch`**

```bash
# from .worktrees/memsearch-pivot
git log --oneline main..HEAD             # review the chain
uv run pytest -q                         # final green check
```

Then invoke `superpowers:finishing-a-development-branch` to merge `memsearch-pivot` into `main`.

---

## Summary of expected commits

After Task 0 (cherry-pick, no new authored commits) and Tasks 1–6, the `memsearch-pivot` branch contains:

```
[3 cherry-picked commits from vom-integration: Qwen3PlainTemplate, render_blocks, iv_recall.jinja]
refactor: drop dollos.memory module; add memsearch dep; stub kernel/inner_voice
feat(config): replace [memory]/[embedder] with [data]/[memsearch]
feat(inner_voice): rewrite InnerVoice to call memsearch.search()
feat(kernel): build_memsearch + DollOS wires memsearch + recall before big LLM
test(e2e): exercise memsearch wiring end-to-end
docs: mark roadmap step 3 (VoM via memsearch) merged, point to step 4
```

Total: 6 new commits + 3 cherry-picked = 9 commits going into main.
