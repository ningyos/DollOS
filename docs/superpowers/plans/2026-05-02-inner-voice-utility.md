# Inner Voice + VoM RECALL Utility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `InnerVoice.recall(query) → "RECALL:\n..."` — a small-LLM-backed memory recall synthesizer for VoM, built on Plan 3's Provider/Template abstraction and Plan 2's Memory.

**Architecture:** New file `src/dollos/inner_voice.py` exposes one class `InnerVoice` with single async method `recall()`. Construction takes `(memory: Memory, llm: LLMAdapter)` — both abstract — so unit tests can mock without HTTP. New `Qwen3PlainTemplate` joins Plan 3's templates module. New `[inner_voice]` config section + `build_inner_voice()` factory wire production runs to `LlamaCppProvider + Qwen3PlainTemplate`. `daemon.py` request handler is NOT modified (Plan 5 wires the Inner Voice into Doll turns).

**Tech Stack:**
- Python 3.13+, existing project (no new deps)
- pydantic v2 for config (already in)
- pytest + pytest-asyncio for tests (already in)

**Spec reference:** `docs/superpowers/specs/2026-05-02-inner-voice-utility-design.md`

---

## File Structure

After this plan, the new and modified files are:

```
src/dollos/
├── inner_voice.py             # NEW: InnerVoice class + system prompt
├── config.py                   # MODIFY: add InnerVoiceConfig + Settings.inner_voice
├── daemon.py                   # MODIFY: add build_inner_voice() factory
└── llm/
    └── templates.py            # MODIFY: add Qwen3PlainTemplate

tests/
├── test_inner_voice.py         # NEW: InnerVoice.recall() behavior
├── test_llm_templates.py       # MODIFY: append Qwen3PlainTemplate tests
├── test_config.py              # MODIFY: append inner_voice config test
└── test_daemon_factories.py    # NEW: build_inner_voice() factory smoke

config.example.toml             # MODIFY: append [inner_voice] section
```

`daemon.py` `Daemon` class itself is NOT modified — only a free `build_inner_voice()` factory function is added. The factory is unused by the daemon at runtime in v1; Plan 5 will call it.

---

## Worktree Setup

Run from main repo root, before any task.

```bash
cd /home/progcat/Projects/DollOS
git worktree add .worktrees/inner-voice -b feature/inner-voice
cd .worktrees/inner-voice
uv sync
uv run pytest 2>&1 | tail -3
```

Expected baseline: **69 passed** (Plan 1 + Plan 2 + Plan 3 merged on main).

All subsequent task commands run in `/home/progcat/Projects/DollOS/.worktrees/inner-voice/`.

---

## Task 1: Qwen3PlainTemplate

**Files:**
- Modify: `src/dollos/llm/templates.py`
- Modify: `tests/test_llm_templates.py`

`Qwen3PlainTemplate` is a sibling of `Qwen3ThinkingTemplate` — same ChatML envelope but no `<think>` block opener. Used by Inner Voice with non-thinking small models (Qwen3-0.6B/1.7B Instruct).

- [ ] **Step 1: Append failing tests to `tests/test_llm_templates.py`**

Open the file. After the existing tests, append:

```python
from dollos.llm.templates import Qwen3PlainTemplate


def test_qwen3_plain_renders_chatml_envelope_without_think():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="SYS", user="USR", prefill="")

    assert "<|im_start|>system\nSYS\n<|im_end|>" in out
    assert "<|im_start|>user\nUSR\n<|im_end|>" in out
    assert "<|im_start|>assistant\n" in out
    # Critical: no <think> block opened
    assert "<think>" not in out


def test_qwen3_plain_appends_prefill_after_assistant_marker():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="s", user="u", prefill="bullet 1\nbullet 2")
    assert out.endswith("<|im_start|>assistant\nbullet 1\nbullet 2")


def test_qwen3_plain_empty_prefill_ends_with_assistant_marker():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="s", user="u", prefill="")
    # When prefill is empty, prompt ends with the assistant turn opener
    assert out.endswith("<|im_start|>assistant\n")


def test_qwen3_plain_preserves_special_chars_in_inputs():
    tpl = Qwen3PlainTemplate()
    out = tpl.render(system="multi\nline", user="<tag>", prefill="")
    assert "multi\nline" in out
    assert "<tag>" in out


def test_qwen3_plain_subclasses_prompt_template():
    from dollos.llm.templates import PromptTemplate
    assert issubclass(Qwen3PlainTemplate, PromptTemplate)
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/inner-voice
uv run pytest tests/test_llm_templates.py -v
```

