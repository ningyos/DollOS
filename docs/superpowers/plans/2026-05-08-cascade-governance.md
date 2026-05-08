# Plan: InvokeSkill / cascade failure governance

**Worktree**: `.worktrees/cascade-governance/`
**Branch**: `cascade-governance`
**Date**: 2026-05-08

## Why

Step 12 deterministic smoke (temp=0/top_k=1) scored 3/8 — InvokeSkill
hallucination is the model's deterministic baseline on T4/T5 (`用 Shell
跑 pwd` / `用 Shell ls`). Doll guesses skill body filenames
(`system/initialization.md`, `debug_shell_errors.md`, `create_skill.md`,
`setup_skill.md`) — all ENOENT — and burns 4-5 cascade retries before
producing any user-visible Say. The new `Recall` tool from step 12 did
not redirect this preference on its own; scaffolding still over-prompts
skill lookup, the tool error message has no corrective signal, and
`MAX_CASCADE_DEPTH=50` lets pathological loops run far past useful.

Four-pronged fix: (A) tighten scaffolding so skill invocation is
gated on actually seeing an entry, (B) make InvokeSkill failure a
success-cascade with corrective guidance instead of a raw `Errno 2`,
(C) drop MAX_CASCADE_DEPTH to 5, (D) break cascade when a single tool
name fails ≥3 consecutive times in the same turn.

## Out of scope

- Adding new tools or wire formats. Only behavior + governance.
- T7 echo bug ("我剛才說了什麼" returns last Say literally) — that's a
  perception-formatting issue, separate work.
- T8 cascade Say weakening (`_format_results_perception` doesn't push
  model to forward tool result) — separate.

## Changes

### 1. `src/dollos/prompts/templates/scaffolding.jinja`

Replace the `# Skills` Usage block:

```
Usage:

- **只有**在 `[Memory context]` 或 `Recall` 結果**真的有具體 skill entry**
  （明確 skill name）才 call `InvokeSkill(name=...)`。
- **沒看到具體 entry 時不要猜檔名**——改用 Shell 直接動手 / Say 直接回答 / 
  Recall 主動找其他記憶。
- InvokeSkill 失敗（skill 不存在）= 你猜錯了；不要再猜下一個名字，換 tool。
- 寫新 skill → 用 Shell 同時寫 entry + body
```

### 2. `src/dollos/tools.py` — `InvokeSkill.run` corrective failure

Current:
```python
async def run(self, ctx: ToolCtx) -> str:
    path = ctx.memory_root / "skill_bodies" / f"{self.name}.md"
    return path.read_text()
```

New:
```python
async def run(self, ctx: ToolCtx) -> str:
    path = ctx.memory_root / "skill_bodies" / f"{self.name}.md"
    if not path.exists():
        skill_dir = ctx.memory_root / "skill_bodies"
        if skill_dir.exists():
            existing = sorted(p.stem for p in skill_dir.glob("*.md"))
        else:
            existing = []
        if existing:
            available = ", ".join(existing)
        else:
            available = "(none yet)"
        return (
            f"Skill '{self.name}' 不存在。"
            f"目前可用 skills: {available}\n"
            f"建議：用 Shell 動手做 / Say 直接回答 / 用 Recall 找其他相關記憶。"
            f"不要再猜其他 skill 名字。"
        )
    return path.read_text()
```

