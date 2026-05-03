# Prompt Rendering Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `PromptRenderer.render(template_name, **ctx) → str` — a jinja2-backed renderer for generic prompt scaffolding, plus rename `daemon.py / Daemon → kernel.py / DollOS`, plus load an external character profile jinja file as `{{ character }}` ctx into the system prompt.

**Architecture:** New `src/dollos/prompts/` package hosts `PromptRenderer` and a generic `scaffolding.jinja` template (no character identity baked in — only conditional blocks for character / rules / tools / examples). Character profile is a plain jinja file loaded from `settings.character.profile_path`; daemon reads it once at startup, passes content as `character` ctx when rendering the system prompt for each turn. Step 10 (character pack) will swap the file-path source for `.doll` zip extraction.

**Tech Stack:**
- jinja2 ≥ 3.1 (new dep)
- Python 3.13+, pydantic v2, pytest + pytest-asyncio (already in)

**Spec reference:** `docs/superpowers/specs/2026-05-03-prompt-rendering-design.md`

---

## File Structure

After this plan, the new and modified files are:

```
src/dollos/
├── kernel.py                       # RENAMED from daemon.py; class Daemon → DollOS
├── __main__.py                     # MODIFY: import from dollos.kernel
├── config.py                       # MODIFY: add CharacterConfig + Settings.character
└── prompts/
    ├── __init__.py                 # NEW: exports PromptRenderer
    ├── renderer.py                 # NEW: PromptRenderer
    └── templates/
        ├── scaffolding.jinja       # NEW: generic scaffolding template
        └── _test_fixture.jinja     # NEW: tiny fixture used by tests

experiments/
└── test_character.jinja            # NEW: Gura mock character profile (dev only)

tests/
├── test_prompt_renderer.py         # NEW: PromptRenderer behavior tests
├── test_config.py                  # MODIFY: add 2 character tests + extend 6 fixtures
└── test_e2e.py                     # MODIFY: import DollOS from kernel, add CharacterConfig

config.example.toml                 # MODIFY: append [character] section
pyproject.toml                      # MODIFY: add jinja2 dep
```

---

## Worktree Setup

Run from main repo root, before any task.

```bash
cd /home/progcat/Projects/DollOS
git worktree add .worktrees/step-2-prompt-rendering -b step-2-prompt-rendering
cd .worktrees/step-2-prompt-rendering
uv sync
uv run pytest 2>&1 | tail -3
```

Expected baseline: **69 passed** (Plan 1 + Plan 2 + Plan 3 merged on main; Plan 4 not merged).

All subsequent task commands run in `/home/progcat/Projects/DollOS/.worktrees/step-2-prompt-rendering/`.

---

## Task 1: Add jinja2 dep + experiments/test_character.jinja

**Files:**
- Modify: `pyproject.toml`
- Create: `experiments/test_character.jinja`

- [ ] **Step 1: Add jinja2 to dependencies in `pyproject.toml`**

Open `pyproject.toml`. In the `[project]` table, modify `dependencies` to add `jinja2`:

```toml
[project]
name = "dollos"
version = "0.1.0"
description = "DollOS — Python brain: event loop, Instinct, Memory, Conversation Engine"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.27",
    "jinja2>=3.1",
    "pydantic>=2.6",
    "sqlite-vec>=0.1",
    "websockets>=12.0",
]
```

(Keep alphabetical ordering.)

- [ ] **Step 2: Sync dependencies**

Run:
```bash
uv sync
```

Expected: jinja2 resolves and installs; no conflicts.

- [ ] **Step 3: Create `experiments/` directory and test character file**

Create the directory if missing, then write `experiments/test_character.jinja` with this exact content:

```
You are Gura, a 9000-year-old shark.
You are curious, sometimes mischievous, and prone to "a"-laughs.
```

- [ ] **Step 4: Verify baseline tests still pass**

```bash
uv run pytest 2>&1 | tail -3
```

