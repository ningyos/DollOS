# Tool System Polish (Spec A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Doll 用得襯手、砍掉機械型 fail-tooling——解鎖被 grammar 鎖死的 optional 工具參數、對齊整數語意、移除「單一工具 build 失敗→全部 unconstrained」沉默懸崖、友善化錯誤訊息、精簡狀態工具、合一單一工具 dispatch。

**Architecture:** 純機械層打磨，不碰跨-turn 學習迴路（Spec B）。grammar 改在 `llm/templates.py`；dispatch 合一在 `cascade/tool_loop.py`；工具定義在 `tools.py`；live loop 初始化在 `mind/mind_loop.py`。

**Tech Stack:** Python 3、pydantic v2（`model_json_schema()` / `model_validate()` / `ValidationError`）、GBNF（llama.cpp grammar）、pytest（含 `pytest.mark.asyncio`）。

## Global Constraints

- **No fallback**：不得實作降級/fallback。grammar build 失敗 = 啟動時 raise，不得靜默轉 unconstrained。
- **B4 grammar 維持 required-only**：`build_qwen3_think_tool_grammar`（subagent 用）不開 optional；只有 `build_voice_first_grammar`（Doll live）開 `include_optional=True`。
- **既有 B4 grammar 測試須維持綠**：新 `_build_tool_call_rule` 對 required-only 輸出須 byte-相容（zero-required → 空 `{}`）。
- **TDD**：每個行為先寫失敗測試，再實作，跑綠，commit。
- 測試指令：`cd /home/progcat/Projects/DollOS && uv run pytest <path> -q`。
- 全程在 branch `feat/tool-system-polish`。

---

## File Structure

- `src/dollos/llm/templates.py` — `_JSON_STR_RULES`（signed integer）、`_build_tool_call_rule`（重寫：`include_optional` 參數 + anyOf 抽型別）、`build_voice_first_grammar`（傳 `include_optional=True`）、新 helper `_field_value_token`。
- `src/dollos/mind/mind_loop.py` — `__init__`（移除 grammar build 吞例外）、`_dispatch_tool`（改呼叫共用 `dispatch_one`）。
- `src/dollos/cascade/tool_loop.py` — 新增 `format_unknown_tool` / `format_validation_error` / `dispatch_one`；`dispatch_tool_call` 改薄包裝；`ctx: "ToolCtx"` 註解改 `MindCtx`。
- `src/dollos/tools.py` — 新增 `Scratchpad`、移除 4 個舊 scratchpad 工具、移除 `ToolCtx`、更新 `MAIN_TOOLS`/`SUB_TOOLS`、清過時描述。
- `tests/test_llm_grammar.py` — 新增 optional/signed-int 測試；更新寫死的 rule-id dict。
- `tests/test_tools.py` — Scratchpad 測試；移除舊 scratchpad 測試。
- `tests/test_tool_loop.py`（新檔）— `dispatch_one` + 友善錯誤測試。
- `tests/test_mind_loop.py` — 新增 grammar-cliff raise 測試。

---

## Task 1: Grammar 整數規則支援負數

**Files:**
- Modify: `src/dollos/llm/templates.py`（`_JSON_STR_RULES`，約 line 109-112）
- Test: `tests/test_llm_grammar.py`

**Interfaces:**
- Produces: `_JSON_STR_RULES` 內含 `integer ::= "-"? ( "0" | [1-9] [0-9]* )`。

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_llm_grammar.py`：

```python
def test_integer_rule_allows_negative():
    """ReadToolOutput.offset 描述支援負數（從尾端數）；grammar 的 integer
    規則須容許前導負號。語意邊界(ge/le)仍由 pydantic 守門。"""
    from dollos.tools import NoteMemory
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([NoteMemory])
    assert 'integer ::= "-"? ( "0" | [1-9] [0-9]* )' in g
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_llm_grammar.py::test_integer_rule_allows_negative -q`
Expected: FAIL（目前是 `integer ::= "0" | [1-9] [0-9]*`，無 `"-"?`）。

- [ ] **Step 3: 實作**

在 `src/dollos/llm/templates.py` 的 `_JSON_STR_RULES`，把：

```python
    "integer ::= \"0\" | [1-9] [0-9]*\n"
```

改為：

```python
    "integer ::= \"-\"? ( \"0\" | [1-9] [0-9]* )\n"
```

（保留上方關於「pydantic 仍驗 bounds」的註解，並補一句負號用於 offset 從尾端數。）

- [ ] **Step 4: 跑測試確認通過 + 既有 grammar 測試不破**

Run: `uv run pytest tests/test_llm_grammar.py -q`
Expected: PASS（含既有測試；無測試斷言舊整數規則的精確字串）。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_grammar.py
git commit -m "feat(grammar): integer rule allows leading minus (offset from end)"
```

---

