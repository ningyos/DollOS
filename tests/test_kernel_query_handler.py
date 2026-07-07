"""kernel authenticated read-query side-channel (P2 Task 1, spec §C.3).

QueryState/QueryRecent are a debug-only read surface for the MCP debug
connector. SECURITY-CRITICAL: the daemon IPC has no connection auth, so
``settings.mcp.query_token`` IS the boundary — every test here either
proves the fail-closed gate or proves the tier-scope exclusion actually
excludes owner-private content (not just "the code runs").
"""
import asyncio
from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    McpConfig,
    MemsearchConfig,
    Settings,
)
from dollos.ipc.messages import QueryRecent, QueryResult, QueryState
from dollos.kernel import DollOS
from dollos.mind import self_history
from dollos.mind.mind_state import OutputRecord, Perception


def _make_settings(tmp_path: Path, *, query_token: str | None = None) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
        'self = "You are Doll."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        mcp=McpConfig(query_token=query_token),
    )


async def _drain(sink: "asyncio.Queue") -> list:
    out = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


# ----- 1. token gate fail-closed -----


@pytest.mark.asyncio
async def test_query_rejected_when_token_missing_or_wrong(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(QueryState(query_id="q1", token="wrong"), sink)
    msgs = await _drain(sink)
    assert len(msgs) == 1
    assert isinstance(msgs[0], QueryResult)
    assert msgs[0].query_id == "q1"
    assert msgs[0].ok is False
    assert msgs[0].payload == {}

    # query_token unset entirely on settings.mcp → surface fully disabled,
    # even a token that would otherwise be "right" is rejected.
    settings2 = _make_settings(tmp_path / "b", query_token=None)
    dollos2 = DollOS(settings2)
    sink2: asyncio.Queue = asyncio.Queue()
    await dollos2._handle_message(QueryState(query_id="q2", token="right"), sink2)
    msgs2 = await _drain(sink2)
    assert msgs2[0].ok is False
    assert msgs2[0].payload == {}


# ----- 2. get_state snapshot: real Mood + real self_history.jsonl -----


@pytest.mark.asyncio
async def test_query_state_returns_mood_and_current_self(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    dollos._mind_state.mood.emotion = "平靜"
    dollos._mind_state.mood.reason = "test seed"

    # Before any adoption: current_self == "" (pack-only, no evo_adopt yet).
    await dollos._handle_message(QueryState(query_id="q1", token="right"), sink)
    msgs = await _drain(sink)
    assert msgs[0].ok is True
    assert msgs[0].payload["mood"] == "平靜"  # the .emotion field, not str(Mood)
    assert msgs[0].payload["current_self"] == ""
    assert "energy" not in msgs[0].payload
    assert set(msgs[0].payload.keys()) == {"mood", "current_self"}

    # Seed a REAL self_history.jsonl adoption (mirror mind_loop.py:242-245's
    # sanctioned_text read) — current_self must come from the ratified prose,
    # not any ad-hoc MindState attribute (MindState has no current_self field).
    hist_path = settings.data.root / "memory" / "self_history.jsonl"
    self_history.log_event(hist_path, kind="evo_adopt", text="我是被批准的現在的我")

    await dollos._handle_message(QueryState(query_id="q2", token="right"), sink)
    msgs2 = await _drain(sink)
    assert msgs2[0].payload["current_self"] == "我是被批准的現在的我"
    assert msgs2[0].payload["mood"] == "平靜"


# ----- 3. get_recent tier scope — the security test -----


@pytest.mark.asyncio
async def test_query_recent_only_external_public_perceptions(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    owner_msg = Perception(
        kind="ChannelMessage", t=1.0,
        data={
            "content": "OWNER-SECRET-do-not-leak", "channel_id": "disc:1",
            "author_is_owner": True, "is_dm": True,
        },
    )
    user_spoke = Perception(
        kind="UserSpoke", t=2.0, data={"text": "OWNER-LOCAL-CHAT-do-not-leak"},
    )
    peer_msg = Perception(
        kind="ChannelMessage", t=3.0,
        data={
            "content": "peer says hi over mcp", "channel_id": "mcp:conn1",
            "channel_kind": "mcp", "author_is_owner": False, "author": "PeerAI",
            "is_dm": True,
        },
    )
    awoke = Perception(kind="Awoke", t=4.0, data={})

    for p in (owner_msg, user_spoke, peer_msg, awoke):
        dollos._mind_state.recent_perceptions.append(p)

    await dollos._handle_message(QueryRecent(query_id="q1", token="right", n=20), sink)
    msgs = await _drain(sink)
    assert msgs[0].ok is True
    items = msgs[0].payload["items"]
    texts = [it["text"] for it in items]

    assert texts == ["peer says hi over mcp"]
    dump = str(msgs[0].payload)
    assert "OWNER-SECRET-do-not-leak" not in dump
    assert "OWNER-LOCAL-CHAT-do-not-leak" not in dump


# ----- 4. recent_outputs excluded entirely -----


@pytest.mark.asyncio
async def test_query_recent_excludes_outputs(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    peer_msg = Perception(
        kind="ChannelMessage", t=1.0,
        data={
            "content": "peer text", "channel_id": "mcp:conn1",
            "channel_kind": "mcp", "author_is_owner": False, "author": "PeerAI",
        },
    )
    dollos._mind_state.recent_perceptions.append(peer_msg)
    dollos._mind_state.recent_outputs.append(
        OutputRecord(t=1.0, kind="Speech", summary="OWNER-REPLY-SUMMARY-do-not-leak")
    )

    await dollos._handle_message(QueryRecent(query_id="q1", token="right", n=20), sink)
    msgs = await _drain(sink)
    assert "outputs" not in msgs[0].payload
    dump = str(msgs[0].payload)
    assert "OWNER-REPLY-SUMMARY-do-not-leak" not in dump
    assert any(it["text"] == "peer text" for it in msgs[0].payload["items"])


# ----- 5. n clamp + n=0 edge -----


@pytest.mark.asyncio
async def test_query_recent_clamps_n(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)

    for i in range(20):
        dollos._mind_state.recent_perceptions.append(Perception(
            kind="ChannelMessage", t=float(i),
            data={
                "content": f"peer msg {i}", "channel_id": "mcp:conn1",
                "channel_kind": "mcp", "author_is_owner": False,
            },
        ))

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(QueryRecent(query_id="q1", token="right", n=9999), sink)
    msgs = await _drain(sink)
    assert len(msgs[0].payload["items"]) <= 100  # clamped, no crash

    sink2: asyncio.Queue = asyncio.Queue()
    await dollos._handle_message(QueryRecent(query_id="q2", token="right", n=0), sink2)
    msgs2 = await _drain(sink2)
    assert msgs2[0].payload["items"] == []  # n=0 must be [], NOT items[-0:] (== all)


# ----- 6. read-only: no perception enqueued, no cascade -----


@pytest.mark.asyncio
async def test_query_does_not_enqueue_perception(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, query_token="right")
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    before = await dollos._perception_queue.drain(timeout_s=0.05)
    assert before == []

    await dollos._handle_message(QueryState(query_id="q1", token="right"), sink)
    await dollos._handle_message(QueryRecent(query_id="q2", token="right"), sink)

    after = await dollos._perception_queue.drain(timeout_s=0.05)
    assert after == []
