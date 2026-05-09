# Plan: Rolling cascade compact (episodic memory)

**Worktree**: `.worktrees/rolling-compact/`
**Branch**: `rolling-compact`
**Date**: 2026-05-09

## Why

DollOS doesn't have a "session" concept — Doll is event-driven, no
conversation boundary. The architecturally-correct memory model is:

> Each cascade is an episodic unit. At cascade end, the small model
> compacts what just happened into one sentence. The compacted summary
> appends to a rolling buffer. The next cascade's perception sees the
> rolling buffer in `[Recent activity]` block.

This is what the original `Instinct.process()` was for (step 5), but
its output was wired to STATE prefill which got killed by the prefill
mimicry bug (step 12). Now: bring back the small-model compactor,
wire it through the user-message RAG channel that step 12 established.

T2 / T7 instability resolves naturally: Doll sees prior cascade
summaries (not literal transcript) and can recall "我剛剛問了主人喜歡
喝什麼" without depending on memsearch lottery.

## Out of scope

- Cross-character buffer separation (single-character runtime; multi
  is future).
- Persistence across daemon restart (rolling resets on restart).
- Sliding window / token cap on rolling (start naive, just append).
- Rolling re-compaction (small model "重新 phrase 舊的"). Future
  refinement if context-length pressure shows.
- Replacing memsearch — `[Memory context]` block stays for long-term
  facts; `[Recent activity]` block is for short-term cascade history.

## Architecture

### Wire format

Cascade-N's first user message structure:

```
[Recent activity]
- 主人問我好不好。我說很好然後反問主人。
- 主人問我喜歡喝什麼。我承認沒記過，請主人告訴我。

[Memory context]
{memsearch result, or "(no relevant memory)"}

[Message]
{this turn's perception}
```

`[Recent activity]` block:
- Appears only when `_rolling` is non-empty (first cascade has no block).
- Bullets are first-person past-tense summaries written by Instinct.
- One per cascade in chronological order (oldest first).

### Data flow

```
RawEvent → _handle → _respond:
  - build first user message with [Recent activity] (from self._rolling)
  - cascade as usual (multi-message)
  - cascade ends (natural break / abort / exceeded)
  - compact: small-model summarizes THIS cascade's messages
  - append summary to self._rolling
  - return
```

`self._rolling` is a global dispatcher attribute — daemon-lifetime
buffer, no cleanup.

### Concurrency

`dispatcher.dispatch()` spawns one asyncio.Task per event. Multiple
events may run concurrently. `self._rolling.append(...)` is atomic
per CPython list semantics. No lock needed for naive version.

## Changes

### 1. New jinja: `src/dollos/prompts/templates/iv_compact.jinja`

System block:
```
You are Doll's Inner Voice. Compact a just-finished cascade into a
1-2 sentence summary in first-person past tense from Doll's view.

風格：
- 第一人稱「我」（Doll 自己）
- 過去時態 / 完成式
- 1-2 句話結束
- 自然語言獨白，不用 bullet / label / 機器人格式
- 不寫 tool 技術細節（不寫 "call Shell"），描述做了什麼事
- 摘要主人問了什麼、我做了什麼、結果如何
- 直接寫摘要，不要前綴 / 引號 / 解釋
```

User block:
```
主人問：{{ perception }}

剛才這 turn 我經歷的 cascade：
{% for msg in cascade_messages -%}
{%- if msg.role == "assistant" %}
[我]
{{ msg.content }}
{%- elif msg.role == "user" %}
[Result]
{{ msg.content }}
{%- endif %}
{% endfor %}

寫一句話總結這次 turn 我做了什麼。
```

### 2. `src/dollos/instinct.py` — add `compact_cascade`

Abstract on `Instinct`:
```python
@abstractmethod
async def compact_cascade(
    self,
    *,
    perception: str,
    cascade_messages: list[dict],
) -> str:
    """Compact a finished cascade into a 1-sentence first-person summary."""
```

`SmallModelInstinct.compact_cascade`:
```python
async def compact_cascade(self, *, perception, cascade_messages):
    blocks = self._renderer.render_blocks(
        "iv_compact",
        perception=perception,
        cascade_messages=cascade_messages,
    )
    chunks = []
    async for chunk in self._llm.stream_completion(
        system=blocks["system"],
        user=blocks["user"],
        prefill="",
        max_tokens=256,
    ):
        if chunk.text:
            chunks.append(chunk.text)
        if chunk.done:
            break
    return "".join(chunks).strip()
```

Module docstring update: noted as the compactor backbone (no longer
"reserved for future wake-gating" — now active duty).

### 3. `src/dollos/dispatcher.py`

Add instance attribute `self._rolling: list[str] = []` in `__init__`.

Add helper:
```python
def _format_recent_activity(self) -> str:
    if not self._rolling:
        return ""
    bullets = "\n".join(f"- {s}" for s in self._rolling)
    return f"[Recent activity]\n{bullets}\n\n"
```

In `_respond`, change the first-user-message construction to prepend
`_format_recent_activity()`:

```python
recall_text = await self._inner_voice.recall(doll_event.perception)
recent_activity = self._format_recent_activity()  # "" or non-empty block
if recall_text:
    memory_block = f"[Memory context]\n{recall_text}\n\n"
else:
    memory_block = "[Memory context]\n(no relevant memory)\n\n"
first_user = (
    f"{recent_activity}"  # may be ""
    f"{memory_block}"
    f"[Message]\n{doll_event.perception}"
)
messages = [{"role": "user", "content": first_user}]
```

At the END of `_respond` (after the cascade `while True` exits, but
before `sink.put_nowait(TurnEnd())`), add:

```python
try:
    summary = await self._instinct.compact_cascade(
        perception=doll_event.perception,
        cascade_messages=messages,
    )
    if summary:
        self._rolling.append(summary)
except Exception:
    logger.exception("compact_cascade failed; rolling buffer not updated")

sink.put_nowait(TurnEnd())
```

Wrap in try/except so compactor failure doesn't crash dispatcher
(turn already produced visible output by this point).

The compact runs **regardless** of cascade exit reason: natural break,
depth-cap exceed, same-tool-fail abort. Even partial / failed cascades
are worth summarizing ("我試了幾次找不到答案，停下來").

### 4. Tests

`tests/test_instinct.py`:
- `test_compact_cascade_returns_stripped_string`: fake LLM returns
  "  我問了主人。  ", assert result is "我問了主人。".
- `test_compact_cascade_renders_jinja_blocks`: assert renderer called
  with "iv_compact" template + correct args.
- `test_compact_cascade_passes_messages_to_user_block`: render and
  assert user block contains the message contents.

`tests/test_dispatcher.py`:
- `_FakeInstinct`: add `compact_cascade(perception, cascade_messages)`
  method, configurable return value sequence (default each call
  returns a numbered summary like "summary {N}").
- `test_dispatcher_rolling_starts_empty`: fresh dispatcher,
  `_rolling == []`, first turn user message has NO `[Recent activity]`
  block.
- `test_dispatcher_rolling_appends_after_each_turn`: dispatch 3
  RawEvents in sequence; assert `_rolling` has 3 entries in order.
- `test_dispatcher_subsequent_turn_includes_recent_activity_block`:
  dispatch 2 events; assert second turn's first user message contains
  `[Recent activity]\n- summary 1\n\n` before `[Memory context]`.
- `test_dispatcher_compact_called_with_full_cascade_messages`: spy
  on compact_cascade calls; after a 2-iter cascade, assert the
  cascade_messages arg has all 4 items (user, assistant, user(<tool_response>),
  assistant_say).
- `test_dispatcher_compact_runs_after_cascade_exceeded`: monkeypatch
  MAX_CASCADE_DEPTH=1, dispatch event, assert compact still called.
- `test_dispatcher_compact_runs_after_same_tool_abort`: trigger
  same-tool-3-fail abort, assert compact still called.
- `test_dispatcher_compact_failure_does_not_crash_turn`: instinct
  compact raises; assert turn still ends with TurnEnd, no ErrorMsg
  for compact, _rolling unchanged.

`tests/test_kernel.py`:
- Update `_FakeAdapter` / fake instinct stub to satisfy new
  `compact_cascade` abstract.

`tests/test_e2e.py`:
- Add stub `compact_cascade` returning "test summary" for the
  monkeypatched SmallModelInstinct (mirrors `sanity_check` pattern
  that's now removed).

`tests/test_prompt_renderer.py`:
- `test_iv_compact_template_renders_with_perception_and_messages`.

### 5. Run pytest

`uv run pytest`. All green.

## Risks

- **Compact quality**: small model 0.6-1.7B may produce stilted /
  inaccurate summaries. If "我..." 走偏成 "Doll..." or 摘要太長
  (5+ sentences instead of 1-2), prompt tuning needed. Smoke first.
- **Rolling unbounded**: 100 turns later, `[Recent activity]` block is
  100 lines. Big-model context budget (131k) won't break for casual
  use, but eventually pressure shows. Future: cap or
  re-compact-old-bullets.
- **Concurrent cascades**: if two RawEvents fire in parallel, both
  read same `_rolling` snapshot then both append. Order may be
  non-deterministic. Acceptable for naive — multi-event concurrency
  is rare in current testing flow.
- **Compactor adds latency**: ~500ms-1s per cascade end. Hidden after
  TurnEnd is sent? Currently the order is: cascade ends → compact →
  TurnEnd. If we move TurnEnd before compact, user sees response
  faster, compact runs in background. **Decision**: keep compact
  before TurnEnd for simplicity; user already waited for cascade,
  +1s for compact is negligible.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] Smoke (3 sampling runs):
  - T7「我剛才說了什麼」: should now reliably reference T1-T6 history
    via `[Recent activity]` block (no longer dependent on memsearch
    lottery). Target ≥2/3.
  - T2 less likely to misread as repeat (sees T1's summary in recent
    activity).
  - No regression on T1/T3/T4/T5/T6/T8.
  - Manual log inspection: confirm `[Recent activity]` block appears
    in turn 2+ user message; rolling buffer grows in dispatcher state.
