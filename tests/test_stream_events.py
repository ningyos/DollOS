def test_stream_events_construct():
    from dollos.stream_events import SpeakChunk, ToolCallReady, StreamDone
    a = SpeakChunk(text="hello")
    b = ToolCallReady(name="NoteMemory", arguments={"text": "x"})
    c = StreamDone()
    assert a.text == "hello"
    assert b.name == "NoteMemory"
    assert b.arguments == {"text": "x"}
    assert isinstance(c, StreamDone)


def test_stream_events_are_frozen():
    """Events are immutable to keep the type clean."""
    import dataclasses
    from dollos.stream_events import SpeakChunk
    a = SpeakChunk(text="x")
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.text = "y"
