#!/usr/bin/env python
"""smoke_mind_idle_sleep.py — idle escalation smoke.

Starts the daemon, connects a WS client but sends NO user input.
Observes for 5 minutes whether:
1. Doll calls Sleep (escalates idle interval) — expected
2. Doll does NOT spam Say messages (say-inhibition via recent_outputs)
3. MindLoop doesn't crash

Pass: at least one Sleep action seen AND no more than 2 unprompted Say messages
in 5 minutes.
Partial: daemon stays alive but Sleep not seen.
Fail: crash or Say spam (>2 unprompted Says).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from datetime import date as _date
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dollos.config import (  # noqa: E402
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS  # noqa: E402

LOG_PATH = Path("/tmp/smoke_mind_idle_sleep.log")
OBSERVE_S = 5 * 60  # 5 minutes


def _setup_logging() -> None:
    LOG_PATH.write_text("")
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)


def _build_settings(tmp_root: Path) -> Settings:
    pack = REPO_ROOT / "character_packs" / "gura"
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp_root / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack),
    )


async def _run() -> int:
    _setup_logging()
    t0 = time.monotonic()
    print(f"[smoke_idle] log → {LOG_PATH}")
    print(f"[smoke_idle] observing for {OBSERVE_S}s")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-idle-"))
    print(f"[smoke_idle] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    # Suppress daily bootstrap schedule writing
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()

    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    import time as _time
    from dollos.mind.mind_state import Perception
    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "cold_start"})
    )

    say_texts: list[str] = []
    crashes = 0

    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke_idle] WS → {uri} (no input will be sent)")

        async with websockets.connect(uri) as ws:
            deadline = time.monotonic() + OBSERVE_S
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
                    msg = json.loads(raw)
                    if msg.get("type") == "text_chunk":
                        say_texts.append(msg.get("text", ""))
                        print(f"[smoke_idle] Say chunk: {msg.get('text', '')[:80]!r}")
                except asyncio.TimeoutError:
                    # Periodic status print
                    elapsed = time.monotonic() - t0
                    iters = dollos._mind_state.iter_count
                    sleep_hint = dollos._mind_state._sleep_hint_until
                    sleep_active = sleep_hint > _time.time()
                    print(
                        f"[smoke_idle] {elapsed:.0f}s — iter={iters}, "
                        f"sleep_hint_active={sleep_active}, "
                        f"say_count={len(say_texts)}"
                    )
    except Exception as e:
        crashes += 1
        print(f"[smoke_idle] CRASH: {e}")
    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    wall = time.monotonic() - t0
    outputs = list(dollos._mind_state.recent_outputs)
    sleep_calls = sum(1 for o in outputs if o.kind == "Sleep")
    idle_calls = sum(1 for o in outputs if o.kind == "Idle")
    say_calls = sum(1 for o in outputs if o.kind == "Say")
    # recent_outputs maxlen=15, so if many iterations happened some may be gone
    total_iter = dollos._mind_state.iter_count

    full_say = "".join(say_texts)
    unprompted_says = say_calls  # everything is unprompted (no user input)

    print("\n========== smoke_mind_idle_sleep RESULT ==========")
    if crashes > 0:
        verdict = "FAIL — crashed"
    elif sleep_calls >= 1 and unprompted_says <= 2:
        verdict = "PASS"
    elif sleep_calls >= 1 and unprompted_says > 2:
        verdict = "PARTIAL — Sleep seen but too many unprompted Says"
    elif unprompted_says <= 2:
        verdict = "PARTIAL — no crash, no say-spam, but Sleep not observed"
    else:
        verdict = "FAIL — say-spam"
    print(f"Verdict: {verdict}")
    print(f"Wall time: {wall:.1f}s")
    print(f"Total iterations: {total_iter}")
    print(f"Sleep calls (in recent_outputs): {sleep_calls}")
    print(f"Idle calls (in recent_outputs): {idle_calls}")
    print(f"Say calls (in recent_outputs): {say_calls}")
    print(f"Unprompted Says: {unprompted_says}")
    sleep_hint_until = dollos._mind_state._sleep_hint_until
    print(f"Final sleep_hint_until: {sleep_hint_until:.0f} (now={_time.time():.0f})")
    print(f"Full say text: {full_say[:300]!r}")
    print(f"Recent outputs: {[(o.kind, o.summary[:50]) for o in outputs]}")
    print(f"Log: {LOG_PATH}")

    return 0 if verdict.startswith("PASS") else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
