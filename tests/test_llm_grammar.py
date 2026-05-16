"""Tests for build_qwen3_think_tool_grammar (B4-typed GBNF generator)
and build_mind_actions_grammar (MindLoop JSON-array grammar).
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, Field

from dollos.llm.templates import build_mind_actions_grammar, build_qwen3_think_tool_grammar
from dollos.tools import MAIN_TOOLS as TOOLS

# Action names used by the MindLoop experiment (actions.py _VALID_KINDS).
_MIND_ACTIONS = [
    "Say", "Think", "SetFocus", "OpenLoop", "CloseLoop",
    "Dispatch",
]


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


# ---------------------------------------------------------------------------
# build_mind_actions_grammar tests
# ---------------------------------------------------------------------------


def test_mind_actions_grammar_starts_with_root_rule():
    """MindLoop uses prefill to close think block; grammar covers JSON array only."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert g.startswith("root ::= mind-actions\n")


def test_mind_actions_grammar_no_think_block_rule():
    """No think-block in mind actions grammar — prefill closes the think block."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "think-block" not in g
    assert "<think>" not in g


def test_mind_actions_grammar_no_seen_intent_mood_in_grammar():
    """SEEN/INTENT/MOOD not in grammar — MindLoop uses prefill, not GBNF think enforcement."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "SEEN:" not in g
    assert "INTENT:" not in g
    assert "TOOL:" not in g


def test_mind_actions_grammar_has_mind_actions_rule():
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "mind-actions ::= " in g
    # Must allow empty array
    assert '"[" ws "]"' in g
    # Must allow non-empty array
    assert "action-call" in g


def test_mind_actions_grammar_action_name_enum_includes_all_actions():
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    for name in _MIND_ACTIONS:
        assert f'"{name}"' in g


def test_mind_actions_grammar_action_call_has_action_key():
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert '"\\\"action\\\""' in g or '\\"action\\"' in g


def test_mind_actions_grammar_has_recursive_json_value_rule():
    """json-value must be present to allow arbitrary action payloads."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "json-value ::= " in g
    assert "json-object ::= " in g
    assert "json-array ::= " in g
    assert "json-number ::= " in g


def test_mind_actions_grammar_includes_json_str_rules():
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "str ::= " in g
    assert "str-char ::= " in g
    assert "hex ::= [0-9a-fA-F]" in g


def test_mind_actions_grammar_str_char_denies_cjk_and_typographic_quotes():
    """CJK corner-bracket and typographic quotes must be denied in str-char."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    str_char_line = next(
        line for line in g.splitlines() if line.startswith("str-char ::=")
    )
    for code in ("\\u201C", "\\u201D", "\\u2018", "\\u2019",
                 "\\u300C", "\\u300D", "\\u300E", "\\u300F"):
        assert code in str_char_line, f"{code} missing from str-char deny list"


def test_mind_actions_grammar_empty_names_raises():
    with pytest.raises(ValueError):
        build_mind_actions_grammar([])


def test_mind_actions_grammar_name_with_quote_raises():
    with pytest.raises(ValueError):
        build_mind_actions_grammar(['Bad"Name'])


def test_mind_actions_grammar_ws_rule_present():
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "ws ::= " in g


def test_mind_actions_grammar_single_action_subset():
    """Grammar builds correctly for a minimal one-action list."""
    g = build_mind_actions_grammar(["Think"])
    assert '"Think"' in g
    assert "root ::= mind-actions\n" in g


def test_mind_actions_grammar_all_main_tools_names():
    """Grammar can be built from all MAIN_TOOLS class names (no crash)."""
    names = [cls.__name__ for cls in TOOLS]
    g = build_mind_actions_grammar(names)
    for name in names:
        assert f'"{name}"' in g


def test_mind_actions_grammar_no_line_rule():
    """No line rule needed — grammar covers JSON only, no think body."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert 'line ::= ' not in g


def test_mind_actions_grammar_format_incompatible_with_tool_call_envelope():
    """build_mind_actions_grammar output must NOT contain <tool_call> tags.

    The MindLoop parser expects a plain JSON array, not tool_call XML blocks.
    If <tool_call> appears, the formats are mixed — which would break _parse_actions.
    """
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    assert "<tool_call>" not in g


def test_mind_actions_grammar_action_call_rule_structure():
    """action-call rule must enforce the 'action' key before the name enum."""
    g = build_mind_actions_grammar(_MIND_ACTIONS)
    # Find the action-call rule line
    action_call_line = next(
        (line for line in g.splitlines() if line.startswith("action-call ::=")),
        None,
    )
    assert action_call_line is not None, "action-call rule not found"
    # Must reference action-name rule (not inline literal list)
    assert "action-name" in action_call_line
