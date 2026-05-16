"""Multi-task progress-awareness test for the Persistent Mind prototype.

Scenario: user requests THREE concurrent shells, then probes progress
at three moments. Verify that the [Active tasks] block surfaces the
running shells and that the mind reads it correctly.

Run:
    uv run python experiments/persistent_mind/run_multitask_test.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from llm_client import LLMClient
from memory import Memory
from mind_loop import MindLoop
from perceptions import Awoke, UserSpoke
from prompt import render_mind_prompt


SYSTEM_PROMPT = (HERE / "system_prompt.txt").read_text()

MEMORY_ROOT = Path("/tmp/persistent_mind_multitask_memory")
TRACE_PATH = Path("/tmp/persistent_mind_multitask.log")
STATE_PATH = Path("/tmp/persistent_mind_multitask_state.json")
PROMPT_T11 = Path("/tmp/persistent_mind_multitask_prompt_t11.txt")
PROMPT_T14 = Path("/tmp/persistent_mind_multitask_prompt_t14.txt")
PROMPT_T21 = Path("/tmp/persistent_mind_multitask_prompt_t21.txt")


def reset_paths() -> None:
    if MEMORY_ROOT.exists():
        shutil.rmtree(MEMORY_ROOT)
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    (MEMORY_ROOT / "shared").mkdir(parents=True, exist_ok=True)
    for p in (TRACE_PATH, STATE_PATH, PROMPT_T11, PROMPT_T14, PROMPT_T21):
        if p.exists():
            p.unlink()


# A pending prompt-dump request: when iter begins and a UserSpoke matching
# `marker` is in perceptions, dump rendered prompt to `path`.
class PromptCapture:
    def __init__(self) -> None:
        self.pending: list[tuple[str, Path]] = []  # (marker substring, path)
        self.captured: set[Path] = set()

    def request(self, marker: str, path: Path) -> None:
        self.pending.append((marker, path))


async def driver(loop: MindLoop, timeline, t0: float, stop_evt: asyncio.Event,
                 capture: PromptCapture, prompt_markers: dict) -> None:
    for offset, kind, payload in timeline:
        delay = (t0 + offset) - time.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        wall = time.time() - t0
        print(f"\n[DRIVER t={wall:.1f}s / target={offset}s] inject {kind}: {payload!r}")
        if kind == "UserSpoke":
            # Mark this for prompt capture
            if payload in prompt_markers:
                capture.request(payload, prompt_markers[payload])
            await loop.inject(UserSpoke(payload))
        elif kind == "Awoke":
            await loop.inject(Awoke())


async def main() -> None:
    reset_paths()
    llm = LLMClient()
    if not await llm.health():
        print("BLOCKED: llama-server at 127.0.0.1:8001 not reachable")
        sys.exit(1)
    print(f"LLM healthy: model={llm.model}")

    memory = Memory(MEMORY_ROOT)
    await memory.ensure_indexed()

    says: list[tuple[float, str]] = []
    def say_sink(t: str) -> None:
        says.append((time.time(), t))
        print(f"\n>>> DOLL SAYS: {t}\n")

    # Short idle interval so the mind iterates frequently and can pick up
    # injected UserSpoke perceptions quickly.
    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=1.5,
        max_sleep=10.0,
        memory=memory,
        say_sink=say_sink,
        trace_path=TRACE_PATH,
        state_path=STATE_PATH,
    )

    USER_REQ = (
        "幫我同時跑三件事："
        "1) sleep 12 && echo 'A done'；"
        "2) sleep 8 && echo 'B done'；"
        "3) ls -la /tmp | head -20，把結果留著之後給我看。"
        "請同時派發，不要一件一件做。"
    )
    Q1 = "進度怎麼樣？"
    Q2 = "現在呢？"
    Q3 = "全部好了沒？"

    prompt_markers = {
        Q1: PROMPT_T11,
        Q2: PROMPT_T14,
        Q3: PROMPT_T21,
    }
    capture = PromptCapture()

    timeline = [
        (0,  "Awoke",     ""),
        (5,  "UserSpoke", USER_REQ),
        (11, "UserSpoke", Q1),
        (14, "UserSpoke", Q2),
        (21, "UserSpoke", Q3),
    ]

    duration_s = 28.0
    t0 = time.time()
    stop_evt = asyncio.Event()
    drv = asyncio.create_task(driver(loop, timeline, t0, stop_evt, capture, prompt_markers))

    end = t0 + duration_s
    iter_count = 0

    while time.time() < end:
        try:
            # If a capture is pending and the queued perceptions contain its
            # marker, we want to dump the prompt AFTER drain but BEFORE step.
            # The simplest way: wrap step. But step() drains internally.
            # Instead: peek at the queue before step.
            # We can't easily peek without consuming. Alternative: snapshot
            # state right after step() — but then perceptions are already
            # in state.recent_perceptions, which is exactly what we want for
            # the prompt.
            await loop.step()
            iter_count += 1

            # After step: check if the latest perceptions include any
            # pending marker, and dump the prompt as it was rendered this
            # iteration.
            recent_texts = [
                p.data.get("text", "") for p in list(loop.state.recent_perceptions)[-5:]
                if p.kind == "UserSpoke"
            ]
            remaining: list[tuple[str, Path]] = []
            for marker, path in capture.pending:
                if path in capture.captured:
                    continue
                if any(marker == t for t in recent_texts):
                    # Re-render the prompt as it would be NOW (state already
                    # mutated by step). Best effort: build current memory ctx
                    # too. Simpler: just re-render without memory ctx.
                    p = render_mind_prompt(loop.state)
                    path.write_text(p)
                    capture.captured.add(path)
                    print(f"\n[CAPTURE] dumped prompt for {marker!r} → {path}\n")
                else:
                    remaining.append((marker, path))
            capture.pending = remaining
        except Exception as e:
            print(f"!! step crashed: {e!r}")

    stop_evt.set()
    drv.cancel()
    try:
        await drv
    except (asyncio.CancelledError, Exception):
        pass

    wall = time.time() - t0

    # Look through trace for iter-end records — find dispatch actions
    dispatches: list[dict] = []
    iters_with_dispatch: list[int] = []
    iter_actions: dict[int, list[str]] = {}
    with TRACE_PATH.open() as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "iter_end":
                it = ev.get("iter")
                acts = [a["kind"] for a in ev.get("actions", [])]
                iter_actions[it] = acts
                disps = [a for a in ev.get("actions", []) if a["kind"] == "Dispatch"]
                if disps:
                    iters_with_dispatch.append(it)
                    for d in disps:
                        dispatches.append({"iter": it, "command": d["payload"].get("command", "")})

    print("\n\n========== RESULTS ==========")
    print(f"wall_time_s: {wall:.1f}")
    print(f"total_iters: {iter_count}")
    print(f"total_says: {len(says)}")

    print("\nDispatches recorded:")
    for d in dispatches:
        print(f"  iter={d['iter']}: {d['command'][:90]}")
    print(f"\nNum dispatches: {len(dispatches)}")
    print(f"Iters with dispatch: {iters_with_dispatch}")
    print(f"Dispatched in N distinct iters: {len(set(iters_with_dispatch))}")

    print("\nAll Says (offset s):")
    for t, s in says:
        print(f"  [{t - t0:.1f}s] {s[:200]}")

    # Save concise summary
    Path("/tmp/persistent_mind_multitask_results.json").write_text(json.dumps({
        "wall_s": wall,
        "iters": iter_count,
        "total_says": len(says),
        "says": [(round(t - t0, 1), s) for t, s in says],
        "dispatches": dispatches,
        "iters_with_dispatch": iters_with_dispatch,
        "n_distinct_dispatch_iters": len(set(iters_with_dispatch)),
        "iter_actions": iter_actions,
    }, ensure_ascii=False, indent=2))

    print("\n--- prompt at t=11 (Q1) ---")
    if PROMPT_T11.exists():
        print(PROMPT_T11.read_text()[:4000])
    else:
        print("(NOT CAPTURED)")
    print("\n--- prompt at t=14 (Q2) ---")
    if PROMPT_T14.exists():
        print(PROMPT_T14.read_text()[:4000])
    else:
        print("(NOT CAPTURED)")
    print("\n--- prompt at t=21 (Q3) ---")
    if PROMPT_T21.exists():
        print(PROMPT_T21.read_text()[:4000])
    else:
        print("(NOT CAPTURED)")

    memory.close()


if __name__ == "__main__":
    asyncio.run(main())
