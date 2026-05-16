# scripts/smoke_doll_conversation_history_e2e.py
"""Real-LLM e2e: conversation history solves T2-forgetting.

Sends Doll: "I need you to do this for me: run `seq 1 200` in a shell.
After the result comes back, look at line 150 specifically and tell me
what it is."

Expected: T2 sees T1's full reasoning (her own <think> + Shell call) in
the message history, so she recognizes the incoming ShellResult as her
own action and calls ReadToolOutput to fetch line 150.

Pass criterion (observational): Doll calls ReadToolOutput with offset ≈
149 AND her final Say explicitly identifies "150" as the value of line
150. Not a pytest assertion — log the cascade trace and judge by eye.
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
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS  # noqa: E402

PROMPT = (
    "I need you to do this for me: run `seq 1 200` in a shell. "
    "After the result comes back, look at line 150 specifically and tell me what it is."
)
LOG_PATH = Path("/tmp/iv_doll_conversation_history_e2e.log")
WALL_TIMEOUT_S = 180.0


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
        inner_voice=InnerVoiceConfig(
            base_url="http://127.0.0.1:8003",
            timeout_s=30.0,
        ),
        conversation_history=ConversationHistoryConfig(max_turns=6),
    )


async def _run() -> int:
    _setup_logging(LOG_PATH)
    print(f"[smoke] log → {LOG_PATH}")
    print(f"[smoke] prompt: {PROMPT}")

    tmp_root = Path(tempfile.mkdtemp(prefix="iv-doll-conv-history-"))
    print(f"[smoke] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    # Skip the daily bootstrap so we exercise only the user turn.
    dollos._bootstrapped_dates.add(_date.today())

    # Hook structlog to capture cascade records
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

    t0 = time.monotonic()
    await dollos.memsearch.index()
    await dollos.server.start()

    received: list[dict] = []
    say_texts: list[str] = []
    turn1_end_history_size: int = -1
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke] WS → {uri}")
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT}))
            deadline = time.monotonic() + WALL_TIMEOUT_S
            current_say = ""
            turn_count = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print("[smoke] WALL TIMEOUT")
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    print("[smoke] recv timeout")
                    break
                msg = json.loads(raw)
                received.append(msg)
                t = msg.get("type")
                if t == "text_chunk":
                    current_say += msg.get("text", "")
                elif t == "turn_end":
                    turn_count += 1
                    if current_say.strip():
                        say_texts.append(current_say.strip())
                        current_say = ""
                    # After the first turn_end, capture history depth.
                    if turn_count == 1:
                        turn1_end_history_size = dollos._conversation_history.turn_count()
                        print(f"[smoke] After T1 turn_end: history.turn_count() = {turn1_end_history_size}")
                    # For this smoke we want to observe BOTH T1 and T2 (T1=Shell
                    # dispatch turn, T2=ShellResult processing turn).  We wait for
                    # two turn_ends OR until the deadline.
                    if turn_count >= 2:
                        # Drain any straggling messages briefly, then exit.
                        try:
                            while True:
                                r2 = await asyncio.wait_for(ws.recv(), timeout=1.0)
                                received.append(json.loads(r2))
                        except asyncio.TimeoutError:
                            pass
                        break
    finally:
        final_history_size = dollos._conversation_history.turn_count()
        await dollos.server.stop()

    wall = time.monotonic() - t0
    print(f"\n[smoke] wall time: {wall:.1f}s")
    print(f"[smoke] received {len(received)} WS messages")
    print(f"[smoke] {len(cascade_records)} cascade iters captured")
    print(f"[smoke] history.turn_count() at end: {final_history_size}")

    # Chronological cascade trace
    print("\n========== CASCADE TRACE ==========")
    for i, rec in enumerate(cascade_records):
        tcs = rec.get("tool_calls") or []
        results = rec.get("results") or []
        print(
            f"\n--- iter {i} (turn={rec.get('turn_id')}, "
            f"iter#={rec.get('iter')}, dur={rec.get('duration_ms')}ms) ---"
        )
        if rec.get("seen"):
            print(f"  SEEN:   {rec['seen']}")
        if rec.get("intent"):
            print(f"  INTENT: {rec['intent']}")
        if rec.get("review"):
            print(f"  REVIEW: {rec['review']}")
        if rec.get("mood"):
            print(f"  MOOD:   {rec['mood']}")
        if rec.get("tool"):
            print(f"  TOOL:   {rec['tool']}")
        for tc in tcs:
            print(f"  call -> {tc.get('name')}({json.dumps(tc.get('args'), ensure_ascii=False)[:300]})")
        for r in results:
            print(f"  result <- {r.get('tool_name')} success={r.get('success')} "
                  f"detail={(r.get('detail') or '')[:300]!r}")

    # Final Say
    print("\n========== FINAL SAY ==========")
    for s in say_texts:
        print(s)

    # Observations
    print("\n========== OBSERVATIONS ==========")
    tool_call_seq: list[tuple[str, dict]] = []
    for rec in cascade_records:
        for tc in rec.get("tool_calls") or []:
            tool_call_seq.append((tc.get("name") or "?", tc.get("args") or {}))
    print("Tool call sequence:")
    for i, (n, a) in enumerate(tool_call_seq):
        print(f"  {i}. {n} args={json.dumps(a, ensure_ascii=False)[:200]}")

    used_shell = any(n == "Shell" for n, _ in tool_call_seq)
    used_read = any(n == "ReadToolOutput" for n, _ in tool_call_seq)
    used_grep = any(n == "GrepToolOutput" for n, _ in tool_call_seq)

    # Did her ReadToolOutput slice land near line 150?
    landed_on_150 = False
    for n, a in tool_call_seq:
        if n == "ReadToolOutput":
            try:
                offset = int(a.get("offset", -1))
                limit = int(a.get("limit", 0))
            except (TypeError, ValueError):
                continue
            # zero-indexed: line 150 is index 149. accept 1-indexed too.
            if offset <= 150 <= offset + max(limit, 0):
                landed_on_150 = True
            if offset <= 149 <= offset + max(limit, 0):
                landed_on_150 = True

    final_say = " ".join(say_texts)
    answer_mentions_150 = "150" in final_say

    # Evidence for T2 history prepend:
    # After T1 turn_end, history should have >= 1 turn (the T1 user+assistant pair).
    history_populated_after_t1 = turn1_end_history_size >= 1

    print(f"\nT1 Shell fired:                     {used_shell}")
    print(f"history.turn_count() after T1:      {turn1_end_history_size}  (>=1 means T1 was recorded)")
    print(f"history populated after T1:         {history_populated_after_t1}")
    print(f"history.turn_count() at end:        {final_history_size}")
    print(f"T2 ReadToolOutput called:           {used_read}")
    print(f"T2 GrepToolOutput called:           {used_grep}")
    print(f"ReadToolOutput slice covered ~150:  {landed_on_150}")
    print(f"Say mentions '150':                 {answer_mentions_150}")
    print(f"wall time:                          {wall:.1f}s")

    # Summary verdict
    print("\n========== VERDICT ==========")
    if used_shell and history_populated_after_t1 and used_read and landed_on_150 and answer_mentions_150:
        print("PASS — history solved T2 forgetting: Doll fired Shell in T1, "
              "T2 saw the history, called ReadToolOutput at ~150, answered correctly.")
    elif used_shell and history_populated_after_t1 and used_read:
        print("PARTIAL — history is working (turn recorded, T2 called ReadToolOutput) "
              "but slice did not land on line 150 or Say doesn't mention it.")
    elif used_shell and not used_read:
        print("FAIL — T2 did NOT call ReadToolOutput. "
              "History may be missing or T2 still failing to connect the result to her own Shell.")
    else:
        print("FAIL — Shell was never fired in T1.")

    # Persist full WS transcript to log for offline inspection.
    with LOG_PATH.open("a") as f:
        f.write("\n\n========== WS MESSAGES ==========\n")
        for m in received:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        f.write("\n========== CASCADE RECORDS ==========\n")
        for rec in cascade_records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    print(f"\ntrace saved: {LOG_PATH}")
    print("\nDONE — eyeball the log to judge whether history solved T2 forgetting.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
