"""AttentionGate — flow-agnostic admit decision + engagement session state
(P1c spec §3.3/§3.4 L0 + L1).

This module is PURE LOGIC: no I/O, no async, no imports from kernel /
discord_bridge / mind_loop. It answers one question — given a plain-dict
message event and a monotonic timestamp, should Doll's attention be
admitted (surfaced as a perception) — and tracks per-channel engagement
session state driving the L1 continuation branch.

L0 hard-rule signal logic was moved here from the old
``discord_bridge/wake.py::l0_wake`` (removed, P1c whole-branch review — it
had zero production callers once Task 3 landed), MINUS the self-filter: the
bridge no longer forwards self-authored events at all (Task 3), so
AttentionGate never sees them and does not re-check ``bot_id``.

Task 1 of the P1c plan built L0 branch + session open. Task 2 (this)
fills the L1 continuation branch (session-aware re-admit without a tag,
within an active engagement window) plus the disengage gate: ``note_reply``
(kernel calls after Doll speaks — advances turn_count, decays window_s,
disengages at ``max_session_turns``), ``window_for`` (differentiated
debounce), and ``is_engaged``. Reset of turn_count/window to base happens
ONLY on an L0 re-mention (see ``admit``'s L0 branch) — L1 continuation and
``note_reply`` only ever extend/decay/accumulate, never reset.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


def _name_match(content: str, tokens: frozenset[str]) -> bool:
    """L0 name-match, hardened (spec §3.4/D2): a raw ``alias in content``
    substring check is dangerous for a wake trigger — a short/ASCII alias
    like "Gura" would hit "Gurapp", and any 2-char token would hit almost
    everything. Two branches:

    - ASCII or mixed token (contains at least one ASCII char): lowercased
      **word-boundary** match (``\\bgura\\b``) — "hey gura" matches,
      "gurapp" does not.
    - Pure CJK / non-ASCII token: substring match (CJK has no whitespace
      to delimit a "word", so a boundary regex doesn't apply) — but the
      token is only ever in ``tokens`` if it already passed the caller's
      min-length guard (>=2), so this can't degrade to a 1-char CJK
      landmine.

    Empty/falsy content or an empty token set never matches (fails
    closed, no I/O, pure logic — this is called from ``_l0_signal``).
    """
    if not content:
        return False
    for token in tokens:
        if not token:
            continue
        if any(ch.isascii() for ch in token):
            if re.search(rf"\b{re.escape(token)}\b", content, re.IGNORECASE):
                return True
        elif token in content:
            return True
    return False


@dataclass
class AdmitDecision:
    """Result of :meth:`AttentionGate.admit`.

    ``reason`` is one of:
    ``"l0_dm" | "l0_mention" | "l0_name" | "l0_reply" | "l0_always" |
    "l1_continuation" | "not_admitted"``.
    """

    admit: bool
    reason: str


@dataclass
class Session:
    """Per-channel engagement session state (L1 continuation, Task 2)."""

    channel_id: str
    participants: set[str]
    last_activity: float
    turn_count: int
    window_s: float


class AttentionGate:
    """Pure-logic gate: L0 hard rules + engagement session bookkeeping.

    Flow-agnostic — does not know how events arrive (Discord, voice, etc.);
    the swappable flow layer (Tasks 3-5) is responsible for building the
    plain-dict ``event`` this class consumes.
    """

    def __init__(
        self,
        *,
        alias_provider: Callable[[], frozenset[str]],
        always_wake_channels: set[str] | list[str] | tuple[str, ...],
        owner_id: str,
        max_session_turns: int,
        window_base_s: float,
        window_decay: float,
        debounce_engaged_s: float,
        debounce_cold_s: float,
    ) -> None:
        # Provider is injected (spec §3.5, A3) — this class stays pure
        # logic and does not know whether aliases come from a pack seed, a
        # config floor, a learned-alias JSON file, or a test lambda. It is
        # called at MATCH time (inside ``_l0_signal``), not cached here,
        # so a learned alias becomes wake-eligible on the very next message
        # after the owner teaches it.
        self._alias_provider = alias_provider
        self._always_wake = set(always_wake_channels)
        self._owner_id = owner_id
        self._max_session_turns = max_session_turns
        self._window_base_s = window_base_s
        self._window_decay = window_decay
        self._debounce_engaged_s = debounce_engaged_s
        self._debounce_cold_s = debounce_cold_s
        self._sessions: dict[str, Session] = {}

    def _l0_signal(self, event: dict) -> str | None:
        """L0 hard-rule signal check (spec §3.4), self-filter EXCLUDED —
        the bridge no longer forwards self-authored events (Task 3)."""
        if event.get("is_dm"):
            return "l0_dm"
        if event.get("mentioned"):
            return "l0_mention"
        if _name_match(event.get("content") or "", self._alias_provider()):
            return "l0_name"
        if event.get("reply_to_bot"):
            return "l0_reply"
        if event.get("channel_id") in self._always_wake:
            return "l0_always"
        return None

    def admit(self, event: dict, now: float) -> AdmitDecision:
        # Task 4 review (whole-branch): the event shape is now pinned to the
        # bridge's forwarded ChannelEvent payload (always carries channel_id/
        # author_id in real Discord traffic — see client.py's translator).
        # Still use `.get(...)` rather than bracket-indexing here: this class
        # is flow-agnostic pure logic with no guarantee a future/other caller
        # feeds it a well-formed dict, and a malformed event must fail CLOSED
        # (not_admitted) rather than raise KeyError and crash the whole
        # attention path — that would be worse than under-admitting, given
        # this gate's entire purpose is safety ("default silence").
        channel_id = event.get("channel_id")
        if channel_id is None:
            # No channel to track a session against or route a reply to.
            return AdmitDecision(False, "not_admitted")

        sig = self._l0_signal(event)
        if sig is not None:
            # L0 = (re)mention → reset her budget/window per the disengage
            # rule. For a channel that ALREADY has a session, MERGE the new
            # author into participants (multi-person: a second person tagging
            # Doll broadens who she's talking with — it must never evict
            # someone she was already engaged with, or that person's tagless
            # continuation would stop being admitted). Spec participant model:
            # {她 + 觸發她的 author + 窗內對她發言者}. Only turn_count/window/
            # last_activity reset; participants accumulate.
            author_id = event.get("author_id")
            s = self._sessions.get(channel_id)
            if s is None:
                self._sessions[channel_id] = Session(
                    channel_id=channel_id,
                    participants={author_id},
                    last_activity=now,
                    turn_count=0,
                    window_s=self._window_base_s,
                )
            else:
                s.participants.add(author_id)
                s.last_activity = now
                s.turn_count = 0
                s.window_s = self._window_base_s
            return AdmitDecision(True, sig)

        # L1 continuation (Task 2): session-aware re-admit without a tag.
        s = self._sessions.get(channel_id)
        if s is None:
            return AdmitDecision(False, "not_admitted")

        expired = now - s.last_activity >= s.window_s
        disengaged = s.turn_count >= self._max_session_turns
        if expired or disengaged:
            # She's no longer engaged on this channel; only a fresh L0
            # mention reopens it.
            del self._sessions[channel_id]
            return AdmitDecision(False, "not_admitted")

        if event.get("author_id") not in s.participants:
            # Bystander in an active channel — narrows over-fire; don't
            # touch the existing session.
            return AdmitDecision(False, "not_admitted")

        # Continuation admitted: extend activity, but do NOT reset
        # turn_count/window — only an L0 re-mention resets those.
        s.last_activity = now
        return AdmitDecision(True, "l1_continuation")

    def forget(self, channel_id: str) -> None:
        """Reap ``channel_id``'s engagement Session (whole-branch review,
        mcp-p1-peer): the kernel calls this from ``_handle_disconnect`` for
        every channel_id a disconnecting sink had registered, mirroring how
        it already reaps that sink's SinkResolver/ChannelRegistry entries.

        Without this, ``_sessions`` grows O(total admitted channel_ids) for
        the daemon's entire uptime — harmless for Discord (stable, long-lived
        channel_ids) but unbounded for the MCP connector, which mints a
        unique one-shot ``mcp:<conn>:<call>`` channel_id per ``talk()`` call
        and never revisits it after disconnect.

        Idempotent: popping an absent key is a no-op, not an error — a
        session may already be gone (window expiry / disengage) by the time
        disconnect runs.
        """
        self._sessions.pop(channel_id, None)

    def note_reply(self, channel_id: str, now: float) -> None:
        """Record that Doll spoke on ``channel_id`` (kernel calls this AFTER
        she replies, Task 4). Advances turn_count and decays the window;
        disengages (deletes the session) once she hits her consecutive-reply
        cap. Never resets — only an L0 re-mention reopens a fresh session."""
        s = self._sessions.get(channel_id)
        if s is None:
            return
        s.turn_count += 1
        s.window_s *= self._window_decay
        if s.turn_count >= self._max_session_turns:
            del self._sessions[channel_id]

    def is_engaged(self, channel_id: str, now: float) -> bool:
        """True iff a non-expired session exists for ``channel_id``."""
        s = self._sessions.get(channel_id)
        if s is None:
            return False
        return now - s.last_activity < s.window_s

    def window_for(
        self,
        channel_id: str,
        now: float,
        *,
        is_dm: bool = False,
        author_is_owner: bool = False,
    ) -> float:
        """Differentiated debounce: shorter while engaged, longer while
        cold — surfaced conversation should feel responsive; cold-channel
        chatter should not.

        The cold long window exists to protect COLD PUBLIC channels from
        flooding — it must never delay a DM or an owner message, which are
        direct 1:1 conversations regardless of session state (P1c
        whole-branch review Important #2: an owner/DM first message after
        idle has no session yet, so `is_engaged` alone would misclassify it
        as cold). `is_dm` / `author_is_owner` therefore short-circuit to the
        engaged window even when no session is open yet.
        """
        if is_dm or author_is_owner or self.is_engaged(channel_id, now):
            return self._debounce_engaged_s
        return self._debounce_cold_s