This converts a runtime exception (currently `FileNotFoundError` →
dispatcher's "runtime error" cascade message) into a success-cascade
str with corrective signal. The model now sees a structured guidance
message in the next perception instead of a raw `Errno 2`.

### 3. `src/dollos/dispatcher.py` — depth cap

`MAX_CASCADE_DEPTH = 50` → `MAX_CASCADE_DEPTH = 5`. Single constant
change. Comment with rationale (5 covers legit multi-step turns:
Shell+Say, Shell+NoteMemory+Say, etc.; pathological loops on the same
tool are caught earlier by item 4).

### 4. `src/dollos/dispatcher.py` — same-tool consecutive failure cap

In `_respond`'s cascade `while True` loop, add a tracker that counts
consecutive failures of the **same** tool name across iterations.
When a tool name has failed (`ToolResult.success == False`) 3 times in
a row, push an `ErrorMsg` to the sink with text like:

> 我卡住了：剛剛連續 3 次 InvokeSkill tool 都失敗。停下來換思路。

…and break the cascade loop.

Implementation sketch:

```python
consecutive_fails: dict[str, int] = {}
last_failed_tool: str | None = None

# inside the cascade loop, after collecting `results: list[ToolResult]`:
for r in results:
    if r.success:
        consecutive_fails.clear()
        last_failed_tool = None
    else:
        if r.tool_name == last_failed_tool:
            consecutive_fails[r.tool_name] = consecutive_fails.get(r.tool_name, 1) + 1
        else:
            last_failed_tool = r.tool_name
            consecutive_fails = {r.tool_name: 1}

stuck_tool = next(
    (name for name, count in consecutive_fails.items() if count >= 3),
    None,
)
if stuck_tool is not None:
    sink.put_nowait(ErrorMsg(
        message=(
            f"cascade aborted: 連續 3 次 {stuck_tool} tool 失敗，停下來換思路。"
        )
    ))
    break
```

Edge cases:
- Mixed-tool failures (Shell fails, then NoteMemory fails, then Shell
  fails) → does NOT trigger (consecutive same-tool only). Correct: if
  model is exploring different paths, let it.
- A success between two failures resets the counter — correct (model
  changed approach successfully).

### 5. Tests

`tests/test_dispatcher.py`:
- `test_cascade_breaks_at_max_depth`: synthesize 6 always-failing
  parser results, assert ErrorMsg contains "MAX_CASCADE_DEPTH (5)".
  Update existing depth-related test if any.
- `test_cascade_breaks_on_same_tool_consecutive_3_failures`:
  3 InvokeSkill failures in a row → ErrorMsg contains
  "連續 3 次 InvokeSkill"; cascade breaks; subsequent iterations
  don't run.
- `test_cascade_resets_consecutive_counter_on_success`: failure,
  failure, success, failure → does NOT break.
- `test_cascade_does_not_break_on_alternating_tool_failures`:
  ToolA fail, ToolB fail, ToolA fail → does NOT break (different
  tools interleaved means counter reset).

`tests/test_tools.py`:
- `test_invoke_skill_missing_returns_corrective_message`: build ctx
  with empty `skill_bodies` dir; call InvokeSkill(name="nope");
  assert returned str contains "(none yet)" and "Shell" / "Recall"
  guidance keywords; **no exception raised**.
- `test_invoke_skill_missing_lists_existing_skills`: write
  `data/memory/skill_bodies/morning.md` and `bedtime.md`; call
  InvokeSkill(name="nope"); assert returned str lists "morning,
  bedtime".

`tests/test_prompt_renderer.py`:
- `test_scaffolding_contains_skill_negative_guidance`: rendered
  scaffolding text contains "不要猜檔名" and "InvokeSkill 失敗".

### 6. Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] No new external deps.
- [ ] `MAX_CASCADE_DEPTH` is now 5 in source + tests reflect.
- [ ] Smoke (manual, deterministic temp=0): T4 / T5 still hallucinate
      InvokeSkill (model behavior; expected), but cascade aborts after
      ≤3 retries, user gets a single ErrorMsg + a final Say (vs the
      4–5 ENOENT spam observed pre-fix).

## Risks

- **Same-tool counter false positive**: a legit tool that legitimately
  takes 3 retries to get arguments right (e.g., complex Shell command
  iteration). Mitigation: 3 is an upper bound for our current toolset;
  if a future tool legitimately needs more, we can raise per-tool.
- **scaffolding rule that says "skill 失敗 = 換 tool" interacts with
  the cap**: if model sees "skill 失敗 = 你猜錯了" once and obeys, the
  3-failure cap won't fire. Both layers cooperate.
- **InvokeSkill returning informative str now masks "real bugs"**: a
  genuine FS error (permission denied, broken symlink) wouldn't raise
  FileNotFoundError so still cascades through the existing exception
  path. We only short-circuit ENOENT, not other I/O errors.