Expected: **69 passed** (baseline unchanged).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock experiments/test_character.jinja
git commit -m "feat: add jinja2 dep + experiments test character profile"
```

---

## Task 2: Daemon → kernel.py / DollOS rename (mechanical)

**Files:**
- Rename: `src/dollos/daemon.py` → `src/dollos/kernel.py`
- Modify: `src/dollos/kernel.py` (class name only)
- Modify: `src/dollos/__main__.py`
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Rename file with git mv**

```bash
git mv src/dollos/daemon.py src/dollos/kernel.py
```

- [ ] **Step 2: Rename class `Daemon` → `DollOS` inside `src/dollos/kernel.py`**

Find this line in `src/dollos/kernel.py`:

```python
class Daemon:
```

Replace with:

```python
class DollOS:
```

The rest of the class body (including `__init__`, `_handle_text_input`, `run`) is unchanged.

Also update the module docstring at the top:

```python
"""DollOS kernel: wires LLM adapter and IPC server together."""
```

(was: `"""Daemon: wires LLM adapter and IPC server together."""`)

- [ ] **Step 3: Update `src/dollos/__main__.py`**

Find these two lines:

```python
from dollos.daemon import Daemon
```
```python
    daemon = Daemon(settings)
    try:
        asyncio.run(daemon.run())
```

Replace with:

```python
from dollos.kernel import DollOS
```
```python
    dollos = DollOS(settings)
    try:
        asyncio.run(dollos.run())
```

- [ ] **Step 4: Update `tests/test_e2e.py`**

Find:
```python
from dollos.daemon import Daemon
```

Replace with:
```python
from dollos.kernel import DollOS
```

Find:
```python
    daemon = Daemon(settings)
```

Replace with:
```python
    dollos = DollOS(settings)
```

Find every other reference to `daemon.` (e.g. `daemon.server.start()`, `daemon.server.port`, `daemon.server.stop()`) inside the test function and replace with `dollos.`. There are three such references after the construction line.

- [ ] **Step 5: Run all tests — verify rename did not break anything**

```bash
uv run pytest -v 2>&1 | tail -3
```

Expected: **69 passed** (same count, just renamed).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/kernel.py src/dollos/__main__.py tests/test_e2e.py
git commit -m "refactor: rename Daemon → DollOS, daemon.py → kernel.py"
```

---

## Task 3: PromptRenderer + scaffolding template

**Files:**
- Create: `src/dollos/prompts/__init__.py`
- Create: `src/dollos/prompts/renderer.py`
- Create: `src/dollos/prompts/templates/scaffolding.jinja`
- Create: `src/dollos/prompts/templates/_test_fixture.jinja`
- Create: `tests/test_prompt_renderer.py`

`PromptRenderer` wraps a jinja2 `Environment` configured with a `PackageLoader` rooted at `dollos.prompts.templates`. `render(name, **ctx)` returns a string.

- [ ] **Step 1: Create the package skeleton**

Create `src/dollos/prompts/__init__.py` with this exact content:

```python
"""Prompt rendering layer — jinja2 templates for prompt content composition."""

from dollos.prompts.renderer import PromptRenderer

__all__ = ["PromptRenderer"]
```

- [ ] **Step 2: Create the templates directory + scaffolding template**

Create `src/dollos/prompts/templates/scaffolding.jinja` with this exact content:

```jinja
{%- if character %}
{{ character }}
{%- endif %}
{%- if rules %}

Rules:
{%- for rule in rules %}
- {{ rule }}
{%- endfor %}
{%- endif %}
{%- if tools %}

Available tools:
{%- for tool in tools %}
- {{ tool.name }}: {{ tool.description }}
{%- endfor %}
{%- endif %}
{%- if examples %}

Examples:
{%- for example in examples %}
{{ example }}
{%- endfor %}
{%- endif %}
```