## Task 2: Grammar optional 參數可達（voice-only）

**Files:**
- Modify: `src/dollos/llm/templates.py`（重寫 `_build_tool_call_rule`；新增 `_field_value_token`；`build_voice_first_grammar` 傳 `include_optional=True`）
- Test: `tests/test_llm_grammar.py`

**Interfaces:**
- Consumes: 既有 helper `_rule_id` / `_resolve_ref` / `_aux_rule_id` / `_check_ident`（保留於 `_build_tool_call_rule` 內或提到模組層）。
- Produces:
  - `_build_tool_call_rule(tool, used_aux_rule_ids=None, *, include_optional=False) -> tuple[str, str]`
  - `_field_value_token(...) -> str`（回傳 `str` / `integer` / `(<enum-alts>)` / `<array-rule-id>`；處理 `anyOf` 的 `X|None`）。
  - `build_voice_first_grammar` 內呼叫 `_build_tool_call_rule(tool, used_aux_rule_ids, include_optional=True)`。
  - `build_qwen3_think_tool_grammar` 維持 `include_optional` 預設 False（required-only 不變）。

- [ ] **Step 1: 寫失敗測試（voice 開 optional / B4 不開 / anyOf / zero-required gate）**

加到 `tests/test_llm_grammar.py`：

```python
def test_voice_grammar_emits_optional_integer_suffix():
    """Shell.timeout_s 有 default(60) → optional；voice grammar 須以
    『( ", \\"timeout_s\\": " integer )?』後綴讓 Doll 能設定它。"""
    from dollos.tools import Shell
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([Shell])
    shell_rule = next(l for l in g.splitlines() if l.startswith("shell-call ::="))
    assert r'( ", \"timeout_s\": " integer )?' in shell_rule
    # required field still present, before the optional suffix
    assert r'\"command\": " str' in shell_rule


def test_voice_grammar_extracts_anyof_optional_string():
    """SpawnMonitor.match_regex 是 str|None（schema anyOf）→ optional string。"""
    from dollos.tools import SpawnMonitor
    from dollos.llm.templates import build_voice_first_grammar
    g = build_voice_first_grammar([SpawnMonitor])
    rule = next(l for l in g.splitlines() if l.startswith("spawn-monitor-call ::="))
    assert r'( ", \"match_regex\": " str )?' in rule
    assert r'( ", \"rate_limit_s\": " integer )?' in rule


def test_b4_grammar_stays_required_only():
    """B4 (subagent) grammar 不開 optional — Shell 仍只有 command。"""
    from dollos.tools import Shell
    from dollos.llm.templates import build_qwen3_think_tool_grammar
    g = build_qwen3_think_tool_grammar([Shell])
    shell_rule = next(l for l in g.splitlines() if l.startswith("shell-call ::="))
    assert "timeout_s" not in shell_rule


def test_voice_grammar_zero_required_optional_only_stays_empty():
    """Zero-required 工具即使 include_optional 也維持空 {}（前導逗號邊界，spec §3.1）。"""
    from pydantic import BaseModel, Field
    from dollos.llm.templates import build_voice_first_grammar

    class _OptOnly(BaseModel):
        x: str = Field(default="hi", description="opt")

    g = build_voice_first_grammar([_OptOnly])
    assert r'\"arguments\": {}}\n</tool_call>"' in g
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_llm_grammar.py::test_voice_grammar_emits_optional_integer_suffix tests/test_llm_grammar.py::test_voice_grammar_extracts_anyof_optional_string tests/test_llm_grammar.py::test_b4_grammar_stays_required_only tests/test_llm_grammar.py::test_voice_grammar_zero_required_optional_only_stays_empty -q`
Expected: FAIL（optional 後綴尚未實作）。

- [ ] **Step 3: 實作——重寫 `_build_tool_call_rule` + 新增 `_field_value_token`**

在 `src/dollos/llm/templates.py`，把整個 `_build_tool_call_rule`（約 line 116-265）替換為下列兩個函式。內部 helper（`_check_ident` / `_rule_id` / `_resolve_ref` / `_aux_rule_id`）行為不變，這裡提到 `_build_tool_call_rule` 外或保留在內皆可——以下版本保留在內並讓 `_field_value_token` 收下它們所需的閉包資料：

