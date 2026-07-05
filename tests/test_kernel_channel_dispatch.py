"""kernel ChannelRegister/ChannelEvent dispatch (spec §3.2 + carry I-1)."""

import asyncio
from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.ipc.messages import ChannelEvent, ChannelRegister
from dollos.kernel import DollOS


def _make_settings(tmp_path: Path) -> Settings:
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
    )


def _stub_cancels(dollos: DollOS) -> list[str]:
    """Replace the cancel methods with recorders; return the shared log."""
    cancelled: list[str] = []
    dollos._cancel_consolidation = lambda: cancelled.append("consolidation")
    dollos._cancel_evolution = lambda: cancelled.append("evolution")
    return cancelled


# ----- (a) ChannelRegister(external) dual-registers -----


@pytest.mark.asyncio
async def test_channel_register_external_dual_registers(tmp_path: Path) -> None:
    """ChannelRegister(locus='external') registers into BOTH ChannelRegistry
    AND SinkResolver atomically (carry I-1) — the sink becomes resolvable by
    its channel_id origin."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelRegister(channel_id="discord:123", locus="external", kind="discord"),
        sink,
    )

    info = dollos._channel_registry.get("discord:123")
    assert info is not None
    assert info.locus == "external"
    assert dollos._sink_resolver(origin="discord:123") is sink


# ----- (b) ChannelEvent(stranger) → perception queued, no cancel -----


@pytest.mark.asyncio
async def test_channel_event_stranger_queues_perception_without_cancel(
    tmp_path: Path,
) -> None:
    """A stranger's ChannelEvent becomes a ChannelMessage perception but must
    NOT preempt/cancel Doll's in-flight consolidation/evolution — only the
    owner speaking does that (spec §3.2).

    ``mentioned=True`` clears the P1c AttentionGate admission gate (kernel.py
    Task 4) — this test is about the cancel/preempt gate specifically, which
    is orthogonal to and sits AFTER admission; a plain unsignalled stranger
    message is covered (dropped) by tests/test_kernel_attention.py instead.
    """
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    cancelled = _stub_cancels(dollos)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:123",
            payload={
                "author_id": "stranger",
                "author_is_owner": False,
                "mentioned": True,
                "text": "hi from a stranger",
            },
        ),
        sink,
    )

    # P1c Task 5: admitted perceptions now sit in a per-channel debounce
    # batch — flush it (as shutdown would) to observe the queued perception.
    await dollos._accumulator.flush_all()
    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(
        p.kind == "ChannelMessage"
        and p.data["channel_id"] == "discord:123"
        and p.data["text"] == "hi from a stranger"
        and p.data["author_is_owner"] is False
        for p in perceptions
    )
    assert cancelled == []


# ----- (c) ChannelEvent(owner) → perception queued AND cancel fired -----


@pytest.mark.asyncio
async def test_channel_event_owner_queues_perception_and_cancels(
    tmp_path: Path,
) -> None:
    """The owner speaking from an external channel is TextInput-equivalent:
    it preempts/cancels in-flight consolidation and evolution.

    ``is_dm=True`` clears the P1c AttentionGate admission gate — in real
    Discord traffic the owner is always L0-admitted via DM or mention (spec
    §3.2), so this is not a hypothetical addition for the test."""
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
                "text": "hi it's me",
            },
        ),
        sink,
    )

    # Preempt/cancel already fired synchronously above, ahead of the debounce
    # (P1c Task 5) — flush the accumulator to observe the queued perception.
    await dollos._accumulator.flush_all()
    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    assert any(
        p.kind == "ChannelMessage"
        and p.data["channel_id"] == "discord:123"
        and p.data["text"] == "hi it's me"
        and p.data["author_is_owner"] is True
        for p in perceptions
    )
    assert cancelled == ["consolidation", "evolution"]


# ----- Envelope channel_id is authoritative over payload -----


@pytest.mark.asyncio
async def test_channel_event_envelope_channel_id_wins_over_payload(
    tmp_path: Path,
) -> None:
    """The envelope msg.channel_id (which matches the ChannelRegister/
    SinkResolver handle) must be the routing origin — a channel_id inside the
    payload must NOT overwrite it, or replies silently mis-route (P1b review).

    ``mentioned=True`` clears the P1c AttentionGate admission gate — the
    envelope-vs-payload precedence this test targets is orthogonal to (and
    must survive) that gate now sitting in front of it."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    _stub_cancels(dollos)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        ChannelEvent(
            channel_id="discord:namespaced",
            payload={
                "channel_id": "999_raw_numeric",
                "author_id": "u1",
                "mentioned": True,
                "text": "hi",
            },
        ),
        sink,
    )

    # P1c Task 5: admitted perceptions now sit in a per-channel debounce
    # batch — flush it (as shutdown would) to observe the queued perception.
    await dollos._accumulator.flush_all()
    perceptions = await dollos._perception_queue.drain(timeout_s=0.1)
    channel_msgs = [p for p in perceptions if p.kind == "ChannelMessage"]
    assert len(channel_msgs) == 1
    assert channel_msgs[0].data["channel_id"] == "discord:namespaced"


# ----- Carry I-1 disconnect: unregister from both registries -----


@pytest.mark.asyncio
async def test_disconnect_unregisters_channel_from_both_registries(
    tmp_path: Path,
) -> None:
    """On disconnect, any channels this sink registered are removed from
    BOTH ChannelRegistry and SinkResolver (carry I-1)."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(__import__("datetime").date.today())
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_connect(sink)
    await dollos._handle_message(
        ChannelRegister(channel_id="discord:123", locus="external", kind="discord"),
        sink,
    )
    assert dollos._channel_registry.get("discord:123") is not None
    assert dollos._sink_resolver(origin="discord:123") is sink

    await dollos._handle_disconnect(sink)

    assert dollos._channel_registry.get("discord:123") is None
    from dollos.mind.sink_resolver import DummySink

    assert isinstance(dollos._sink_resolver(origin="discord:123"), DummySink)
