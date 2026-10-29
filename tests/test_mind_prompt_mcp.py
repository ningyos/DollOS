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
    assert out == "[AI peer, DM] Claude (another AI, self-described, unverified): hey Doll"
    assert "stranger" not in out          # must NOT fall through to the stranger branch
