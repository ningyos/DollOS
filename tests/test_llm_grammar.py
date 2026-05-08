"""Tests for build_qwen3_think_tool_grammar (B4-typed GBNF generator)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.tools import TOOLS


def test_grammar_starts_with_root_rule():
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert g.startswith("root ::= think tool-call\n")


def test_grammar_has_think_skeleton():
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert 'think ::= "SEEN: " line "INTENT: " line "TOOL: " tool-name' in g
    assert 'line ::= [^\\n]+ "\\n"' in g


def test_grammar_tool_name_enum_includes_all_tools():
    g = build_qwen3_think_tool_grammar(TOOLS)
    expected_alts = ' | '.join(f'"{cls.__name__}"' for cls in TOOLS)
    assert f"tool-name ::= {expected_alts}\n" in g


def test_grammar_has_per_tool_call_rule_for_each_tool():
    g = build_qwen3_think_tool_grammar(TOOLS)
    # Per-tool rule ids: ToolName -> tool-name-call
    expected_rule_ids = {
        "Say": "say-call",
        "NoteMemory": "note-memory-call",
        "WriteDiary": "write-diary-call",
        "Shell": "shell-call",
        "InvokeSkill": "invoke-skill-call",
    }
    for cls in TOOLS:
        rid = expected_rule_ids[cls.__name__]
        assert f"{rid} ::= " in g, f"missing rule for {cls.__name__}"
    # tool-call alternation lists all of them
    assert "tool-call ::= " + " | ".join(
        expected_rule_ids[cls.__name__] for cls in TOOLS
    ) + "\n" in g


def test_grammar_per_tool_field_names_match_required_strings():
    """Each tool's call rule must reference its required-string field names.

    Shell.timeout_s is optional → should NOT appear in grammar.
    """
    g = build_qwen3_think_tool_grammar(TOOLS)
    # Required string fields per tool (current TOOLS definitions):
    assert r'\"text\":' in g  # Say + NoteMemory both use "text"
    assert r'\"content\":' in g  # WriteDiary
    assert r'\"command\":' in g  # Shell
    assert r'\"name\":' in g  # InvokeSkill (and the envelope "name")
    # Shell's optional timeout_s must not appear in the grammar body.
    assert "timeout_s" not in g


def test_grammar_includes_json_str_rules():
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert "str ::= " in g
    assert "str-char ::= " in g
    assert "hex ::= [0-9a-fA-F]" in g


def test_grammar_str_char_denies_cjk_and_typographic_quotes():
    """Byte-level [^\"\\\\] lets the model close JSON strings with CJK
    quote-likes (observed 2026-05-08 T8: 」}}). str-char must deny those
    codepoints explicitly via \\uXXXX escapes."""
    g = build_qwen3_think_tool_grammar(TOOLS)
    # Locate the str-char rule line.
    str_char_line = next(
        line for line in g.splitlines() if line.startswith("str-char ::=")
    )
    for code in ("\\u201C", "\\u201D", "\\u2018", "\\u2019",
                 "\\u300C", "\\u300D", "\\u300E", "\\u300F"):
        assert code in str_char_line, f"{code} missing from str-char deny list"


def test_grammar_each_call_rule_has_tool_call_envelope():
    g = build_qwen3_think_tool_grammar(TOOLS)
    for cls in TOOLS:
        # Look for the prefix that opens this tool's <tool_call> envelope.
        marker = f'\\"name\\": \\"{cls.__name__}\\", \\"arguments\\":'
        assert marker in g, f"envelope for {cls.__name__} not found"


def test_empty_tools_raises():
    with pytest.raises(ValueError):
        build_qwen3_think_tool_grammar([])


class _IntField(BaseModel):
    """Tool whose required field is non-string."""

    n: int = Field(description="a number")


def test_non_string_required_field_raises():
    with pytest.raises(NotImplementedError):
        build_qwen3_think_tool_grammar([_IntField])


class _GoodTool(BaseModel):
    """Tool with two required strings — should comma-join."""

    a: str = Field(description="first")
    b: str = Field(description="second")


def test_multi_required_strings_comma_joined():
    g = build_qwen3_think_tool_grammar([_GoodTool])
    # Body should contain: \"a\": " str ", \"b\": " str "
    assert r'\"a\": " str ", \"b\": " str "' in g


class _OptionalOnlyTool(BaseModel):
    """Tool with only optional fields — body should be empty {}."""

    x: str = Field(default="hi", description="opt")


def test_optional_only_tool_has_empty_args_body():
    g = build_qwen3_think_tool_grammar([_OptionalOnlyTool])
    # Empty arguments block: "arguments": {}
    assert r'\"arguments\": {}}\n</tool_call>"' in g
