# Qwen-Agent Research: Agent Loop & Tool-Call Format

**Date:** 2026-05-07  
**Source:** https://github.com/QwenLM/Qwen-Agent  
**Relevance:** DollOS uses Qwen3.6-35B-A3B. We observe ReAct-style plaintext (`THOUGHT: ... ACTION: ...`) leaking into model output instead of proper `<tool_call>` JSON. This report documents how Qwen's own agent framework is structured.

---

## 1. TL;DR — Ordered by Relevance to DollOS's ReAct-Leakage Problem

- **Qwen-Agent defaults to the "Nous" prompt, not the native `<tool_call>` format.** The `fncall_prompt_type` defaults to `'nous'` in `function_calling.py`. The Nous format injects tool schemas via `<tools>...</tools>` XML in the system prompt and expects `<tool_call>\n{"name":..., "arguments":...}\n</tool_call>` in the assistant turn. Tool results go back as `<tool_response>\n...\n</tool_response>` appended to a USER role message.

- **The "Qwen" prompt format (legacy) uses `✿FUNCTION✿` / `✿ARGS✿` / `✿RESULT✿` / `✿RETURN✿` tokens — NOT ReAct.** If DollOS is seeing `THOUGHT:` / `ACTION:` / `PLAN:` leakage, this is NOT coming from Qwen-Agent's own scaffolding. It is coming from the model's RLHF / SFT training data, likely triggered when the system prompt does not clearly assert a tool-call format.

- **The key stop-word mechanism:** When using the Qwen (not Nous) fncall prompt type, Qwen-Agent injects `FN_STOP_WORDS = ['✿RESULT✿', '✿RETURN✿']` as stop sequences to prevent the model from hallucinating tool results. No equivalent stop-word injection exists for the Nous format — the model is expected to close `</tool_call>` on its own.

- **Parallel function calls default to `False` in `fncall_prompts`.** The `preprocess_fncall_messages()` call in `function_calling.py` passes `parallel_function_calls=generate_cfg.get('parallel_function_calls', False)`. DollOS should match this default to avoid confusing the model with parallel-call template phrasing when it isn't needed.

- **Tool results are injected into USER role messages (Nous format), NOT a separate FUNCTION/tool role.** Qwen-Agent merges `<tool_response>...</tool_response>` content into the preceding or new USER message. DollOS currently uses `<tool_response>` per Qwen3 model card — this is correct for native API use, but differs from how Qwen-Agent's text-mode pipeline feeds results back.

- **`use_raw_api=True` skips all prompt injection entirely** and delegates tool formatting to the underlying API (OpenAI-compat tool_call JSON). This is the recommended path for vLLM/llama.cpp with `--tool-call-parser`. DollOS using llama.cpp should consider this path.

---

## 2. Qwen-Agent System Prompt Structure

Qwen-Agent supports **two independent prompt backends**, selectable via `generate_cfg['fncall_prompt_type']`:

### 2a. Nous Format (DEFAULT since recent versions)

**Source:** `qwen_agent/llm/fncall_prompts/nous_fncall_prompt.py`

System prompt appended (after any user-defined system message):

```
# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {...}}
{"type": "function", "function": {...}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
```

Tool schemas are serialized as newline-separated JSON objects (one per line) inside `<tools>`. No `name_for_human` / `name_for_model` distinction; uses the raw OpenAI-format function dict.

**Conversation format (Nous):**

| Turn | Role | Content |
|---|---|---|
| Tool call | ASSISTANT | `<tool_call>\n{"name":"foo","arguments":{"x":1}}\n</tool_call>` |
| Tool result | USER | `<tool_response>\n{result}\n</tool_response>` |

Tool results are **merged into USER role**, not a separate `tool`/`function` role. The `preprocess_fncall_messages` code appends `<tool_response>` content to the last USER message (or creates a new USER message).

### 2b. Qwen Format (Legacy, opt-in via `fncall_prompt_type='qwen'`)

**Source:** `qwen_agent/llm/fncall_prompts/qwen_fncall_prompt.py`

System prompt appended (English, non-parallel):

```
# Tools

## You have access to the following tools:

### {name_for_human}

{name_for_model}: {description_for_model} Parameters: {json_schema} Format the arguments as a JSON object.

## When you need to call a tool, please insert the following command in your reply, which can be called zero or multiple times according to your needs:

✿FUNCTION✿: The tool to use, should be one of [{tool_names}]
✿ARGS✿: The input of the tool
✿RESULT✿: Tool results
✿RETURN✿: Reply based on tool results. Images need to be rendered as ![](url)
```

**Conversation format (Qwen):**

The entire multi-turn tool-calling exchange is **flattened into a single ASSISTANT message** (not separate message objects):

```
✿FUNCTION✿: tool_name
✿ARGS✿: {"param": "value"}
✿RESULT✿: [tool output here]
✿RETURN✿: [model's final reply]
```

