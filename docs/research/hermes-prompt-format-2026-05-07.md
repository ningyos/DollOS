# Hermes Function-Calling Prompt Format Research

**Date**: 2026-05-07  
**Purpose**: Compare Hermes tool-calling prompt format against DollOS `_format_tools_block` to identify compaction opportunities.

---

## 1. Hermes Function-Calling System Prompt

Nous Research maintains a reference implementation at https://github.com/NousResearch/Hermes-Function-Calling.  
The canonical system prompt is assembled from a YAML file (`prompt_assets/sys_prompt.yml`) with named sections concatenated at render time. Verbatim content:

### Hermes 2 Pro / Hermes 3 (single-turn compact variant)

This is the most widely cited one-liner template, used in most integrations and cited in the Hermes-3 model card:

```
You are a function calling AI model. You are provided with function signatures within <tools></tools> XML tags. You may call one or more functions to assist with the user query. Don't make assumptions about what values to plug into functions. Here are the available tools: <tools> {tools_json} </tools> Use the following pydantic model json schema for each tool call you will make: {"properties": {"arguments": {"title": "Arguments", "type": "object"}, "name": {"title": "Name", "type": "string"}}, "required": ["arguments", "name"], "title": "FunctionCall", "type": "object"} For each function call return a json object with function name and arguments within <tool_call></tool_call> XML tags as follows:
<tool_call>
{"arguments": <args-dict>, "name": <function-name>}
</tool_call>
```

**Character count**: ~777 chars (excluding the tool JSON itself).

### Hermes 3 / full agentic variant (from `sys_prompt.yml`)

The YAML-based version adds multi-step reasoning scaffolding and a code-interpreter escape hatch. Condensed:

```
Role:
  You are a function calling AI agent with self-recursion.
  You can call only one function at a time and analyse data you get from function response.
  You are provided with function signatures within <tools></tools> XML tags.
  The current date is: {date}.

Objective:
  You may use agentic frameworks for reasoning and planning to help with user query.
  Please call a function and wait for function results to be provided to you in the next iteration.
  Don't make assumptions about what values to plug into function arguments.
  Once you have called a function, results will be fed back to you within <tool_response></tool_response> XML tags.
  ...
  Your final response should directly answer the user query with an analysis or summary of the results.

Tools:
  Here are the available tools:
  <tools> {tools} </tools>
  If the provided function signatures doesn't have the function you must call, you may write
  executable python code in markdown syntax and call code_interpreter() as a fallback.

Schema:
  Use the following pydantic model json schema for each tool call you will make:
  {schema}

Instructions:
  At the very first turn you don't have <tool_results> so you should not make up the results.
  ...
  For each function call return a valid json object (using double quotes) with function name
  and arguments within <tool_call></tool_call> XML tags as follows:
  <tool_call>
  {"name": <function-name>, "arguments": <args-dict>}
  </tool_call>
```

### Hermes 4.3 (Qwen2.5-based, with thinking integration)

Hermes 4.3-36B uses the Llama-3 chat format header (`<|start_header_id|>system<|end_header_id|>`) and integrates `<think>` blocks natively:

```
<|start_header_id|>system<|end_header_id|>
You are a function-calling AI. Tools are provided inside <tools>…</tools>.
When appropriate, call a tool by emitting a <tool_call>{...}</tool_call> object.
After a tool responds (as <tool_response>), continue reasoning inside <think> and produce the final answer.
<tools>
{tools_json_one_per_line_no_indent}
</tools><|eot_id|>
```

The model's assistant turn looks like:

```
<think>
…internal reasoning…
</think>
<tool_call>{"name": "get_weather", "arguments": {"city": "Tokyo"}}</tool_call>
```

Hermes 4.3 has **built-in vLLM/SGLang parser support** (`--tool-call-parser hermes` / `--tool-call-parser qwen25`).

### Tool response turn

All Hermes versions use:

```
<tool_response>
{"name": "function_name", "content": {returned_data}}
</tool_response>
```

---

## 2. Tool Schema Format

### Serialization method

Hermes injects tools as a **JSON array** of OpenAI-style function objects placed verbatim inside `<tools>...</tools>` tags. No pretty-printing in the compact variant — Hermes 4 uses **no indentation** (newline-separated JSON objects):

```json
{"type":"function","function":{"name":"get_weather","description":"Get weather by city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}
```

The full agentic variant (`sys_prompt.yml`) passes `tools` as a Python object rendered by the template engine — effectively the same JSON, but the indentation is up to the caller.

### Fields per tool

