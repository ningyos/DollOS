"""Kernel differentiated debounce (P1c Task 5): admitted ChannelEvent
perceptions pass through BatchAccumulator before reaching the queue —
engaged sessions get the short ``debounce_engaged_s`` window (keep up with
live chat), cold channels get the long ``debounce_cold_s`` window (flood
protection). The window is chosen by ``AttentionGate.window_for`` using
engagement state as of BEFORE this message's admit() call (see kernel.py's
comment on that ordering) — a channel with no open session yet is "cold"
for this very message, even though admission immediately opens a session.

Owner preempt/cancel is unaffected: it still fires synchronously at
admit-time, ahead of the debounce — the debounce only delays the
perception ENQUEUE, never the interrupt of Doll's current cascade.
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


def _channel_event(channel_id: str, **payload) -> ChannelEvent:
    return ChannelEvent(channel_id=channel_id, payload=payload)


def _spy_on_add(dollos: DollOS) -> list[float | None]:
    """Wrap accumulator.add to record the window_s each call received,
    while still delegating to the real bound method — so real debounce /
    coalescing behavior still happens underneath the spy."""
    original_add = dollos._accumulator.add
    windows: list[float | None] = []

    async def spy(channel_id, item, window_s=None):
        windows.append(window_s)
        await original_add(channel_id, item, window_s)

    dollos._accumulator.add = spy
    return windows


# ----- cold channel: first admitted message uses the long window -----


@pytest.mark.asyncio
async def test_cold_channel_uses_long_debounce_window(tmp_path: Path) -> None:
    """A channel with no open session yet is cold — its first admitted
    (L0-mention) message must debounce with debounce_cold_s, not the short
    engaged window, even though admission opens a session for it."""
    settings = _make_settings(tmp_path, debounce_engaged_s=0.05, debounce_cold_s=0.2)
    dollos = DollOS(settings)
    windows = _spy_on_add(dollos)
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll",
        ),
        sink,
    )

    assert windows == [0.2]

    await dollos._accumulator.flush_all()


# ----- engaged channel: continuation messages use the short window -----


@pytest.mark.asyncio
async def test_engaged_channel_uses_short_debounce_window(tmp_path: Path) -> None:
    """Once a session is open (engaged), a same-participant continuation
    message (no fresh L0 signal) must debounce with the short
    debounce_engaged_s window."""
    settings = _make_settings(tmp_path, debounce_engaged_s=0.05, debounce_cold_s=0.2)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    # Open + settle an engaged session first (this uses the cold window —
    # covered by the test above), then clear it so it doesn't interfere.
    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll",
        ),
        sink,
    )
    await dollos._accumulator.flush_all()
    await dollos._perception_queue.drain(timeout_s=0.05)

    windows = _spy_on_add(dollos)

    # Same participant continuing without a fresh mention -> L1 continuation.
    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="following up",
        ),
        sink,
    )

    assert windows == [0.05]

    await dollos._accumulator.flush_all()


# ----- multiple engaged messages coalesce into ONE flush, none lost -----


@pytest.mark.asyncio
async def test_engaged_messages_coalesce_into_one_flush_no_message_lost(
    tmp_path: Path,
) -> None:
    """Two rapid continuations within debounce_engaged_s must land in ONE
    flush (one batch -> one turn's worth of perceptions), not two separate
    flushes/turns — and both messages must survive into that batch."""
    settings = _make_settings(tmp_path, debounce_engaged_s=0.05, debounce_cold_s=0.2)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    # Patch the accumulator's stored enqueue callback directly — it captured
    # the bound _enqueue_channel_batch method at construction time, so
    # reassigning dollos._enqueue_channel_batch afterward would not be seen.
    flushed: list[list[dict]] = []
    dollos._accumulator._enqueue = lambda items: flushed.append(items)

    # Open the session (cold path) and let it settle first.
    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll",
        ),
        sink,
    )
    await dollos._accumulator.flush_all()
    flushed.clear()

    # Two rapid continuations, well within the (short) engaged window —
    # no await point long enough for the debounce timer to fire between them.
    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="msg 2",
        ),
        sink,
    )
    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="msg 3",
        ),
        sink,
    )

    assert flushed == []  # window hasn't fired yet

    await asyncio.sleep(0.15)  # comfortably past debounce_engaged_s

    assert len(flushed) == 1  # ONE flush — coalesced, not two separate turns
    batch = flushed[0]
    assert [item["event"]["content"] for item in batch] == ["msg 2", "msg 3"]


# ----- different channels never merge into one batch -----


@pytest.mark.asyncio
async def test_different_channels_do_not_merge_into_one_batch(
    tmp_path: Path,
) -> None:
    """Per-channel single-origin (P1a drain_grouped compatibility): two
    different channels' admitted messages must flush as separate batches,
    never merged into one."""
    settings = _make_settings(tmp_path, debounce_engaged_s=0.05, debounce_cold_s=0.05)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()

    flushed: list[list[dict]] = []
    dollos._accumulator._enqueue = lambda items: flushed.append(items)

    await dollos._handle_message(
        _channel_event(
            "discord:AAA",
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="from A",
        ),
        sink,
    )
    await dollos._handle_message(
        _channel_event(
            "discord:BBB",
            author_id="u2",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="from B",
        ),
        sink,
    )

    await asyncio.sleep(0.1)

    assert len(flushed) == 2
    channel_ids = {items[0]["event"]["channel_id"] for items in flushed}
    assert channel_ids == {"discord:AAA", "discord:BBB"}


# ----- owner preempt/cancel still fires immediately, ahead of debounce -----


@pytest.mark.asyncio
async def test_owner_preempt_cancel_fires_before_debounce_window_elapses(
    tmp_path: Path,
) -> None:
    """The owner's preempt/cancel must not wait for the debounce window —
    it's about interrupting Doll's current cascade, not about batching the
    reply-trigger. Uses a long cold window to prove the perception itself
    genuinely hasn't reached the queue yet when the cancel already has."""
    settings = _make_settings(tmp_path, debounce_engaged_s=0.05, debounce_cold_s=5.0)
    dollos = DollOS(settings)
    cancelled: list[str] = []
    dollos._cancel_consolidation = lambda: cancelled.append("consolidation")
    dollos._cancel_evolution = lambda: cancelled.append("evolution")
    sink: asyncio.Queue = asyncio.Queue()

    await dollos._handle_message(
        _channel_event(
            "discord:123",
            author_id="owner1",
            author_is_owner=True,
            is_dm=True,
            mentioned=False,
            content="hi it's me",
        ),
        sink,
    )

    # Preempt/cancel already happened — synchronously, at admit-time.
    assert cancelled == ["consolidation", "evolution"]

    # But the perception itself is still inside the (long, cold) debounce
    # window — not yet in the queue.
    perceptions = await dollos._perception_queue.drain(timeout_s=0.05)
    assert perceptions == []

    await dollos._accumulator.flush_all()
    perceptions = await dollos._perception_queue.drain(timeout_s=0.05)
    assert any(p.kind == "ChannelMessage" for p in perceptions)