Stop words `['✿RESULT✿', '✿RETURN✿']` are injected as generation stop sequences to prevent the model from inventing results.

### 2c. Raw API mode (`use_raw_api=True`)

Skips all prompt injection. Passes tools via the OpenAI `tools=` parameter directly to the API. The model must natively handle tool-call JSON (as llama.cpp does with `--jinja --tool-call-parser`). This is the correct path for DollOS.

**Note:** Qwen3-Max on DashScope is force-set to `use_raw_api=True` automatically. vLLM/SGLang with `--enable-auto-tool-choice --tool-call-parser hermes` should also use `use_raw_api=True` per the README.

---

## 3. Tool Calling Flow — Multi-Step Agent Loop

**Source:** `qwen_agent/agents/fncall_agent.py`, `qwen_agent/settings.py`

```python
# Simplified loop from FnCallAgent._run()
while True and num_llm_calls_available > 0:
    num_llm_calls_available -= 1
    output = call_llm(messages, functions=functions)
    yield output
    if not used_any_tool:
        break
    # execute tool, append function result message
    tool_result = execute_tool(fn_call)
    messages.append(Message(role=FUNCTION, name=tool_name, content=tool_result, extra={function_id: ...}))
    # loop continues → next LLM call sees updated messages
```

Key parameters:
- `MAX_LLM_CALL_PER_RUN = 20` — hard ceiling per run
- `DEFAULT_MAX_INPUT_TOKENS = 58000` — truncation threshold

**Cascade pattern:** This is functionally identical to DollOS's inner while-loop cascade. The loop continues until the model produces a response with no tool call, or hits the 20-call limit.

**History management:** Each iteration, the full conversation (user messages + all prior tool call/result pairs) is passed to the LLM. No summarization or truncation within the loop itself — the 58k token limit handles overflow by truncating old messages.

**Thinking model special-casing:** The code detects `'qwq' in model.lower() or 'qvq' in model.lower() or 'qwen3' in model.lower()`. For these models (DashScope only), a lighter `qwen-turbo` model is substituted for the memory/RAG summarization step. The main agent loop itself is unchanged.

---

## 4. Anti-Patterns / Pitfalls

### 4a. DO NOT use `--enable-auto-tool-choice --tool-call-parser hermes` with Qwen-Agent on vLLM/SGLang (unless `use_raw_api=True`)

From README:
> "For QwQ and Qwen3 models deployed via vLLM, do not add the `--enable-auto-tool-choice` and `--tool-call-parser hermes` parameters, as Qwen-Agent will parse the tool outputs from vLLM on its own."

This is the most likely root cause for DollOS's ReAct leakage if it applies. When the server-side parser and the prompt-injection both try to handle tool calls, they conflict. **For DollOS (llama.cpp with `--jinja`), use `use_raw_api=True` equivalent behavior** — pass tools via the OpenAI `tools` parameter and let llama.cpp handle parsing.

### 4b. Default `parallel_function_calls=False`

The Qwen fncall template has a parallel variant that instructs the model to emit multiple `✿FUNCTION✿` blocks in sequence. If you set `parallel_function_calls=True` but the model isn't well-trained for it, you can get malformed multi-call output. Default is `False`.

### 4c. `✿RESULT✿` / `✿RETURN✿` must be stop words

When using the Qwen prompt format, `FN_STOP_WORDS = ['✿RESULT✿', '✿RETURN✿']` MUST be injected as stop sequences. Without them, the model may hallucinate the tool result and continue. This is only relevant if DollOS adopts the Qwen fncall prompt format.

### 4d. Don't mix prompt-injection and native API tool calling

The `use_raw_api=False` path injects tool schemas into the system prompt AND sends the messages as plain text to the API (no `tools=` parameter). The `use_raw_api=True` path passes `tools=` and does NOT inject. Mixing both causes double-format confusion and is the likely source of ReAct bleed-through.

### 4e. `_rm_think` strips `<think>` blocks from text content

```python
def _rm_think(text: str) -> str:
    if '</think>' in text:
        return text.split('</think>')[-1].lstrip('\n')
    return text
```

Qwen-Agent strips thinking content from the post-processed message text. The `reasoning_content` field carries it separately. This means the `<think>` block is never treated as part of the "response" — a design DollOS should mirror.

### 4f. `thought_in_content` flag

`generate_cfg['thought_in_content']` controls whether the `<think>` block text is included in the returned content. Default behavior is to NOT include thinking in the structured message content. The Nous postprocess respects this flag.

---

## 5. Sampling Parameters

**Source:** `qwen_agent/llm/oai.py`, `qwen_agent/llm/base.py`, examples

Qwen-Agent does NOT set its own sampling defaults. It passes `generate_cfg` through directly to the underlying API. The user is responsible for setting:
- `temperature`
- `top_p`
- `top_k`
- `repetition_penalty`

