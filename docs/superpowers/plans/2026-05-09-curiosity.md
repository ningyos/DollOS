# Plan: Curiosity for unknown questions

**Worktree**: `.worktrees/cascade-governance/`
**Branch**: `cascade-governance` (4th commit on top of multi-message + skills audit + research log)
**Date**: 2026-05-09

## Why

When the user asks Doll something Doll doesn't have memory of (T2「我喜歡
喝什麼？」, sometimes T7), current behavior is a sampling lottery between:
- Echo: "剛剛問過了嘛" (wrong)
- Fabricate: "你喜歡喝咖啡啊" (wrong)
- Passive: "翻了記憶沒記過" + wait (mediocre)
- Self-First: "我自己愛冰美式，你呢？" (good)

Goal: make the Self-First / proactive-curiosity branch the **stable**
behavior. When Doll doesn't know:
1. Try `Recall` with alternate keywords (synonyms / related concepts)
2. Still nothing → ask user directly with curiosity ("我不知道，告訴我？"),
   no echo, no fabrication
3. After user tells → `NoteMemory` to record for next time

Per CLAUDE.md "self emerges from architecture (character description +
memory entries surfacing through [Memory context] / Recall), not from
prompt commands": split the change between **character** (identity-level
curiosity trait) and **scaffolding** (mechanism-level technique).

## Out of scope

- Cross-turn conversation history (that's a separate architectural item).
- Deeper Self-First (mood, emotional residue, persistent preferences) —
  that's spec §8 long-term work.
- Tuning Recall's small-LLM filter prompt.

## Changes

### 1. `experiments/test_character.jinja`

Strengthen the curiosity bullet (existing「個性好奇又有點調皮」is too
soft — model treats it as flavor, not driver):

OLD:
```
- 個性好奇又有點調皮，會逗主人但不會煩
```

NEW:
```
- 個性好奇又有點調皮，會逗主人但不會煩
- 我超愛知道主人的事。**不知道的東西我寧可主動問或自己找，絕不假裝知道**——
  問完會記下來，下次就會了
```

Two bullets: one is the existing trait, the new one explicitly anchors
"don't know → ask, don't fabricate". Both are identity ("這是我這隻鯊魚的
性格"), not prescription.

Also fix the stale `RECALL` reference in 思考方式 example (we removed
RECALL prefill in step 12; the example should use `[Memory context]`):

OLD:
```
在 `<think>` 區塊裡，我用第一人稱、自然語言想事情，像獨白。例如：「嗯，
主人問我喜歡喝什麼，剛剛 RECALL 有看到美式咖啡，那就告訴他吧。」想完就直接
出 `<tool_call>` JSON。
```

NEW:
```
在 `<think>` 區塊裡，我用第一人稱、自然語言想事情，像獨白。例如：「嗯，
主人問我喜歡喝什麼，[Memory context] 有撈到他偏好黑咖啡，那就直接 Say 答
他。」想完就直接出 `<tool_call>` JSON。
```

Also remove stale `STATE/RECALL` reference in「不重複 prefill」bullet:

OLD:
```
- **不重複 prefill**：prefill 給我的 STATE/RECALL 是讓我「知道」的，
  不是讓我「複述」的。我看過就好，不會再寫一遍
```

NEW:
```
- **不重複 [Memory context]**：那塊是讓我「知道」的，不是讓我「複述」的。
  我看過就好，不會再寫一遍
```

Also remove the stale `InvokeSkill` line in「我用工具的方式」(the skill
audit only renders the skill section conditionally; an unconditional
character-level reference to InvokeSkill primes hallucination):

OLD:
```
- 建/讀 skill → InvokeSkill tool
```

DELETE (the scaffolding template handles skill mechanism conditionally
when skills exist). Don't replace.

### 2. `src/dollos/prompts/templates/scaffolding.jinja`

Add to the `# Memory` section a third bullet covering the don't-know
flow (mechanism, not prescription):

OLD:
```
# Memory

每個 user message 開頭會帶一個 `[Memory context]` block...

- `[Memory context]` 為空（顯示 `(no relevant memory)`）= 自動檢索沒撈到
  相關記憶，不代表那件事不存在。
- 想找更多 / 更具體的記憶 → 主動 call `Recall(query="...")` tool，回傳
  會是 raw memsearch hits（你自己判斷相關性）。
```

NEW:
```
# Memory

每個 user message 開頭會帶一個 `[Memory context]` block...

- `[Memory context]` 為空（顯示 `(no relevant memory)`）= 自動檢索沒撈到
  相關記憶，不代表那件事不存在。
- 想找更多 / 更具體的記憶 → 主動 call `Recall(query="...")` tool，回傳
  會是 raw memsearch hits（你自己判斷相關性）。
- **主人問你不確定的事**：先 Recall 試一兩次（換 keyword、換相關概念）；
  還是沒撈到 → 直接問主人「告訴我？」，不 echo「剛剛問過了」也不瞎掰；
  主人答完 → NoteMemory 記下，下次就會了。
```

### 3. Tests — `tests/test_prompt_renderer.py`

Add:
- `test_scaffolding_memory_section_includes_curiosity_fallback`: render
  scaffolding, assert `# Memory` section contains both `Recall` and
  `NoteMemory` references AND the literal phrase `不確定` (or
  `不要瞎掰`) anchoring the fallback.

For `experiments/test_character.jinja`, there's no current test
infrastructure (it's experiments-level, not packaged). Skip — manual
inspection sufficient.

### 4. Run pytest

`uv run pytest`. All green.

## Risks

- **Doll becomes too proactive** (spams Recall on every minor doubt): if
  smoke shows this, tighten character prompt to "weighty curiosity, not
  reflex".
- **Character prompt becomes too long**: current is ~25 lines; +1 bullet
  is fine. If it bloats more, reorganize.
- **Stale-reference cleanup risks**: removing `STATE/RECALL` /
  `InvokeSkill` mentions could remove signal model relies on. Mitigation:
  scaffolding `# Memory` covers the [Memory context] mechanism;
  scaffolding `# Skills` covers InvokeSkill conditionally. Character is
  for personality, not protocol.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Smoke (3 sampling runs, fresh data each):
  - T2「我喜歡喝什麼？」: ≥2/3 Doll asks user with curiosity (no
    fabrication, no echo).
  - T7「我剛才說了什麼？」: ≥2/3 either recalls successfully OR asks
    user gracefully (no echo / fabrication).
  - No regression on T1/T3/T4/T5/T6/T8.
- [ ] Manual inspection: rendered scaffolding has the new bullet;
  rendered character.jinja has the new curiosity bullet.
