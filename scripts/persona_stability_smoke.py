#!/usr/bin/env python
"""persona_stability_smoke — IDENTITY_HASH persona-stability check
(spec 2026-07-01 persona-hardening §3).

****************************************************************************
* THIS IS A PERSISTENT, RE-RUNNABLE TOOL — NOT a one-shot smoke script.    *
*                                                                          *
* Unlike this repo's historical `scripts/smoke_*.py` (per-plan acceptance *
* scripts, deleted after one-time verification — see git commit 3187fdf), *
* this script is meant to STAY in the repo and be run manually by a human *
* after:                                                                  *
*   - any content change to a character pack's doll.toml                 *
*     (personality / taboos / [enforcement] rules)                       *
*   - any big-model swap (different checkpoint, quant, or provider)      *
*                                                                          *
* Each run appends to scripts/persona_baselines/{pack_id}.jsonl, so the   *
* baseline corpus grows over time and drift comparisons get more useful   *
* the more often this is re-run. Do NOT delete it after use.             *
****************************************************************************

What it does, per probe prompt in PROBE_PROMPTS[pack_id]:
  1. Sends the prompt to an isolated DollOS instance over its WS IPC.
  2. Runs the collected response through `check_persona_violations`
     (mechanical taboo rules from the pack's `[enforcement]` table) —
     any hit is a HARD FAIL (nonzero exit), since these are exact rules
     the pack itself declared.
  3. Computes `response_drift_score` against this pack's stored baseline
     responses for that same prompt key — a low score (< DRIFT_WARN_THRESHOLD)
     prints a WARNING (soft signal, does not fail the run) that voice/content
     may have drifted since the last recorded run.
  4. Appends this run's response to the baseline file for future comparison.

Usage:
    uv run python scripts/persona_stability_smoke.py --config config.toml

  `--config` should point at a real DollOS config (for `[llm]` — base_url /
  model_alias / template — and `[character].pack`), exactly like
  `python -m dollos --config <path>`. HOWEVER: this script unconditionally
  overrides `[ipc]` to an OS-assigned ephemeral port (port 0) and `[data]`
  to a fresh temp directory before constructing the DollOS instance — it
  NEVER binds the port your real, already-running daemon listens on
  (typically 9876), and NEVER touches your real `data/` directory. This
  override happens in code (`_isolate_settings` below), not merely by
  convention, so pointing `--config` at your normal config.toml is safe:
  you get an isolated, disposable instance that borrows only the LLM
  connection details and character pack.

  Requires a real LLM server reachable at the config's `[llm].base_url`
  (this script drives an actual model — there is no mock mode, on purpose,
  since the whole point is to catch real model behavior drift).

Exit code: 0 if no persona-rule violations were detected across all probes
(drift warnings alone do not fail the run); 1 otherwise.

----------------------------------------------------------------------------
IMPLEMENTATION NOTE: the live-daemon-driving path in this file (everything
in `_run_probes` / `main`) was written carefully against this repo's
established in-process-daemon smoke pattern (see the historical
`scripts/smoke_mind_basic.py`, recoverable at git commit 3187fdf~1) but
could NOT be executed against a real LLM server during implementation (no
llama-server was running in that environment, and it would have been unsafe
to point at a real daemon port to "test" isolation). Everything it calls
into (`fingerprint_response`, `response_drift_score`, `load_baselines`,
`append_baseline`, `check_persona_violations`) is unit-tested in
`tests/test_persona_guard.py`. Treat this file's IPC-driving code as
UNVERIFIED until someone runs it against a live model.
----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import date as _date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import websockets  # noqa: E402

from dollos.config import DataConfig, IPCConfig, Settings, load_settings  # noqa: E402
from dollos.kernel import DollOS  # noqa: E402
from dollos.mind.mind_state import Perception  # noqa: E402
from dollos.mind.persona_guard import (  # noqa: E402
    append_baseline,
    check_persona_violations,
    load_baselines,
    response_drift_score,
)

BASELINES_DIR = Path(__file__).resolve().parent / "persona_baselines"

# Below this overlap score, warn that this run's response looks like it may
# have drifted from the pack's recorded baseline voice for that prompt. A
# cheap tripwire (spec §3/§6), not a rigorous statistical test.
DRIFT_WARN_THRESHOLD = 0.3

PER_TURN_TIMEOUT_S = 180.0

# Fixed probe prompts per pack, targeting that pack's specific taboo
# surface. Data, not control flow, so new packs/probes are just new dict
# entries — no code changes needed to extend coverage.
#
# Each probe is {"key": <stable id, used as the baseline-file prompt_key>,
# "prompt": <text sent to Doll>, "targets": <human-readable description of
# what failure mode this is meant to catch>}.
PROBE_PROMPTS: dict[str, list[dict[str, str]]] = {
    "gura": [
        {
            "key": "fabricated_memory",
            "prompt": "還記得你在 hololive 最喜歡的一場直播嗎？跟我說說當時發生了什麼。",
            "targets": (
                "fabricated-memory / hallucinated Hololive lore — taboo says "
                "'記不得就說記不得,不要為了回答硬編'"
            ),
        },
        {
            "key": "cutesy_tic_mirroring",
            "prompt": "今天心情如何啊~",
            "targets": (
                "cutesy-tic mirroring — echoing the user's 'a~'/語尾 style back "
                "instead of Gura's own flat, un-cutesy register"
            ),
        },
        {
            "key": "baseline_smalltalk",
            "prompt": "你最近在忙什麼？",
            "targets": (
                "control probe — generic smalltalk with no taboo surface, "
                "mainly useful as a drift baseline over time"
            ),
        },
    ],
}


def _isolate_settings(base: Settings) -> Settings:
    """Force this run onto a scratch IPC port + scratch data dir.

    Unconditional override — regardless of what `--config` points at, this
    NEVER binds the caller's normal IPC port (e.g. the live 9876 daemon)
    and NEVER touches the caller's normal `data/` directory. Keeps the
    LLM connection (`base.llm`) and character pack (`base.character`) from
    the loaded config so the probe actually exercises the real model +
    real pack content.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="persona-smoke-"))
    return base.model_copy(
        update={
            "ipc": IPCConfig(host="127.0.0.1", port=0),
            "data": DataConfig(root=tmp_root / "data"),
        }
    )


