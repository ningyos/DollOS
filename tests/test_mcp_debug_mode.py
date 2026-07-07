"""P2 Task 2: mcp connector debug mode — secret gate + query round-trip +
debug_reliable (spec §C.1/§C.3).

SECURITY-BEARING: with FastMCP (mcp 1.28.1) exposing a single GLOBAL tool
set, the per-session ``_authed`` check inside get_state/get_recent's
bodies IS the access control for MCP clients — these tests prove it
actually blocks an unauthed session, not just that the happy path works.
Mirrors tests/test_mcp_daemon_link.py's fake-WS record/inject pattern for
the DaemonLink.query() half; the secret-gate half is tested at the plain
`_try_authenticate`/`_require_debug` function seam (no real MCP client
needed — see __main__.py's module docstring).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from dollos.ipc.messages import QueryRecent, QueryState
from dollos.mcp_server.__main__ import _authed, _require_debug, _try_authenticate
from dollos.mcp_server.daemon_link import DaemonLink


class _FakeWS:
    """Records every frame the connector sends to the daemon."""
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


def _sent_of_type(ws: _FakeWS, t: str) -> list[dict]:
    return [d for d in (json.loads(s) for s in ws.sent) if d.get("type") == t]


class _FakeSession:
    """Stand-in for ctx.session — an ordinary (non-slotted) object, so it
    supports weak references just like a real mcp ServerSession. Only
    identity/hashability matter for the ``_authed`` membership checks.
    """


class _FakeLink:
    """Stand-in for DaemonLink — records every query() call and returns a
    canned payload, so tests can assert whether the daemon was ever hit.
    """
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.query_calls: list = []

    async def query(self, msg):
        self.query_calls.append(msg)
        return self._payload


# ===== 1. query round-trip =====

@pytest.mark.asyncio
async def test_query_round_trip_returns_payload():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)

    msg = QueryState(query_id="q1", token="tok")
    task = asyncio.create_task(link.query(msg))
    await asyncio.sleep(0.02)

    sent = _sent_of_type(ws, "query_state")
    assert len(sent) == 1
    assert sent[0]["query_id"] == "q1"
    assert sent[0]["token"] == "tok"

    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "q1", "ok": True,
        "payload": {"mood": "calm", "current_self": "..."},
    }))
    result = await task
    assert result == {"mood": "calm", "current_self": "..."}


@pytest.mark.asyncio
async def test_query_recent_sends_n():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)

    msg = QueryRecent(query_id="q2", token="tok", n=5)
    task = asyncio.create_task(link.query(msg))
    await asyncio.sleep(0.02)

    sent = _sent_of_type(ws, "query_recent")
    assert sent[0]["query_id"] == "q2" and sent[0]["n"] == 5

    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "q2", "ok": True,
        "payload": {"items": []},
    }))
    result = await task
    assert result == {"items": []}


# ===== 2. two concurrent queries — query_id demux, no cross-delivery =====

@pytest.mark.asyncio
async def test_concurrent_queries_do_not_cross_deliver():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)

    t1 = asyncio.create_task(link.query(QueryState(query_id="qA", token="tok")))
    t2 = asyncio.create_task(link.query(QueryRecent(query_id="qB", token="tok", n=5)))
    await asyncio.sleep(0.02)

    # reply to B first, then A — proves correlation is by query_id, not
    # send/receive order.
    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "qB", "ok": True, "payload": {"items": ["B"]},
    }))
    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "qA", "ok": True, "payload": {"mood": "A"},
    }))
    r1, r2 = await t1, await t2
    assert r1 == {"mood": "A"}
    assert r2 == {"items": ["B"]}


@pytest.mark.asyncio
async def test_query_result_for_unknown_query_id_is_ignored():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.query(QueryState(query_id="q1", token="tok")))
    await asyncio.sleep(0.02)
    # a stray/late result for someone else's query_id must not satisfy this one
    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "not-mine", "ok": True, "payload": {"x": 1},
    }))
    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "q1", "ok": True, "payload": {"mood": "ok"},
    }))
    result = await task
    assert result == {"mood": "ok"}


# ===== 3. ok=False surfaces as an error — not a hang, not fabricated data =====

@pytest.mark.asyncio
async def test_ok_false_raises_not_hangs():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.query(QueryState(query_id="qX", token="wrong")))
    await asyncio.sleep(0.02)
    link.dispatch(json.dumps({
        "type": "query_result", "query_id": "qX", "ok": False, "payload": {},
    }))
    with pytest.raises(RuntimeError):
        await task


@pytest.mark.asyncio
async def test_query_timeout_raises():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    with pytest.raises(RuntimeError):
        await link.query(QueryState(query_id="qY", token="tok"), timeout_s=0.05)


# ===== 4. secret gate: right secret authenticates, wrong/absent does not =====
#
# NOTE: sessions are real (non-slotted) objects, not bare ints — _authed is
# a weakref.WeakSet keyed on the session object itself (see I1 below), and
# CPython ints cannot be weakly referenced.

def test_wrong_secret_does_not_authenticate():
    _authed.clear()
    session = _FakeSession()
    assert _try_authenticate(session, "wrong", "correct-secret") is False
    assert session not in _authed


def test_empty_debug_secret_never_authenticates_fail_closed():
    _authed.clear()
    # debug mode disabled (mcp.toml debug_secret unset/empty) → NEVER
    # authenticates, even with an empty or arbitrary presented secret.
    session = _FakeSession()
    assert _try_authenticate(session, "", "") is False
    assert _try_authenticate(session, "anything", "") is False
    assert session not in _authed


def test_right_secret_authenticates():
    _authed.clear()
    session = _FakeSession()
    assert _try_authenticate(session, "correct-secret", "correct-secret") is True
    assert session in _authed


def test_require_debug_blocks_unauthed_session():
    _authed.clear()
    session = _FakeSession()
    with pytest.raises(PermissionError):
        _require_debug(session)


def test_require_debug_allows_authed_session():
    _authed.clear()
    session = _FakeSession()
    assert _try_authenticate(session, "s", "s") is True
    _require_debug(session)  # must not raise


# ===== 4b. I1 — auth is keyed on session OBJECT, immune to id() reuse =====

def test_authed_membership_is_by_object_not_shared_state():
    """Two distinct session objects — only the one that actually presented
    the secret is authed. This is the property that closes the id() reuse
    bleed: a stale int key could be satisfied by ANY later object allocated
    at the same address, but object membership in a WeakSet cannot.
    """
    _authed.clear()
    authed_session = _FakeSession()
    other_session = _FakeSession()
    assert _try_authenticate(authed_session, "secret", "secret") is True

    _require_debug(authed_session)  # must not raise
    with pytest.raises(PermissionError):
        _require_debug(other_session)


def test_authed_entry_is_reclaimed_when_session_is_garbage_collected():
    """A WeakSet drops its entry once the session object is GC'd — so a
    brand-new (unauthenticated) session object can never "inherit" a stale
    authed entry, even if CPython later reuses the freed memory address for
    it (the classic id()-reuse bleed this fix closes).
    """
    import gc
    import weakref

    _authed.clear()
    session = _FakeSession()
    assert _try_authenticate(session, "secret", "secret") is True
    assert len(_authed) == 1

    weak = weakref.ref(session)
    del session
    gc.collect()
    assert weak() is None  # actually collected
    assert len(_authed) == 0  # WeakSet auto-dropped the entry


# ===== 4c. I2 — get_state/get_recent bodies enforce _require_debug BEFORE
# ever touching DaemonLink.query(); an id()/order refactor that dropped the
# gate would leave link.query_calls non-empty for an unauthed session. =====

@pytest.mark.asyncio
async def test_get_state_impl_blocks_unauthed_session_and_never_queries():
    from dollos.mcp_server.__main__ import _get_state_impl
    _authed.clear()
    link = _FakeLink({"mood": "calm", "current_self": "..."})
    session = _FakeSession()
    with pytest.raises(PermissionError):
        await _get_state_impl(link, "tok", session)
    assert link.query_calls == []


@pytest.mark.asyncio
async def test_get_state_impl_allows_authed_session_and_queries():
    from dollos.mcp_server.__main__ import _get_state_impl
    _authed.clear()
    link = _FakeLink({"mood": "calm", "current_self": "..."})
    session = _FakeSession()
    assert _try_authenticate(session, "s", "s") is True
    result = await _get_state_impl(link, "tok", session)
    assert result == {"mood": "calm", "current_self": "..."}
    assert len(link.query_calls) == 1
    assert link.query_calls[0].token == "tok"


@pytest.mark.asyncio
async def test_get_recent_impl_blocks_unauthed_session_and_never_queries():
    from dollos.mcp_server.__main__ import _get_recent_impl
    _authed.clear()
    link = _FakeLink({"items": []})
    session = _FakeSession()
    with pytest.raises(PermissionError):
        await _get_recent_impl(link, "tok", session, 20)
    assert link.query_calls == []


@pytest.mark.asyncio
async def test_get_recent_impl_allows_authed_session_and_queries():
    from dollos.mcp_server.__main__ import _get_recent_impl
    _authed.clear()
    link = _FakeLink({"items": ["a"]})
    session = _FakeSession()
    assert _try_authenticate(session, "s", "s") is True
    result = await _get_recent_impl(link, "tok", session, 5)
    assert result == {"items": ["a"]}
    assert len(link.query_calls) == 1
    assert link.query_calls[0].n == 5


# ===== 5. debug talk() stamps debug_reliable=True; non-debug omits it =====

@pytest.mark.asyncio
async def test_debug_talk_stamps_debug_reliable_true():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(
        link.talk("conn1", "Claude", "hi", debug_reliable=True)
    )
    await asyncio.sleep(0.02)
    evts = _sent_of_type(ws, "channel_event")
    assert evts[0]["payload"]["debug_reliable"] is True

    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    await task


@pytest.mark.asyncio
async def test_non_debug_talk_omits_debug_reliable():
    link = DaemonLink()
    ws = _FakeWS()
    link.set_ws(ws)
    task = asyncio.create_task(link.talk("conn1", "Claude", "hi"))
    await asyncio.sleep(0.02)
    evts = _sent_of_type(ws, "channel_event")
    assert "debug_reliable" not in evts[0]["payload"]

    cid = _sent_of_type(ws, "channel_register")[0]["channel_id"]
    link.dispatch(json.dumps({"type": "turn_end_addressed", "channel_id": cid}))
    await task


# ===== mcp.toml debug_secret/query_token loading (fail-closed defaults) =====

def test_load_debug_config_reads_secret_and_token(tmp_path):
    from dollos.mcp_server.__main__ import _load_debug_config
    cfg = tmp_path / "mcp.toml"
    cfg.write_text(
        '[server]\nbind_host = "127.0.0.1"\nbind_port = 9877\n'
        'debug_secret = "s3cr3t"\nquery_token = "tok"\n'
    )
    assert _load_debug_config(cfg) == ("s3cr3t", "tok")


def test_load_debug_config_defaults_empty_when_unset(tmp_path):
    from dollos.mcp_server.__main__ import _load_debug_config
    cfg = tmp_path / "mcp.toml"
    cfg.write_text('[server]\nbind_host = "127.0.0.1"\nbind_port = 9877\n')
    assert _load_debug_config(cfg) == ("", "")