(Note: `{%-` and `-%}` whitespace control trims surrounding whitespace so missing sections don't leave blank lines.)

- [ ] **Step 3: Create the test fixture template**

Create `src/dollos/prompts/templates/_test_fixture.jinja` with this exact content:

```jinja
{{ greeting }}
```

(Single line, exact: `{{ greeting }}` followed by a newline.)

- [ ] **Step 4: Write failing tests in `tests/test_prompt_renderer.py`**

Create `tests/test_prompt_renderer.py` with this exact content:

```python
"""Tests for PromptRenderer."""

import pytest
from jinja2 import TemplateNotFound

from dollos.prompts import PromptRenderer


def test_render_scaffolding_with_character_includes_text():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Gura.")
    assert "You are Gura." in out


def test_render_scaffolding_no_ctx_returns_empty():
    """No ctx vars → all conditional blocks skipped → output is empty / whitespace only."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding")
    assert out.strip() == ""


def test_render_with_ctx_substitutes_variables():
    renderer = PromptRenderer()
    out = renderer.render("_test_fixture", greeting="hi")
    assert out.strip() == "hi"


def test_render_unknown_template_raises():
    renderer = PromptRenderer()
    with pytest.raises(TemplateNotFound):
        renderer.render("does_not_exist")


def test_renderer_does_not_html_escape():
    """Prompts are plain text, not HTML — angle brackets must pass through verbatim."""
    renderer = PromptRenderer()
    out = renderer.render("_test_fixture", greeting="<tag>")
    assert "<tag>" in out
```

- [ ] **Step 5: Run tests — verify they fail**

```bash
uv run pytest tests/test_prompt_renderer.py -v
```

Expected: 5 tests FAIL with `ModuleNotFoundError: No module named 'dollos.prompts.renderer'` (the renderer module hasn't been written yet).

- [ ] **Step 6: Write `src/dollos/prompts/renderer.py`**

Create `src/dollos/prompts/renderer.py` with this exact content:

```python
"""PromptRenderer — render jinja2 templates from the embedded templates package."""

from jinja2 import Environment, PackageLoader, select_autoescape


class PromptRenderer:
    """Render jinja2 templates from the embedded `dollos.prompts.templates` package.

    Caller passes a template name (without `.jinja` suffix) and ctx kwargs;
    receives back the rendered string. Autoescape is OFF — prompts are plain
    text, not HTML.
    """

    def __init__(self) -> None:
        self._env = Environment(
            loader=PackageLoader("dollos.prompts", "templates"),
            autoescape=select_autoescape(disabled_extensions=("jinja",), default=False),
            keep_trailing_newline=False,
        )

    def render(self, template_name: str, **ctx: object) -> str:
        """Render the named template with ctx vars and return the resulting string.

        template_name must NOT include the `.jinja` suffix; "scaffolding" loads
        "scaffolding.jinja". Raises jinja2.TemplateNotFound if the template
        isn't found in the templates package.
        """
        template = self._env.get_template(f"{template_name}.jinja")
        return template.render(**ctx)
```

- [ ] **Step 7: Run tests — verify they pass**

```bash
uv run pytest tests/test_prompt_renderer.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 8: Run full suite — verify no regressions**

```bash
uv run pytest 2>&1 | tail -3
```

Expected: **74 passed** (69 baseline + 5 new).

- [ ] **Step 9: Commit**

```bash
git add src/dollos/prompts tests/test_prompt_renderer.py
git commit -m "feat(prompts): PromptRenderer + scaffolding template"
```

---

## Task 4: CharacterConfig + Settings + test_config fixture updates

**Files:**
- Modify: `src/dollos/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_e2e.py`

`Settings` gains a required `character: CharacterConfig` field (no default — same migration pattern as Plan 4 InnerVoiceConfig). Existing config TOML fixtures all need a `[character]` section appended.

- [ ] **Step 1: Append failing tests to `tests/test_config.py`**

Open `tests/test_config.py`. After the existing tests (after `test_load_settings_unknown_provider_raises`), append:

```python
def test_load_settings_includes_character(tmp_path: Path):
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

[character]
profile_path = "experiments/test_character.jinja"
"""
    )

    settings = load_settings(config_path)

    assert str(settings.character.profile_path) == "experiments/test_character.jinja"


def test_character_section_required(tmp_path: Path):
    """Settings.character has no default — must be present in config."""
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
# missing [character]
"""
    )

    with pytest.raises(ValueError):
        load_settings(config_path)
```

- [ ] **Step 2: Append `[character]` to existing TOML fixtures in `tests/test_config.py`**

Five existing tests build TOML strings that will now be missing a required field. Each one needs `[character]\nprofile_path = "experiments/test_character.jinja"\n` appended to the TOML string literal:

1. `test_load_settings_from_toml` (around line 14)
2. `test_load_settings_default_log_level` (around line 67)
3. `test_load_settings_includes_memory_and_embedder` (around line 96)
4. `test_settings_db_path_expands_user` (around line 133)
5. `test_load_settings_unknown_provider_raises` (around line 192)

For each test, find the closing `"""` of the multi-line TOML string and insert before it:

```toml

[character]
profile_path = "experiments/test_character.jinja"
```

(Including the leading blank line for readability.)

EXCEPTION 1: `test_load_settings_missing_required_field` (around line 51) — leave this one alone. It already expects `ValueError` because `base_url` and `model_alias` are missing; missing `[character]` will only add to the rejection reasons, which is fine.

EXCEPTION 2: `test_load_settings_old_backend_field_raises` (around line 160) — this test expects `ValidationError` due to the old `backend` field on `[llm]`. Append `[character]` so the only rejection reason is the `[llm]` issue (otherwise the missing `[character]` would also trigger rejection, masking the test's intent).

So `test_load_settings_old_backend_field_raises` ALSO gets the `[character]` appendix — total 6 fixtures updated.

- [ ] **Step 3: Run config tests — verify failures**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 2 new tests FAIL (`AttributeError: 'Settings' object has no attribute 'character'` and ValueError-already-raises). Existing fixtures may also fail because they now contain `[character]` but `Settings` doesn't accept it.

- [ ] **Step 4: Modify `src/dollos/config.py`**

Open `src/dollos/config.py`. Add a `CharacterConfig` class after `EmbedderConfig` (line 45) and before `Settings` (line 47):

```python
class CharacterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_path: Path

    @field_validator("profile_path", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v
```

Then update `Settings` to add the `character` field:

```python
class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    memory: MemoryConfig
    embedder: EmbedderConfig
    character: CharacterConfig
```

(Required field — no default, no Field(default_factory=...).)

- [ ] **Step 5: Run config tests — verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all config tests PASS.

- [ ] **Step 6: Update `tests/test_e2e.py` Settings construction**

Open `tests/test_e2e.py`. The Settings construction at the top of `test_full_round_trip_with_mocked_llamacpp` is missing the new required `character` field. Update the imports at the top:

```python
from dollos.config import (
    CharacterConfig,
    EmbedderConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)
```

(Add `CharacterConfig` alphabetically.)

Then update the `Settings(...)` call. Find:

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

Replace with (added `character=...`):

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
        character=CharacterConfig(
            profile_path=Path("experiments/test_character.jinja"),
        ),
    )
```

- [ ] **Step 7: Run full suite — verify all green**

```bash
uv run pytest 2>&1 | tail -3
```

Expected: **76 passed** (74 + 2 new config tests). Existing e2e test still passes because `DollOS` doesn't yet read `settings.character` (Task 5 wires that).

- [ ] **Step 8: Commit**

```bash
git add src/dollos/config.py tests/test_config.py tests/test_e2e.py
git commit -m "feat(config): add CharacterConfig with profile_path"
```

---

## Task 5: Kernel integration — load profile + render system prompt

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_e2e.py`

`DollOS.__init__` reads `settings.character.profile_path` once and stores its content. `_handle_text_input` uses `PromptRenderer` to produce the system prompt with `character=<profile_text>` ctx, replacing the hardcoded `PLACEHOLDER_SYSTEM_PROMPT` constant.

- [ ] **Step 1: Modify `src/dollos/kernel.py` — add imports and renderer/profile init**

Open `src/dollos/kernel.py`. Update the imports section. Find:

```python
from dollos.config import Settings
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
```

Replace with (added `PromptRenderer` import):

```python
from dollos.config import Settings
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer
```

Then find and **delete** these lines (the placeholder constant):

```python
PLACEHOLDER_SYSTEM_PROMPT = "You are Doll, a helpful AI companion."
"""Placeholder until character pack loading lands in a later plan."""
```

(Just delete them — three lines including the blank line above and below if present. Keep one blank line of separation between the `logger = ...` line and `def build_adapter(...)`.)

Then find `class DollOS:` and update `__init__` to construct the renderer and pre-load the character profile. Find:

```python
class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()
```

Replace with:

```python
class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self._character_profile = settings.character.profile_path.read_text()
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()
```

Then update `_handle_text_input` to use the renderer. Find:

```python
    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            async for chunk in self.adapter.stream_completion(
                system=PLACEHOLDER_SYSTEM_PROMPT,
                user=msg.text,
                prefill="",
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")
```

Replace with:

```python
    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            system = self.renderer.render(
                "scaffolding",
                character=self._character_profile,
            )
            async for chunk in self.adapter.stream_completion(
                system=system,
                user=msg.text,
                prefill="",
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")
```

- [ ] **Step 2: Update `tests/test_e2e.py` to use a temporary character file**

The e2e test currently uses `Path("experiments/test_character.jinja")` which works only if cwd is the repo root. Make the test self-contained by writing a temp file. Find the existing test function signature:

```python
@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp():
```

Replace with (add `tmp_path` fixture):

```python
@pytest.mark.asyncio
async def test_full_round_trip_with_mocked_llamacpp(tmp_path: Path):
```

Then, immediately after the function definition line (before the `settings = ...`), add:

```python
    character_path = tmp_path / "test_character.jinja"
    character_path.write_text("You are Gura, a 9000-year-old shark.")

```

Then in the `Settings(...)` call, change the `character=CharacterConfig(...)` block:

```python
        character=CharacterConfig(
            profile_path=Path("experiments/test_character.jinja"),
        ),
```

Replace with:

```python
        character=CharacterConfig(
            profile_path=character_path,
        ),
```

- [ ] **Step 3: Add an assertion verifying the rendered system prompt contains the character text**

The current test asserts on text chunks but doesn't verify the system prompt. Add a way to capture the system prompt. The mocked respx route can use a side-effect callback. Find the existing mock setup:

```python
    sse_body = (
        'data: {"content": "Hi", "stop": false}\n\n'
        'data: {"content": " there", "stop": false}\n\n'
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
```

Replace with (capture the request body for assertion):

```python
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
```

Then at the bottom of the test, after the existing assertions (`assert "".join(c["text"] for c in text_chunks) == "Hi there"` and `assert received[-1]["type"] == "turn_end"`), add:

```python
            # Verify the system prompt rendered by PromptRenderer contains the character profile.
            assert len(captured_requests) == 1
            prompt = captured_requests[0]["prompt"]
            assert "You are Gura, a 9000-year-old shark." in prompt
```

(The llama.cpp `/completion` endpoint receives the full prompt as a string in the `prompt` field — the rendered system text is part of it after the ChatML wrapping by Qwen3ThinkingTemplate.)

- [ ] **Step 4: Run the e2e test — verify it passes**

```bash
uv run pytest tests/test_e2e.py -v
```

Expected: 1 test PASS. The captured prompt string contains `"You are Gura, a 9000-year-old shark."` (system content rendered through scaffolding).

- [ ] **Step 5: Run full suite — verify no regressions**

```bash
uv run pytest 2>&1 | tail -3
```

Expected: **76 passed** (same as Task 4; e2e test still 1 test, just now verifies more).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/kernel.py tests/test_e2e.py
git commit -m "feat(kernel): render system prompt via PromptRenderer + character profile"
```

---

## Task 6: config.example.toml + final smoke

**Files:**
- Modify: `config.example.toml`

- [ ] **Step 1: Append `[character]` section to `config.example.toml`**

Open `config.example.toml`. After the `[embedder]` section (the last section in the file), append (note the leading blank line for readability):

```toml

[character]
profile_path = "experiments/test_character.jinja"   # development character profile (Gura mock)
```

- [ ] **Step 2: Verify `config.example.toml` loads cleanly**

```bash
uv run python -c "
from pathlib import Path
from dollos.config import load_settings
s = load_settings(Path('config.example.toml'))
print('character.profile_path:', s.character.profile_path)
"
```

Expected output:
```
character.profile_path: experiments/test_character.jinja
```

- [ ] **Step 3: Smoke test — DollOS starts cleanly with new config**

```bash
cp config.example.toml config.toml
timeout 2 uv run python -m dollos --config config.toml > /tmp/smoke.log 2>&1 || true
cat /tmp/smoke.log
rm config.toml /tmp/smoke.log
```

Expected: log shows `WebSocket server listening on 127.0.0.1:9876` (or similar). DollOS starts, loads `experiments/test_character.jinja`, registers the WS server, then is killed by timeout.

If you see `FileNotFoundError: experiments/test_character.jinja`, ensure the Task 1 file exists in the worktree.

- [ ] **Step 4: Run full suite — final verification**

```bash
uv run pytest 2>&1 | tail -3
```

Expected: **76 passed**.

- [ ] **Step 5: Commit**

```bash
git add config.example.toml
git commit -m "docs(config): add [character] section to example"
```

---

## Done — What This Plan Produced

After all tasks complete you have:

- `dollos.prompts.PromptRenderer` — jinja2-backed renderer with `render(name, **ctx) -> str`
- `src/dollos/prompts/templates/scaffolding.jinja` — generic prompt scaffolding (no character identity baked in; conditional blocks for character / rules / tools / examples)
- `experiments/test_character.jinja` — Gura mock character profile for dev testing
- `CharacterConfig` (`profile_path: Path`) + `[character]` TOML section
- `DollOS` class (renamed from `Daemon`) in `src/dollos/kernel.py` (renamed from `daemon.py`)
- `DollOS` loads character profile at startup and renders the system prompt via `PromptRenderer` for each turn
- 76 passing automated tests (69 baseline + 5 prompt renderer + 2 character config; e2e test extended)

**What is NOT in this plan (deferred to later steps):**
- Plan 4 InnerVoice's hardcoded `recall` prompt is NOT migrated to a jinja template (deferred to step 3 VoM where Plan 4 merges)
- No `RenderedPrompt(system, user, prefill)` multi-slot return (deferred until composition needs arise)
- No `.doll` zip support (step 10 character pack)
- No tools / rules / examples ctx vars actually populated (renderer supports them; callers don't pass yet)

Next step: **Roadmap step 3 — VoM** (merge Plan 4 InnerVoice + migrate its recall prompt to `iv_recall.jinja` + connect recall result into the `DollOS._handle_text_input` prefill).

---

## Self-Review

**Spec coverage check** (each spec section → which task implements it):
- Goal (jinja2 rendering + scaffolding + external character profile + Daemon rename) → Task 1 (dep + character file), Task 2 (rename), Task 3 (renderer), Task 4 (config), Task 5 (kernel integration), Task 6 (example)
- Non-goals (no Plan 4 migration, no RenderedPrompt, no .doll, no tools ctx) → respected; not implemented
- Module structure → matches plan exactly
- API (`render(name, **ctx) -> str`, autoescape off) → Task 3
- Scaffolding template (conditional blocks for character/rules/tools/examples) → Task 3
- Test character profile (Gura mock) → Task 1
- CharacterConfig + Settings.character (required) → Task 4
- DollOS integration (load profile, renderer, render scaffolding with `character` ctx) → Task 5
- Tests (5 renderer + 2 config + e2e captures system) → Tasks 3, 4, 5
- File summary → matches Task list

**Placeholder scan:** searched for "TBD", "TODO", "implement later", "fill in details", "appropriate" — none present.

**Type consistency check:**
- `PromptRenderer.render(template_name: str, **ctx: object) -> str` — Task 3 implementation, Tasks 3 / 5 callers
- `CharacterConfig(profile_path: Path)` — Task 4 schema, Task 4 tests, Task 4 e2e fixture, Task 5 kernel reads `settings.character.profile_path`, Task 6 example
- `DollOS.__init__(settings: Settings)` — Task 2 rename, Task 5 expansion (adds `renderer`, `_character_profile`)
- `_handle_text_input` signature unchanged across Task 2 (rename) and Task 5 (body change)

No inconsistencies found.
