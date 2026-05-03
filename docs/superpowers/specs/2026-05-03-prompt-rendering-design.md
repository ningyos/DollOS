# Prompt Rendering Layer — Design

**Date**: 2026-05-03
**Roadmap step**: #2 第一版 system prompt + rendering
**Branch**: `step-2-prompt-rendering`（從 main 開）

## Goal

加 jinja2 渲染基礎設施。把 `daemon.py` 寫死的 `PLACEHOLDER_SYSTEM_PROMPT` 常數搬進 jinja template，IPC handler 改用 renderer 取得 system prompt 字串。為將來 plan（character pack、IV process / review prompts、含 ctx 變數的動態 prompt）鋪基礎。

對外行為完全不變 — 同樣字串送大模型，使用者看不出差異。

## Non-goals

- **Plan 4 InnerVoice 不動**：Plan 4 的 hardcoded recall prompt **不**搬遷。Step 3 VoM merge Plan 4 時一併處理
- **無 RenderedPrompt 結構**：v1 渲染單一 template 回 `str`。多 template 組合 / `RenderedPrompt(system, user, prefill)` 結構等需求出現再設計
- **無外部 templates dir**：v1 templates 純 package embedded。Character pack 覆寫機制（step 10）才引入外部來源
- **無 prompts config section**：`config.toml` 不增 `[prompts]`
- **無 ctx 變數預設**：v1 `character.jinja` 是純字串，無變數。Renderer 支援 `**ctx` 但目前 caller 不傳

## Module structure

```
src/dollos/prompts/
├── __init__.py                 # exports PromptRenderer
├── renderer.py                 # PromptRenderer 類別
└── templates/
    └── character.jinja         # 預設 Doll 人格（取代既有 PLACEHOLDER）
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

        template_name: 不含副檔名（"character" 找 "character.jinja"）。
        Raises jinja2.TemplateNotFound if 找不到。
        """
        ...
```

## Default template

**`src/dollos/prompts/templates/character.jinja`**（v1 內容）：

```
You are Doll, a helpful AI companion.
```

無變數。等於現有 `PLACEHOLDER_SYSTEM_PROMPT` 常數的內容。

## Daemon 整合

修改 `src/dollos/daemon.py`：

1. 拿掉 `PLACEHOLDER_SYSTEM_PROMPT` 常數及其 docstring
2. `Daemon.__init__` 加 `self.renderer = PromptRenderer()`
3. 修改 `_handle_text_input`：
   ```python
   system = self.renderer.render("character")
   async for chunk in self.adapter.stream_completion(
       system=system, user=msg.text, prefill=""
   ):
       if chunk.text: yield TextChunk(text=chunk.text)
       if chunk.done: break
   yield TurnEnd()
   ```

對外行為不變：mocked LLM e2e test 仍應收到 `"You are Doll, a helpful AI companion."` 作為 system 字串。

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

- `test_render_character_returns_default_text` — `render("character")` 回固定字串
- `test_render_with_ctx_substitutes_variables` — 用 fixture template `_test_var.jinja` 含 `{{ greeting }}`，驗 `render("_test_var", greeting="hi")` 回 `"hi"`
- `test_render_unknown_template_raises` — `render("does_not_exist")` 拋 `jinja2.TemplateNotFound`
- `test_renderer_does_not_html_escape` — render `<tag>` 回原樣 `<tag>`（autoescape off）

Fixture template `tests/_test_var.jinja` — 不放 package 內，用 jinja 的 `DictLoader` 或在 test 內直接設定 PromptRenderer alternative loader（v1 API 不暴露 — 改成在 test 用 patch / 內建 fixture template）。

最簡：直接在 `src/dollos/prompts/templates/` 加一個 `_test_fixture.jinja`，內容 `{{ greeting }}`，用底線開頭表示 internal/test only。

### `tests/test_daemon_handler.py`（既有 / 改）或 `test_e2e.py`

- 確認 `_handle_text_input` 不再 reference `PLACEHOLDER_SYSTEM_PROMPT`
- 確認 mocked LLM provider 收到 `system="You are Doll, a helpful AI companion."`
- TextChunk / TurnEnd 流程不變

### Smoke

- `python -m dollos --config config.example.toml` 啟動成功（renderer 構造不爆）

## Worktree / branch

- Branch: `step-2-prompt-rendering`（從 main `64bddf1` 或當前 HEAD 開）
- Worktree: `.worktrees/step-2-prompt-rendering/`
- Plan 4 branch (`plan-4-inner-voice`) 完全不碰

## File summary

新增：
- `src/dollos/prompts/__init__.py`
- `src/dollos/prompts/renderer.py`
- `src/dollos/prompts/templates/character.jinja`
- `src/dollos/prompts/templates/_test_fixture.jinja`
- `tests/test_prompt_renderer.py`

修改：
- `src/dollos/daemon.py`（拿掉常數、加 renderer、改 handler）
- `pyproject.toml`（加 jinja2 dep）
- `tests/test_e2e.py` 或同等（驗證 system 字串路徑沒變）

## Estimated scope

4 task：
1. 加 jinja2 dep + `dollos.prompts` package 骨架 + character.jinja
2. PromptRenderer 類別 + tests
3. Daemon 整合 + 更新 e2e test
4. Smoke check

預期測試：69 baseline + 4-5 新 = 73-74 passed。

## Open questions

無。