```json
{
  "type": "function",
  "function": {
    "name": "function_name",
    "description": "one-line or multi-line description",
    "parameters": {
      "type": "object",
      "properties": {
        "param_name": {
          "type": "string",
          "description": "param description"
        }
      },
      "required": ["param_name"]
    }
  }
}
```

**Fields present**: `type`, `function.name`, `function.description`, `function.parameters.type`, `function.parameters.properties[*].type`, `function.parameters.properties[*].description`, `function.parameters.required`.

**Fields absent** (compared to Pydantic `model_json_schema()`): `title` (at both object and property level), `description` duplicated at the top-level parameters object, `minimum`/`maximum` constraints (optional, present when semantically needed).

### Thinking / `<think>` bridge

Hermes 4.3 explicitly bridges thinking to tool calls: `<think>` block contains deliberation, then the assistant emits `<tool_call>` immediately after `</think>`. There is no gap text between `</think>` and the first `<tool_call>`. This is the same pattern DollOS uses.

### Multi-step / reflection

Hermes supports multi-step tool calling within a single assistant session. The YAML-based prompt instructs the model to call one function at a time, receive `<tool_response>`, then continue calling functions until the task is complete (max 10 iterations). Tool responses are injected back as `<tool_response>` tags inside the conversation.

---

## 3. Comparison with DollOS Current Format

### Current `_format_tools_block` output (5 tools: Say, NoteMemory, WriteDiary, Shell, InvokeSkill)

**Total block size: 4,662 chars**

The JSON schemas alone are:

| Tool | `model_json_schema()` size |
|---|---|
| Say | 298 chars |
| NoteMemory | 328 chars |
| WriteDiary | 615 chars |
| Shell | 875 chars |
| InvokeSkill | 549 chars |
| **Total** | **2,665 chars raw** |

After JSON serialization with `indent=2` and being embedded in the list wrapper, the schemas block expands to **~4,050 chars**.

### Structural problems with the current format

**Problem 1: `description` duplicated at two levels.**  
`model_json_schema()` outputs the class docstring as both the top-level `"description"` field *and* inside `parameters.description`. The current `_format_tools_block` wraps `model_json_schema()` verbatim, so every multi-line docstring appears twice.

Example for `Say`:
```json
{
  "name": "Say",
  "description": "Stream text to the user. Call this whenever Doll wants to speak.",
  "parameters": {
    "description": "Stream text to the user. Call this whenever Doll wants to speak.",  // ← DUPLICATE
    "properties": { ... },
    "title": "Say",   // ← REDUNDANT (model already knows the name)
    "type": "object"
  }
}
```

**Problem 2: `title` fields at every level are noise.**  
Pydantic emits `"title": "Say"` on the parameters object and `"title": "Text"` on every property. Models do not use these for anything; they are Pydantic's JSON Schema compliance fields, not tool-calling hints.

**Problem 3: Pydantic top-level `description` bleeds into `parameters`.**  
`model_json_schema()` puts the class docstring at the top level of the schema dict (not inside `function.parameters`), but the current code passes the whole dict as `parameters`, so the docstring appears there instead of or in addition to the outer `description` field.

**Problem 4: `indent=2` on a 5-tool list is expensive.**  
`indent=2` serialization of the current 5-tool list = ~4,050 chars. `indent=None` (compact) would reduce this by ~600 chars from whitespace alone.

### Side-by-side schema for `Shell`

**Current (DollOS)** — 875 chars in raw schema:
```json
{
  "name": "Shell",
  "description": "Execute a shell command. Returns combined stdout+stderr.\n\n...",
  "parameters": {
    "description": "Execute a shell command. Returns combined stdout+stderr.\n\n...",  ← DUPLICATE (298 chars)
    "properties": {
      "command": { "description": "...", "title": "Command", "type": "string" },
      "timeout_s": { "default": 30, "description": "...", "maximum": 300, "minimum": 1, "title": "Timeout S", "type": "integer" }
    },
    "required": ["command"],
    "title": "Shell",   ← REDUNDANT
    "type": "object"
  }
}
```

**Hermes-compact** — same information, ~490 chars:
```json
{
  "type": "function",
  "function": {
    "name": "Shell",
    "description": "Execute a shell command. Returns combined stdout+stderr.\n\n...",
    "parameters": {
      "type": "object",
      "properties": {
        "command": { "type": "string", "description": "The shell command to run (will be passed to bash -c)." },
        "timeout_s": { "type": "integer", "description": "Seconds before timeout. Default 30, max 300.", "default": 30, "minimum": 1, "maximum": 300 }
      },
      "required": ["command"]
    }
  }
}
```

### Total size comparison

