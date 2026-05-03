# Prompt Rendering Layer — Design

**Date**: 2026-05-03
**Roadmap step**: #2 第一版 system prompt + rendering
**Branch**: `step-2-prompt-rendering`（從 main 開）

## Goal

加 jinja2 渲染基礎設施。Package 內提供**通用 prompt scaffolding**（規則 / 說明 / zero-shot 範例 / tool 描述 / 條件式區塊），不放角色身份。角色 profile 從外部 jinja 檔案載入，作為 `{{ character }}` ctx 餵給 scaffolding 渲染。Step 10 character pack 把 profile 來源從 plain file 升級成 `.doll` zip。

順手把 `daemon.py / Daemon` 改名為 `kernel.py / DollOS`（原本 roadmap step 4 包的 rename，提前到此 plan）。

對外行為：使用者打字 → DollOS 用渲染後的 system prompt + user 文字呼叫大模型 → stream tokens 回 IPC。Demo 跟 step 1 比，差別在 system prompt 是 rendered 出來的，且角色描述從 file 載入。

## Non-goals

- **Plan 4 InnerVoice 不動**：Plan 4 的 hardcoded recall prompt **不**搬遷。Step 3 VoM merge Plan 4 時一併處理
- **無 RenderedPrompt 結構**：v1 渲染單一 template 回 `str`。多 template 組合 / `RenderedPrompt(system, user, prefill)` 結構等需求出現再設計
- **package templates 不含角色身份**：generic scaffolding only。角色 profile 是 caller 傳進來的 ctx
- **無 .doll zip 載入**：v1 character profile 是 plain `.jinja` file（path 從 config 來）。Step 10 才升級成 `.doll` 解壓
- **無 tools / rules / examples ctx**：v1 scaffolding 為將來保留條件區塊（`{% if tools %}` 等）但 v1 caller 不傳這些 ctx，渲染時為空

## Module structure

```
src/dollos/
├── kernel.py                   # 重命名自 daemon.py，class DollOS（原 Daemon）
└── prompts/
    ├── __init__.py             # exports PromptRenderer
    ├── renderer.py             # PromptRenderer 類別
    └── templates/
        └── scaffolding.jinja   # 通用 prompt scaffolding（generic, no identity）

experiments/
└── test_character.jinja        # 開發測試用角色 profile（mock 未來 gura.doll 形態）
```

## API

```python
class PromptRenderer:
    """Render jinja2 templates from the embedded `dollos.prompts.templates` package."""

    def __init__(self) -> None:
        # 使用 jinja2 PackageLoader 載 dollos.prompts.templates
        # autoescape 關閉（prompt 是純文字、不是 HTML）
        ...

    def render(self, template_name: str, **ctx: object) -> str:
        """Render given template with ctx vars. Returns rendered string.

        template_name: 不含副檔名（"scaffolding" 找 "scaffolding.jinja"）。
        Raises jinja2.TemplateNotFound if 找不到。
        """
        ...
```

## Scaffolding template

**`src/dollos/prompts/templates/scaffolding.jinja`**：

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

V1 caller 只傳 `character` ctx；其餘條件區塊（rules / tools / examples）保留供之後 plan 使用。

## Test character profile

**`experiments/test_character.jinja`**（開發測試用，模擬未來 gura.doll 形態）：

```
You are Gura, a 9000-year-old shark.
You are curious, sometimes mischievous, and prone to "a"-laughs.
```

不放 package 內。將來 step 10 character pack 引入後，這個檔案內容會搬進 `gura.doll/prompts/character.jinja`。

## Config

`Settings` 加 `CharacterConfig`：

```python
class CharacterConfig(BaseModel):
    profile_path: Path  # 必，指向 character profile jinja 檔
```

`config.example.toml`：

```toml
[character]
profile_path = "experiments/test_character.jinja"
```

Settings 必填（無 default），跟 Plan 4 InnerVoiceConfig 同模式。

## DollOS 整合

重命名 `src/dollos/daemon.py` → `src/dollos/kernel.py`，class `Daemon` → `DollOS`。`build_adapter` 等 module-level 函式留 kernel.py。

修改後 `_handle_text_input`：