Expected: 5 new tests fail with `ImportError: cannot import name 'Qwen3PlainTemplate' from 'dollos.llm.templates'`. Existing 5 tests still pass.

- [ ] **Step 3: Add `Qwen3PlainTemplate` to `src/dollos/llm/templates.py`**

Open the file. After the existing `Qwen3ThinkingTemplate` class, append:

```python
class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x instruct (non-thinking) ChatML.

    Same envelope as Qwen3ThinkingTemplate but does NOT open a <think>
    block — Inner Voice's small models (Qwen3-0.6B/1.7B Instruct) are
    not trained to use <think>...</think>; opening one would confuse
    them. Prefill goes directly inside the assistant turn.
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
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
```

- [ ] **Step 4: Run tests, verify all pass**

```bash
uv run pytest tests/test_llm_templates.py -v
```

Expected: 10 tests PASS (5 thinking + 5 plain).

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 74 passed (69 baseline + 5 new).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_templates.py
git commit -m "feat(llm): Qwen3PlainTemplate for non-thinking small models"
```

---

## Task 2: InnerVoice class + system prompt

**Files:**
- Create: `src/dollos/inner_voice.py`
- Create: `tests/test_inner_voice.py`

The core deliverable. `InnerVoice` takes `Memory` and `LLMAdapter` ABCs, exposes single async method `recall()`. Tests use a fake LLMAdapter (yields canned chunks) plus real `:memory:` SQLite Memory + `StubEmbedder`.

- [ ] **Step 1: Write the failing test `tests/test_inner_voice.py`**

```python
"""Tests for InnerVoice.recall() behavior."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dollos.inner_voice import INNER_VOICE_SYSTEM_PROMPT, InnerVoice
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.memory.embedder import StubEmbedder
from dollos.memory.store import Memory


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


@pytest.mark.asyncio
async def test_recall_with_facts_returns_recall_block(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("the sky is blue")
    await mem.write("user likes coffee")

    fake_llm = _FakeLLMAdapter(response="- user likes coffee\n- the sky is blue")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    block = await iv.recall("what about coffee?")

    assert block.startswith("RECALL:\n")
    assert "user likes coffee" in block
    assert block.endswith("\n")
    await mem.close()


@pytest.mark.asyncio
async def test_recall_passes_system_prompt_to_llm(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("a fact")

    fake_llm = _FakeLLMAdapter(response="- a fact")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    await iv.recall("query")

    assert fake_llm.last_system == INNER_VOICE_SYSTEM_PROMPT
    assert fake_llm.call_count == 1
    await mem.close()


@pytest.mark.asyncio
async def test_recall_user_block_includes_query_and_candidates(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("fact alpha")
    await mem.write("fact beta")

    fake_llm = _FakeLLMAdapter(response="- fact alpha")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    await iv.recall("alpha?")

    user_block = fake_llm.last_user
    assert user_block is not None
    assert "Query: alpha?" in user_block
    assert "Candidates:" in user_block
    # Candidates appear with 1-based numbering
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
    iv = InnerVoice(memory=mem, llm=fake_llm)

    await iv.recall("q")

    assert fake_llm.last_prefill == ""
    await mem.close()


@pytest.mark.asyncio
async def test_recall_empty_memory_returns_no_relevant_block(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fake_llm = _FakeLLMAdapter(response="should not be called")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    block = await iv.recall("anything")

    # Empty memory → no LLM call, return canned empty block.
    assert block == "RECALL:\n(no relevant memories)\n"
    assert fake_llm.call_count == 0
    await mem.close()


@pytest.mark.asyncio
async def test_recall_strips_whitespace_from_model_output(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("fact")

    fake_llm = _FakeLLMAdapter(response="  \n- fact\n  \n")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    block = await iv.recall("q")

    # Model output is stripped before being wrapped.
    assert block == "RECALL:\n- fact\n"
    await mem.close()


@pytest.mark.asyncio
async def test_recall_passes_character_id_to_memory(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    # Shared fact and a private one for "gura"
    await mem.write("shared fact")
    await mem.write("private gura fact", character_id="gura")
    await mem.write("private rin fact", character_id="rin")

    fake_llm = _FakeLLMAdapter(response="- shared fact\n- private gura fact")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    await iv.recall("anything", character_id="gura")

    user_block = fake_llm.last_user
    assert user_block is not None
    # gura sees shared + own private; never rin's private
    assert "shared fact" in user_block
    assert "private gura fact" in user_block
    assert "private rin fact" not in user_block
    await mem.close()


@pytest.mark.asyncio
async def test_recall_top_k_limits_candidates(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    for i in range(20):
        await mem.write(f"fact {i}")

    fake_llm = _FakeLLMAdapter(response="- fact 0")
    iv = InnerVoice(memory=mem, llm=fake_llm)

    await iv.recall("anything", top_k=3)

    user_block = fake_llm.last_user
    assert user_block is not None
    # Memory.search returns top_k facts, all of which appear in the prompt.
    # We can't assert exact contents (RRF ordering is non-deterministic for
    # equal scores) but we can assert at most 3 numbered lines are present.
    numbered_lines = [
        line for line in user_block.split("\n") if line and line[0].isdigit()
    ]
    assert len(numbered_lines) == 3
    await mem.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/inner-voice
uv run pytest tests/test_inner_voice.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.inner_voice'`.

- [ ] **Step 3: Write `src/dollos/inner_voice.py`**

```python
"""InnerVoice — small-model VoM RECALL block synthesizer.