```python
def _field_value_token(
    name: str,
    fname: str,
    finfo: dict,
    schema: dict,
    used_aux_rule_ids: set[str],
    extra_rules: list[str],
    check_ident,
    resolve_ref,
    aux_rule_id,
) -> str:
    """GBNF value token for one field: 'str' / 'integer' / '(<enum-alts>)' /
    '<array-rule-id>'. Appends any array aux rules to extra_rules.

    Handles `anyOf: [<T>, {"type":"null"}]` (optional X | None) by extracting
    the single non-null branch. Raises NotImplementedError on unsupported types.
    """
    ftype = finfo.get("type")
    enum_vals = finfo.get("enum")
    if ftype is None and "anyOf" in finfo:
        non_null = [s for s in finfo["anyOf"] if s.get("type") != "null"]
        if len(non_null) != 1:
            raise NotImplementedError(
                f"tool {name} field {fname!r} anyOf {finfo['anyOf']!r} is not a "
                f"simple `X | None`; grammar build unsupported"
            )
        finfo = non_null[0]
        ftype = finfo.get("type")
        enum_vals = finfo.get("enum")
    if ftype == "string" and enum_vals:
        for v in enum_vals:
            if not isinstance(v, str):
                raise NotImplementedError(
                    f"tool {name} field {fname!r} enum value {v!r} is not a string"
                )
            check_ident(v, "enum value")
        alt = " | ".join(f'"\\"{v}\\""' for v in enum_vals)
        return f"({alt})"
    if ftype == "string":
        return "str"
    if ftype == "integer":
        return "integer"
    if ftype == "array":
        items = finfo.get("items", {})
        ref = items.get("$ref")
        if not ref:
            raise NotImplementedError(
                f"tool {name} required field {fname!r} array items have no $ref; "
                f"only $ref-typed array items supported"
            )
        item_schema = resolve_ref(schema, ref)
        if item_schema.get("type") != "object":
            raise NotImplementedError(
                f"tool {name} required field {fname!r} item is not an object; "
                f"grammar build unsupported"
            )
        item_props = item_schema.get("properties", {})
        item_required = item_schema.get("required", [])
        inner_parts: list[str] = []
        for ifname in item_required:
            check_ident(ifname, "field name")
            iinfo = item_props.get(ifname, {})
            ityp = iinfo.get("type")
            if ityp == "string":
                inner_parts.append(f'\\"{ifname}\\": " str "')
            elif ityp == "integer":
                inner_parts.append(f'\\"{ifname}\\": " integer "')
            else:
                raise NotImplementedError(
                    f"tool {name} field {fname!r} item field {ifname!r} has "
                    f"unsupported type {ityp!r}; grammar build unsupported"
                )
        inner_joined = (
            ', '.join(inner_parts) if len(inner_parts) > 1
            else (inner_parts[0] if inner_parts else "")
        )
        item_rule_id = aux_rule_id(name, f"{fname}-item")
        array_rule_id = aux_rule_id(name, f"{fname}-array")
        if item_rule_id not in used_aux_rule_ids:
            extra_rules.append(f'{item_rule_id} ::= "{{{inner_joined}}}"')
            used_aux_rule_ids.add(item_rule_id)
        if array_rule_id not in used_aux_rule_ids:
            extra_rules.append(
                f'{array_rule_id} ::= "[" {item_rule_id} ("," {item_rule_id})* "]"'
            )
            used_aux_rule_ids.add(array_rule_id)
        return array_rule_id
    raise NotImplementedError(
        f"tool {name} required field {fname!r} has unsupported type {ftype!r}; "
        f"grammar build unsupported"
    )


def _build_tool_call_rule(
    tool: type[BaseModel],
    used_aux_rule_ids: set[str] | None = None,
    *,
    include_optional: bool = False,
) -> tuple[str, str]:
    """Build the GBNF rule for a single <tool_call>...</tool_call>.

    Returns ``(rule_id, rule_text)``. With ``include_optional=True`` AND ≥1
    required field, optional (default-valued) fields are appended as
    fixed-order ``( ", \\"<name>\\": " <type> )?`` suffixes so they can be
    emitted-or-omitted; each carries its own leading comma so the JSON stays
    valid. Zero-required tools keep an empty ``{}`` body regardless (spec §3.1).

    Required-only output (include_optional=False) is byte-compatible with the
    pre-refactor builder.

    Raises NotImplementedError if any field has an unsupported type, or if any
    name contains characters needing escaping.
    """
    if used_aux_rule_ids is None:
        used_aux_rule_ids = set()

    def _check_ident(s: str, what: str) -> None:
        if "\\" in s or '"' in s:
            raise NotImplementedError(
                f"{what} {s!r} contains backslash/quote; grammar escape unsupported"
            )

    def _rule_id(name: str) -> str:
        out: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        return "".join(out) + "-call"

    def _resolve_ref(schema: dict, ref: str) -> dict:
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            raise NotImplementedError(
                f"unsupported $ref {ref!r}; only #/$defs/<Name> supported"
            )
        return schema["$defs"][ref[len(prefix):]]

    def _aux_rule_id(base: str, suffix: str) -> str:
        out: list[str] = []
        for i, ch in enumerate(base):
            if ch.isupper() and i > 0:
                out.append("-")
            out.append(ch.lower())
        return "".join(out) + "-" + suffix

    name = tool.__name__
    _check_ident(name, "tool name")
    schema = tool.model_json_schema()
    props = schema.get("properties", {})
    required = schema.get("required", [])
    rule_id = _rule_id(name)
    extra_rules: list[str] = []

    if not required:
        # Zero required fields → empty arguments body. Optional fields (if any)
        # are unreachable; see spec §3.1. Preserves prior behavior + tests.
        call_rule = (
            f'{rule_id} ::= "<tool_call>\\n'
            f'{{\\"name\\": \\"{name}\\", \\"arguments\\": {{}}}}\\n</tool_call>"'
        )
        return rule_id, call_rule

    for fname in required:
        _check_ident(fname, "field name")

    def _val(fname: str) -> str:
        return _field_value_token(
            name, fname, props.get(fname, {}), schema, used_aux_rule_ids,
            extra_rules, _check_ident, _resolve_ref, _aux_rule_id,
        )

    first = required[0]
    terms: list[str] = [
        f'"<tool_call>\\n{{\\"name\\": \\"{name}\\", \\"arguments\\": '
        f'{{\\"{first}\\": "',
        _val(first),
    ]
    for fname in required[1:]:
        terms.append(f'", \\"{fname}\\": "')
        terms.append(_val(fname))

    if include_optional:
        for fname in [f for f in props if f not in required]:
            _check_ident(fname, "field name")
            terms.append(f'( ", \\"{fname}\\": " {_val(fname)} )?')

    terms.append('"}}\\n</tool_call>"')
    call_rule = f"{rule_id} ::= " + " ".join(terms)
    rule_text = call_rule + ("\n" + "\n".join(extra_rules) if extra_rules else "")
    return rule_id, rule_text
```

