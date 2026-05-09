# Plan: Add REVIEW field to think grammar

**Worktree**: `.worktrees/rolling-compact/`
**Branch**: `rolling-compact`
**Date**: 2026-05-09

## Why

Verbose-log analysis of an actual cascade loop (smoke 19 run 3, T2):
model emitted **identical 3-line think** ~70 times in a row:

```
SEEN: [Recent activity]: "我很好，主人呢？" and [Message]: "我喜歡喝什麼？"
INTENT: The user is asking what I like to drink.
TOOL: Recall
</think>
```

Root cause: B4-typed think grammar (`SEEN/INTENT/TOOL`) constrains
think to 3 short slots. **There is no syntactic space for the model
to write "I've tried Recall 5 times, none worked, time to give up
and Say I don't know"**. Self-reflection is grammatically forbidden.

Step 11 grammar achieved 15× think-token compression with maintained
pass@1 on **single-turn** evaluation. Cascade exposes the cost: model
cannot meta-cognize about its own progress, so it cannot self-terminate.

Add a 4th `REVIEW` field between INTENT and TOOL: `SEEN/INTENT/REVIEW/TOOL`.
This is the surgical fix — minimum grammar change to unblock
introspection.

## Out of scope

- Loosening grammar entirely after iter 1 (option B in earlier
  brainstorm) — bigger change, save if REVIEW alone insufficient.
- Removing the cascade behavior of grammar regeneration — grammar
  still applies every iter.
- Per-iter different grammar — one grammar handles all cascade iters.

## Changes

### 1. `src/dollos/llm/templates.py`

Update `build_qwen3_think_tool_grammar`. Find the existing think rule:
```python
'think ::= "SEEN: " line "INTENT: " line "TOOL: " tool-name "\\n</think>\\n\\n"\n'
```

Change to:
```python
'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line "TOOL: " tool-name "\\n</think>\\n\\n"\n'
```

That's the entire grammar generator change — REVIEW is a new
required line between INTENT and TOOL.

### 2. `src/dollos/prompts/templates/scaffolding.jinja`

Add a `# Think structure` section explaining the 4 fields. Currently
the scaffolding has `# Behavior` then `# Memory` then optional `# Skills`.
Insert after `# Behavior`:

```
# Think structure

`<think>` 區塊的 4 個欄位：
- **SEEN**: 字面複述當前看到的事（user message + relevant context）
- **INTENT**: 用戶想要什麼
- **REVIEW**: 目前進度。第一輪寫「first attempt」或「沒試過」；後續輪
  寫「我做了什麼、結果如何、是否該繼續用同樣 tool」。如果連續多次同
  tool 沒進展、改用 Say 或別的 tool。
- **TOOL**: 下一個 tool 的名字

REVIEW 欄位是給你看自己有沒有卡住用的。如果 REVIEW 寫到「我已經 N
次嘗試 X 都沒結果」，下一個 TOOL 就應該不是 X。
```

### 3. character.jinja

The `## 我的思考方式` section currently shows an example:
```
在 `<think>` 區塊裡，我用第一人稱、自然語言想事情，像獨白。例如：
「主人問我喜歡喝什麼，[Memory context] 有撈到他偏好黑咖啡，那就直接 Say 答他。」
```

This was free-form language; the actual grammar enforces 4 slots.
Update to match the structured field reality:

```
在 `<think>` 區塊裡用 4 個欄位：SEEN（看到什麼）/ INTENT（用戶要什麼）
/ REVIEW（進度評估）/ TOOL（下個工具）。REVIEW 重要 — 看自己有沒有
卡同樣的事，卡了就換 tool 或 Say。
```

### 4. Tests

`tests/test_llm_grammar.py`:
- Update `test_grammar_has_think_skeleton` (or whatever asserts the
  think rule) to expect REVIEW between INTENT and TOOL.
- Add `test_grammar_think_has_review_field`: assert
  `"REVIEW: " line ` in generated grammar.
- Update / extend any think-shape snapshot tests.

`tests/test_prompt_renderer.py`:
- `test_scaffolding_has_think_structure_section`: assert the new
  `# Think structure` section is present, with all 4 field names.

`tests/test_dispatcher.py`:
- Existing dispatcher tests that use a fake adapter feeding hardcoded
  chunks like `<think>SEEN: ... INTENT: ... TOOL: Say</think>...`
  may need their chunks updated to include REVIEW. Search for any
  test fixture chunks that hardcode the SEEN/INTENT/TOOL pattern
  without REVIEW; update them. (The actual grammar isn't enforced
  at the parser level — parser just reads `<tool_call>` blocks. So
  these tests should still pass without REVIEW in chunks. But
  e2e/integration-style tests asserting prompt content might check
  the grammar string and will need updates.)

### 5. Run pytest

`uv run pytest`. All green.

## Risks

- **Model fills REVIEW with garbage**: small model might not understand
  the semantic, just emit "REVIEW: looking at it" filler. Smoke will
  tell. If REVIEW content is empty / non-substantive, escalate to
  free-form think (option B in brainstorm).
- **Token cost**: ~1 extra line per think × N iterations. Negligible.
- **Looping still possible if model writes useless REVIEW**: REVIEW
  unlocks the *capacity* for self-reflection but doesn't *guarantee*
  it. If the model keeps writing "REVIEW: continuing search" without
  changing TOOL, we're back to the same problem. Mitigation: scaffolding
  is explicit about "REVIEW says stuck → next TOOL ≠ same tool".

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Smoke (3 sampling runs):
  - Verbose log shows REVIEW field content varies meaningfully across
    cascade iters (not just template-fill).
  - T2 / T7 cascade loops resolve naturally (model writes "REVIEW: 試
    過 X 次 Recall 沒結果，改用 Say" → next emit Say).
  - Average cascade length DROPS noticeably; no infinite loops within
    the smoke timeout window.
  - Eyeball: think output looks more like reasoning, less like template
    auto-fill.
