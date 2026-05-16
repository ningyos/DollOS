#!/usr/bin/env python
"""Multi-turn end-to-end smoke against the real DollOS daemon.

Spins up DollOS in-process (Gura pack), opens one WS client, sends 6
prompts sequentially (wait for each `turn_end`), then dumps full trace +
observations.

Goal: exercise the broader behavior surface (paging + scratchpad +
conversation_history + IV-removal) and surface oddities. Observational
only — no asserts.
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

import structlog
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dollos.config import (  # noqa: E402
    CharacterConfig,
    ConversationHistoryConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS  # noqa: E402

PROMPTS = [
    "你好",
    "我最近在用 blender 做一個 powdur 的模型，正在卡頭髮的部分",
    "幫我跑 'echo hello && ls /tmp | head -20'，告訴我裡面有沒有 dollos 相關的東西",
    "我剛剛跟你說我在做什麼？",
    "記下這個：我習慣用 25°C 冷氣",
    "等等，先停下。",
]
LOG_PATH = Path("/tmp/doll_multiturn_e2e.log")
PER_TURN_TIMEOUT_S = 180.0


def _setup_logging(log_path: Path) -> None:
    log_path.write_text("")
    fh = logging.FileHandler(log_path)
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
        conversation_history=ConversationHistoryConfig(max_turns=12),
    )


async def _drain_until_turn_end(ws, deadline_s: float, captured: list[dict]) -> tuple[str, bool]:
    """Read messages until a turn_end arrives (or deadline). Append all to
    ``captured``. Return (current_say_accumulated, timed_out)."""
    current_say = ""
    while True:
        remaining = deadline_s - time.monotonic()
        if remaining <= 0:
            return current_say, True
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return current_say, True
        msg = json.loads(raw)
        captured.append(msg)
        t = msg.get("type")
        if t == "text_chunk":
            current_say += msg.get("text", "")
        elif t == "turn_end":
            # Brief drain for any straggling messages right after turn_end.
            try:
                while True:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    msg2 = json.loads(raw2)
                    captured.append(msg2)
                    if msg2.get("type") == "text_chunk":
                        current_say += msg2.get("text", "")
            except asyncio.TimeoutError:
                pass
            return current_say, False


async def _run() -> int:
    _setup_logging(LOG_PATH)
    print(f"[smoke] log → {LOG_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-multiturn-"))
    print(f"[smoke] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    # Skip daily bootstrap — we want only user turns.
    dollos._bootstrapped_dates.add(_date.today())

    # Hook structlog to capture cascade records into an ordered list.
    cascade_records: list[dict] = []

    def _capture_processor(logger, method_name, event_dict):
        cascade_records.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _capture_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=open(LOG_PATH, "a")),
        cache_logger_on_first_use=False,
    )

    await dollos.memsearch.index()
    await dollos.server.start()

    # Per-turn book-keeping.
    turns_meta: list[dict] = []  # {prompt, say, wall_s, cascade_start, cascade_end}
    ws_messages_all: list[dict] = []
    overall_t0 = time.monotonic()

    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke] WS → {uri}")

        async with websockets.connect(uri) as ws:
            for i, prompt in enumerate(PROMPTS):
                print(f"\n[smoke] === turn {i + 1}/{len(PROMPTS)}: {prompt!r}")
                cascade_start = len(cascade_records)
                turn_t0 = time.monotonic()
                await ws.send(json.dumps({"type": "text_input", "text": prompt}))
                deadline = time.monotonic() + PER_TURN_TIMEOUT_S
                say_text, timed_out = await _drain_until_turn_end(
                    ws, deadline, ws_messages_all
                )
                turn_wall = time.monotonic() - turn_t0
                cascade_end = len(cascade_records)
                print(
                    f"[smoke]   say={say_text[:120]!r}  "
                    f"wall={turn_wall:.1f}s  "
                    f"timed_out={timed_out}  "
                    f"cascade_iters={cascade_end - cascade_start}"
                )
                turns_meta.append({
                    "prompt": prompt,
                    "say": say_text,
                    "wall_s": turn_wall,
                    "timed_out": timed_out,
                    "cascade_start": cascade_start,
                    "cascade_end": cascade_end,
                    "history_turn_count_after": dollos._conversation_history.turn_count(),
                    "scratchpad_after": dollos._scratchpad.read(),
                })
    finally:
        final_history = dollos._conversation_history.turn_count()
        final_scratchpad = dollos._scratchpad.read()
        await dollos.server.stop()

    overall_wall = time.monotonic() - overall_t0

    # Discover memory files written.
    memory_root = tmp_root / "data" / "memory"
    memory_files: list[tuple[str, str]] = []
    if memory_root.exists():
        for p in sorted(memory_root.rglob("*.md")):
            try:
                memory_files.append((str(p.relative_to(tmp_root)), p.read_text()))
            except OSError:
                pass

    # ============================ REPORT ============================
    print("\n\n========== PER-TURN REPORT ==========")
    for i, meta in enumerate(turns_meta):
        tool_seq: list[tuple[str, dict]] = []
        for rec in cascade_records[meta["cascade_start"]:meta["cascade_end"]]:
            for tc in rec.get("tool_calls") or []:
                tool_seq.append((tc.get("name") or "?", tc.get("args") or {}))
        print(f"\n--- TURN {i + 1} ---")
        print(f"prompt:           {meta['prompt']}")
        print(f"wall:             {meta['wall_s']:.1f}s  (timed_out={meta['timed_out']})")
        print(f"cascade iters:    {meta['cascade_end'] - meta['cascade_start']}")
        print(f"tool calls ({len(tool_seq)}):")
        for j, (n, a) in enumerate(tool_seq):
            ja = json.dumps(a, ensure_ascii=False)
            if len(ja) > 240:
                ja = ja[:240] + "..."
            print(f"  {j}. {n}({ja})")
        say_short = meta["say"].strip()[:200]
        print(f"say (≤200):       {say_short!r}")
        print(f"history.turn_count() after: {meta['history_turn_count_after']}")
        sp = meta["scratchpad_after"].strip()
        if sp:
            print(f"scratchpad after (≤300): {sp[:300]!r}")

    print("\n========== DAEMON STATE AT END ==========")
    print(f"history.turn_count(): {final_history}")
    print(f"scratchpad (full):\n{final_scratchpad or '(empty)'}")

    print("\n========== MEMORY FILES WRITTEN ==========")
    if not memory_files:
        print("(none)")
    for rel, content in memory_files:
        print(f"\n--- {rel} ---")
        print(content.rstrip())

    print("\n========== FULL CASCADE TRACE ==========")
    for i, rec in enumerate(cascade_records):
        tcs = rec.get("tool_calls") or []
        results = rec.get("results") or []
        if not (
            rec.get("turn_id") or rec.get("iter") is not None or tcs or results
            or rec.get("intent") or rec.get("review") or rec.get("seen")
        ):
            continue
        print(
            f"\n--- iter {i} (turn={rec.get('turn_id')}, "
            f"iter#={rec.get('iter')}, dur={rec.get('duration_ms')}ms, "
            f"event={rec.get('event')}) ---"
        )
        for k in ("seen", "intent", "review", "mood", "tool"):
            v = rec.get(k)
            if v:
                vs = str(v)
                if len(vs) > 400:
                    vs = vs[:400] + "..."
                print(f"  {k.upper()}: {vs}")
        for tc in tcs:
            args_s = json.dumps(tc.get("args"), ensure_ascii=False)
            if len(args_s) > 300:
                args_s = args_s[:300] + "..."
            print(f"  call  -> {tc.get('name')}({args_s})")
        for r in results:
            det = (r.get("detail") or "")
            if len(det) > 300:
                det = det[:300] + "..."
            print(f"  result <- {r.get('tool_name')} success={r.get('success')} detail={det!r}")

    print(f"\n[smoke] overall wall: {overall_wall:.1f}s")
    print(f"[smoke] log: {LOG_PATH}")

    # Persist trace.
    with LOG_PATH.open("a") as f:
        f.write("\n\n========== WS MESSAGES ==========\n")
        for m in ws_messages_all:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        f.write("\n========== CASCADE RECORDS ==========\n")
        for rec in cascade_records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        f.write("\n========== TURNS META ==========\n")
        f.write(json.dumps(turns_meta, ensure_ascii=False, default=str, indent=2))
        f.write("\n\n========== MEMORY FILES ==========\n")
        for rel, content in memory_files:
            f.write(f"\n--- {rel} ---\n{content}\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
