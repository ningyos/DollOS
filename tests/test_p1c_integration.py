"""P1c Task 7 (FINAL, acceptance test): end-to-end integration net driving
the REAL kernel + AttentionGate + mind_loop, proving smolGura's three
failure modes don't recur:

  1. 亂回 (rambling replies to irrelevant chatter) — a stranger with no L0
     signal and no open session must be dropped before ever becoming a
     perception: no cascade, no reply.
  2. 跟不上 (losing the thread) — once engaged via an @-mention, the SAME
     author's tagless follow-up must still be admitted (L1 continuation)
     and get a reply.
  3. 串內 over-fire (replying to every single message in an ongoing
     conversation forever) — after `max_session_turns` replies she must
     DISENGAGE; a further tagless continuation gets no reply, until a
     fresh @-mention reopens the session.

Plus the R2-required "隔壁閒聊" companion: a non-participant bystander
posting in a channel with an ACTIVE session must not get swept in either.

Seam driven (per the task brief's "acceptable seam" clause): this drives
`DollOS._handle_message` (real `ChannelEvent`/`ChannelRegister` dispatch),
`BatchAccumulator.flush_all()` (real debounce), and `MindLoop.iterate()`
(real per-turn cascade, real `on_turn_complete` → `AttentionGate.note_reply`
wiring) directly, rather than through `DollOS.run()`'s full asyncio-task
soup (system_pulse poller, schedule runner, consolidation/evolution
triggers, IPC WebSocket transport). Nothing about the ATTENTION decision
itself is mocked: `AttentionGate.admit`/`note_reply`/`window_for` all run
as real, unmocked code — the daemon's own network LLM call is the only
thing replaced, with a scripted no-tool-call responder (`_FakeLLM` +
`_speech_pass`, both reused verbatim from `tests/test_mind_loop.py`, the
same fakes `test_kernel_attention.py` already relies on for its
on_turn_complete wiring proof).

"Reply" is observed as a real `AddressedText` landing on an externally
registered sink for that channel_id (mirrors how a real Discord bridge
connection is registered via `ChannelRegister` + carry I-1's dual
SinkResolver/ChannelRegistry registration) — not a mocked notification.
"Admitted" is observed as a real Perception landing in
`DollOS._perception_queue` after the real debounce flush — not by calling
`AttentionGate.admit` directly (that would bypass the kernel wiring this
test exists to prove).
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
from dollos.ipc.messages import AddressedText, ChannelEvent, ChannelRegister
from dollos.kernel import DollOS
from tests.test_mind_loop import _FakeLLM, _speech_pass

REPLY_TEXT = "我在的"


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


async def _build_dollos(
    tmp_path: Path,
    sink: "asyncio.Queue",
    channel_id: str,
    **attention_kwargs,
) -> DollOS:
    """Real DollOS kernel (real AttentionGate, real BatchAccumulator, real
    MindLoop) with two substitutions, both purely at the I/O edges:

      - the network LLM call is replaced with a scripted, stateless,
        no-tool-call responder (same text every turn — a bare "Say") so a
        turn always converges in exactly one pass with no <tool_call>;
      - `channel_id` is registered external via the same ChannelRegister
        path a real Discord bridge connection uses, wired to `sink`, so a
        reply is observable as a genuine `AddressedText`.

    Nothing about admission/debounce/disengage is touched.
    """
    settings = _make_settings(tmp_path, **attention_kwargs)
    dollos = DollOS(settings)
    dollos._mind_loop._llm = _FakeLLM(_speech_pass(REPLY_TEXT))
    await dollos._handle_message(
        ChannelRegister(channel_id=channel_id, locus="external", kind="discord"),
        sink,
    )
    return dollos


def _event(channel_id: str, **payload) -> ChannelEvent:
    return ChannelEvent(channel_id=channel_id, payload=payload)


def _queued_count(dollos: DollOS) -> int:
    """Perceptions currently sitting in the (undrained) perception queue —
    read-only peek, does not consume. Mirrors the private-attribute access
    tests/test_kernel_attention.py already relies on (`dollos._attention._sessions[...]`)."""
    return dollos._perception_queue._queue.qsize()


async def _flush(dollos: DollOS) -> int:
    """Force-flush the debounce accumulator (real BatchAccumulator.flush_all
    — no reliance on real-time sleep) and return how many perceptions are
    now queued."""
    await dollos._accumulator.flush_all()
    return _queued_count(dollos)


async def _run_one_turn(dollos: DollOS) -> None:
    """Drive exactly one real MindLoop turn. Caller MUST have already
    confirmed (via `_flush`) that a perception is queued — `iterate()`
    blocks forever on an empty queue, so calling this blind on a message
    that failed to admit would hang the suite instead of failing it."""
    await dollos._mind_loop.iterate()


def _addressed_texts(sink: "asyncio.Queue", channel_id: str) -> list[str]:
    """Drain (destructively) every AddressedText addressed to channel_id
    currently sitting on sink. Non-AddressedText items (None turn-end
    separators, etc.) are discarded — this test only cares whether a real
    reply landed."""
    out: list[str] = []
    while not sink.empty():
        item = sink.get_nowait()
        if isinstance(item, AddressedText) and item.channel_id == channel_id:
            out.append(item.text)
    return out


# ----- 1. 亂回 不重演: stranger, no L0 signal, no session -> dropped -----


@pytest.mark.asyncio
async def test_stranger_unrelated_chatter_dropped_no_reply(tmp_path: Path) -> None:
    """A stranger posting unrelated chatter in a multi-person channel — no
    mention, no name-alias, not a session participant, no active session —
    must never become a perception, so no cascade ever runs and no reply
    lands. Fails if the anti-亂回 admission gate regresses and a stranger's
    idle chatter gets admitted (queued > 0) or replied to."""
    channel_id = "discord:general"
    sink: asyncio.Queue = asyncio.Queue()
    dollos = await _build_dollos(tmp_path, sink, channel_id)

    await dollos._handle_message(
        _event(
            channel_id,
            author_id="stranger",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="anyone want to grab lunch later",
        ),
        sink,
    )

    assert await _flush(dollos) == 0, (
        "亂回 regression: a stranger's unrelated chatter with no L0 signal "
        "and no open session was admitted into the perception queue"
    )
    # No turn ran (nothing was ever queued for it to run on), so no reply.
    assert _addressed_texts(sink, channel_id) == []
    assert channel_id not in dollos._attention._sessions


# ----- 2. 跟不上 不重演: @-mention opens session, tagless continuation still replied -----


@pytest.mark.asyncio
async def test_mentioned_then_tagless_continuation_still_gets_reply(
    tmp_path: Path,
) -> None:
    """She gets @-mentioned (L0) -> session opens -> she replies -> the SAME
    author continues WITHOUT a tag -> must still be admitted as
    l1_continuation and get a second reply. Fails if tagless continuation
    admission regresses (message never reaches the queue, second turn never
    runs, no second reply)."""
    channel_id = "discord:general"
    sink: asyncio.Queue = asyncio.Queue()
    dollos = await _build_dollos(tmp_path, sink, channel_id, max_session_turns=5)

    # --- turn 1: @-mention opens the session ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll, what do you think about coffee",
        ),
        sink,
    )
    assert await _flush(dollos) == 1
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT], (
        "expected a reply to the @-mention that opened the session"
    )
    session = dollos._attention._sessions[channel_id]
    assert session.turn_count == 1  # note_reply fired exactly once

    # --- turn 2: same author, no tag ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="also what about tea",
        ),
        sink,
    )
    assert await _flush(dollos) == 1, (
        "跟不上 regression: same-author tagless continuation inside an "
        "active session was not admitted (L1 continuation broke)"
    )
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT], (
        "跟不上 regression: tagless continuation was admitted but produced "
        "no reply"
    )
    assert dollos._attention._sessions[channel_id].turn_count == 2


# ----- 3. 串內 over-fire 不重演: disengage after max_session_turns, rejoin on re-mention -----


@pytest.mark.asyncio
async def test_disengage_after_max_session_turns_then_remention_rejoins(
    tmp_path: Path,
) -> None:
    """After she's engaged, the conversation continues and she keeps
    replying; after `max_session_turns` note_reply calls she DISENGAGES — a
    subsequent tagless continuation from the same participant is NOT
    admitted (no reply). A fresh @-mention then reopens the session (she
    rejoins). Fails if she keeps replying past the cap (over-fire) or fails
    to rejoin on remention."""
    channel_id = "discord:general"
    sink: asyncio.Queue = asyncio.Queue()
    dollos = await _build_dollos(tmp_path, sink, channel_id, max_session_turns=2)

    # --- turn 1: @-mention opens the session (turn_count 0 -> 1 after reply) ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll",
        ),
        sink,
    )
    assert await _flush(dollos) == 1
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT]
    assert dollos._attention._sessions[channel_id].turn_count == 1

    # --- turn 2: tagless continuation, still under the cap (turn_count 1 -> 2) ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="still talking",
        ),
        sink,
    )
    assert await _flush(dollos) == 1
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT]
    # turn_count hit max_session_turns (2) -> note_reply disengaged (deleted) the session.
    assert channel_id not in dollos._attention._sessions, (
        "over-fire regression: session should have disengaged (been deleted) "
        "once turn_count reached max_session_turns"
    )

    # --- turn 3: SAME author, still no tag, session gone -> must NOT be admitted ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="hello? still there?",
        ),
        sink,
    )
    assert await _flush(dollos) == 0, (
        "over-fire regression: a tagless continuation was admitted AFTER "
        "the session had already disengaged at max_session_turns — she "
        "would keep replying to every message forever"
    )
    assert _addressed_texts(sink, channel_id) == []  # she stayed silent

    # --- turn 4: fresh @-mention reopens the session (rejoin) ---
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="doll, are you there",
        ),
        sink,
    )
    assert await _flush(dollos) == 1, (
        "rejoin regression: a fresh @-mention after disengage failed to "
        "reopen the session"
    )
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT], (
        "rejoin regression: re-mention was admitted but produced no reply"
    )
    assert dollos._attention._sessions[channel_id].turn_count == 1


# ----- 4. 隔壁閒聊不插: bystander in an active session is not swept in -----


@pytest.mark.asyncio
async def test_bystander_in_active_session_not_admitted_no_reply(
    tmp_path: Path,
) -> None:
    """While a session is active with participant A, a NON-participant B
    posts unrelated chatter (no mention) in the same channel -> must NOT be
    admitted (B isn't a session participant) -> no reply to B's message,
    and A's participation is left untouched."""
    channel_id = "discord:general"
    sink: asyncio.Queue = asyncio.Queue()
    dollos = await _build_dollos(tmp_path, sink, channel_id, max_session_turns=5)

    # A opens the session via @-mention and gets a reply.
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="A",
            author_is_owner=False,
            is_dm=False,
            mentioned=True,
            content="hey doll, question for you",
        ),
        sink,
    )
    assert await _flush(dollos) == 1
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT]

    # B (never mentioned, not a participant) posts unrelated chatter in the
    # SAME channel while A's session is still active.
    await dollos._handle_message(
        _event(
            channel_id,
            author_id="B",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            content="lol did anyone see that game last night",
        ),
        sink,
    )
    assert await _flush(dollos) == 0, (
        "隔壁閒聊 regression: a non-participant bystander's unrelated "
        "chatter was admitted into an active session it wasn't part of"
    )
    assert _addressed_texts(sink, channel_id) == []

    # The session is untouched: still just A, turn_count unchanged.
    session = dollos._attention._sessions[channel_id]
    assert session.participants == {"A"}
    assert session.turn_count == 1


