"""Tests for ToolStreamParser."""

import logging

from dollos.tool_parser import ToolStreamParser


def test_single_tool_call_in_one_chunk():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>\n{"name": "Say", "arguments": {"text": "hi"}}\n</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "hi"}}]


def test_two_consecutive_tool_calls():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>{"name":"A","arguments":{}}</tool_call>'
        '<tool_call>{"name":"B","arguments":{}}</tool_call>'
    )
    assert out == [
        {"name": "A", "arguments": {}},
        {"name": "B", "arguments": {}},
    ]


def test_tool_call_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed('<tool_call>{"name":"Say",')
    b = p.feed('"arguments":{"text":"x"}}</tool_call>')
    assert a == []
    assert b == [{"name": "Say", "arguments": {"text": "x"}}]


def test_naked_text_outside_tool_call_is_dropped(caplog):
    p = ToolStreamParser()
    with caplog.at_level(logging.DEBUG, logger="dollos.tool_parser"):
        out = p.feed("some thinking\n")
    assert out == []


def test_think_content_is_dropped_just_like_naked_text():
    p = ToolStreamParser()
    out = p.feed("<think>internal reasoning</think>\n")
    assert out == []


def test_naked_text_then_tool_call_extracts_only_tool():
    p = ToolStreamParser()
    out = p.feed(
        'pre-think reasoning\n'
        '<tool_call>{"name":"Say","arguments":{"text":"hi"}}</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "hi"}}]


def test_malformed_json_in_tool_call_logs_warning_and_continues(caplog):
    p = ToolStreamParser()
    with caplog.at_level(logging.WARNING, logger="dollos.tool_parser"):
        out = p.feed(
            '<tool_call>{not json}</tool_call>'
            '<tool_call>{"name":"Say","arguments":{"text":"after"}}</tool_call>'
        )
    assert out == [{"name": "Say", "arguments": {"text": "after"}}]
    assert any("malformed" in r.message.lower() or "json" in r.message.lower()
               for r in caplog.records)


def test_unclosed_tool_call_logs_on_flush(caplog):
    p = ToolStreamParser()
    out = p.feed('<tool_call>{"name":"Say"')
    assert out == []
    with caplog.at_level(logging.WARNING, logger="dollos.tool_parser"):
        rest = p.flush()
    assert rest == []
    assert any("unclosed" in r.message.lower() or "unfinished" in r.message.lower()
               for r in caplog.records)


def test_unicode_in_tool_call():
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>{"name":"Say","arguments":{"text":"你好"}}</tool_call>'
    )
    assert out == [{"name": "Say", "arguments": {"text": "你好"}}]


def test_open_marker_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed("<tool_")
    b = p.feed('call>{"name":"X","arguments":{}}</tool_call>')
    assert a == []
    assert b == [{"name": "X", "arguments": {}}]


def test_close_marker_split_across_chunks():
    p = ToolStreamParser()
    a = p.feed('<tool_call>{"name":"X","arguments":{}}</tool_')
    b = p.feed("call>")
    assert a == []
    assert b == [{"name": "X", "arguments": {}}]


def test_flush_on_clean_state_returns_empty():
    p = ToolStreamParser()
    p.feed('<tool_call>{"name":"X","arguments":{}}</tool_call>')
    assert p.flush() == []


def test_non_dict_json_payload_logs_warning_and_skips(caplog):
    p = ToolStreamParser()
    with caplog.at_level(logging.WARNING, logger="dollos.tool_parser"):
        out = p.feed(
            '<tool_call>[1, 2, 3]</tool_call>'
            '<tool_call>{"name":"Say","arguments":{"text":"after"}}</tool_call>'
        )
    assert out == [{"name": "Say", "arguments": {"text": "after"}}]
    assert any("not a JSON object" in r.message or "object" in r.message.lower()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Optional-field round-trip tests (Spec §6 / MF-1)
# ---------------------------------------------------------------------------


def test_optional_field_present_roundtrips_to_pydantic():
    from dollos.tools import Shell
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>\n{"name":"Shell","arguments":{"command":"ls","timeout_s":120}}\n</tool_call>'
    )
    assert out == [{"name": "Shell", "arguments": {"command": "ls", "timeout_s": 120}}]
    tool = Shell.model_validate(out[0]["arguments"])
    assert tool.command == "ls"
    assert tool.timeout_s == 120


def test_optional_field_absent_roundtrips_to_default():
    from dollos.tools import Shell
    p = ToolStreamParser()
    out = p.feed('<tool_call>\n{"name":"Shell","arguments":{"command":"ls"}}\n</tool_call>')
    tool = Shell.model_validate(out[0]["arguments"])
    assert tool.command == "ls"
    assert tool.timeout_s == 60  # pydantic default when grammar omits the optional


def test_optional_anyof_present_roundtrips():
    from dollos.tools import SpawnMonitor
    p = ToolStreamParser()
    out = p.feed(
        '<tool_call>\n{"name":"SpawnMonitor","arguments":{"command":"tail -F x","match_regex":"ERROR"}}\n</tool_call>'
    )
    tool = SpawnMonitor.model_validate(out[0]["arguments"])
    assert tool.command == "tail -F x"
    assert tool.match_regex == "ERROR"


def test_optional_anyof_absent_roundtrips_to_none():
    from dollos.tools import SpawnMonitor
    p = ToolStreamParser()
    out = p.feed('<tool_call>\n{"name":"SpawnMonitor","arguments":{"command":"tail -F x"}}\n</tool_call>')
    tool = SpawnMonitor.model_validate(out[0]["arguments"])
    assert tool.match_regex is None
