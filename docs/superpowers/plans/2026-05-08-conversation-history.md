# Plan: Multi-message conversation history within a turn's cascade

**Worktree**: `.worktrees/cascade-governance/`
**Branch**: `cascade-governance` (3rd commit on top of cascade-gov + skills-audit)
**Date**: 2026-05-08

## Why

Step 12 sampling smoke + 3-run sampling smoke after skills-audit confirmed
Doll's cascade-Say weakness (T4/T5/T7) is independent of the InvokeSkill
problem. Root cause analysis: dispatcher renders the prompt **single-shot**
each cascade iteration. Iteration 1 is the original perception; iteration N
replaces user content with `_format_results_perception` summary like
"你 call 了 Shell tool 成功，回傳：…". Model has lost:

1. The **original user request** (it's been overwritten)
2. Its **own prior tool_call XML** (only seen via "你 call 了 X" summary)
3. Multi-turn ChatML structure that **Hermes 4 / Qwen3 are trained on**
   (assistant-emits-tool_call → tool-response message → assistant-continues)

Hermes #6 + Claude Code consensus: keep the full `user → assistant(think+tool_call)
→ user(<tool_response>) → assistant(...)` alternation in context. Qwen3's chat
template natively supports this with tool results in `user` role wrapped
`<tool_response>...</tool_response>`.

Per spec/CLAUDE.md "no fallback mechanisms": the new path replaces the old
single-shot path; no dual code-path. Cross-turn history is **out of scope**;
only within-cascade history is added.

## Out of scope

- Cross-turn / cross-event history. Each new RawEvent still starts fresh.
- Conversation persistence to disk. Local var only, freed when `_respond`
  returns.
- Token-budget management for accumulated context. Trust llama.cpp's
  cache_prompt; cascade depth=5 plus same-tool 3-fail cap from the prior
  commit already bound growth.
- Anthropic / OpenAI provider message-array shape. Stays a llama.cpp
  raw-prompt model for now.

## Format

### Cascade prompt structure (target)

```
<|im_start|>system
{scaffolding (with character + tools block + optional # Skills section)}<|im_end|>
<|im_start|>user
[Memory context]
{IV.recall result or "(no relevant memory)"}

[Message]
{original perception text}<|im_end|>
<|im_start|>assistant
<think>
SEEN: ...
INTENT: ...
TOOL: Shell
</think>

<tool_call>
{"name": "Shell", "arguments": {"command": "pwd"}}
</tool_call><|im_end|>
<|im_start|>user
<tool_response>
[exit 0]
/home/progcat/Projects/DollOS/data
</tool_response><|im_end|>
<|im_start|>assistant
<think>
```

Note:
- `[Memory context]` block stays only on the first user message, not
  re-injected between tool_response messages.
- Original `[Message]` perception persists in the first user — never
  overwritten.
- Each cascade iteration appends one new assistant message (the model's
  emit) + tool_response messages (one per tool_call result).
- The final iteration ends with the standard `<|im_start|>assistant\n<think>\n`
  open turn so the model can continue.

### What counts as the "assistant message" content

The full text the big model emits in this turn — including the closing
`</think>` and the `<tool_call>` block. ToolStreamParser already extracts
the tool_call dict; we additionally need to capture the **raw text** for
the assistant message. Buffer model output during the stream, append as
the assistant message after `chunk.done` (or after parser.flush()).

If the model emits multiple tool_calls in one turn (rare but possible),
all go in the same assistant message. Each gets a corresponding
tool_response user message in order.

### Tool result formatting

```
<tool_response>
{tool.run() return value, or formatted error}
</tool_response>
```

Errors get the same wrapper; the content is the corrective string
(success-path return like InvokeSkill ENOENT) or — for genuine raises —
the existing `runtime error: {e}` text. Model can read both.

`_format_results_perception` is removed entirely.

## Changes

### 1. `src/dollos/llm/templates.py`

Add a new method on `Qwen3ThinkingTemplate`:

```python
def render_messages(
    self,
    *,
    system: str,
    messages: list[dict],  # each: {"role": "user" | "assistant", "content": str}
    tools: list[type[BaseModel]] | None = None,
) -> str:
    """Multi-message ChatML render. The final message must NOT be assistant —
    the renderer always opens a new <|im_start|>assistant\\n<think>\\n turn
    at the end for the model to continue from.
    """
```

Implementation: prepend `# Tools` block to system if tools provided, exactly
as `render()` does. Then iterate `messages`, emit each as
`<|im_start|>{role}\n{content}<|im_end|>\n`. Finally append
`<|im_start|>assistant\n<think>\n` open turn.

The legacy `render(*, system, user, prefill, tools)` signature is **kept**
for backward compat with InnerVoice / Instinct (non-cascade single-shot
small-model use). It internally just calls `render_messages` with a
single `[{"role":"user","content":user}]` (and prefill appended). Or
keep its existing implementation; up to you. Simplest: leave legacy
`render` untouched, just add `render_messages` alongside.

`Qwen3PlainTemplate` does NOT need `render_messages` (small-model paths
don't cascade). Leave it as-is.

### 2. `src/dollos/llm/adapter.py`

Change `LLMAdapter.stream_completion` signature from
`(system, user, prefill, ...)` to either:

(A) Accept `messages: list[dict]` only (replacing user/prefill), keeping system separate, OR

(B) Add a new method `stream_messages(system, messages, ...)` and let dispatcher use the new method while small-model callers stay on the old method.

**Pick (B)** to minimize churn. New method on `LLMAdapter`:

```python
@abstractmethod
async def stream_messages(
    self,
    *,
    system: str,
    messages: list[dict],
    stop: list[str] | None = None,
    max_tokens: int = 1024,
    tools: list[type[BaseModel]] | None = None,
    grammar: str | None = None,
) -> AsyncIterator[StreamChunk]: ...
```

Old `stream_completion(system, user, prefill, ...)` stays. InnerVoice /
Instinct keep using it.

### 3. `src/dollos/llm/composed.py`

Implement `stream_messages` in `ComposedLLMAdapter`: render via
`template.render_messages(...)`, then provider.stream(prompt=...).

### 4. `src/dollos/llm/transport.py`

Unchanged — already takes raw prompt.

### 5. `src/dollos/dispatcher.py`

Rewrite `_respond`:

```python
async def _respond(self, doll_event, sink):
    grammar = build_qwen3_think_tool_grammar(TOOLS)
    
    # Initial message list. [Memory context] only on first user msg.
    recall_text = await self._inner_voice.recall(doll_event.perception)
    if recall_text:
        first_user = (
            "[Memory context]\n"
            f"{recall_text}\n\n"
            "[Message]\n"
            f"{doll_event.perception}"
        )
    else:
        first_user = (
            "[Memory context]\n"
            "(no relevant memory)\n\n"
            "[Message]\n"
            f"{doll_event.perception}"
        )
    messages: list[dict] = [{"role": "user", "content": first_user}]
    
    # Per-turn skill discovery (still per-turn, not per-iteration; skills
    # don't change mid-turn unless Doll writes one — accept the 1-iter lag).
    skills_dir = self._memory_root / "skills"
    if skills_dir.exists():
        available_skills = sorted(p.stem for p in skills_dir.glob("*.md"))
    else:
        available_skills = []
    system = self._renderer.render(
        "scaffolding",
        character=self._character_profile,
        available_skills=available_skills,
    )
    
    iteration = 0
    consecutive_fails: dict[str, int] = {}
    last_failed_tool: str | None = None
    
    while True:
        parser = ToolStreamParser()
        ctx = ToolCtx(...)
        results: list[ToolResult] = []
        assistant_buf: list[str] = []
        
        async for chunk in self._adapter.stream_messages(
            system=system,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
            grammar=grammar,
        ):
            assistant_buf.append(chunk.text)
            for call in parser.feed(chunk.text):
                result = await self._dispatch_tool_call(call, ctx)
                if result is not None:
                    results.append(result)
            if chunk.done:
                break
        for call in parser.flush():
            result = await self._dispatch_tool_call(call, ctx)
            if result is not None:
                results.append(result)
        
        # Append assistant turn (model's full emit) to history.
        messages.append({
            "role": "assistant",
            "content": "".join(assistant_buf),
        })
        
        if not results:
            break
        
        # Append a tool_response user message for each result.
        for r in results:
            messages.append({
                "role": "user",
                "content": f"<tool_response>\n{r.detail or '(no output)'}\n</tool_response>",
            })
        
        # Same-tool consecutive-fail tracker (unchanged from prior commit).
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
            (n for n, c in consecutive_fails.items() if c >= 3),
            None,
        )
        if stuck_tool is not None:
            sink.put_nowait(ErrorMsg(
                message=f"cascade aborted: 連續 3 次 {stuck_tool} tool 失敗，停下來換思路。"
            ))
            break
        
        iteration += 1
        if iteration > _disp_mod.MAX_CASCADE_DEPTH:
            sink.put_nowait(ErrorMsg(
                message=f"cascade exceeded MAX_CASCADE_DEPTH ({_disp_mod.MAX_CASCADE_DEPTH})"
            ))
            break
    
    sink.put_nowait(TurnEnd())
```

`_format_results_perception` method **deleted**.

`_handle` no longer passes `summary` to `_respond` (it didn't, post step-12; signature already simplified).

`available_skills` glob moves OUT of the cascade loop (per-turn, not per-iter)
to avoid re-rendering scaffolding. Acceptable lag: if Doll creates a skill
mid-turn via Shell, the just-created skill won't appear in scaffolding for
the rest of THIS turn. Re-glob next turn picks it up. Trade-off favors
prompt-cache stability.

### 6. Tests

- `tests/test_llm_templates.py`: add `test_qwen3_thinking_render_messages_*`
  - Multi-message round-trip: 3 messages → output has 3 `<|im_start|>` blocks + final assistant open
  - Tool result wrapped in user role with `<tool_response>` literal
  - System with tools includes # Tools block
- `tests/test_llm_composed.py`: extend fake provider, assert `stream_messages`
  passes through grammar/tools/messages correctly
- `tests/test_dispatcher.py`: significant rework
  - Existing tests asserting `adapter.calls[0]["user"]` content (the
    framed user msg) → assert against `messages[0]["content"]` instead
  - Tests using `_FakeAdapter`'s old `stream_completion` need either:
    (a) update fake to also implement `stream_messages`, OR
    (b) point dispatcher's calls to a new fake. Choose (a).
  - Cascade-depth + same-tool tests rewrite slightly: adapter chunks
    must produce new tool_call per iteration; assert messages list
    grows correctly between iterations.
  - New: `test_cascade_preserves_original_user_in_messages_first`:
    after 3 cascade iterations, `messages[0]["role"] == "user"` and
    contains "[Message]\n{original perception}".
  - New: `test_cascade_appends_tool_response_after_assistant`: after
    1 successful tool_call, messages = [user, assistant, user(<tool_response>...), assistant(open... but cascade-depth=5 so this is the next iter input)]. Verify ordering.
- `tests/test_e2e.py`: assertion that `<tool_response>` appears in prompt
  when there's a cascade round. Update existing prompt-shape assertion.

### 7. Documentation

Add the new wire-format pattern to the plan doc (this file). Update
`CLAUDE.md`'s VoM wire format note ONLY if the user requests; the new
behavior is consistent with "RAG context block + Recall tool" — the
[Memory context] still goes in user message, just only on the first one.

## Risks

- **Prompt growth** at cascade iteration 5: original user (~200 chars)
  + 5× assistant turns (~500 chars each, think+tool_call) + 5×
  tool_response (~500 chars Shell output, more if longer outputs) ≈
  5-7k chars overhead per turn. With cache_prompt this is mostly cached
  so not a latency hit. Memory at scale: dispatcher holds messages list
  in local var, freed at turn end.
- **Streaming + assistant_buf**: the model emits in chunks; we append
  every chunk to buf. ToolStreamParser already separates think from
  tool_call from naked text. We capture the **raw stream** verbatim
  for the assistant message (whatever the grammar+model emits, with
  </think> and tool_call tags intact), so the next iteration sees a
  history that round-trips through the model's own output.
- **Last-message-not-assistant constraint**: render_messages always
  opens a fresh `<|im_start|>assistant\n<think>\n` regardless of last
  message role. If last message is `assistant` (which shouldn't happen
  in our flow but defensively), we still open a new assistant turn.
  Document this in the docstring.

## Acceptance

- [ ] `uv run pytest` 全綠.
- [ ] InnerVoice / Instinct小模型 stream still uses old single-shot
      `stream_completion` API (legacy method retained).
- [ ] Smoke (manual, sampling temp=0.6, 3 runs each, fresh data):
      - T4/T5 Shell: forwards tool result in Say more reliably than
        the 1/3 baseline (target ≥2/3)
      - T7 cross-turn recall: tests cascade-Say not, but should still
        be ≥1/3 like before (this fix doesn't directly target T7
        across turns; if it improves it's a bonus from clean original
        perception preservation)
      - 0 InvokeSkill ENOENT spam (continued from skills-audit)
      - 0 ERROR / cascade aborted (target)
