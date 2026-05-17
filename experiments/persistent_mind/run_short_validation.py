"""Short Persistent Mind validation — verify recent_outputs fix kills say-spam.

5-minute scripted scenario targeted at the say-spam regression. See
`docs` / commit message for context.

Run:
    uv run python experiments/persistent_mind/run_short_validation.py
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

MEMORY_ROOT = Path("/tmp/persistent_mind_short_memory")
TRACE_PATH = Path("/tmp/persistent_mind_short_trace.log")
STATE_PATH = Path("/tmp/persistent_mind_short_state.json")
PROMPT_DUMP = Path("/tmp/persistent_mind_short_prompt_iter5.txt")
PROMPT_DUMP_2 = Path("/tmp/persistent_mind_short_prompt_iter2.txt")


def reset_paths() -> None:
    if MEMORY_ROOT.exists():
        shutil.rmtree(MEMORY_ROOT)
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    (MEMORY_ROOT / "shared").mkdir(parents=True, exist_ok=True)
    for p in (TRACE_PATH, STATE_PATH, PROMPT_DUMP, PROMPT_DUMP_2):
        if p.exists():
            p.unlink()


async def driver_task(loop: MindLoop, timeline, t0: float, stop_evt: asyncio.Event) -> None:
    for offset, kind, payload in timeline:
        delay = (t0 + offset) - time.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        print(f"\n[DRIVER t={offset:.0f}s] inject {kind}: {payload!r}")
        if kind == "UserSpoke":
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

    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=8.0,
        max_sleep=60.0,
        memory=memory,
        say_sink=say_sink,
        trace_path=TRACE_PATH,
        state_path=STATE_PATH,
    )

    # Timeline per the validation spec
    timeline = [
        (0,    "Awoke",     ""),
        (10,   None,        ""),  # idle marker; no inject
        (30,   None,        ""),
        (50,   "UserSpoke", "早"),
        (70,   None,        ""),
        (90,   None,        ""),
        (120,  "UserSpoke", "幫我看 /tmp 有什麼"),
        (150,  None,        ""),
        (180,  None,        ""),
        (210,  "UserSpoke", "OK 謝謝"),
        (240,  None,        ""),
        (270,  None,        ""),
    ]
    # only keep entries with actual injection
    injected = [t for t in timeline if t[1] is not None]

    duration_s = 300.0
    t0 = time.time()
    stop_evt = asyncio.Event()
    drv = asyncio.create_task(driver_task(loop, injected, t0, stop_evt))

    end = t0 + duration_s
    iter_count = 0
    prompt_dumped_iter5 = False
    prompt_dumped_iter2 = False

    while time.time() < end:
        # Snapshot prompt before step for iter 2 / iter 5
        try:
            if loop.state.iter_count == 2 and not prompt_dumped_iter2:
                p = render_mind_prompt(loop.state)
                PROMPT_DUMP_2.write_text(p)
                prompt_dumped_iter2 = True
            if loop.state.iter_count == 5 and not prompt_dumped_iter5:
                p = render_mind_prompt(loop.state)
                PROMPT_DUMP.write_text(p)
                prompt_dumped_iter5 = True
            await loop.step()
            iter_count += 1
        except Exception as e:
            print(f"!! step crashed: {e!r}")

    stop_evt.set()
    drv.cancel()
    try:
        await drv
    except (asyncio.CancelledError, Exception):
        pass

    wall = time.time() - t0

    # Compute metrics
    total_says = len(says)
    say_texts = [s for _, s in says]
    unique_prefixes = set(s[:50] for s in say_texts)
    unique_says = len(unique_prefixes)
    duplicate_ratio = (1 - unique_says / total_says) if total_says else 0.0

    # Detect approximate duplicates (within 60s of same 30-char prefix)
    approx_dups: list[tuple[str, str, float]] = []
    for i in range(len(says)):
        for j in range(i + 1, len(says)):
            t1, s1 = says[i]
            t2, s2 = says[j]
            if abs(t2 - t1) <= 60 and s1[:30] == s2[:30]:
                approx_dups.append((s1, s2, t2 - t1))

    print("\n\n========== METRICS ==========")
    print(f"wall_time_s: {wall:.1f}")
    print(f"total_iters: {iter_count}")
    print(f"total_says: {total_says}")
    print(f"unique_says: {unique_says}")
    print(f"duplicate_ratio: {duplicate_ratio:.2f}")
    print(f"approx_duplicates_within_60s: {len(approx_dups)}")
    for s1, s2, dt in approx_dups:
        print(f"  - dup ({dt:.1f}s apart): {s1[:60]!r} vs {s2[:60]!r}")

    print("\nAll Says:")
    for t, s in says:
        print(f"  [{t - t0:.0f}s] {s[:120]}")

    # Pass criteria
    print("\n--- PASS CHECKS ---")
    c1 = total_says <= 4
    c2 = total_says == unique_says  # zero exact dups
    c3 = len(approx_dups) == 0
    print(f"<=4 Says total: {c1} ({total_says})")
    print(f"zero exact duplicates: {c2}")
    print(f"zero approx duplicates: {c3}")

    verdict = c1 and c2 and c3
    print(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")

    # Save iter5 prompt snippet content for report
    if PROMPT_DUMP.exists():
        print(f"\n--- prompt at iter 5 (first 3000 chars) ---")
        print(PROMPT_DUMP.read_text()[:3000])
        print("--- end iter 5 prompt ---")
    elif PROMPT_DUMP_2.exists():
        print(f"\n--- prompt at iter 2 (first 3000 chars) ---")
        print(PROMPT_DUMP_2.read_text()[:3000])

    memory.close()

    results = {
        "wall_s": wall,
        "iters": iter_count,
        "total_says": total_says,
        "unique_says": unique_says,
        "duplicate_ratio": duplicate_ratio,
        "approx_duplicates": [(s1, s2, dt) for s1, s2, dt in approx_dups],
        "says": [(t - t0, s) for t, s in says],
        "verdict": "PASS" if verdict else "FAIL",
    }
    Path("/tmp/persistent_mind_short_results.json").write_text(
        json.dumps(results, default=str, indent=2, ensure_ascii=False)
    )


if __name__ == "__main__":
    asyncio.run(main())
