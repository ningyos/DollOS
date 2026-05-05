"""Instinct — System 1 per-event preprocessing layer.

Step 5 minimal: only `process()` returning a rolling natural-language summary.
Future steps will extend with first_instinct (step 7 reflex), wake gating, etc.
The summary is injected into the big-model prefill as the STATE block.

Prompt content lives in `dollos/prompts/templates/iv_summary.jinja`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dollos.events import DollEvent
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


class Instinct(ABC):
    """Per-event small-model preprocessing layer (System 1)."""

    @abstractmethod
    async def process(self, event: DollEvent) -> str:
        """Return updated rolling summary for this event.

        Implementations may maintain in-memory state across calls.
        Empty string means "no STATE block" (caller skips injection).
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
