# Latency Compression — Speculative Decoding Implementation Plan

> **STATUS: ABANDONED (2026-06-02).** Halted at Task 2 / Gate A — the
> `Qwen3.6-35B-A3B` target (vocab 248,320, arch `qwen35moe`) has no small
> vocab-compatible draft model (the Qwen3.6 line is only 27B/35B). Tasks 1
> (draft download) completed; Tasks 2–7 not executed. Pivoted to think
> restructuring — see the design note in the spec. This plan is kept for
> the record; do not execute it.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Speed up the `Qwen3.6-35B-A3B-UD-Q4_K_XL` big model losslessly by adding a speculative-decoding draft model to llama-server, lowering per-turn think+Say generation time.

**Architecture:** Pure llama-server inference config — zero DollOS Python change. Download a Qwen3 draft GGUF, clear two pre-flight gates (vocab compatibility, CUDA-restart-without-reboot), benchmark 0.6B vs 1.7B draft across a `--draft-max` sweep on representative CJK-think + EN-Say prompts, pick the winner by measured tps, then capture the blessed launch command as `scripts/run_llama_server.sh` and update docs.

**Tech Stack:** llama.cpp (`llama-server`, `--model-draft`), Qwen3 GGUF draft models, bash + curl + python for benchmarking against the `/completion` endpoint.

**Spec:** `docs/superpowers/specs/2026-06-02-latency-compression-design.md`

---

## Operating notes (read before starting)

- **No reboot** (user constraint). `nvidia-smi` is currently broken (`Driver/library version mismatch`); the running server (port 8001) was loaded before the driver update and still works. Do not kill it until Gate B (Task 3) proves a fresh CUDA process can init.
- **No fallback** (project rule): if a gate fails, STOP and report. Do not ship a degraded config.
- Paths: llama.cpp at `/home/progcat/Projects/llamacpp/llama.cpp/`, models under `/home/progcat/Projects/llamacpp/unsloth/`. Target GGUF: `/home/progcat/Projects/llamacpp/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`.
- **Background processes**: never pipe a background server to `tail`; redirect to a log file and read the file (project rule).
- The current server's launch flags (the baseline to preserve): `--jinja --reasoning-format none --chat-template-kwargs '{"enable_thinking": true}' --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 --ctx-size 131072 --fit on --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --cont-batching --parallel 2 -ngl 99 --batch-size 2048 --ubatch-size 512 --threads 8 --keep -1 --port 8001`.

---

## Task 1: Download the two draft-model GGUFs

**Files:**
- Create: `/home/progcat/Projects/llamacpp/unsloth/Qwen3-0.6B-GGUF/` (Q8_0 GGUF)
- Create: `/home/progcat/Projects/llamacpp/unsloth/Qwen3-1.7B-GGUF/` (Q8_0 GGUF)

- [ ] **Step 1: Pick exact draft repos**