# ----- 5. reply-to-her (l0_reply) admits even with NO active session -----


@pytest.mark.asyncio
async def test_reply_to_bot_with_no_active_session_gets_reply(tmp_path: Path) -> None:
    """A user hitting Discord's reply button on one of her messages, with
    ping OFF (`mentioned=False`) and no active session, must still be
    admitted — via the `l0_reply` L0 signal (spec §3.4: L0 =
    dm/mention/name/reply-to-her/always_wake) — and get a reply. This is the
    real 跟不上-adjacent path `tests/test_attention_engagement.py` only faked
    by injecting `reply_to_bot` manually; this drives the real kernel +
    AttentionGate wiring end to end. Fails if `reply_to_bot` isn't wired
    from the bridge translator through to `AttentionGate.admit` (P1c
    whole-branch review Important #1) — see
    `tests/test_discord_bridge_client.py` for the translator-side half of
    this proof."""
    channel_id = "discord:general"
    sink: asyncio.Queue = asyncio.Queue()
    dollos = await _build_dollos(tmp_path, sink, channel_id)

    await dollos._handle_message(
        _event(
            channel_id,
            author_id="u1",
            author_is_owner=False,
            is_dm=False,
            mentioned=False,
            reply_to_bot=True,
            content="I agree with what you said",
        ),
        sink,
    )

    assert await _flush(dollos) == 1, (
        "l0_reply regression: a reply-to-bot message with mentioned=False "
        "and no active session was not admitted"
    )
    await _run_one_turn(dollos)
    assert _addressed_texts(sink, channel_id) == [REPLY_TEXT]