然後在 `build_voice_first_grammar` 內，把：

```python
        rid, rtext = _build_tool_call_rule(tool, used_aux_rule_ids)
```

改為：

```python
        rid, rtext = _build_tool_call_rule(
            tool, used_aux_rule_ids, include_optional=True
        )
```

（`build_qwen3_think_tool_grammar` 內的呼叫**不動**，維持 required-only。）

- [ ] **Step 4: 跑測試確認通過 + 既有 grammar 測試全綠（byte-相容驗證）**

Run: `uv run pytest tests/test_llm_grammar.py -q`
Expected: PASS——新測試綠，且既有 `test_multi_required_strings_comma_joined` /
`test_optional_only_tool_has_empty_args_body` / `test_integer_required_field_emits_integer_rule` /
`test_grammar_each_call_rule_has_tool_call_envelope` /
`test_grammar_per_tool_field_names_match_required_strings` 全綠（證明 required-only byte-相容）。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_llm_grammar.py
git commit -m "feat(grammar): voice grammar emits optional field suffixes (Doll can set timeout/regex/date filters)"
```

---

## Task 3: 移除「grammar build 失敗→全部 unconstrained」沉默懸崖

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`（`__init__`，約 line 113-124）
- Test: `tests/test_mind_loop.py`

**Interfaces:**
- Consumes: `_build_tool_call_rule` 對未支援型別 raise `NotImplementedError`（Task 2 行為）。
- Produces: `MindLoop.__init__` 在 grammar build 失敗時**讓例外往上拋**（不再 `grammar=None`）。

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_mind_loop.py`（頂部已 import `MindLoop` / `MindState` / `PerceptionQueue`）：

```python
def test_mind_loop_init_raises_on_unbuildable_grammar(tmp_path):
    """No-fallback: 一個帶未支援型別欄位的工具讓 grammar build 失敗時，
    MindLoop 必須在啟動時 raise，而不是靜默以 grammar=None 跑無約束 decode。"""
    from pydantic import BaseModel, Field
    from tests._dispatcher_helpers import _make_mind_ctx

    class _BadTool(BaseModel):
        flag: bool = Field(description="unsupported type")

    ctx = _make_mind_ctx(tmp_path)
    with pytest.raises(NotImplementedError):
        MindLoop(
            state=MindState(),
            queue=PerceptionQueue(),
            ctx=ctx,
            llm=_FakeLLM(""),
            system_prompt="",
            state_persist_path=tmp_path / "s.json",
            tool_registry={"_BadTool": _BadTool},
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_mind_loop.py::test_mind_loop_init_raises_on_unbuildable_grammar -q`
Expected: FAIL（目前 `except Exception` 吞掉 → 不 raise，`grammar=None`）。

- [ ] **Step 3: 實作**

在 `src/dollos/mind/mind_loop.py` 的 `__init__`，把：

```python
        if self._tool_registry:
            try:
                self._grammar = build_voice_first_grammar(
                    list(self._tool_registry.values())
                )
            except Exception:
                logger.exception("failed to build voice_first grammar; running unconstrained")
                self._grammar = None
        else:
            self._grammar = None
```

改為：

```python
        # No-fallback (spec §3.3): a grammar build failure is a tool-set config
        # error. Let it raise at startup — the daemon must refuse to run with a
        # half-built / unconstrained tool set rather than silently degrade.
        if self._tool_registry:
            self._grammar = build_voice_first_grammar(
                list(self._tool_registry.values())
            )
        else:
            self._grammar = None
```

- [ ] **Step 4: 跑測試確認通過 + 既有 mind_loop 測試不破**

Run: `uv run pytest tests/test_mind_loop.py -q`
Expected: PASS（既有測試用 `MAIN_TOOLS`，build 成功，不受影響）。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop.py
git commit -m "fix(mind): grammar build failure raises at startup (no silent unconstrained fallback)"
```

---

## Task 4: 友善錯誤訊息 helper（純函式）

**Files:**
- Modify: `src/dollos/cascade/tool_loop.py`（新增兩個純函式）
- Test: `tests/test_tool_loop.py`（新檔）

**Interfaces:**
- Produces:
  - `format_unknown_tool(name: str, registry: dict[str, type]) -> str`
  - `format_validation_error(exc: ValidationError, tool_name: str) -> str`

- [ ] **Step 1: 寫失敗測試**

新檔 `tests/test_tool_loop.py`：

```python
"""Tests for cascade.tool_loop shared dispatch + friendly error formatting."""
from __future__ import annotations

from pydantic import ValidationError

from dollos.cascade.tool_loop import format_unknown_tool, format_validation_error
from dollos.tools import ReadToolOutput, Recall, Shell


def test_format_unknown_tool_lists_available():
    msg = format_unknown_tool("Foo", {"Shell": Shell, "Recall": Recall})
    assert "Foo" in msg
    assert "Shell" in msg and "Recall" in msg


def test_format_validation_error_names_field_not_raw_wall():
    """限制超界(limit ge=1) → 友善訊息含工具名+欄位名+給定值，不含 pydantic 原始牆。"""
    try:
        ReadToolOutput.model_validate({"id": "x", "offset": 0, "limit": 0})
    except ValidationError as e:
        msg = format_validation_error(e, "ReadToolOutput")
    assert "ReadToolOutput" in msg
    assert "limit" in msg
    assert "0" in msg
    # 不是 pydantic 原始錯誤牆（不含 URL / 'validation error(s) for'）
    assert "https://" not in msg
    assert "validation error" not in msg.lower()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tool_loop.py -q`
Expected: FAIL（`ImportError`：函式未定義）。

- [ ] **Step 3: 實作**

在 `src/dollos/cascade/tool_loop.py`（`ToolResult` 定義之後、`dispatch_tool_call` 之前）新增：

```python
def format_unknown_tool(name: str, registry: dict[str, type]) -> str:
    """LLM-friendly unknown-tool message listing the valid tool names."""
    available = ", ".join(sorted(registry)) if registry else "(none)"
    return f"未知工具 {name!r}。可用工具：{available}"


def format_validation_error(exc: ValidationError, tool_name: str) -> str:
    """Flatten a pydantic ValidationError into a terse, actionable message.

    One line per bad field: ``<field>: <msg> (你給了 <value>)``. Avoids the raw
    pydantic error wall (URLs / 'N validation errors for ...') which costs
    tokens and reads as noise to the model.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        given = err.get("input", None)
        lines.append(f"{loc}: {msg}（你給了 {given!r}）")
    body = "; ".join(lines)
    return f"工具 {tool_name} 參數錯誤：{body}"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_tool_loop.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/cascade/tool_loop.py tests/test_tool_loop.py
git commit -m "feat(cascade): LLM-friendly unknown-tool + validation-error formatters"
```

---

## Task 5: 合一單一工具 dispatch（`dispatch_one`）+ 接友善錯誤 + 刪 ToolCtx

**Files:**
- Modify: `src/dollos/cascade/tool_loop.py`（新增 `dispatch_one`；`dispatch_tool_call` 改薄包裝；`ctx: "ToolCtx"` 註解改 `MindCtx`）
- Modify: `src/dollos/mind/mind_loop.py`（`_dispatch_tool` 改呼叫 `dispatch_one`）
- Modify: `src/dollos/tools.py`（移除 `ToolCtx` class，約 line 86-106）
- Test: `tests/test_tool_loop.py`

**Interfaces:**
- Consumes: `format_unknown_tool` / `format_validation_error`（Task 4）；`MindCtx`（含 `.sink` 恆 None）。
- Produces: `async dispatch_one(name: str, arguments: dict, ctx, registry: dict[str, type], *, error_sink=None) -> ToolResult | None`。
  - unknown → `ToolResult(success=False, detail=format_unknown_tool(...))`
  - 驗證失敗 → `ToolResult(success=False, detail=format_validation_error(...))`
  - run() 例外 → 若 `error_sink` 非 None 推 `ErrorMsg`，回 `ToolResult(success=False, detail="runtime error: ...")`
  - run() 回 None → None；回 str → `ToolResult(success=True, detail=str)`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_tool_loop.py`：

```python
import pytest

from dollos.cascade.tool_loop import dispatch_one


@pytest.mark.asyncio
async def test_dispatch_one_unknown_tool_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Nope", {}, ctx, {"Shell": Shell})
    assert r is not None and r.success is False
    assert "Nope" in r.detail and "Shell" in r.detail


@pytest.mark.asyncio
async def test_dispatch_one_validation_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    registry = {"ReadToolOutput": ReadToolOutput}
    r = await dispatch_one(
        "ReadToolOutput", {"id": "x", "offset": 0, "limit": 0}, ctx, registry
    )
    assert r is not None and r.success is False
    assert "limit" in r.detail
    assert "validation error" not in r.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_one_success_returns_detail(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one(
        "SetFocus", {"text": "writing the plan"}, ctx, {"SetFocus": __import__(
            "dollos.tools", fromlist=["SetFocus"]).SetFocus}
    )
    assert r is not None and r.success is True
    assert "writing the plan" in r.detail
    assert ctx.mind_state.focus == "writing the plan"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tool_loop.py -q`
Expected: FAIL（`dispatch_one` 未定義）。

- [ ] **Step 3: 實作 `dispatch_one` + 改寫 `dispatch_tool_call`**

在 `src/dollos/cascade/tool_loop.py`，先把 `if TYPE_CHECKING:` 區塊的
`from dollos.tools import ToolCtx` 改成 `from dollos.mind.mind_ctx import MindCtx`，
並把檔內所有 `ctx: "ToolCtx"` 型別註解（`dispatch_tool_call` / `run_tool_cascade` /
`check_early_exit` callable）改為 `ctx: "MindCtx"`。

新增 `dispatch_one`（放在 `format_validation_error` 之後）：

```python
async def dispatch_one(
    name: str,
    arguments: dict,
    ctx: "MindCtx",
    registry: dict[str, type],
    *,
    error_sink=None,
) -> ToolResult | None:
    """Validate + run one tool call. Single source of truth for both the live
    MindLoop and the subagent cascade (spec §3.6).

    Returns None for side-effect tools (run() -> None); ToolResult otherwise.
    Friendly messages via format_unknown_tool / format_validation_error.
    """
    tool_cls = registry.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %r", name)
        return ToolResult(
            tool_name=name, success=False, detail=format_unknown_tool(name, registry)
        )
    try:
        tool = tool_cls.model_validate(arguments)
    except ValidationError as e:
        logger.warning("tool args validation failed for %s: %s", name, e)
        return ToolResult(
            tool_name=name, success=False, detail=format_validation_error(e, name)
        )
    try:
        returned = await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        if error_sink is not None:
            error_sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
        return ToolResult(
            tool_name=name, success=False, detail=f"runtime error: {e}"
        )
    if returned is None:
        return None
    return ToolResult(tool_name=name, success=True, detail=returned)
```

把既有 `dispatch_tool_call` 改寫為薄包裝（保留 name 非字串守門）：

```python
async def dispatch_tool_call(
    call: dict,
    ctx: "MindCtx",
    tools_by_name: dict[str, type],
) -> ToolResult | None:
    """Thin wrapper over dispatch_one for the run_tool_cascade call path."""
    name = call.get("name")
    if not isinstance(name, str):
        return ToolResult(
            tool_name=str(name),
            success=False,
            detail="missing or non-string 'name' field in tool_call",
        )
    return await dispatch_one(
        name, call.get("arguments", {}) or {}, ctx, tools_by_name,
        error_sink=ctx.sink,
    )
```

- [ ] **Step 4: 改寫 `mind_loop._dispatch_tool`**

在 `src/dollos/mind/mind_loop.py`，把 import 加上 `dispatch_one`：

```python
from dollos.cascade.tool_loop import ToolResult, dispatch_one
```

把 `_dispatch_tool`（約 line 573-611）整段 body 改為呼叫共用核心：

```python
    async def _dispatch_tool(
        self, name: str, arguments: dict
    ) -> ToolResult | None:
        """Dispatch via the shared cascade.tool_loop.dispatch_one (spec §3.6).

        Applies the safe-mode-narrowed registry; MindCtx.sink is always None so
        no user-facing ErrorMsg is pushed here.
        """
        return await dispatch_one(
            name, arguments, self._ctx, self._active_tool_registry()
        )
```

- [ ] **Step 5: 刪除 ToolCtx 死碼**

在 `src/dollos/tools.py`，移除 `ToolCtx` class 與其上方 DEPRECATED 註解區塊
（約 line 86-106，含 `@dataclass\nclass ToolCtx: ...`）。確認 `tools.py` 不再被任何
非-TYPE_CHECKING 程式碼 import `ToolCtx`：

Run: `grep -rn "ToolCtx" src tests`
Expected: 無結果（或僅剩需一併清掉的註解）。

- [ ] **Step 6: 跑測試確認通過（含既有 subagent / tools / mind_loop）**

Run: `uv run pytest tests/test_tool_loop.py tests/test_tools.py tests/test_subagent.py tests/test_mind_loop.py -q`
Expected: PASS（dispatch 合一後 live + subagent 行為一致）。

- [ ] **Step 7: Commit**

```bash
git add src/dollos/cascade/tool_loop.py src/dollos/mind/mind_loop.py src/dollos/tools.py tests/test_tool_loop.py
git commit -m "refactor(cascade): unify single-tool dispatch into dispatch_one; wire friendly errors; drop dead ToolCtx"
```

---

## Task 6: 工具去重——Scratchpad 合一、砍 EditScratchpad

**Files:**
- Modify: `src/dollos/tools.py`（新增 `Scratchpad`；移除 `WriteScratchpad`/`AppendScratchpad`/`EditScratchpad`/`ClearScratchpad`；更新 `MAIN_TOOLS`/`SUB_TOOLS`；清 `ReadToolOutput` 過時描述）
- Modify: `tests/test_tools.py`（Scratchpad 測試；移除舊 scratchpad 測試）
- Modify: `tests/test_llm_grammar.py`（更新 `test_grammar_has_per_tool_call_rule_for_each_tool` 寫死的 dict）
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: 既有 `dollos.mind.scratchpad_helpers`（`write` / `append` / `clear`）。
- Produces: `Scratchpad(op: Literal["set","append","clear"], content: str)`；更新後的 `MAIN_TOOLS` / `SUB_TOOLS`。

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_tools.py`（沿用既有 `_make_ctx` helper）：

```python
@pytest.mark.asyncio
async def test_scratchpad_set_then_append_then_clear(tmp_path):
    from dollos.tools import Scratchpad
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await Scratchpad(op="set", content="line A").run(ctx)
    assert ctx.mind_state.scratchpad == "line A"
    await Scratchpad(op="append", content="line B").run(ctx)
    assert "line A" in ctx.mind_state.scratchpad
    assert "line B" in ctx.mind_state.scratchpad
    await Scratchpad(op="clear", content="").run(ctx)
    assert ctx.mind_state.scratchpad == ""


def test_main_tools_has_scratchpad_not_old_four():
    from dollos.tools import MAIN_TOOLS
    names = {c.__name__ for c in MAIN_TOOLS}
    assert "Scratchpad" in names
    for gone in ("WriteScratchpad", "AppendScratchpad", "EditScratchpad", "ClearScratchpad"):
        assert gone not in names
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools.py::test_scratchpad_set_then_append_then_clear tests/test_tools.py::test_main_tools_has_scratchpad_not_old_four -q`
Expected: FAIL（`Scratchpad` 未定義；舊四工具仍在）。

- [ ] **Step 3: 實作 Scratchpad、移除舊四工具**

在 `src/dollos/tools.py`，移除 `WriteScratchpad` / `AppendScratchpad` /
`EditScratchpad` / `ClearScratchpad` 四個 class（約 line 616-683），以單一 `Scratchpad`
取代：

```python
class Scratchpad(BaseModel):
    """Manage your working-memory scratchpad (hard cap 2000 chars).

    op="set": overwrite with `content`.
    op="append": add `content` as a new line (newline auto-prepended if non-empty).
    op="clear": wipe to empty (`content` is ignored).

    Use "set" to start fresh, "append" to jot a running note. To replace a
    specific substring, just "set" the full new contents.
    """

    op: Literal["set", "append", "clear"] = Field(
        description='"set" overwrite | "append" add a line | "clear" wipe'
    )
    content: str = Field(
        description="New content for set/append (≤2000 chars). Ignored for clear.",
    )

    def _summary(self) -> str:
        return f"scratchpad {self.op} ({len(self.content)} chars)"

    async def run(self, ctx: "MindCtx") -> str:
        if self.op == "set":
            scratchpad_helpers.write(ctx.mind_state, self.content)
            result = f"scratchpad set ({len(self.content)} chars)"
        elif self.op == "append":
            new_total = scratchpad_helpers.append(ctx.mind_state, self.content)
            result = f"scratchpad now {new_total} chars"
        else:  # clear
            scratchpad_helpers.clear(ctx.mind_state)
            result = "scratchpad cleared"
        _record(ctx, "Scratchpad", self._summary())
        return result
```

更新 `MAIN_TOOLS`：把 `WriteScratchpad, AppendScratchpad, EditScratchpad, ClearScratchpad`
那行換成 `Scratchpad,`：

```python
MAIN_TOOLS: list[type[BaseModel]] = [
    NoteMemory, WriteDiary, WriteSchedule, Shell,
    InvokeSkill, Recall, SpawnSubagent, SpawnMonitor, RemoveMonitor,
    ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
    MoodTool,
]
```

更新 `SUB_TOOLS`：把 `WriteScratchpad, AppendScratchpad, EditScratchpad, ClearScratchpad`
換成 `Scratchpad`：

```python
SUB_TOOLS: list[type[BaseModel]] = [
    Shell, NoteMemory, Recall, InvokeSkill, Report,
    SpawnMonitor, RemoveMonitor, ReadToolOutput, GrepToolOutput,
    Scratchpad,
    SetFocus, OpenLoop, CloseLoop,
]
```

清掉 `ReadToolOutput` 過時的「REQUIRED — do not omit」哀求式描述——把 `offset` / `limit`
的 Field description 改為中性說明：

```python
    offset: int = Field(
        ...,
        description="zero-indexed start line; 0 = beginning; negative counts from end.",
    )
    limit: int = Field(
        ...,
        ge=1,
        le=500,
        description="max lines to return (1-500).",
    )
```

- [ ] **Step 4: 移除舊 scratchpad 測試 + 更新 B4 grammar dict**

在 `tests/test_tools.py`，移除任何針對 `WriteScratchpad` / `AppendScratchpad` /
`EditScratchpad` / `ClearScratchpad` 的測試（grep 確認）：

Run: `grep -n "Scratchpad" tests/test_tools.py`
移除 set/append/edit/clear 各別舊測試，僅保留 Step 1 新增的 `Scratchpad` 測試。

在 `tests/test_llm_grammar.py` 的 `test_grammar_has_per_tool_call_rule_for_each_tool`，
把 `expected_rule_ids` dict 內這四行：

```python
        "WriteScratchpad": "write-scratchpad-call",
        "AppendScratchpad": "append-scratchpad-call",
        "EditScratchpad": "edit-scratchpad-call",
        "ClearScratchpad": "clear-scratchpad-call",
```

換成：

```python
        "Scratchpad": "scratchpad-call",
```

- [ ] **Step 5: 跑測試確認通過 + 全套迴歸**

Run: `uv run pytest tests/test_tools.py tests/test_llm_grammar.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/dollos/tools.py tests/test_tools.py tests/test_llm_grammar.py
git commit -m "refactor(tools): consolidate scratchpad into one Scratchpad(op) tool; drop fragile EditScratchpad"
```

---

## Task 7: 全套迴歸 + 整合驗證

**Files:** 無（驗證 only）

- [ ] **Step 1: 跑全套測試**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest -q`
Expected: 全綠（626+ 既有 + 本計劃新增）。

- [ ] **Step 2: grep 確認死碼清除**

Run: `grep -rn "ToolCtx\|WriteScratchpad\|AppendScratchpad\|EditScratchpad\|ClearScratchpad" src`
Expected: 無結果。

- [ ] **Step 3: 確認 grammar 對完整 MAIN_TOOLS / SUB_TOOLS 可 build（含新 Scratchpad enum + 解鎖的 optional）**

Run:
```bash
uv run python -c "
from dollos.tools import MAIN_TOOLS, SUB_TOOLS
from dollos.llm.templates import build_voice_first_grammar, build_qwen3_think_tool_grammar
v = build_voice_first_grammar(MAIN_TOOLS)
b = build_qwen3_think_tool_grammar(SUB_TOOLS)
assert 'scratchpad-call ::=' in v
assert r'( \", \\\"timeout_s\\\": \" integer )?' in v
print('voice+b4 grammar build OK; optional suffix present')
"
```
Expected: 印出 OK，無例外。

- [ ] **Step 4: 最終 commit（若有殘留）**

```bash
git status
# 若有未提交的零星修正：
git add -A && git commit -m "chore: tool-system-polish regression fixes"
```

---

## Self-Review（plan 對 spec 覆蓋檢查）

- **§3.1 optional 可達** → Task 2 ✅
- **§3.2 整數對齊** → Task 1 ✅
- **§3.3 移除沉默懸崖** → Task 3 ✅
- **§3.4 友善錯誤** → Task 4（helper）+ Task 5（接線）✅
- **§3.5 工具去重** → Task 6 ✅
- **§3.6 dispatch 合一 + 刪 ToolCtx** → Task 5 ✅
- **§6 測試** → 各 Task 的 TDD 步驟 + Task 7 迴歸 ✅
- 型別/命名一致性：`dispatch_one` 簽章在 Task 5 定義、被 `dispatch_tool_call` 與
  `_dispatch_tool` 一致呼叫；`Scratchpad(op, content)` 在 Task 6 定義並於
  grammar dict / registry 一致引用 ✅
