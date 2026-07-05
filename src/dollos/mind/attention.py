"""AttentionGate — flow-agnostic admit decision + engagement session state
(P1c spec §3.3/§3.4 L0 + L1).

This module is PURE LOGIC: no I/O, no async, no imports from kernel /
discord_bridge / mind_loop. It answers one question — given a plain-dict
message event and a monotonic timestamp, should Doll's attention be
admitted (surfaced as a perception) — and tracks per-channel engagement
session state driving the L1 continuation branch.

L0 hard-rule signal logic is moved here from
``discord_bridge/wake.py::l0_wake``, MINUS the self-filter: the bridge no
longer forwards self-authored events at all (Task 3), so AttentionGate
never sees them and does not re-check ``bot_id``.

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

from dataclasses import dataclass


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
        name_aliases: list[str],
        always_wake_channels: set[str] | list[str] | tuple[str, ...],
        owner_id: str,
        max_session_turns: int,
        window_base_s: float,
        window_decay: float,
        debounce_engaged_s: float,
        debounce_cold_s: float,
    ) -> None:
        self._name_aliases = list(name_aliases)
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
        if any(alias in (event.get("content") or "") for alias in self._name_aliases):
            return "l0_name"
        if event.get("reply_to_bot"):
            return "l0_reply"
        if event.get("channel_id") in self._always_wake:
            return "l0_always"
        return None

    def admit(self, event: dict, now: float) -> AdmitDecision:
        sig = self._l0_signal(event)
        if sig is not None:
            # L0 = (re)mention → reset/open session per disengage rule.
            channel_id = event["channel_id"]
            self._sessions[channel_id] = Session(
                channel_id=channel_id,
                participants={event["author_id"]},
                last_activity=now,
                turn_count=0,
                window_s=self._window_base_s,
            )
            return AdmitDecision(True, sig)

        # L1 continuation (Task 2): session-aware re-admit without a tag.
        channel_id = event["channel_id"]
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

        if event["author_id"] not in s.participants:
            # Bystander in an active channel — narrows over-fire; don't
            # touch the existing session.
            return AdmitDecision(False, "not_admitted")

        # Continuation admitted: extend activity, but do NOT reset
        # turn_count/window — only an L0 re-mention resets those.
        s.last_activity = now
        return AdmitDecision(True, "l1_continuation")

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

    def window_for(self, channel_id: str, now: float) -> float:
        """Differentiated debounce: shorter while engaged, longer while
        cold — surfaced conversation should feel responsive; cold-channel
        chatter should not."""
        return self._debounce_engaged_s if self.is_engaged(channel_id, now) else self._debounce_cold_s