InnerVoice is the entry point to DollOS's signature feature: read a query,
pull relevant facts from Memory, and synthesize a concise RECALL block via
a small LLM. Caller (Plan 5 Conversation Engine) embeds the resulting
string into the prefill for Doll's turn.

This module is pure utility — it doesn't process events, write memory,
manage state, or know about characters beyond passing character_id through
to Memory.search().
"""

from dollos.llm.adapter import LLMAdapter
from dollos.memory.store import Memory


INNER_VOICE_SYSTEM_PROMPT = """\
You are Doll's memory recall helper. Read the query and candidate facts \
from memory, output ONLY the facts relevant to the query as bullets.

Rules:
- One bullet per relevant fact: "- <fact in concise prose>"
- If a candidate is irrelevant, skip it
- Do NOT add facts not in candidates
- Do NOT speculate or fill gaps
- Output bullets only. Don't repeat the query, don't add header.
- If no candidates are relevant, output a single line: (no relevant memories)
"""


class InnerVoice:
    """Synthesize VoM RECALL blocks from memory using a small LLM."""

    def __init__(self, memory: Memory, llm: LLMAdapter) -> None:
        self._memory = memory
        self._llm = llm

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
        user_block = f"Query: {query}\n\nCandidates:\n{candidates}"

        chunks: list[str] = []
        async for chunk in self._llm.stream_completion(
            system=INNER_VOICE_SYSTEM_PROMPT,
            user=user_block,
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        body = "".join(chunks).strip()
        return f"RECALL:\n{body}\n"
```

- [ ] **Step 4: Run tests, verify all pass**

```bash
uv run pytest tests/test_inner_voice.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 82 passed (74 + 8).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/inner_voice.py tests/test_inner_voice.py
git commit -m "feat: InnerVoice utility for VoM RECALL synthesis"
```

---

## Task 3: InnerVoiceConfig + Settings

**Files:**
- Modify: `src/dollos/config.py`
- Modify: `tests/test_config.py`

Adds `[inner_voice]` config section. Minimal: `base_url` + `timeout_s`.

- [ ] **Step 1: Append failing tests to `tests/test_config.py`**

After the existing tests, append:

```python
def test_load_settings_includes_inner_voice(tmp_path: Path):
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

[memory]
db_path = "/tmp/test/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"

[inner_voice]
base_url = "http://127.0.0.1:8003"
timeout_s = 30.0
"""
    )

    settings = load_settings(config_path)

    assert settings.inner_voice.base_url == "http://127.0.0.1:8003"
    assert settings.inner_voice.timeout_s == 30.0


def test_inner_voice_timeout_default(tmp_path: Path):
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

[memory]
db_path = "/tmp/test/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"

[inner_voice]
base_url = "http://127.0.0.1:8003"
"""
    )

    settings = load_settings(config_path)

    # Default timeout is 30.0 seconds when not specified.
    assert settings.inner_voice.timeout_s == 30.0


def test_inner_voice_section_required(tmp_path: Path):
    """Settings.inner_voice has no default — must be present in config."""
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

[memory]
db_path = "/tmp/test/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"
# missing [inner_voice]
"""
    )

    with pytest.raises(ValueError):
        load_settings(config_path)
