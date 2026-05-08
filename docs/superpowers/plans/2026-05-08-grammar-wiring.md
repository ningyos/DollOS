# Plan: Wire B4-typed GBNF grammar into LlamaCppProvider

**Worktree**: `.worktrees/grammar-wiring/`  
**Branch**: `grammar-wiring`  
**Date**: 2026-05-08

## Why

iter probes (`/tmp/probe_grammar.py`, `/tmp/probe_grammar_iterB.py`,
`/tmp/probe_grammar_iter2.py`) converged on **B4-typed**: a GBNF grammar that
constrains the `<think>` block to `SEEN/INTENT/TOOL` fields plus a typed
per-tool `<tool_call>` JSON envelope. 7/8 on T1-T8 smoke. Currently the
grammar field is NOT wired through — `transport.py` request body has no
`grammar` key, so all sampling is free-form. This plan wires it.

## Out of scope

- Inner Voice grammar (small model). Stays free-form for now.
- Memsearch context injection for T2 (separate concern — fabrication is a
  memory-retrieval gap, not a grammar gap).
- Per-request grammar switching (e.g. emoji unlock). Hooks in for it but
  doesn't implement gates.
- scaffolding STATE/RECALL leak cleanup (separate task).

## Changes

### 1. `src/dollos/llm/templates.py`

Add `build_qwen3_think_tool_grammar(tools)` function that, given a list of
pydantic tool classes, returns a GBNF string matching B4-typed:

```
root ::= think tool-call
think ::= "SEEN: " line "INTENT: " line "TOOL: " tool-name "\n</think>\n\n"
line ::= [^\n]+ "\n"
tool-name ::= "Say" | "NoteMemory" | ...
tool-call ::= say-call | note-call | ...
say-call ::= "<tool_call>\n{\"name\": \"Say\", \"arguments\": {\"text\": " str "}}\n</tool_call>"
...
str ::= "\"" str-char* "\""
str-char ::= [^"\\] | "\\" ["\\/bfnrt] | "\\u" hex hex hex hex
hex ::= [0-9a-fA-F]
```

**Per-tool JSON shape rules:**
- Iterate tool's `model_json_schema()` properties.
- **Only include required fields** (skip optionals). Rationale: probe results
  showed extra fields (B7/T5 timeout_s) just add noise; required-only matches
  proven B4-typed structure.
- For required string fields: `\"<fieldname>\": <str>`. Comma-join when
  multiple required strings.
- If a tool has any non-string required field (currently none), raise
  `NotImplementedError`. Keep mechanism narrow; expand when needed.
- Tool names + field names go through a static escape (they're identifiers,
  no special chars expected — assert if any field name contains backslash or
  quote).

### 2. `src/dollos/llm/adapter.py`

Add `grammar: str | None = None` parameter to `LLMAdapter.stream_completion`.

### 3. `src/dollos/llm/transport.py`

- Add `grammar: str | None = None` to `Provider.stream` abstract signature.
- `LlamaCppProvider.stream`: include `"grammar": grammar` in request body
  when non-None. (llama.cpp ignores empty/None grammar; passing only when set
  keeps body clean and old tests stable.)

### 4. `src/dollos/llm/composed.py`

`ComposedLLMAdapter.stream_completion` forwards `grammar` to provider.

### 5. `src/dollos/dispatcher.py`

In `_respond` build grammar once at the top of the cascade loop (or once
before the loop — same TOOLS list each iteration):

```python
grammar = build_qwen3_think_tool_grammar(TOOLS)
```

Pass `grammar=grammar` to `self._adapter.stream_completion(...)`.

### 6. Tests

**`tests/llm/test_grammar.py`** (new):
- Generate grammar for current TOOLS list.
- Assert tool-name enum includes all 5.
- Assert each tool has its own `*-call` rule.
- Assert each rule contains the right field name(s).
- Assert grammar is non-empty and starts with `root ::=`.
- Snapshot test: golden string for current TOOLS (regenerable).

**`tests/llm/test_transport.py`** (extend or new):
- Mock httpx; assert `grammar` key is in body when passed; absent when None.

### 7. Manual smoke

Run `/tmp/smoke_v2.py` (T1-T8 via WS) against daemon with grammar wired.
Expect ≥7/8 pass-rate matching B4 probe results.

## Risks

- **Grammar stops mid-token**: llama.cpp grammar constrains tokenizer-level
  output. Field names must be tokenizable as-is. Sanity: B4 probe already
  worked → not an issue for current TOOLS.
- **Tool args evolution**: if a future tool adds a required nested object,
  grammar generator raises NotImplementedError — surfacing the limit instead
  of silently breaking. Acceptable.

## Acceptance

- [ ] All new tests pass (`uv run pytest tests/llm/`).
- [ ] Existing tests still pass (`uv run pytest`).
- [ ] T1-T8 smoke ≥7/8 against running daemon.
- [ ] Grammar visible in llama-server access log when daemon issues request.