`top_k` and `repetition_penalty` are transparently moved to `extra_body` for OpenAI API v1 compatibility (since OAI v1 doesn't accept them as top-level params):

```python
extra_params = ['top_k', 'repetition_penalty']
if any((k in kwargs) for k in extra_params):
    kwargs['extra_body'] = copy.deepcopy(kwargs.get('extra_body', {}))
    for k in extra_params:
        if k in kwargs:
            kwargs['extra_body'][k] = kwargs.pop(k)
```

The example `assistant_qwen3.py` shows `generate_cfg` as optional/commented out. `enable_thinking` is passed as `extra_body` for OpenAI-compat endpoints:
```python
# generate_cfg = {'extra_body': {'enable_thinking': True}}  # for OAI-compat
# generate_cfg = {'enable_thinking': True}  # for DashScope
```

There is NO `repetition_penalty` default set anywhere in Qwen-Agent. No default temperature either. The framework trusts the server's defaults.

---

## 6. Comparison with DollOS Current Setup

| Aspect | Qwen-Agent (canonical) | DollOS current | Recommendation |
|---|---|---|---|
| **Tool schema injection** | `use_raw_api=True` → pass via `tools=` param, no system prompt injection | Schemas injected into system prompt via `<tools>` block | Align: use `tools=` API param only, remove manual system prompt injection |
| **Tool call format** | `<tool_call>\n{"name":...,"arguments":...}\n</tool_call>` (Nous) | `<tool_call>` per Qwen3 model card | Consistent — no change needed |
| **Tool result format** | `<tool_response>...</tool_response>` in USER role | `<tool_response>` in `tool` role | Minor divergence — Qwen-Agent merges into USER message; Qwen3 native API uses `role: "tool"`. With llama.cpp `use_raw_api` path, use `role: "tool"` (OpenAI convention). |
| **Stop words** | `['✿RESULT✿', '✿RETURN✿']` only for Qwen fncall format; none for Nous/raw | Not documented | If using Nous format text mode: add `✿RESULT✿` / `✿RETURN✿` stop words. If using raw API: not needed. |
| **Agent loop** | `while True; break if no tool call; max 20 iterations` | Inner while-loop cascade | Aligned |
| **Thinking model** | `reasoning_content` separated from `content`; `_rm_think` strips `</think>` text | VoM prefills into `<think>` block | Different purpose — VoM is input prefill, not output stripping. Ensure post-`</think>` content is what's parsed for tool calls. |
| **Parallel tool calls** | Default `False` | Unknown | Set `parallel_function_calls=False` explicitly |
| **Sampling** | No framework defaults; pass through to API | Unknown | No action needed; framework doesn't override |
| **`use_raw_api` for llama.cpp** | Recommended for all OpenAI-compat backends | Likely already using OAI-compat | Verify: if using `tools=` param already → correct; if injecting schemas into system prompt → fix |

### Root cause hypothesis for DollOS's ReAct leakage

The Qwen3 model's RLHF training includes ReAct-format trajectories. The model defaults to ReAct when it receives a system prompt that looks like "here are tools, think step by step" without a clear FORMAT instruction specifying `<tool_call>` JSON. The specific triggers are:

1. **System prompt does not include the exact `<tool_call>` format instruction** from the Nous template (or equivalent) — model falls back to ReAct.
2. **Double-injection conflict** — if DollOS injects tool schemas into the system prompt AND passes `tools=` to the API, llama.cpp's template may inject them again, causing format confusion.
3. **`--reasoning-format none` must be set on llama.cpp** (DollOS already does this per CLAUDE.md) — without it, the server strips/rewrites `<think>` blocks and can corrupt the tool-call parsing.

**Recommended fix:** Use the Nous template wording verbatim in the system prompt, OR rely purely on `tools=` API parameter with llama.cpp `--jinja --tool-call-parser` (the `use_raw_api=True` path). Do NOT mix both.

---

## Sources

- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/fncall_prompts/qwen_fncall_prompt.py — Qwen format prompt + ✿ tokens
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/fncall_prompts/nous_fncall_prompt.py — Nous format (default), `<tool_call>` + `<tool_response>` 
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/function_calling.py — fncall routing, stop word injection, `use_raw_api` path
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/agents/fncall_agent.py — while-loop, MAX_LLM_CALL_PER_RUN, FUNCTION role
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/settings.py — MAX_LLM_CALL_PER_RUN=20, DEFAULT_MAX_INPUT_TOKENS=58000
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/base.py — `_rm_think`, `use_raw_api`, `thought_in_content`
- https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/oai.py — `extra_body` for `top_k`/`repetition_penalty`, `reasoning_content` handling
- https://github.com/QwenLM/Qwen-Agent/blob/main/examples/assistant_qwen3.py — canonical Qwen3 setup example
- https://github.com/QwenLM/Qwen-Agent/blob/main/README.md — vLLM/SGLang warning about `--tool-call-parser`