```

Note: existing config tests already use the post-Plan-3 schema (provider/template). They need their TOML fixtures extended with the `[inner_voice]` section since the new field is required. Update each existing test fixture to add `[inner_voice]` block. Specifically, the existing tests `test_load_settings_from_toml`, `test_load_settings_default_log_level`, `test_load_settings_includes_memory_and_embedder`, `test_settings_db_path_expands_user`, `test_load_settings_old_backend_field_raises`, `test_load_settings_unknown_provider_raises` — each writes a `config.toml` with a TOML string that lacks `[inner_voice]`. Append `[inner_voice]\nbase_url = "http://127.0.0.1:8003"\n` to every such TOML literal. The `test_load_settings_missing_required_field` test already expects a ValueError so it can stay as-is (the missing `inner_voice` will be one of the missing fields it raises on).

Inspect and edit:

```bash
cd /home/progcat/Projects/DollOS/.worktrees/inner-voice
grep -n "tmp_path" tests/test_config.py | head
```

For each test that constructs a TOML string with sections like `[llm]`, `[ipc]`, etc., add:

```
[inner_voice]
base_url = "http://127.0.0.1:8003"
```

at the end of the TOML literal.

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 3 new tests fail (`AttributeError: 'Settings' object has no attribute 'inner_voice'` for the first two, ValueError already-raises for the third). Existing tests fail because their fixtures now include `[inner_voice]` but `Settings` doesn't accept it.

- [ ] **Step 3: Modify `src/dollos/config.py`**

Open the file. After the existing `EmbedderConfig` class (and before `Settings`), add:

```python
class InnerVoiceConfig(BaseModel):
    base_url: str
    timeout_s: float = 30.0
```

Then update `Settings` to require it:

```python
class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    memory: MemoryConfig
    embedder: EmbedderConfig
    inner_voice: InnerVoiceConfig
```

(Adds the field at the end. No default — must be present in config.)

- [ ] **Step 4: Run tests, verify all pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all config tests pass (existing fixtures have `[inner_voice]`, new tests pass).

- [ ] **Step 5: Run full suite — but expect test_e2e to break**

```bash
uv run pytest -v 2>&1 | tail -10
```

Expected: `tests/test_e2e.py::test_full_round_trip_with_mocked_llamacpp` FAILS — the test constructs `Settings(...)` directly without `inner_voice`. Need to fix it in this same task (since the rename is interdependent like Plan 3 Task 4).

- [ ] **Step 6: Update `tests/test_e2e.py`**

Find the existing `Settings(...)` construction in `test_e2e.py`. It currently looks like:

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
    memory=MemoryConfig(db_path=Path("/tmp/dollos-test.db")),
    embedder=EmbedderConfig(
        backend="llamacpp",
        base_url="http://test.local:8002",
        model_id="test-emb",
    ),
)
```

Add the import at the top of `test_e2e.py`:

```python
from dollos.config import (
    EmbedderConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)
```

(Add `InnerVoiceConfig` to whatever import group is already there; the rest of the imports likely already exist.)

Update the `Settings(...)` call to include `inner_voice`:

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
    memory=MemoryConfig(db_path=Path("/tmp/dollos-test.db")),
    embedder=EmbedderConfig(
        backend="llamacpp",
        base_url="http://test.local:8002",
        model_id="test-emb",
    ),
    inner_voice=InnerVoiceConfig(base_url="http://test.local:8003"),
)
```

- [ ] **Step 7: Run full suite again — verify all green**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 85 passed (82 + 3 new config tests).

- [ ] **Step 8: Commit**

```bash
git add src/dollos/config.py tests/test_config.py tests/test_e2e.py
git commit -m "feat(config): add [inner_voice] section with base_url + timeout_s"
```

---

## Task 4: build_inner_voice() factory + smoke test

**Files:**
- Modify: `src/dollos/daemon.py`
- Create: `tests/test_daemon_factories.py`

Adds a free `build_inner_voice(settings, memory) -> InnerVoice` function. NOT called from `Daemon.__init__` — Plan 5 will integrate. This task just makes the wiring available.

- [ ] **Step 1: Write the failing test `tests/test_daemon_factories.py`**

```python
"""Tests for daemon factory functions."""