| Format | Schemas JSON chars | Full block chars |
|---|---|---|
| DollOS current (`model_json_schema()`, `indent=2`) | ~4,050 | **4,662** |
| Hermes-compact (`indent=2`, fields stripped) | **3,315** | ~3,927 |
| Hermes-compact (`indent=None`, no whitespace) | ~2,400 | ~3,010 |

Stripping `title` and deduplicating `description` saves **~735 chars** with `indent=2`. Switching to `indent=None` saves a further **~900 chars**. Combined: **~1,640 char reduction** (~35% smaller tool block).

---

## 4. Recommendations for DollOS

### Concrete changes to `_format_tools_block`

Replace the current `_format_tools_block` in `/home/progcat/Projects/DollOS/src/dollos/llm/templates.py`:

```python
def _format_tools_block(tools: list[type[BaseModel]]) -> str:
    """Render the `# Tools` system-prompt section — Hermes-compact format."""

    def _compact_schema(cls: type[BaseModel]) -> dict:
        raw = cls.model_json_schema()
        props_raw = raw.get("properties", {})
        props: dict = {}
        for fname, finfo in props_raw.items():
            entry: dict = {}
            if "type" in finfo:
                entry["type"] = finfo["type"]
            if "description" in finfo:
                entry["description"] = finfo["description"]
            if "default" in finfo:
                entry["default"] = finfo["default"]
            if "minimum" in finfo:
                entry["minimum"] = finfo["minimum"]
            if "maximum" in finfo:
                entry["maximum"] = finfo["maximum"]
            props[fname] = entry
        return {
            "type": "function",
            "function": {
                "name": cls.__name__,
                "description": (cls.__doc__ or "").strip(),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": raw.get("required", []),
                },
            },
        }

    schemas = [_compact_schema(cls) for cls in tools]
    schemas_json = json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
    return (
        "\n\n# Tools\n\n"
        "You have tools. To call a tool, emit:\n"
        "<tool_call>\n"
        '{"name": "<tool_name>", "arguments": {<args>}}\n'
        "</tool_call>\n\n"
        "After </think>, output ONLY <tool_call> blocks. "
        "Plain text after </think> is invalid.\n\n"
        "Available tools:\n"
        "<tools>\n"
        f"{schemas_json}\n"
        "</tools>"
    )
```

Key changes:
1. **Strip `title` fields** at both the parameters object level and each property level.
2. **Remove duplicate `description`** from the `parameters` object (it stays on `function.description` only).
3. **Wrap in `type: function` / `function: {...}`** envelope (OpenAI/Hermes canonical form — models are widely trained on this).
4. **Use `separators=(",", ":")`** (compact JSON) instead of `indent=2`. For a tool definition being read by a model, there is no benefit to pretty-printing — the model reads tokens, not columns.

### Estimated character reduction

| Change | Savings |
|---|---|
| Remove duplicate `description` in `parameters` | ~700 chars |
| Remove `title` fields (object + all properties) | ~120 chars |
| Switch `indent=2` → compact separators | ~900 chars |
| **Total estimated** | **~1,720 chars** (~37% of current 4,662-char block) |

### Field retention decisions

- **Keep `description` on each property**: this is the most token-efficient way to tell the model what each argument does. Do not remove.
- **Keep `default` on optional fields**: needed for the model to know it doesn't have to supply the argument.
- **Keep `minimum`/`maximum` on `timeout_s`**: these are semantically meaningful constraints, not schema boilerplate.
- **Drop `title` everywhere**: Pydantic adds these for JSON Schema compliance; tool-calling models ignore them entirely.
- **Drop top-level `parameters.description`**: already present as `function.description`; duplication is pure waste.

### Caveats

- **`type: function` wrapper**: Hermes and OpenAI both use `{"type": "function", "function": {...}}`. Qwen3's own tool-calling training also uses this envelope. Adding it makes the format more universally recognizable; omitting it saves 28 chars per tool but risks ambiguity.
- **Compact JSON readability**: `indent=None` makes the schemas hard for humans to read in logs. Consider keeping `indent=2` in a debug/verbose mode only.
- **`$defs` / nested models**: if a future tool uses nested Pydantic models, `model_json_schema()` will emit a `$defs` block. The `_compact_schema` helper above does not handle `$ref` resolution. For the current 5 tools (all flat), this is a non-issue.
- **Framing text**: the current framing ("You have tools. To call a tool, emit:...") is already compact (~200 chars). Hermes's framing is longer (~777 chars) because it includes the full `FunctionCall` pydantic schema inline. DollOS's framing is better; keep it as-is.
