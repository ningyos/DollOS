"""Extended Persistent Mind validation.

Phase A: Memory SoT integration (Scenarios 4-6)
Phase B: 30-min long-running stability run

Run with:
    uv run python experiments/persistent_mind/run_extended.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from llm_client import LLMClient
from memory import Memory
from mind_loop import MindLoop
from mind_state import MindState
from perceptions import Awoke, UserSpoke


SYSTEM_PROMPT = (HERE / "system_prompt.txt").read_text()

# Isolated experiment paths
MEMORY_ROOT = Path("/tmp/persistent_mind_memory")
TRACE_PATH_A = Path("/tmp/persistent_mind_phaseA.log")
TRACE_PATH_B = Path("/tmp/persistent_mind_phaseB.log")
LONGRUN_METRICS = Path("/tmp/persistent_mind_longrun.log")
STATE_PATH_A = Path("/tmp/persistent_mind_state_A.json")
STATE_PATH_S6 = Path("/tmp/persistent_mind_state_scenario6.json")
STATE_PATH_B = Path("/tmp/persistent_mind_state_B.json")

SEED_FACTS = """\
- 主人喜歡早上喝手沖咖啡，平時喝燕麥奶拿鐵
- 主人正在用 Blender 做 Powdur 的 3D 模型，卡在頭髮的部分
- 主人的母親住在高雄，每月來訪一次
- 主人的貓 Mochi 是 4 歲棕色虎斑貓
- DollOS 跑在雙 RTX 4060 Ti 的工作站上
- 主人的生日是 11 月 25 日
- 主人最近在學日文，準備明年去東京
- 主人偏好深色 IDE 主題，使用 Neovim
- 主人對花生過敏（嚴重）
- 主人習慣晚上 1 點睡覺
"""


def banner(msg: str) -> None:
    line = "=" * 70
    print(f"\n{line}\n{msg}\n{line}")


def reset_paths() -> None:
    if MEMORY_ROOT.exists():
        shutil.rmtree(MEMORY_ROOT)
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    (MEMORY_ROOT / "shared").mkdir(parents=True, exist_ok=True)
    # Seed today's markdown
    import datetime as _dt

    seed_file = MEMORY_ROOT / "shared" / f"{_dt.date.today().isoformat()}.md"
    seed_file.write_text(SEED_FACTS, encoding="utf-8")
    for p in (TRACE_PATH_A, TRACE_PATH_B, LONGRUN_METRICS, STATE_PATH_A, STATE_PATH_S6, STATE_PATH_B):
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# Phase A
# ---------------------------------------------------------------------------

async def scenario_4(llm: LLMClient, memory: Memory) -> dict:
    banner("SCENARIO 4: Memory recall during conversation (auto-RAG)")
    says: list[str] = []
    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=4.0, memory=memory,
        say_sink=lambda t: (says.append(t), print(f"\n>>> DOLL SAYS: {t}\n")),
        trace_path=TRACE_PATH_A, state_path=STATE_PATH_A,
    )
    await loop.inject(Awoke())
    await loop.step()  # iter 0: Awoke

    print("\n[INJECT] UserSpoke('what does the user like to drink in the morning?')")
    await loop.inject(UserSpoke("what does the user like to drink in the morning?"))
    for _ in range(3):
        await loop.step()
        if says:
            break
    return {"scenario": 4, "iters": loop.state.iter_count, "says": says}


async def scenario_5(llm: LLMClient, memory: Memory) -> dict:
    banner("SCENARIO 5: Voluntary NoteMemory + later Recall")
    says: list[str] = []
    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=4.0, memory=memory,
        say_sink=lambda t: (says.append(t), print(f"\n>>> DOLL SAYS: {t}\n")),
        trace_path=TRACE_PATH_A, state_path=STATE_PATH_A,
    )
    await loop.inject(Awoke())
    await loop.step()

    facts_before = memory.fact_count()

    print("\n[INJECT] UserSpoke('幫我記下：我習慣用 25°C 冷氣')")
    await loop.inject(UserSpoke("幫我記下：我習慣用 25°C 冷氣"))
    for _ in range(3):
        await loop.step()
        if memory.write_count >= 1:
            break

    # Wait a moment for re-index
    await asyncio.sleep(0.5)
    facts_after_note = memory.fact_count()

    says_before_recall = len(says)
    print("\n[INJECT] UserSpoke('我喜歡什麼溫度的冷氣？')")
    await loop.inject(UserSpoke("我喜歡什麼溫度的冷氣？"))
    for _ in range(3):
        await loop.step()
        if len(says) > says_before_recall:
            break

    # Persist for scenario 6
    loop.state.persist(STATE_PATH_S6)

    return {
        "scenario": 5,
        "iters": loop.state.iter_count,
        "memory_writes": memory.write_count,
        "facts_before": facts_before,
        "facts_after_note": facts_after_note,
        "says": says,
    }


async def scenario_6(llm: LLMClient, memory: Memory) -> dict:
    banner("SCENARIO 6: Cross-session persistence")
    # Spawn a NEW MindLoop loading the persisted state from scenario 5
    new_state = MindState.load(STATE_PATH_S6)
    print(f"[loaded state] iter_count={new_state.iter_count}, "
          f"recent_perceptions={len(new_state.recent_perceptions)}, "
          f"recent_thoughts={len(new_state.recent_thoughts)}")

    says: list[str] = []
    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=4.0, memory=memory,
        say_sink=lambda t: (says.append(t), print(f"\n>>> DOLL SAYS: {t}\n")),
        trace_path=TRACE_PATH_A, state_path=STATE_PATH_A,
    )
    loop.state = new_state  # adopt persisted state

    print("\n[INJECT] UserSpoke('what was I asking about earlier?')")
    await loop.inject(UserSpoke("what was I asking about earlier?"))
    for _ in range(3):
        await loop.step()
        if says:
            break

    # Now test the 25°C fact survives across the session boundary
    says_mark = len(says)
    print("\n[INJECT] UserSpoke('我之前設定冷氣幾度？')")
    await loop.inject(UserSpoke("我之前設定冷氣幾度？"))
    for _ in range(3):
        await loop.step()
        if len(says) > says_mark:
            break

    return {
        "scenario": 6,
        "loaded_iter_count": new_state.iter_count,
        "loaded_recent_perceptions": len(new_state.recent_perceptions),
        "says": says,
    }


# ---------------------------------------------------------------------------
# Phase B — 30 min long-run
# ---------------------------------------------------------------------------

async def driver_task(loop: MindLoop, timeline: list[tuple[float, str, str]], t0: float, stop_evt: asyncio.Event) -> None:
    """Wall-clock event driver. timeline = [(t_offset_sec, kind, payload)]."""
    for offset, kind, payload in timeline:
        delay = (t0 + offset) - time.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=delay)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
        print(f"\n[DRIVER t={offset:.0f}s] inject {kind}: {payload!r}")
        if kind == "UserSpoke":
            await loop.inject(UserSpoke(payload))
        elif kind == "Awoke":
            await loop.inject(Awoke())


async def phase_b_longrun(llm: LLMClient, memory: Memory, duration_s: float) -> dict:
    banner(f"PHASE B: long-running stability ({duration_s/60:.0f} min)")

    # Open metrics sink
    metrics_fp = LONGRUN_METRICS.open("a")
    def metrics_sink(rec: dict) -> None:
        metrics_fp.write(json.dumps(rec, default=str) + "\n")
        metrics_fp.flush()

    says: list[tuple[float, str]] = []
    notes: list[float] = []
    def say_sink(t: str) -> None:
        says.append((time.time(), t))
        print(f"\n>>> DOLL SAYS: {t}\n")

    loop = MindLoop(
        llm, SYSTEM_PROMPT,
        idle_interval=8.0,
        max_sleep=90.0,
        memory=memory,
        say_sink=say_sink,
        trace_path=TRACE_PATH_B,
        state_path=STATE_PATH_B,
        metrics_sink=metrics_sink,
    )

    # Event timeline (offsets in seconds from t0)
    timeline = [
        (0,    "Awoke",     ""),
        (30,   "UserSpoke", "早安"),
        (60,   "UserSpoke", "幫我想想 blender 頭髮的問題"),
        # idle 90s
        (210,  "UserSpoke", "我需要跑個 shell 看磁碟用量"),
        (240,  "UserSpoke", "順便看看 CPU"),  # mid-shell
        (300,  "UserSpoke", "都好嗎？"),
        # long idle (4 min)
        (660,  "UserSpoke", "你還在嗎？"),
        (900,  "UserSpoke", "記下：我下週要去日本"),
        (1020, "UserSpoke", "我下週要去哪？"),  # memory recall
        (1200, "UserSpoke", "幫我跑 'sleep 30' 並通知我"),
        # 60s in, shell still running
        (1320, "UserSpoke", "改主意了，跑另一個 'echo hello'"),
        # long idle (3 min)
        (1790, "UserSpoke", "我要睡了，晚安"),
    ]

    t0 = time.time()
    stop_evt = asyncio.Event()
    drv = asyncio.create_task(driver_task(loop, timeline, t0, stop_evt))

    end = t0 + duration_s
    iter_count = 0
    crash_count = 0
    while time.time() < end:
        try:
            await loop.step()
            iter_count += 1
        except Exception as e:
            crash_count += 1
            print(f"!! step crashed: {e!r}")
        # Print progress every 10 iters
        if iter_count % 10 == 0:
            elapsed = time.time() - t0
            print(f"[longrun progress] elapsed={elapsed:.0f}s iters={iter_count} "
                  f"facts={memory.fact_count()} writes={memory.write_count}")

    stop_evt.set()
    drv.cancel()
    try:
        await drv
    except (asyncio.CancelledError, Exception):
        pass

    metrics_fp.close()

    return {
        "iters": iter_count,
        "crashes": crash_count,
        "wall_s": time.time() - t0,
        "says_count": len(says),
        "says": [s for _, s in says],
        "memory_writes": memory.write_count,
        "memory_facts": memory.fact_count(),
        "open_loops_remaining": list(loop.state.open_loops),
        "active_tasks_remaining": loop.shell.snapshot(),
    }


def summarize_longrun() -> dict:
    """Read LONGRUN_METRICS jsonl and compute end-of-run stats + ascii charts."""
    rows = []
    with LONGRUN_METRICS.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    if not rows:
        return {"rows": 0}

    total_prompt = sum((r.get("prompt_tokens") or 0) for r in rows)
    total_completion = sum((r.get("completion_tokens") or 0) for r in rows)
    total_llm_s = sum((r.get("llm_latency_s") or 0) for r in rows)
    t0 = rows[0]["t"]
    tN = rows[-1]["t"]
    wall = tN - t0
    max_prompt_tokens = max((r.get("prompt_tokens") or 0) for r in rows)
    max_prompt_chars = max(r.get("prompt_chars", 0) for r in rows)
    max_recent_perc = max(r.get("recent_perceptions", 0) for r in rows)
    max_open_loops = max(r.get("open_loops", 0) for r in rows)
    max_state_bytes = max(r.get("state_bytes", 0) for r in rows)

    def ascii_chart(key: str, width: int = 60, height: int = 8) -> str:
        vals = [r.get(key) or 0 for r in rows]
        if not vals or max(vals) == 0:
            return f"(no data for {key})"
        lo, hi = min(vals), max(vals)
        # bucket into width columns
        bucket_size = max(1, len(vals) / width)
        buckets = []
        for i in range(width):
            start = int(i * bucket_size)
            stop = int((i + 1) * bucket_size)
            chunk = vals[start:stop] or [vals[min(start, len(vals)-1)]]
            buckets.append(sum(chunk) / len(chunk))
        bhi = max(buckets) or 1
        blo = min(buckets)
        lines = []
        for h in range(height, 0, -1):
            row_thresh = blo + (bhi - blo) * (h / height)
            line = "".join("#" if b >= row_thresh else " " for b in buckets)
            lines.append(f"{row_thresh:7.1f} |{line}|")
        lines.append("        +" + "-" * width + "+")
        lines.append(f"        0{'':>{width-3}}t={wall:.0f}s")
        return f"{key} (min={lo:.1f} max={hi:.1f}):\n" + "\n".join(lines)

    return {
        "rows": len(rows),
        "wall_s": round(wall, 1),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_llm_time_s": round(total_llm_s, 1),
        "llm_busy_fraction": round(total_llm_s / wall, 3) if wall > 0 else 0,
        "max_prompt_tokens": max_prompt_tokens,
        "max_prompt_chars": max_prompt_chars,
        "max_recent_perceptions": max_recent_perc,
        "max_open_loops": max_open_loops,
        "max_state_bytes": max_state_bytes,
        "charts": {
            "prompt_tokens": ascii_chart("prompt_tokens"),
            "recent_perceptions": ascii_chart("recent_perceptions"),
            "energy": ascii_chart("energy"),
            "open_loops": ascii_chart("open_loops"),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    long_run_min = float(os.environ.get("LONGRUN_MIN", "30"))

    reset_paths()
    llm = LLMClient()
    if not await llm.health():
        print("BLOCKED: llama-server at 127.0.0.1:8001 not reachable")
        sys.exit(1)
    print(f"LLM healthy: model={llm.model}")

    memory = Memory(MEMORY_ROOT)
    n_indexed = await memory.ensure_indexed()
    print(f"Memory seeded, indexed {n_indexed} chunks from {memory.fact_count()} facts.")

    results: dict = {}

    # --- Phase A ---
    r4 = await scenario_4(llm, memory)
    print(f"\n[RESULT scenario_4] {json.dumps(r4, default=str, indent=2, ensure_ascii=False)}")
    results["scenario_4"] = r4

    r5 = await scenario_5(llm, memory)
    print(f"\n[RESULT scenario_5] {json.dumps(r5, default=str, indent=2, ensure_ascii=False)}")
    results["scenario_5"] = r5

    r6 = await scenario_6(llm, memory)
    print(f"\n[RESULT scenario_6] {json.dumps(r6, default=str, indent=2, ensure_ascii=False)}")
    results["scenario_6"] = r6

    # --- Phase B ---
    rb = await phase_b_longrun(llm, memory, duration_s=long_run_min * 60)
    print(f"\n[RESULT phase_b] {json.dumps(rb, default=str, indent=2, ensure_ascii=False)}")
    results["phase_b"] = rb

    summary = summarize_longrun()
    print(f"\n[LONGRUN SUMMARY] " + json.dumps({k: v for k, v in summary.items() if k != 'charts'}, indent=2))
    if "charts" in summary:
        for k, chart in summary["charts"].items():
            print(f"\n--- chart: {k} ---\n{chart}")
    results["longrun_summary"] = {k: v for k, v in summary.items() if k != "charts"}

    out = Path("/tmp/persistent_mind_extended_results.json")
    out.write_text(json.dumps(results, default=str, indent=2, ensure_ascii=False))
    print(f"\nResults saved to {out}")
    print(f"Phase A trace: {TRACE_PATH_A}")
    print(f"Phase B trace: {TRACE_PATH_B}")
    print(f"Long-run metrics: {LONGRUN_METRICS}")

    memory.close()


if __name__ == "__main__":
    asyncio.run(main())
