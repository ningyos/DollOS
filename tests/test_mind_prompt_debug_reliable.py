"""P2 Task 3: debug reliability nudge (prompt-level, tier unchanged).

When a debug connection's `talk` stamps `debug_reliable=True` on a
ChannelMessage perception's payload (Task 2), `_percep_body`'s mcp AI-peer
branch should append a soft nudge line encouraging Doll to give a
substantive reply — best-effort only (spec §C.2), NOT a capability change.
The non-debug path must render byte-identically to P1, and `origin_tier`
must stay `external_public` regardless of the nudge (mirrors the P1 F2
tier test in tests/test_mind_loop_turn_end_addressed.py).
"""
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_prompt import _percep_body
from dollos.mind.mind_state import Perception


def _mcp_payload(**overrides):
    data = {
        "channel_id": "mcp:c1:call1", "author": "Claude",
        "author_is_owner": False, "is_dm": True, "channel_kind": "mcp",
        "content": "hey Doll",
    }
    data.update(overrides)
    return data


def test_debug_reliable_adds_nudge_and_keeps_ai_peer_framing():
    p = Perception(kind="ChannelMessage", t=1.0, data=_mcp_payload(debug_reliable=True))
    out = _percep_body(p)
    # nudge present
    assert "除錯" in out
    assert "務必" in out
    # P1 mcp AI-peer framing still present
    assert "AI peer" in out
    assert "Claude" in out
    assert "hey Doll" in out
    assert "陌生人" not in out


def test_no_debug_reliable_no_nudge_byte_unchanged_from_p1():
    p = Perception(kind="ChannelMessage", t=1.0, data=_mcp_payload())
    out = _percep_body(p)
    assert "除錯" not in out
    assert "務必" not in out
    assert out == "[AI peer 私訊] Claude(另一個 AI,自稱,未驗證):hey Doll"


def test_debug_reliable_does_not_change_origin_tier():
    p = Perception(
        kind="ChannelMessage", t=1.0,
        data=_mcp_payload(author_is_owner=False, debug_reliable=True),
    )
    assert MindLoop._derive_origin_tier([p]) == "external_public"