Use stock Qwen3 dense GGUFs (Qwen3 family tokenizer, matches the target's expected vocab). Candidates on Hugging Face: `Qwen/Qwen3-0.6B-GGUF` and `Qwen/Qwen3-1.7B-GGUF`, or `unsloth/Qwen3-0.6B-GGUF` / `unsloth/Qwen3-1.7B-GGUF`. Prefer the `Q8_0` quant (draft quality matters for acceptance; draft is tiny so Q8 is cheap).

- [ ] **Step 2: Download both via huggingface-cli**

Run (adjust filename to the actual `Q8_0` file in the repo):

```bash
cd /home/progcat/Projects/llamacpp/unsloth
hf download unsloth/Qwen3-0.6B-GGUF Qwen3-0.6B-Q8_0.gguf --local-dir Qwen3-0.6B-GGUF
hf download unsloth/Qwen3-1.7B-GGUF Qwen3-1.7B-Q8_0.gguf --local-dir Qwen3-1.7B-GGUF
```

If `hf` is unavailable, use `huggingface-cli download` (same args) or `curl -L` the resolve URL.

- [ ] **Step 3: Verify files exist and note paths**

Run:

```bash
ls -la /home/progcat/Projects/llamacpp/unsloth/Qwen3-0.6B-GGUF/*.gguf \
       /home/progcat/Projects/llamacpp/unsloth/Qwen3-1.7B-GGUF/*.gguf
```

Expected: two `.gguf` files printed with non-zero size. Record their exact paths for later tasks.

- [ ] **Step 4: Commit nothing**

Models are not committed (gitignored / outside repo). No commit for this task.

---

## Task 2: Gate A — verify draft/target vocab compatibility

**Files:**
- Create (temporary): `/tmp/vocab_check.txt`

- [ ] **Step 1: Dump tokenizer metadata from target + both drafts**

llama.cpp ships `gguf_dump` (or `llama-gguf`). Use whichever exists in the build dir. Run:

```bash
cd /home/progcat/Projects/llamacpp/llama.cpp
for m in \
  /home/progcat/Projects/llamacpp/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  /home/progcat/Projects/llamacpp/unsloth/Qwen3-0.6B-GGUF/*.gguf \
  /home/progcat/Projects/llamacpp/unsloth/Qwen3-1.7B-GGUF/*.gguf ; do
  echo "=== $m ==="
  ./llama-gguf "$m" r 2>/dev/null | grep -iE 'tokenizer.ggml.model|tokenizer.ggml.tokens|vocab|bos_token_id|eos_token_id' | head
done | tee /tmp/vocab_check.txt
```

(If `llama-gguf` is absent, use `python -c "from gguf import GGUFReader; ..."` to read `tokenizer.ggml.model` and the token-list length.)

- [ ] **Step 2: Confirm match**

Check `/tmp/vocab_check.txt`: the target and both drafts must report the **same** `tokenizer.ggml.model` (expect `gpt2`-style BPE for Qwen3) and the **same vocab size** (expect 151,936 for Qwen3). EOS/BOS ids should match too.

- [ ] **Step 3: Decision gate**

- If all three match → Gate A PASSED, proceed to Task 3.
- If the target's vocab differs from the drafts (e.g. the HauhauCS finetune added tokens) → **STOP**. Report to the user that speculative decoding is not viable on this draft/target pair and why. Do not proceed.

---

## Task 3: Gate B — prove a fresh CUDA process inits without a reboot

**Files:**
- Create (temporary): `/tmp/draft-probe.log`

- [ ] **Step 1: Start the draft model alone on a spare port (do NOT touch port 8001)**

The running target server stays up. Launch only the 0.6B draft on port 8009 in the background, logging to a file (never pipe a background server to tail):

```bash
/home/progcat/Projects/llamacpp/llama.cpp/llama-server \
  -m /home/progcat/Projects/llamacpp/unsloth/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q8_0.gguf \
  -ngl 99 --port 8009 --host 127.0.0.1 \
  > /tmp/draft-probe.log 2>&1 &
```

- [ ] **Step 2: Wait for init and read the log**

Give it time to load, then read (do not tail-follow):

```bash
sleep 8
grep -iE 'CUDA|error|listening|server is|failed' /tmp/draft-probe.log | head -30
```

- [ ] **Step 3: Decision gate**

- If the log shows `ggml_cuda_init: found ... CUDA devices` and the server reaches "listening"/"server is listening" with **no CUDA init error** → Gate B PASSED. A fresh CUDA process works; restarting the real server is safe.
- If CUDA init fails (driver/library mismatch surfacing for new processes) → **STOP**. Kill the probe (`pkill -f 'port 8009'`), report that speculative decoding needs a reboot, and let the user decide. Do not kill the working target server.

- [ ] **Step 4: Stop the probe server**

```bash
pkill -f 'Qwen3-0.6B-Q8_0.gguf' ; sleep 1 ; echo "probe stopped"
```

---

## Task 4: Build the benchmark harness

**Files:**
- Create: `scripts/bench_speculative.py`

- [ ] **Step 1: Write the benchmark script**

It hits a llama-server `/completion` endpoint with two fixed prompts and reports decode tps + acceptance. Acceptance is read from the server's `timings` in the JSON response (`draft_n` / `draft_n_accepted` when speculative is on; llama.cpp exposes these under `timings`). Create `scripts/bench_speculative.py`:

```python
"""Benchmark llama-server decode speed + speculative acceptance.

Usage: uv run python scripts/bench_speculative.py --port 8001 --label "0.6B dmax8"
Hits /completion with a CJK-think prompt and an EN-Say prompt, prints
tokens/s and (if speculative) acceptance rate from the response timings.
"""
import argparse
import json
import time

import httpx

CJK_THINK = (
    "<|im_start|>system\n你是 Doll。<|im_end|>\n"
    "<|im_start|>user\n主人說早安，幫我規劃今天。<|im_end|>\n"
    "<|im_start|>assistant\n<think>\n"
)
EN_SAY = (
    "<|im_start|>system\nYou are Yes Man.<|im_end|>\n"
    "<|im_start|>user\nTell me about your day in three sentences.<|im_end|>\n"
    "<|im_start|>assistant\n"
)


def run(port: int, prompt: str, n_predict: int) -> dict:
    body = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "cache_prompt": False,  # measure cold decode, not prompt-cache reuse
        "stream": False,
    }
    t0 = time.monotonic()
    r = httpx.post(f"http://127.0.0.1:{port}/completion", json=body, timeout=300.0)
    dt = time.monotonic() - t0
    r.raise_for_status()
    data = r.json()
    tim = data.get("timings", {})
    out_tok = tim.get("predicted_n")
    tps = tim.get("predicted_per_second")
    draft_n = tim.get("draft_n")
    draft_acc = tim.get("draft_n_accepted")
    acc = (draft_acc / draft_n) if (draft_n and draft_acc is not None) else None
    return {
        "wall_s": round(dt, 2),
        "out_tok": out_tok,
        "tps": round(tps, 1) if tps else None,
        "acceptance": round(acc, 3) if acc is not None else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    print(f"### {args.label} (port {args.port})")
    for name, prompt, npred in (("CJK-think", CJK_THINK, 256), ("EN-Say", EN_SAY, 128)):
        res = run(args.port, prompt, npred)
        print(f"  {name:10} {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke the harness against the CURRENT server (baseline, no draft)**

The target server is still running on 8001. Run:

```bash
cd /home/progcat/Projects/DollOS
uv run python scripts/bench_speculative.py --port 8001 --label "baseline no-draft"
```

Expected: prints `CJK-think` and `EN-Say` lines with `tps` ~40–53 and `acceptance` = None (no draft). This is the baseline row.

- [ ] **Step 3: Commit the harness**

```bash
git add scripts/bench_speculative.py
git commit -m "feat(bench): llama-server decode tps + speculative acceptance harness

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Run the speculative-decoding benchmark sweep

**Files:**
- Create (temporary): `/tmp/llama-spec.log`, `/tmp/bench_results.md`

- [ ] **Step 1: Stop the baseline server, restart with the 0.6B draft (draft-max 8)**

Gate B proved restart is safe. Stop the old server and relaunch with the draft added, keeping every baseline flag:

```bash
pkill -f 'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf' ; sleep 3
/home/progcat/Projects/llamacpp/llama.cpp/llama-server \
  -m /home/progcat/Projects/llamacpp/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  -md /home/progcat/Projects/llamacpp/unsloth/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q8_0.gguf \
  --alias "unsloth/Qwen3.6" --jinja --reasoning-format none \
  --chat-template-kwargs '{"enable_thinking": true}' \
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
  --ctx-size 131072 --fit on --cache-type-k q8_0 --cache-type-v q8_0 \
  --flash-attn on --cont-batching --parallel 2 -ngl 99 \
  --batch-size 2048 --ubatch-size 512 --threads 8 --keep -1 \
  --draft-max 8 --draft-min 1 \
  --port 8001 --host 0.0.0.0 \
  > /tmp/llama-spec.log 2>&1 &
sleep 20
grep -iE 'draft|listening|error|CUDA devices' /tmp/llama-spec.log | head
```

Expected: server loads both models, log mentions the draft, reaches listening. If it OOMs or errors, read the full log and adjust (`--fit on` should auto-shrink; if not, stop and report).

- [ ] **Step 2: Benchmark 0.6B at draft-max 4 / 8 / 16**

For each `--draft-max` value, the server must be relaunched with that flag (it's a launch arg). Repeat Step 1's launch with `--draft-max 4`, then `8`, then `16`, running after each:

```bash
uv run python scripts/bench_speculative.py --port 8001 --label "0.6B dmax=<N>" | tee -a /tmp/bench_results.md
```

- [ ] **Step 3: Benchmark 1.7B at draft-max 4 / 8 / 16**

Same as Step 1–2 but with `-md .../Qwen3-1.7B-GGUF/Qwen3-1.7B-Q8_0.gguf`. Append each result to `/tmp/bench_results.md`.

- [ ] **Step 4: Quality identity check (verify lossless)**

Pick the best-tps config. Restart with it, and separately restart with NO draft, sending the SAME prompt with a fixed seed (`"seed": 42`, `"temperature": 0`) to `/completion` both ways; confirm the two completions are identical (greedy → speculative is exactly lossless at temp 0):

```bash
# with draft running:
uv run python -c "import httpx;print(httpx.post('http://127.0.0.1:8001/completion',json={'prompt':'<|im_start|>user\nCount to ten.<|im_end|>\n<|im_start|>assistant\n','n_predict':40,'temperature':0,'seed':42,'cache_prompt':False,'stream':False},timeout=120).json()['content'])"
```

Run the same line after restarting without `-md`; the two outputs must match character-for-character.

- [ ] **Step 5: Record results**

Confirm `/tmp/bench_results.md` holds the baseline row + all 6 sweep rows + the winning tps and acceptance. This data goes into the spec in Task 7.

---

## Task 6: Pick the winner and capture the blessed launch config

**Files:**
- Create: `scripts/run_llama_server.sh`

- [ ] **Step 1: Choose the winning config**

From `/tmp/bench_results.md`, pick the `(draft, draft-max)` with the highest decode tps on the CJK-think prompt (the latency-dominant case), provided its EN-Say tps also beats baseline. If the best speedup is <1.3× over baseline, STOP and report — per spec §6 do not ship a config that adds VRAM cost for marginal gain; let the user decide.

- [ ] **Step 2: Write the launch script with the winning flags**

Create `scripts/run_llama_server.sh` (fill `<WINNING_DRAFT_GGUF>` and `<WINNING_DMAX>` from Step 1):

```bash
#!/usr/bin/env bash
# DollOS big-model launch — Qwen3.6-35B-A3B + speculative decoding draft.
# Blessed config from docs/superpowers/specs/2026-06-02-latency-compression-design.md
set -euo pipefail

LLAMA=/home/progcat/Projects/llamacpp/llama.cpp/llama-server
TARGET=/home/progcat/Projects/llamacpp/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
DRAFT=<WINNING_DRAFT_GGUF>

exec "$LLAMA" \
  -m "$TARGET" \
  -md "$DRAFT" \
  --alias "unsloth/Qwen3.6" \
  --jinja --reasoning-format none \
  --chat-template-kwargs '{"enable_thinking": true}' \
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
  --ctx-size 131072 --fit on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --flash-attn on --cont-batching --parallel 2 \
  -ngl 99 \
  --batch-size 2048 --ubatch-size 512 \
  --threads 8 --keep -1 \
  --draft-max <WINNING_DMAX> --draft-min 1 \
  --port 8001 --host 0.0.0.0
```

- [ ] **Step 3: Make executable and verify it launches**

```bash
chmod +x scripts/run_llama_server.sh
pkill -f 'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf' ; sleep 3
./scripts/run_llama_server.sh > /tmp/llama-final.log 2>&1 &
sleep 20
grep -iE 'draft|listening|error' /tmp/llama-final.log | head
uv run python scripts/bench_speculative.py --port 8001 --label "FINAL run_llama_server.sh"
```

Expected: server comes up with the draft, bench shows the winning tps. Leave this server running (it replaces the manual baseline server the daemon talks to).

- [ ] **Step 4: Commit the script**

```bash
git add scripts/run_llama_server.sh
git commit -m "feat(infra): run_llama_server.sh with speculative-decoding draft

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Update docs, spec results, and memory

**Files:**
- Modify: `docs/superpowers/specs/2026-06-02-latency-compression-design.md` (append §7 Results)
- Modify: `CLAUDE.md` (self-host section → point at the script)
- Modify: `/home/progcat/.claude/projects/-home-progcat-Projects-DollOS/memory/project_latency_compression.md` + `MEMORY.md` pointer

- [ ] **Step 1: Append the results table to the spec**

Add a `## 7. Results (2026-06-02)` section with the baseline + sweep table from `/tmp/bench_results.md`, the chosen config, the measured speedup (×), acceptance rate, and the lossless-check confirmation.

- [ ] **Step 2: Update CLAUDE.md self-host section**

Replace the long copy-paste `llama-server` blob under "Self-host llama.cpp big model" with a pointer:

```markdown
Run the big model (Qwen3.6-35B-A3B + speculative-decoding draft):

    ./scripts/run_llama_server.sh

Blessed flags + draft choice live in the script. `--reasoning-format none`
is REQUIRED for grammar to apply inside `<think>`. See
docs/superpowers/specs/2026-06-02-latency-compression-design.md.
```

- [ ] **Step 3: Update the latency memory**

In `project_latency_compression.md` append a dated line: speculative decoding landed, draft = `<winner>`, tps `<baseline>`→`<new>` (×`<speedup>`), acceptance `<acc>`, lossless verified. Adjust the description frontmatter to note it's now partly implemented. Keep the MEMORY.md pointer line accurate.

- [ ] **Step 4: Commit docs + spec**

```bash
git add docs/superpowers/specs/2026-06-02-latency-compression-design.md CLAUDE.md
git commit -m "docs: speculative decoding results + run_llama_server.sh pointer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(Memory files live outside the repo — no git commit for them.)

---

## Self-review checklist (done while writing)

- **Spec coverage**: §1 baseline → Task 4 Step 2; §2 approach → Task 5; §3 Gate A → Task 2, Gate B → Task 3; §4 methodology → Tasks 4–5; §5 deliverables → Tasks 6–7; §6 risks (<1.3× stop) → Task 6 Step 1. All covered.
- **No placeholders**: the only intentional fill-ins are `<WINNING_DRAFT_GGUF>` / `<WINNING_DMAX>` / `<N>` (data-dependent, resolved at Task 5–6 from measured results) — these are real values produced by the benchmark, not skipped work.
- **Consistency**: draft flag `-md` / `--draft-max` / `--draft-min` used identically across Tasks 3, 5, 6; bench script name `scripts/bench_speculative.py` consistent; ports 8001 (real) / 8009 (probe) consistent.
