"""Run the three persistent-mind validation scenarios sequentially."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make sibling modules importable when run as a script
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from llm_client import LLMClient
from mind_loop import MindLoop, TRACE_PATH, STATE_PATH
from perceptions import Awoke, UserSpoke


SYSTEM_PROMPT = (HERE / "system_prompt.txt").read_text()


def banner(msg: str) -> None:
    line = "=" * 70
    print(f"\n{line}\n{msg}\n{line}")


async def scenario_1(llm: LLMClient) -> dict:
    banner("SCENARIO 1: find line 150 (multi-turn dispatch + loop matching)")
    loop = MindLoop(llm, SYSTEM_PROMPT, idle_interval=5.0)
    await loop.inject(Awoke())
    t0 = time.time()

    # Iter 1: should idle (no real input yet)
    await loop.step()

    # Inject user message
    print("\n[INJECT] UserSpoke('run `seq 1 200` and tell me line 150')")
    await loop.inject(UserSpoke("run `seq 1 200` and tell me line 150"))

    # Iter 2: should dispatch shell + open loop
    await loop.step()

    # Wait for shell result (push back into queue) and let it land
    # Run up to 4 more iters to give the mind time to see the result
    for _ in range(4):
        await loop.step()
        if not loop.state.open_loops and not loop.shell.snapshot():
            # nothing pending, but give one more chance to see ToolResult
            break

    wall = time.time() - t0
    return {
        "scenario": 1,
        "wall_s": round(wall, 1),
        "iters": loop.state.iter_count,
        "open_loops_remaining": list(loop.state.open_loops),
        "active_tasks_remaining": loop.shell.snapshot(),
    }


async def scenario_2(llm: LLMClient) -> dict:
    banner("SCENARIO 2: idle thinking (no input for ~30s)")
    loop = MindLoop(llm, SYSTEM_PROMPT, idle_interval=5.0)
    await loop.inject(Awoke())
    t0 = time.time()

    # Iter 1: Awoke
    await loop.step()

    # Now just let it idle for several ticks (each ~5s timeout)
    for _ in range(4):
        await loop.step()

    wall = time.time() - t0
    return {
        "scenario": 2,
        "wall_s": round(wall, 1),
        "iters": loop.state.iter_count,
        "final_focus": loop.state.focus,
        "thoughts": list(loop.state.recent_thoughts),
    }


async def scenario_3(llm: LLMClient) -> dict:
    banner("SCENARIO 3: interrupt during shell")
    loop = MindLoop(llm, SYSTEM_PROMPT, idle_interval=4.0)
    await loop.inject(Awoke())
    t0 = time.time()

    # Iter 1: Awoke
    await loop.step()

    # Inject first user message: long-running shell
    print("\n[INJECT] UserSpoke('run `sleep 5` and tell me when done')")
    await loop.inject(UserSpoke("run `sleep 5` and tell me when done"))

    # Iter 2: should dispatch sleep + open loop
    await loop.step()

    # Wait 2s then inject interrupt
    await asyncio.sleep(2.0)
    print("\n[INJECT] UserSpoke('wait, what was I asking?')")
    await loop.inject(UserSpoke("wait, what was I asking?"))

    # Iter 3: should see active_task (sleep still running) + new user perception
    await loop.step()

    # Continue until shell completes + result observed
    for _ in range(4):
        await loop.step()
        if not loop.shell.snapshot() and not loop.state.open_loops:
            break

    wall = time.time() - t0
    return {
        "scenario": 3,
        "wall_s": round(wall, 1),
        "iters": loop.state.iter_count,
        "open_loops_remaining": list(loop.state.open_loops),
    }


async def main() -> None:
    # Reset trace + state
    if TRACE_PATH.exists():
        TRACE_PATH.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    llm = LLMClient()
    if not await llm.health():
        print("BLOCKED: llama-server at 127.0.0.1:8001 not reachable")
        sys.exit(1)
    print(f"LLM healthy: model={llm.model}")

    results = []
    for fn in (scenario_1, scenario_2, scenario_3):
        r = await fn(llm)
        print(f"\n[RESULT {fn.__name__}] {json.dumps(r, default=str, indent=2)}")
        results.append(r)

    print("\n" + "=" * 70)
    print("ALL SCENARIOS DONE")
    print("=" * 70)
    print(json.dumps(results, default=str, indent=2))
    print(f"\nTrace log: {TRACE_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
