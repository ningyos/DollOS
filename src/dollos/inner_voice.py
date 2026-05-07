"""InnerVoice — small-model VoM RECALL block synthesizer.

Reads from memsearch (markdown SoT + Milvus shadow index) and uses a
small LLM to filter / synthesize a RECALL block. Pure utility — no
state, no event handling, no writes.

Prompt content lives in `dollos/prompts/templates/iv_recall.jinja`
(system + user blocks).
"""

from typing import Protocol

from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


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
        """Return a RECALL block string for the given query.

        Always returns an XML-wrapped block starting with "<recall>\\n" so
        the caller can embed verbatim into a Doll prefill.

        If memsearch returns no hits, returns
        "<recall>\\n(no relevant memories)\\n</recall>\\n" without invoking the LLM.
        """
        k = top_k if top_k is not None else self._default_top_k
        hits = await self._memsearch.search(query, top_k=k)
        if not hits:
            return "<recall>\n(no relevant memories)\n</recall>\n"

        candidates = "\n".join(
            f"{i + 1}. {h['content']}" for i, h in enumerate(hits)
        )
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

        body = "".join(chunks).strip()
        return f"<recall>\n{body}\n</recall>\n"