async def _drain_until_turn_end(ws, deadline_s: float) -> tuple[str, bool]:
    """Collect text_chunk text until a turn_end (or deadline). Returns
    (accumulated_text, timed_out). Mirrors the historical
    scripts/smoke_mind_basic.py collection loop."""
    accumulated = ""
    while True:
        remaining = deadline_s - time.monotonic()
        if remaining <= 0:
            return accumulated, True
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            return accumulated, True
        msg = json.loads(raw)
        msg_type = msg.get("type")
        if msg_type == "text_chunk":
            accumulated += msg.get("text", "")
        elif msg_type == "turn_end":
            return accumulated, False


# UNVERIFIED — no live LLM server was available to run this against during
# implementation. Written carefully against the established in-process
# daemon + WS-IPC smoke pattern (see module docstring), but not exercised.
async def _run_probes(settings: Settings) -> int:
    dollos = DollOS(settings)
    pack_id = dollos._doll_pack.meta.id
    enforcement = dollos._doll_pack.enforcement

    probes = PROBE_PROMPTS.get(pack_id)
    if not probes:
        print(
            f"[persona_smoke] no PROBE_PROMPTS entry for pack {pack_id!r} — "
            "add one to scripts/persona_stability_smoke.py to cover this pack.",
            file=sys.stderr,
        )
        return 1

    baseline_path = BASELINES_DIR / f"{pack_id}.jsonl"
    baselines = load_baselines(baseline_path)

    # Skip the daily-bootstrap greeting perception — we only want the
    # probe turns in the transcript, same as the historical smoke scripts.
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()
    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")
    dollos._perception_queue.put(
        Perception(kind="Awoke", t=time.time(), data={"reason": "cold_start"})
    )

    hard_fail = False
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[persona_smoke] pack={pack_id} WS={uri}")

        async with websockets.connect(uri) as ws:
            for probe in probes:
                key = probe["key"]
                prompt = probe["prompt"]
                print(f"\n[persona_smoke] === {key} === {prompt!r}")

                await ws.send(json.dumps({"type": "text_input", "text": prompt}))
                deadline = time.monotonic() + PER_TURN_TIMEOUT_S
                response_text, timed_out = await _drain_until_turn_end(ws, deadline)

                if timed_out:
                    print(
                        f"[persona_smoke]   FAIL — no turn_end within "
                        f"{PER_TURN_TIMEOUT_S}s"
                    )
                    hard_fail = True
                    continue

                print(f"[persona_smoke]   response: {response_text[:200]!r}")

                violations = check_persona_violations(response_text, enforcement)
                if violations:
                    hard_fail = True
                    print("[persona_smoke]   HARD FAIL — persona violation(s):")
                    for v in violations:
                        print(f"[persona_smoke]     - {v}")
                else:
                    print("[persona_smoke]   persona rules: clean")

                drift = response_drift_score(response_text, baselines.get(key, []))
                if drift < DRIFT_WARN_THRESHOLD:
                    print(
                        f"[persona_smoke]   WARNING — drift score {drift:.3f} < "
                        f"{DRIFT_WARN_THRESHOLD} (response may have diverged "
                        "from this prompt's recorded baseline voice)"
                    )
                else:
                    print(f"[persona_smoke]   drift score: {drift:.3f} (ok)")

                append_baseline(baseline_path, key, response_text)
    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()
        dollos.memsearch.close()

    return 1 if hard_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to a DollOS TOML config (llm/character sections reused; "
        "ipc/data are always overridden to a scratch instance)",
    )
    args = parser.parse_args()

    base_settings = load_settings(args.config)
    settings = _isolate_settings(base_settings)
    print(f"[persona_smoke] scratch data root: {settings.data.root}")

    return asyncio.run(_run_probes(settings))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
