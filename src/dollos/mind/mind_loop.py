"""MindLoop — the single persistent coroutine that IS Doll's consciousness."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from dollos.llm.templates import build_mind_actions_grammar
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_prompt import render_mind
from dollos.mind.mind_state import (
    MindState,
    Perception,
    save_state,
)
from dollos.mind.perception_queue import PerceptionQueue

logger = logging.getLogger(__name__)


class MindLoop:
    """The single coroutine that runs Doll's consciousness.

    Lifecycle: spawned once by Kernel at daemon startup. Runs forever
    until shutdown signaled. Each iteration:
      1. Drain perceptions from queue (blocks until at least one arrives)
      2. Auto-sync external state (process_registry → active_tasks, etc.)
      3. Render full MindState as prompt
      4. Call LLM once
      5. Parse 0..N actions
      6. Execute actions (sync inline or async dispatch)
      7. Persist state
    """

    def __init__(
        self,
        *,
        state: MindState,
        queue: PerceptionQueue,
        ctx: MindCtx,
        llm: Any,                    # LLM client with stream_completion API
        system_prompt: str,          # rendered from character pack
        state_persist_path: Path,
        tool_registry: dict[str, type[BaseModel]] | None = None,
    ) -> None:
        self._state = state
        self._queue = queue
        self._ctx = ctx
        self._llm = llm
        self._system_prompt = system_prompt
        self._persist_path = state_persist_path
        self._tool_registry = tool_registry or {}
        self._shutdown = False

        # Build GBNF grammar from action registry to constrain LLM output.
        # Uses prefill="\n\n</think>\n\n" so grammar applies to JSON-only output.
        if self._tool_registry:
            try:
                self._grammar = build_mind_actions_grammar(
                    list(self._tool_registry.keys())
                )
            except Exception:
                logger.exception("failed to build mind actions grammar; running unconstrained")
                self._grammar = None
        else:
            self._grammar = None

    async def run(self) -> None:
        """Main loop. Runs until shutdown."""
        while not self._shutdown:
            try:
                await self.iterate()
            except Exception:
                logger.exception("MindLoop iteration crashed; continuing")

    async def iterate(self) -> None:
        """One iteration: drain → sync → render → llm → execute → persist."""
        perceptions = await self._queue.drain()
        if not perceptions:
            # drain() returned empty — shutdown signaled; skip this iteration
            return
        for p in perceptions:
            self._state.recent_perceptions.append(p)
            if p.kind == "UserSpoke":
                self._state.last_user_at = p.t

        # Auto-sync external state into MindState
        # TODO Task 8.5: ProcessRegistry → state.active_tasks
        # TODO Task 8.5: Schedule → state.pending_events

        # Memsearch query from recent perceptions
        memsearch_hits = await self._derive_memory_hits()

        # Render prompt
        prompt = render_mind(self._state, memsearch_hits, self._system_prompt)

        # Call LLM (single iteration)
        actions = await self._llm_iterate(prompt)

        # Execute actions
        for action in actions:
            try:
                await action.run(self._ctx)
            except Exception:
                logger.exception("action %s failed", type(action).__name__)

        # Update counters + persist
        self._state.iter_count += 1
        self._state.last_iter_at = time.time()
        save_state(self._state, self._persist_path)

    async def _derive_memory_hits(self) -> list[dict]:
        """Query memsearch from the most recent UserSpoke or last 3 perceptions."""
        query = ""
        for p in reversed(self._state.recent_perceptions):
            if p.kind == "UserSpoke":
                query = p.data.get("text", "")
                break
        if not query and len(self._state.recent_perceptions) > 0:
            # fallback: concat last 3 perception bodies
            last3 = list(self._state.recent_perceptions)[-3:]
            query = " ".join(str(p.data) for p in last3)[:500]
        if not query:
            return []
        try:
            return await self._ctx.memsearch.search(query, top_k=10)
        except Exception:
            logger.exception("memsearch query failed; continuing with empty hits")
            return []

    async def _llm_iterate(self, prompt: str) -> list[BaseModel]:
        """Call LLM, parse JSON action array, instantiate pydantic actions."""
        raw = await self._llm_call(prompt)
        return self._parse_actions(raw)

    async def _llm_call(self, prompt: str) -> str:
        """Stream completion from LLM. Adapter to existing LLM provider.

        Uses prefill="\n\n</think>\n\n" to close the thinking block immediately
        so grammar applies to the JSON output only (reasoning bypasses grammar
        in llama-server with --reasoning-format none).
        """
        chunks = []
        async for chunk in self._llm.stream_completion(
            system="",
            user=prompt,
            prefill="\n\n</think>\n\n",
            max_tokens=2048,
            grammar=self._grammar,
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break
        return "".join(chunks)

    def _parse_actions(self, raw: str) -> list[BaseModel]:
        """Parse LLM output. Expect JSON array of action objects. Tolerant fallback."""
        text = raw.strip()

        # Strip any residual <think>...</think> block just in case
        think_end = text.find("</think>")
        if think_end != -1:
            text = text[think_end + len("</think>"):].strip()

        # Strip markdown fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("\n", 1)[0] if "\n" in text else text[:-3]

        # Try strict JSON array parse
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find balanced [...] substring
            m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    data = None
            else:
                data = None

        if data is None:
            # Fallback: treat whole output as a Think action (if present) or empty
            logger.warning("LLM output not JSON; treating as Think")
            think_cls = self._tool_registry.get("Think")
            if think_cls is not None:
                return [think_cls(text=raw[:500])]
            return []

        if not isinstance(data, list):
            data = [data]  # single object → wrap

        # Instantiate pydantic action classes
        actions: list[BaseModel] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # Accept both "action" (canonical) and "type" (scaffolding template compat)
            kind = item.get("action") or item.get("type")
            if not kind:
                continue
            action_cls = self._tool_registry.get(kind)
            if action_cls is None:
                logger.warning("unknown action: %s", kind)
                continue
            discriminator_key = "action" if "action" in item else "type"
            args = {k: v for k, v in item.items() if k != discriminator_key}
            try:
                actions.append(action_cls(**args))
            except ValidationError as e:
                logger.warning("action validation failed for %s: %s", kind, e)
        return actions

    def shutdown(self) -> None:
        """Signal the loop to stop. Unblocks any pending drain()."""
        self._shutdown = True
        self._queue.shutdown()

