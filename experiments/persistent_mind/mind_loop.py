"""MindLoop: the persistent coroutine."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable

from actions import Action, parse_actions
from llm_client import LLMClient
from mind_state import MindState
from perceptions import IdleTick, Perception
from prompt import render_mind_prompt
from shell_runner import ShellRunner


TRACE_PATH = Path("/tmp/persistent_mind_experiment.log")
STATE_PATH = Path("/tmp/persistent_mind_state.json")


def trace(event: dict) -> None:
    event["t"] = time.time()
    with TRACE_PATH.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


class MindLoop:
    def __init__(
        self,
        llm: LLMClient,
        system_prompt: str,
        idle_interval: float = 10.0,
        max_sleep: float = 60.0,
        say_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.system_prompt = system_prompt
        self.idle_interval = idle_interval
        self.max_sleep = max_sleep
        self.queue: asyncio.Queue[Perception] = asyncio.Queue()
        self.state = MindState()
        self.shell = ShellRunner(self.queue)
        self.running = False
        self.say_sink = say_sink or (lambda txt: print(f"\n>>> DOLL SAYS: {txt}\n"))
        self._sleep_extra = 0.0

    async def inject(self, perception: Perception) -> None:
        if perception.kind == "UserSpoke":
            self.state.last_user_at = time.time()
        await self.queue.put(perception)

    async def _drain(self, timeout: float) -> list[Perception]:
        perceptions: list[Perception] = []
        # Block on first one (or timeout)
        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            perceptions.append(first)
        except asyncio.TimeoutError:
            return []
        # Drain remaining without blocking
        while True:
            try:
                p = self.queue.get_nowait()
                perceptions.append(p)
            except asyncio.QueueEmpty:
                break
        return perceptions

    async def _execute(self, action: Action) -> None:
        k = action.kind
        p = action.payload
        trace({"event": "action_exec", "kind": k, "payload": p})

        if k == "Say":
            text = p.get("text", "")
            self.say_sink(text)
        elif k == "Think":
            text = p.get("text", "")
            self.state.recent_thoughts.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        elif k == "SetFocus":
            self.state.focus = p.get("focus", self.state.focus)
        elif k == "OpenLoop":
            lid = p.get("id", "")
            desc = p.get("desc", "")
            if lid:
                self.state.open_loop(lid, desc)
        elif k == "CloseLoop":
            lid = p.get("id", "")
            outcome = p.get("outcome", "")
            self.state.close_loop(lid, outcome)
            self.state.recent_thoughts.append(
                f"[{time.strftime('%H:%M:%S')}] closed loop {lid}: {outcome}"
            )
        elif k == "Dispatch":
            tool = p.get("tool", "Shell")
            if tool == "Shell":
                cmd = p.get("command", "")
                timeout_s = float(p.get("timeout_s", 30))
                did = self.shell.dispatch(cmd, timeout_s)
                trace({"event": "dispatch", "tool": "Shell", "command": cmd, "dispatch_id": did})
        elif k == "Idle":
            pass
        elif k == "Sleep":
            secs = float(p.get("seconds", self.idle_interval))
            self._sleep_extra = min(secs, self.max_sleep)
            trace({"event": "sleep_request", "seconds": self._sleep_extra})

    async def step(self) -> None:
        """One iteration: drain → render → call LLM → parse → execute."""
        timeout = self.idle_interval + self._sleep_extra
        self._sleep_extra = 0.0

        perceptions = await self._drain(timeout)
        if not perceptions:
            perceptions = [IdleTick()]

        self.state.recent_perceptions.extend(perceptions)
        self.state.active_tasks = self.shell.snapshot()
        # Energy decay
        self.state.energy = max(0.0, self.state.energy - 0.01)
        if any(p.kind == "UserSpoke" for p in perceptions):
            self.state.energy = min(1.0, self.state.energy + 0.2)

        user_prompt = render_mind_prompt(self.state)

        iter_id = self.state.iter_count
        trace({
            "event": "iter_begin",
            "iter": iter_id,
            "new_perceptions": [{"kind": p.kind, "data": p.data} for p in perceptions],
            "active_tasks": self.state.active_tasks,
            "open_loops": list(self.state.open_loops),
            "focus": self.state.focus,
        })

        # Print summary to stdout
        print(f"\n===== ITER {iter_id} @ {time.strftime('%H:%M:%S')} =====")
        print(f"perceptions: {[p.kind for p in perceptions]}")
        print(f"active_tasks: {len(self.state.active_tasks)}, open_loops: {[l['id'] for l in self.state.open_loops]}")
        print(f"focus={self.state.focus} mood={self.state.mood} energy={self.state.energy:.2f}")

        # Truncate prompt for stdout (still log full to trace)
        prompt_preview = user_prompt[:600] + ("...[truncated]" if len(user_prompt) > 600 else "")
        print(f"---- prompt (preview) ----\n{prompt_preview}\n--------")

        try:
            text, stats = await self.llm.chat(self.system_prompt, user_prompt)
        except Exception as e:
            trace({"event": "llm_error", "error": repr(e)})
            print(f"!! LLM error: {e!r}")
            self.state.iter_count += 1
            return

        print(f"---- model output ----\n{text[:500]}\n--------")
        print(f"stats: {stats}")

        actions, fallback = parse_actions(text)
        if fallback:
            actions = [Action("Think", {"text": f"(unparsed) {fallback[:300]}"})]
            print(f"  (fallback → Think)")

        print(f"parsed actions: {[a.kind for a in actions]}")
        trace({
            "event": "iter_end",
            "iter": iter_id,
            "raw_output": text,
            "actions": [{"kind": a.kind, "payload": a.payload} for a in actions],
            "stats": stats,
        })

        for a in actions:
            await self._execute(a)

        self.state.last_iter_at = time.time()
        self.state.iter_count += 1
        self.state.persist(STATE_PATH)

    async def run_for(self, duration_s: float) -> None:
        self.running = True
        end = time.time() + duration_s
        while self.running and time.time() < end:
            await self.step()

    def stop(self) -> None:
        self.running = False
