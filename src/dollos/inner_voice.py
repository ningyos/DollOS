"""InnerVoice — small-model memory recall filter.

Reads from memsearch (markdown SoT + Milvus shadow index) and uses a
small LLM to filter / synthesize relevant facts as plain text. The
dispatcher wraps the returned text in a [Memory context] block; this
class does NOT emit framing labels itself. Pure utility — no state,
no event handling, no writes.

Prompt content lives in `dollos/prompts/templates/iv_recall.jinja`
(system + user blocks).
"""

import re
from typing import Protocol

from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer

_FILE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


class _MemSearchLike(Protocol):
    """Structural interface — anything with this `search` method works.

    memsearch.MemSearch satisfies this; the test fake also satisfies it.
    Avoids hard-importing memsearch at module level (test convenience).
    """
    async def search(self, query: str, top_k: int = ...) -> list[dict]: ...


class InnerVoice:
    """Synthesize VoM RECALL blocks from memsearch using a small LLM."""

    def __init__(
        self,
        memsearch: _MemSearchLike,
        llm: LLMAdapter,
        renderer: PromptRenderer,
        default_top_k: int = 10,
    ) -> None:
        self._memsearch = memsearch
        self._llm = llm
        self._renderer = renderer
        self._default_top_k = default_top_k

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,    # ignored in step 3; reserved for step 10
        top_k: int | None = None,
    ) -> str:
        """Return filtered relevant facts as plain text.

        Dispatcher wraps the result in a [Memory context] block; do not
        add own labels (no "RECALL:" prefix, no "(no relevant memories)"
        wrapping). Returns the small-LLM-filtered output verbatim
        (typically a bullet list or natural prose).

        If memsearch returns no hits, returns "" (empty string) without
        invoking the LLM.
        """
        k = top_k if top_k is not None else self._default_top_k
        hits = await self._memsearch.search(query, top_k=k)
        if not hits:
            return ""

        candidates_parts = []
        for i, h in enumerate(hits):
            src = h.get("source", "")
            m = _FILE_DATE_RE.search(src)
            date_prefix = f"{m.group(1)} " if m else ""
            candidates_parts.append(f"{i + 1}. {date_prefix}{h['content']}")
        candidates = "\n".join(candidates_parts)
        blocks = self._renderer.render_blocks(
            "iv_recall",
            query=query,
            candidates=candidates,
        )

        chunks: list[str] = []
        async for chunk in self._llm.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        return "".join(chunks).strip()