from pathlib import Path

import pytest

from dollos.config import (
    EmbedderConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)
from dollos.daemon import build_inner_voice
from dollos.inner_voice import InnerVoice
from dollos.memory.embedder import StubEmbedder
from dollos.memory.store import Memory


def _make_settings() -> Settings:
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        memory=MemoryConfig(db_path=Path("/tmp/dollos-test.db")),
        embedder=EmbedderConfig(
            backend="llamacpp",
            base_url="http://test.local:8002",
            model_id="emb",
        ),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=15.0,
        ),
    )


@pytest.mark.asyncio
async def test_build_inner_voice_returns_innervoice_instance(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    iv = build_inner_voice(_make_settings(), mem)
    assert isinstance(iv, InnerVoice)

    await mem.close()


@pytest.mark.asyncio
async def test_build_inner_voice_uses_inner_voice_config_base_url(tmp_path: Path):
    """The factory must point the InnerVoice's provider at inner_voice.base_url,
    not the main LLM's base_url."""
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    iv = build_inner_voice(_make_settings(), mem)
    # Reach into the composed adapter to verify provider config.
    # InnerVoice exposes neither, so we touch private attrs deliberately.
    assert iv._llm._provider._base_url == "http://test.local:8003"
    assert iv._llm._provider._timeout_s == 15.0
    await mem.close()


@pytest.mark.asyncio
async def test_build_inner_voice_uses_qwen3_plain_template(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    from dollos.llm.templates import Qwen3PlainTemplate

    iv = build_inner_voice(_make_settings(), mem)
    assert isinstance(iv._llm._template, Qwen3PlainTemplate)
    await mem.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/inner-voice
uv run pytest tests/test_daemon_factories.py -v
```

Expected: FAIL with `ImportError: cannot import name 'build_inner_voice' from 'dollos.daemon'`.

- [ ] **Step 3: Modify `src/dollos/daemon.py`**

Add imports near the top (after existing imports):

```python
from dollos.inner_voice import InnerVoice
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.memory.store import Memory
```

(Some of these may already be imported from Plan 3 work; if so, add only the missing ones — `InnerVoice`, `Qwen3PlainTemplate`, `Memory`. Keep imports alphabetized within their group.)

Add the factory function. Place it next to the existing `build_adapter()`:

```python
def build_inner_voice(settings: Settings, memory: Memory) -> InnerVoice:
    """Construct InnerVoice wired to a small llama.cpp model.

    v1 hardcodes (LlamaCppProvider, Qwen3PlainTemplate). Future plans
    may extend [inner_voice] config with provider/template fields and
    branch here.
    """
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    template = Qwen3PlainTemplate()
    llm = ComposedLLMAdapter(provider=provider, template=template)
    return InnerVoice(memory=memory, llm=llm)
```

Don't call `build_inner_voice()` from `Daemon.__init__` — leave it as a free function for Plan 5 / tests.

- [ ] **Step 4: Run tests, verify all pass**

```bash
uv run pytest tests/test_daemon_factories.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 88 passed (85 + 3).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/daemon.py tests/test_daemon_factories.py
git commit -m "feat: build_inner_voice() factory wires Provider + Template + Memory"
```

---

## Task 5: config.example.toml + final smoke

**Files:**
- Modify: `config.example.toml`

- [ ] **Step 1: Append `[inner_voice]` section to `config.example.toml`**

Open the file. After the `[embedder]` section (or wherever the existing config sections end), append:

```toml

[inner_voice]
base_url = "http://127.0.0.1:8003"   # separate llama-server with small Qwen3 instruct (e.g. Qwen3-0.6B-Instruct)
timeout_s = 30.0
```

(Note the leading blank line for readability between sections.)

- [ ] **Step 2: Verify the example loads cleanly**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/inner-voice
uv run python -c "
from pathlib import Path
from dollos.config import load_settings
s = load_settings(Path('config.example.toml'))
print('inner_voice.base_url:', s.inner_voice.base_url)
print('inner_voice.timeout_s:', s.inner_voice.timeout_s)
"
```

Expected:
```
inner_voice.base_url: http://127.0.0.1:8003
inner_voice.timeout_s: 30.0
```

- [ ] **Step 3: Smoke test — daemon starts cleanly with new config**

```bash
cp config.example.toml config.toml
timeout 2 uv run python -m dollos --config config.toml 2>&1 || true
```

Expected: log line `WebSocket server listening on 127.0.0.1:9876`. (The daemon doesn't use Inner Voice at runtime in v1; this only verifies config parsing doesn't break startup.)

- [ ] **Step 4: Run full suite final time**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: 88 passed.

- [ ] **Step 5: Commit**

```bash
git add config.example.toml
git commit -m "docs(config): add [inner_voice] section to example"
```

---

## Done — What This Plan Produced

After all tasks complete you have:

- `Qwen3PlainTemplate` in `src/dollos/llm/templates.py` — non-thinking ChatML for small models
- `InnerVoice` class in `src/dollos/inner_voice.py` with `recall(query, character_id, top_k) → "RECALL:\n..."`
- `INNER_VOICE_SYSTEM_PROMPT` constant defining the small model's behavior contract
- `InnerVoiceConfig` config schema + `[inner_voice]` TOML section
- `build_inner_voice(settings, memory)` factory in `daemon.py`
- 88 passing automated tests (69 baseline + 19 added)

**What is NOT in this plan (deferred):**
- `Daemon.__init__` does NOT construct InnerVoice (Plan 5 will)
- Inner Voice is NOT routed into the WS request handler (Plan 5)
- No real-server smoke test against a running small llama-server (manual; out of scope for automated tests)
- No `digest` / `classify` / `extract` / `tag` / `compress` capabilities (Plan 11)
- No mood / SELF_STATE (Plan 7)

Next plan: **Conversation Engine + Character Pack** (Plan 5) — wires `InnerVoice.recall()` into Doll's turn prefill, loads `.doll` v3 character packs, integrates Memory writes after turn ends.

---

## Self-Review

**Spec coverage check** (each spec section → which task implements it):
- §0 scope (recall only, no event handling) → respected; tests assert no extra capabilities
- §1 motivation (VoM small-model synthesis) → Task 2 implements the synthesis flow
- §2 architecture (InnerVoice + Memory + LLMAdapter) → Task 2 + Task 4 wire all three
- §3 file structure → matches plan exactly
- §4 Qwen3PlainTemplate → Task 1
- §5 InnerVoice class + recall() signature → Task 2
- §5.1 design points (always RECALL prefix, empty memory branch, mode="hybrid", top_k=10, prefill="", drain stream, character_id passthrough) → all asserted in Task 2 tests
- §5.2 explicit non-features (no retry, no caching, etc.) → not implemented, no tests for them
- §6 system prompt — verbatim in spec → verbatim in Task 2 step 3
- §7.1 InnerVoiceConfig minimal → Task 3
- §7.2 build_inner_voice() factory → Task 4
- §8 edge cases — empty memory branch tested in Task 2; other edges (timeout/exception) deliberately bubble up, not tested explicitly
- §9 testing strategy — Task 1 (templates), Task 2 (inner_voice), Task 4 (factory). E2E with real small model is manual and out of scope
- §10 Non-goals → not implemented (correctly)
- §11 Open Questions → §11 small model selection: Plan defers to per-deployment config (base_url choice). top_k=10 default codified in Task 2.
- §12 Plan task count (5) → 5 tasks here

**Placeholder scan:** searched for "TBD", "TODO", "implement later", "fill in details", "Add appropriate" — none present.

**Type consistency check:**
- `InnerVoice.__init__(memory: Memory, llm: LLMAdapter)` — same in Task 2 implementation, Task 4 factory tests
- `InnerVoice.recall(query, *, character_id=None, top_k=10) -> str` — consistent across Task 2 tests, implementation, Task 4 factory tests, spec §5
- `INNER_VOICE_SYSTEM_PROMPT` constant — referenced from Task 2 implementation and Task 2 tests
- `Qwen3PlainTemplate.render(*, system, user, prefill) -> str` — same as Plan 3's `PromptTemplate` ABC; Task 1 tests assert subclass relationship
- `InnerVoiceConfig(base_url: str, timeout_s: float = 30.0)` — consistent in Task 3 schema, Task 3 tests, Task 4 factory tests, Task 5 example
- `build_inner_voice(settings: Settings, memory: Memory) -> InnerVoice` — consistent in Task 4 implementation and tests

No inconsistencies found.
