"""P1 Task 3(b): _percep_body renders an mcp ChannelMessage as an
AI peer (self-declared, unverified), not a human stranger."""
from dollos.mind.mind_prompt import _percep_body
from dollos.mind.mind_state import Perception


def test_mcp_channel_message_renders_as_ai_peer():
    p = Perception(
        kind="ChannelMessage", t=1.0,
        data={"channel_id": "mcp:c1:call1", "author": "Claude",
              "author_is_owner": False, "is_dm": True, "channel_kind": "mcp",
              "content": "hey Doll"},
    )
    out = _percep_body(p)
    assert "AI peer" in out
    assert "Claude" in out
    assert "hey Doll" in out
    assert "陌生人" not in out          # must NOT fall through to the stranger branch


def test_non_mcp_channel_message_still_stranger():
    p = Perception(
        kind="ChannelMessage", t=1.0,
        data={"channel_id": "disc:1", "author": "Bob",
              "author_is_owner": False, "is_dm": True, "content": "yo"},
    )
    out = _percep_body(p)
    assert "陌生人" in out
    assert "AI peer" not in out
