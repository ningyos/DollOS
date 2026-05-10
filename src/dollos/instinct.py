"""Instinct — System 1 per-event preprocessing layer.

Active duty: this class is the cascade compactor backbone. After every
finished cascade the dispatcher calls `compact_cascade()` to produce a
1-sentence first-person summary; the summary lands in the dispatcher's
rolling buffer and reappears as a `[Recent activity]` block on the next
turn's first user message (see `docs/superpowers/plans/2026-05-09-rolling-compact.md`).

Mood is no longer produced here (2026-05-10 pivot): the big model writes
mood as part of its `<think>` block (5th field). The dispatcher parses
the last assistant message's `MOOD:` line. Small model only handles the
summary now.

The legacy `process()` method (per-event rolling summary, originally
wired through STATE prefill in step 5) is retained for backwards compat
and possible future wake-gating callers, but is NOT invoked by the
current dispatcher path.

Prompt content lives in:
  - `dollos/prompts/templates/iv_compact.jinja` (compact_cascade)
  - `dollos/prompts/templates/iv_summary.jinja` (legacy process)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dollos.events import DollEvent
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


# Grammar enforcing single-line summary output from compact_cascade.
COMPACT_GRAMMAR = (
    'root ::= line\n'
    'line ::= [^\\n]{1,150} "\\n"\n'
)


class Instinct(ABC):
    """Per-event small-model preprocessing layer (System 1)."""

    @abstractmethod
    async def process(self, event: DollEvent) -> str:
        """Return updated rolling summary for this event.

        Implementations may maintain in-memory state across calls.
        Empty string means "no STATE block" (caller skips injection).

        Legacy path: not currently called by the dispatcher.
        """

    @abstractmethod
    async def compact_cascade(
        self,
        *,
        perception: str,
        cascade_messages: list[dict],
    ) -> str:
        """Compact a finished cascade into a 1-sentence summary.

        Called by the dispatcher after every cascade exits (natural break,
        depth-cap exceed, or same-tool-fail abort). The returned string
        lands in the rolling buffer and surfaces as `[Recent activity]`
        on the next turn's first user message.
        """


class SmallModelInstinct(Instinct):
    """Instinct backed by a small LLM that maintains a rolling summary."""

    def __init__(
        self,
        adapter: LLMAdapter,
        renderer: PromptRenderer,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._last_summary = ""

    async def process(self, event: DollEvent) -> str:
        prev = self._last_summary or "(none — this is the first event)"
        blocks = self._renderer.render_blocks(
            "iv_summary",
            prev_summary=prev,
            perception=event.perception,
        )

        chunks: list[str] = []
        async for chunk in self._adapter.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        self._last_summary = "".join(chunks).strip()
        return self._last_summary

    async def compact_cascade(
        self,
        *,
        perception: str,
        cascade_messages: list[dict],
    ) -> str:
        blocks = self._renderer.render_blocks(
            "iv_compact",
            perception=perception,
            cascade_messages=cascade_messages,
        )

        chunks: list[str] = []
        async for chunk in self._adapter.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
            max_tokens=256,
            grammar=COMPACT_GRAMMAR,
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        return "".join(chunks).strip()
