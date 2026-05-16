# Persistent Mind — experimental prototype

Standalone toy validating the "single continuously-running mind loop with
persistent state" architecture as an alternative to DollOS's current
per-event cascade model. **Does not touch DollOS daemon code.**

## What this is

One coroutine (`MindLoop`) runs forever. Each iteration:

1. Drain perceptions from a queue (`UserSpoke`, `ToolResultArrived`, `IdleTick`, `Awoke`)
2. Render the **full mind state** into a prompt (focus, mood, active tasks, open loops, recent perceptions, recent thoughts, scratchpad)
3. Call the big LLM (llama-server at `127.0.0.1:8001`) for actions
4. Parse JSON actions and execute them (`Say`, `Think`, `SetFocus`, `OpenLoop`, `CloseLoop`, `Dispatch`, `Idle`, `Sleep`)
5. Persist state, loop

Shell dispatches are fire-and-forget — results come back as a new
`ToolResultArrived` perception in a later iteration. The mind sees its
own previously-opened loop alongside the result and matches them.

## Files

- `mind_state.py` — the dataclass + persist/load
- `perceptions.py` — perception types + renderers
- `actions.py` — action types + tolerant JSON parser
- `prompt.py` — renders mind state → prompt string
- `llm_client.py` — minimal httpx-based llama-server client
- `shell_runner.py` — async subprocess + result push-back
- `mind_loop.py` — the persistent coroutine
- `system_prompt.txt` — minimal persona + action vocabulary
- `run_experiments.py` — runs all 3 scenarios sequentially

## Run

Requires llama-server alive at `http://127.0.0.1:8001` with
`unsloth/Qwen3.6` (any GGUF served under that alias works).

```bash
cd /home/progcat/Projects/DollOS
uv run --with httpx python experiments/persistent_mind/run_experiments.py
```

Trace JSONL: `/tmp/persistent_mind_experiment.log`
Persisted state: `/tmp/persistent_mind_state.json`

## Scenarios

1. **Find line 150** — user asks to run `seq 1 200` and report line 150. Tests whether the mind dispatches the shell, opens a loop, and on the later `ToolResultArrived` iteration correctly matches the result to the loop and reports back.
2. **Idle thinking** — empty queue for ~30s. Tests whether the mind handles `IdleTick` gracefully (Idle / Think / Sleep) without spinning or hallucinating.
3. **Interrupt during shell** — user starts a long shell (`sleep 5`), then interrupts mid-flight. Tests whether the mind, in a single prompt, sees BOTH the active shell task AND the new user perception and responds coherently to both.