```python
class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self._character_template = settings.character.profile_path.read_text()
        self.server = WebSocketServer(...)
        ...

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        system = self.renderer.render("scaffolding", character=self._character_template)
        async for chunk in self.adapter.stream_completion(
            system=system, user=msg.text, prefill=""
        ):
            if chunk.text: yield TextChunk(text=chunk.text)
            if chunk.done: break
        yield TurnEnd()
```

`character_template` 在 `__init__` 一次讀（v1 不熱重載）。

`__main__.py` 跟 tests 的 `from dollos.daemon import Daemon` 全部改 `from dollos.kernel import DollOS`。

## Dependency

`pyproject.toml`：

```toml
[project]
dependencies = [
    ...,
    "jinja2>=3.1",
]
```

## Tests

### `tests/test_prompt_renderer.py`（新檔）

- `test_render_scaffolding_with_character_includes_text` — 傳 `character="hello"` 渲染後字串包含 `"hello"`
- `test_render_scaffolding_no_ctx_returns_empty` — 無任何 ctx 時渲染結果只有空白（strip 後為空字串）
- `test_render_with_ctx_substitutes_variables` — 用 internal fixture template `_test_fixture.jinja` 含 `{{ greeting }}`，驗 `render("_test_fixture", greeting="hi")` 回 `"hi"`
- `test_render_unknown_template_raises` — `render("does_not_exist")` 拋 `jinja2.TemplateNotFound`
- `test_renderer_does_not_html_escape` — render `<tag>` 回原樣 `<tag>`（autoescape off）

Fixture template `src/dollos/prompts/templates/_test_fixture.jinja`（底線開頭表示 internal）：
```
{{ greeting }}
```

### `tests/test_kernel.py` 或 `test_e2e.py`（更新既有 e2e）

- mocked LLM provider 收到 `system` 字串包含 test_character 內容
- `_handle_text_input` 不再 reference `PLACEHOLDER_SYSTEM_PROMPT`（已刪）
- TextChunk / TurnEnd 流程不變
- `tests/test_config.py` 既有 fixtures 加 `[character] profile_path = "..."` section（同 Plan 4 InnerVoiceConfig 模式）

### Smoke

- `python -m dollos --config config.example.toml` 啟動成功（renderer + character profile 載入不爆）

## Worktree / branch

- Branch: `step-2-prompt-rendering`（從 main 開）
- Worktree: `.worktrees/step-2-prompt-rendering/`
- Plan 4 branch (`plan-4-inner-voice`) 完全不碰

## File summary

**新增**：
- `src/dollos/prompts/__init__.py`
- `src/dollos/prompts/renderer.py`
- `src/dollos/prompts/templates/scaffolding.jinja`
- `src/dollos/prompts/templates/_test_fixture.jinja`
- `experiments/test_character.jinja`
- `tests/test_prompt_renderer.py`

**重命名**：
- `src/dollos/daemon.py` → `src/dollos/kernel.py`
- class `Daemon` → `DollOS`

**修改**：
- `src/dollos/kernel.py`（拿掉 `PLACEHOLDER_SYSTEM_PROMPT`、加 `renderer` + `_character_template`、改 handler）
- `src/dollos/__main__.py`（import `DollOS` from `dollos.kernel`）
- `src/dollos/config.py`（加 `CharacterConfig` + `Settings.character`）
- `pyproject.toml`（加 `jinja2>=3.1` dep）
- `config.example.toml`（加 `[character] profile_path`）
- `tests/test_e2e.py`（import 改、`Settings(...)` 構造加 `character=CharacterConfig(...)`、驗證 character 內容進 system）
- `tests/test_config.py` 既有 TOML fixtures 全加 `[character] profile_path = "..."`

## Estimated scope

5-6 task：
1. 加 jinja2 dep + `dollos.prompts` package 骨架 + scaffolding.jinja + _test_fixture.jinja + experiments/test_character.jinja
2. PromptRenderer 類別 + tests
3. `daemon.py` rename + class rename + 更新 imports
4. `CharacterConfig` + Settings 整合 + test_config fixture 全更新
5. Kernel 整合（renderer + character profile load + handler 改用 rendered system）
6. test_e2e 更新 + smoke check

預期測試：69 baseline + 5-7 新 = 74-76 passed。

## Open questions

無。
