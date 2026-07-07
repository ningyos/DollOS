"""P1 Task 4: DaemonLink talk() ⇆ IPC mapping (spec F1/F6), fake daemon WS.
No real MCP client, no real daemon — pure IPC-mapping unit tests."""
from __future__ import annotations

import asyncio
import json

import pytest

from dollos.mcp_server.daemon_link import DaemonLink


class _FakeWS:
    """Records every frame the connector sends to the daemon."""
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


def _sent_of_type(ws: _FakeWS, t: str) -> list[dict]:
    return [d for d in (json.loads(s) for s in ws.sent) if d.get("type") == t]


@pytest.mark.asyncio
async def test_talk_registers_and_sends_channel_event():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)

    task = asyncio.create_task(link.talk("conn1", "Claude", "hello Doll"))
    await asyncio.sleep(0.02)  # let register + event flush

    regs = _sent_of_type(ws, "channel_register")
    evts = _sent_of_type(ws, "channel_event")
    assert len(regs) == 1 and len(evts) == 1
    cid = regs[0]["channel_id"]
    assert cid.startswith("mcp:conn1:")
    assert regs[0]["locus"] == "external"
    assert regs[0]["kind"] == "mcp"
    assert evts[0]["channel_id"] == cid
    pl = evts[0]["payload"]
    assert pl["author_id"] == "mcp:Claude"
    assert pl["author"] == "Claude"
    assert pl["is_dm"] is True
    assert pl["author_is_owner"] is False
    assert pl["content"] == "hello Doll"
    assert pl["channel_kind"] == "mcp"

    # complete the turn so the task doesn't dangle
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid, "text": "hi"}))
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    result = await task
    assert result == {"status": "reply", "text": "hi"}


@pytest.mark.asyncio
async def test_talk_joins_multiple_addressed_text():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.talk("conn1", "Claude", "hi"))
    await asyncio.sleep(0.02)
    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid, "text": "第一句。"}))
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid, "text": "第二句。"}))
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    result = await task
    assert result == {"status": "reply", "text": "第一句。第二句。"}


@pytest.mark.asyncio
async def test_zero_sentences_is_no_response():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.talk("conn1", "Claude", "hi"))
    await asyncio.sleep(0.02)
    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    # turn-end with NO addressed_text → she read it and chose not to reply
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    result = await task
    assert result == {"status": "no_response", "text": ""}


@pytest.mark.asyncio
async def test_timeout_when_no_turn_end():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    result = await link.talk("conn1", "Claude", "hi", timeout_s=0.05)
    assert result["status"] == "timeout"
    assert result["text"] == ""


@pytest.mark.asyncio
async def test_timeout_returns_partial_text():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.talk("conn1", "Claude", "hi", timeout_s=0.1))
    await asyncio.sleep(0.02)
    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid, "text": "半句"}))
    # never send turn_end_addressed → timeout with the partial collected
    result = await task
    assert result["status"] == "timeout"
    assert result["text"] == "半句"


@pytest.mark.asyncio
async def test_parallel_talks_do_not_cross_deliver():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    t1 = asyncio.create_task(link.talk("conn1", "Claude", "one"))
    t2 = asyncio.create_task(link.talk("conn2", "Claude", "two"))
    await asyncio.sleep(0.02)
    regs = _sent_of_type(ws, "channel_register")
    assert len(regs) == 2
    cid1, cid2 = regs[0]["channel_id"], regs[1]["channel_id"]
    assert cid1 != cid2
    # reply to each on its own channel
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid1, "text": "R1"}))
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid2, "text": "R2"}))
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid1}))
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid2}))
    r1, r2 = await t1, await t2
    # match the reply back to the message via send order
    by_cid = {regs[0]["channel_id"]: "one", regs[1]["channel_id"]: "two"}
    # whichever channel carried "one" got R1, the other got R2 — no crosstalk
    assert r1["text"] == "R1" and r2["text"] == "R2"
    assert by_cid  # both channels distinct + resolved


@pytest.mark.asyncio
async def test_ignores_bare_textchunk_and_global_turn_end():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.talk("conn1", "Claude", "hi"))
    await asyncio.sleep(0.02)
    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    # origin-less internal outputs must NOT pollute this talk's result (§B.6)
    link.dispatch(json.dumps({"type": "text_chunk", "text": "内部輸出"}))
    link.dispatch(json.dumps({"type": "turn_end"}))
    link.dispatch(json.dumps({"type": "addressed_text", "channel_id": cid, "text": "真reply"}))
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    result = await task
    assert result == {"status": "reply", "text": "真reply"}
