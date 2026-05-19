import pytest

from dollos.stream_events import SpeakChunk, ToolCallReady
from dollos.tool_parser import ToolStreamParser


def test_voice_mode_emits_speak_chunks():
    p = ToolStreamParser(voice_mode=True)
    events = []
    # voice_mode starts in IN_THINK; consume the think-block close first.
    events.extend(p.feed("SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: q\nTOOL: speak\n</think>\n\n"))
    events.extend(p.feed("Hello "))
    events.extend(p.feed('there<tool_call>\n{"name": "NoteMemory", "arguments": {"text": "ok"}}\n</tool_call>'))
    events.extend(p.feed(" bye"))
    events.extend(p.flush())

    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    tools = [e for e in events if isinstance(e, ToolCallReady)]
    assert "".join(s.text for s in speaks) == "Hello there bye"
    assert len(tools) == 1
    assert tools[0].name == "NoteMemory"
    assert tools[0].arguments == {"text": "ok"}


def test_voice_mode_split_open_marker_no_leak():
    """A tool_call open marker split across feed() boundaries must not leak."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("TOOL: speak\n</think>\n\n"))
    events.extend(p.feed("text <tool"))
    events.extend(p.feed('_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}\n</tool_call>'))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    # Outside should be exactly "text " — the "<tool" was retained as lookahead, not speak
    assert "".join(s.text for s in speaks) == "text "


def test_voice_mode_split_close_marker_no_leak():
    """Close marker split across feed() boundaries must finish the tool_call."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("TOOL: speak\n</think>\n\n"))
    events.extend(p.feed('<tool_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}\n</tool_'))
    events.extend(p.feed("call>after"))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    tools = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(tools) == 1
    assert "".join(s.text for s in speaks) == "after"


def test_voice_mode_invalid_json_in_tool_dropped():
    """Malformed tool_call JSON drops the block + emits warning (no crash)."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("TOOL: speak\n</think>\n\n"))
    events.extend(p.feed("<tool_call>\nnot json\n</tool_call>"))
    events.extend(p.flush())
    assert [e for e in events if isinstance(e, ToolCallReady)] == []


def test_voice_mode_unclosed_tool_at_flush_drops():
    p = ToolStreamParser(voice_mode=True)
    p.feed("TOOL: speak\n</think>\n\n")
    p.feed('<tool_call>\n{"name":"NoteMemory","arguments":{"text":"hi"}}')
    events = p.flush()
    assert [e for e in events if isinstance(e, ToolCallReady)] == []


def test_legacy_default_mode_unchanged():
    """voice_mode=False keeps legacy drop-outside-text policy and dict output."""
    p = ToolStreamParser()
    calls = []
    calls.extend(p.feed("ignored "))
    calls.extend(p.feed('<tool_call>\n{"name":"X","arguments":{}}\n</tool_call>'))
    calls.extend(p.flush())
    assert isinstance(calls[0], dict)
    assert calls[0]["name"] == "X"


def test_voice_mode_strips_think_block_before_speak():
    """Think-block content (SEEN: ..., TOOL: ...) is suppressed; only post-</think> text emits."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("SEEN: hi\nINTENT: greet\nREVIEW: first\n"))
    events.extend(p.feed("MOOD: warm\nTOOL: speak\n</think>\n\n"))
    events.extend(p.feed("你好呀, 主人。"))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    assert "".join(s.text for s in speaks) == "你好呀, 主人。"


def test_voice_mode_think_close_split_across_feeds():
    """</think> split across feed() boundaries should still transition cleanly."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: q\nTOOL: speak\n</thi"))
    events.extend(p.feed("nk>\n\nhello"))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    assert "".join(s.text for s in speaks) == "hello"


def test_voice_mode_unclosed_think_at_flush_drops():
    """If stream ends with no </think>, drop the think buf silently (no leak)."""
    p = ToolStreamParser(voice_mode=True)
    p.feed("SEEN: oops\nINTENT: leak\n")
    events = p.flush()
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    assert speaks == []


def test_voice_mode_tool_call_after_think():
    """Tool call after </think> is still captured."""
    p = ToolStreamParser(voice_mode=True)
    events = []
    events.extend(p.feed("SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: q\nTOOL: noteMemory\n</think>\n\n"))
    events.extend(p.feed("好的<tool_call>\n{\"name\":\"NoteMemory\",\"arguments\":{\"text\":\"ok\"}}\n</tool_call>"))
    events.extend(p.flush())
    speaks = [e for e in events if isinstance(e, SpeakChunk)]
    tools = [e for e in events if isinstance(e, ToolCallReady)]
    assert "".join(s.text for s in speaks) == "好的"
    assert len(tools) == 1


def test_legacy_mode_unchanged_no_think_suppression():
    """Legacy (voice_mode=False) starts in OUTSIDE; think content treated as regular drop-text."""
    p = ToolStreamParser()  # voice_mode default False
    calls = []
    calls.extend(p.feed("SEEN: x\nINTENT: y\n</think>\n\n"))
    calls.extend(p.feed("<tool_call>\n{\"name\":\"X\",\"arguments\":{}}\n</tool_call>"))
    calls.extend(p.flush())
    # Legacy mode returns dicts and ignores all outside text — think block
    # included. The class doesn't know IN_THINK in legacy mode.
    assert len(calls) == 1
    assert calls[0]["name"] == "X"
