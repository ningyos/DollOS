"""Tests for build_qwen3_think_tool_grammar (B4-typed GBNF generator)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.tools import MAIN_TOOLS as TOOLS


def test_grammar_starts_with_root_rule():
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert g.startswith("root ::= think tool-call\n")


def test_grammar_has_think_skeleton():
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert (
        'think ::= "SEEN: " line "INTENT: " line "REVIEW: " line "MOOD: " line "TOOL: " tool-name'
        in g
    )
    assert 'line ::= [^\\n]+ "\\n"' in g


def test_grammar_think_has_review_field():
    """REVIEW field sits between INTENT and MOOD, reusing the `line` rule.
    Gives the model syntactic space to self-reflect on cascade progress
    instead of looping identical SEEN/INTENT/TOOL triples."""
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert '"REVIEW: " line ' in g


def test_grammar_think_has_mood_field():
    """MOOD field sits between REVIEW and TOOL. Big model writes its mood
    snapshot here; dispatcher parses MOOD: line from last assistant message."""
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert '"MOOD: " line' in g


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
        "WriteSchedule": "write-schedule-call",
        "Shell": "shell-call",
        "InvokeSkill": "invoke-skill-call",
        "Recall": "recall-call",
        "SpawnSubagent": "spawn-subagent-call",
        "SpawnMonitor": "spawn-monitor-call",
        "RemoveMonitor": "remove-monitor-call",
        "ReadToolOutput": "read-tool-output-call",
        "GrepToolOutput": "grep-tool-output-call",
        "WriteScratchpad": "write-scratchpad-call",
        "AppendScratchpad": "append-scratchpad-call",
        "EditScratchpad": "edit-scratchpad-call",
        "ClearScratchpad": "clear-scratchpad-call",
        "SetFocus": "set-focus-call",
        "OpenLoop": "open-loop-call",
        "CloseLoop": "close-loop-call",
        "Idle": "idle-call",
        "Sleep": "sleep-call",
        "MoodTool": "mood-tool-call",
        "Think": "think-call",
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

    Shell.timeout_s is required → MUST appear in grammar.
    """
    g = build_qwen3_think_tool_grammar(TOOLS)
    # Required string fields per tool (current TOOLS definitions):
    assert r'\"text\":' in g  # Say + NoteMemory both use "text"
    assert r'\"content\":' in g  # WriteDiary
    assert r'\"command\":' in g  # Shell
    assert r'\"name\":' in g  # InvokeSkill (and the envelope "name")
    # Shell.timeout_s is a required integer field — it must appear in
    # the shell-call rule body.
    shell_rule = next(
        line for line in g.splitlines() if line.startswith("shell-call ::=")
    )
    assert "timeout_s" in shell_rule


def test_grammar_includes_recall_tool():
    """Recall tool appears in the auto-generated tool-name enum and gets
    its own per-tool call rule (proves TOOLS auto-threads new tools through
    grammar build)."""
    g = build_qwen3_think_tool_grammar(TOOLS)
    assert '"Recall"' in g
    assert "recall-call ::= " in g


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


class _BoolField(BaseModel):
    """Tool whose required field type is unsupported by the grammar builder
    (boolean / array / object are all unsupported as of 2026-05-09)."""

    flag: bool = Field(description="a flag")


def test_unsupported_required_field_type_raises():
    with pytest.raises(NotImplementedError):
        build_qwen3_think_tool_grammar([_BoolField])


class _IntField(BaseModel):
    """Tool with a required integer field — supported (used by SpawnSubagent)."""

    n: int = Field(description="a number")


def test_integer_required_field_emits_integer_rule():
    g = build_qwen3_think_tool_grammar([_IntField])
    assert r'\"n\": " integer "' in g
    assert "integer ::= " in g


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
