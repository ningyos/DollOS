"""Kernel admission wiring: ChannelEvent -> AttentionGate -> drop / enqueue,
plus the note_reply turn-complete hook (P1c Task 4).

Default silence: a ChannelEvent that AttentionGate does not admit is DROPPED
before it ever becomes a perception — this is the anti-亂回 gate. The bridge
(Task 3) forwards ALL non-self messages; the daemon is now the one place that
decides what's reply-worthy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dollos.config import (
    AttentionSettings,
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.ipc.messages import ChannelEvent
from dollos.kernel import DollOS
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM, _speech_pass


def _make_settings(tmp_path: Path, **attention_kwargs) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
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
        attention=AttentionSettings(**attention_kwargs),
    )


def _stub_cancels(dollos: DollOS) -> list[str]:
    cancelled: list[str] = []
    dollos._cancel_consolidation = lambda: cancelled.append("consolidation")
    dollos._cancel_evolution = lambda: cancelled.append("evolution")
    return cancelled


# ----- admitted ChannelEvent -> perception enqueued -----


@pytest.mark.asyncio
async def test_admitted_channel_event_queues_perception(tmp_path: Path) -> None:
    """An L0-signalled ChannelEvent (mentioned) clears the AttentionGate and
    lands as a ChannelMessage perception — existing enqueue behavior."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "u1",
                "author_is_owner": False,
                "is_dm": False,
                "mentioned": True,
                "content": "hey doll",
            },
        ),
        sink,
    )

    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(
        p.kind == "ChannelMessage" and p.data["channel_id"] == "discord:123"
        for p in perceptions
    )


# ----- non-admitted ChannelEvent -> DROPPED (default silence) -----


@pytest.mark.asyncio
async def test_non_admitted_channel_event_is_dropped(tmp_path: Path) -> None:
    """A stranger with no L0 signal and no open session must be DROPPED —
    no ChannelMessage perception at all. This is the anti-亂回 gate: it
    fails if the message got enqueued."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "stranger",
                "author_is_owner": False,
                "is_dm": False,
                "mentioned": False,
                "content": "random unrelated chatter",
            },
        ),
        sink,
    )

    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert perceptions == []


# ----- owner message (DM) -> admitted, preempt/cancel still fires -----


@pytest.mark.asyncio
async def test_owner_dm_admitted_and_still_preempts_and_cancels(
    tmp_path: Path,
) -> None:
    """The owner is always L0-admitted (DM or mention) in practice, so the
    existing owner preempt/cancel behavior must survive the admission gate
    sitting in front of it — asserted explicitly."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    cancelled = _stub_cancels(dollos)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "owner1",
                "author_is_owner": True,
                "is_dm": True,
                "mentioned": False,
                "content": "hi it's me",
            },
        ),
        sink,
    )

    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(
        p.kind == "ChannelMessage" and p.data["channel_id"] == "discord:123"
        for p in perceptions
    )
    assert cancelled == ["consolidation", "evolution"]


# ----- kernel's _on_turn_complete -> AttentionGate.note_reply wiring -----


@pytest.mark.asyncio
async def test_on_turn_complete_calls_note_reply_for_external_spoken_turn(
    tmp_path: Path,
) -> None:
    """After Doll speaks (spoke=True) on an external origin, the kernel's
    turn-complete hook must advance that channel's engagement session
    exactly once per call (AttentionGate.note_reply's turn_count bump)."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    # Open a session the same way real traffic would (L0 mention).
    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "u1",
                "author_is_owner": False,
                "is_dm": False,
                "mentioned": True,
                "content": "hey doll",
            },
        ),
        sink,
    )
    session = dollos._attention._sessions["discord:123"]
    assert session.turn_count == 0

    dollos._on_turn_complete("discord:123", True)

    assert session.turn_count == 1


@pytest.mark.asyncio
async def test_on_turn_complete_noop_for_internal_origin(tmp_path: Path) -> None:
    """origin=None (internal turn) must never touch AttentionGate — there is
    no channel_id-keyed session to advance."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    # Must not raise even with zero sessions ever opened.
    dollos._on_turn_complete(None, True)
    assert dollos._attention._sessions == {}


@pytest.mark.asyncio
async def test_on_turn_complete_noop_when_not_spoken(tmp_path: Path) -> None:
    """spoke=False (no speech this turn, e.g. a tool-only turn) must not
    advance the disengage counter."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "u1",
                "author_is_owner": False,
                "is_dm": False,
                "mentioned": True,
                "content": "hey doll",
            },
        ),
        sink,
    )
    session = dollos._attention._sessions["discord:123"]

    dollos._on_turn_complete("discord:123", False)

    assert session.turn_count == 0


# ----- MindLoop actually invokes on_turn_complete at turn end -----


@pytest.mark.asyncio
async def test_mind_loop_invokes_on_turn_complete_after_speech(tmp_path: Path) -> None:
    """Proves the plumbing MindLoop-side: a real turn that produces speech
    on a ChannelMessage's origin fires on_turn_complete(origin, True) exactly
    once — this is what the kernel's callback (tested above) is fed from."""
    calls: list[tuple[str | None, bool]] = []

    def _spy(origin, spoke):
        calls.append((origin, spoke))

    state = MindState()
    queue = PerceptionQueue(wal=None)
    queue.put(
        Perception(
            kind="ChannelMessage",
            t=1.0,
            data={"channel_id": "discord:123", "text": "hi from A"},
        )
    )
    sink: asyncio.Queue = asyncio.Queue()

    loop = make_mindloop(
        memory_root=tmp_path,
        queue=queue,
        state=state,
        sink=sink,
        llm=_FakeLLM(_speech_pass("hello there")),
        on_turn_complete=_spy,
    )

    await loop.iterate()

    assert calls == [("discord:123", True)]
