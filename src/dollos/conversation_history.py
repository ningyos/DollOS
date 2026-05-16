"""ConversationHistory — bounded sliding window of recent turn transcripts.

Each "turn" is the full LLM message list (excluding the system message)
from one cascade. New turns are prepended to the LLM message list before
send so the model sees recent reasoning.

See docs/superpowers/specs/2026-05-16-conversation-history-design.md.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ConversationHistory:
    """In-memory bounded window of recent turn transcripts.

    Each `add_turn(messages)` appends a complete message list from a
    finished cascade. `recent_messages()` flattens all retained turns
    into a single list suitable for prepending before a new user
    message. Storage is FIFO bounded at `max_turns`.
    """

    def __init__(self, max_turns: int = 6) -> None:
        if max_turns < 1:
            raise ValueError(f"max_turns must be >= 1, got {max_turns}")
        self._max_turns = max_turns
        self._turns: list[list[dict]] = []

    def add_turn(self, messages: list[dict]) -> None:
        """Append a turn's full message list; drop oldest if over cap.

        Empty message lists are ignored (no-op).
        """
        if not messages:
            return
        # Defensive copy so subsequent caller mutations don't leak in.
        self._turns.append(list(messages))
        if len(self._turns) > self._max_turns:
            dropped = len(self._turns) - self._max_turns
            self._turns = self._turns[dropped:]

    def recent_messages(self) -> list[dict]:
        """Flatten retained turns into a single message list."""
        out: list[dict] = []
        for turn in self._turns:
            out.extend(turn)
        return out

    def turn_count(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()
