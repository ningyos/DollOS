# Latency Compression — Speculative Decoding

**Date**: 2026-06-02
**Status**: Design approved, pending implementation plan
**Scope**: llama-server inference configuration only. Zero DollOS code change.

## 1. Problem

User-perceived response latency is dominated by the big LLM. Fresh baseline
measured 2026-06-02 from the live daemon's own telemetry
(`data/telemetry/llm_calls-2026-06-02.jsonl`, model
`Qwen3.6-35B-A3B-UD-Q4_K_XL`, 2× RTX 4060 Ti):

| Metric | Value |
|---|---|
| Prompt size | ~4,500 tokens (system + memory context + character pack) |
| LLM TTFT | 0.5 s warm (prompt cache hit) / 1.7 s cold |
| Decode speed | **40–53 tps** |
| Single-call total | 3.0–8.5 s |

### Why think dominates user-perceived latency

In voice mode the tool-stream parser (`src/dollos/tool_parser.py`) starts in
state `IN_THINK` and **discards all `<think>` content** until `</think>`,
only then streaming the spoken Say text sentence-by-sentence
(`SentenceChunker` → `TextChunk` → IPC). The B4-typed think block has four
fields (`seen` / `intent` / `review` / `mood`, ~150–300 tokens observed in
`data/cascade_log/`). So every turn the user waits through:

```
LLM TTFT (0.5–1.7 s) + full think generation (~150–300 tok @ 40 tps ≈ 4–7 s) + first Say sentence
                       ^ the dominant term
```

Say already streams as it generates — that path is fine. The cost is that
Doll generates a long invisible think block before her first spoken word.

### Decision: attack tps, not think

The think block is the source of Self-First behaviour (mood/review drive the
persona). The user prioritises character fidelity and chose to **speed up the
LLM itself** rather than restructure think. Speculative decoding accelerates
think **and** Say uniformly with **no quality loss**, so it shortens the think
wait without touching the cascade or the persona.

The target is already Q4_K_XL; pushing quantisation lower would cost quality
and is explicitly out of scope.

## 2. Approach — lossless speculative decoding

A small **draft model** proposes N tokens ahead; the 35B **target** verifies
them in one batched forward pass and accepts the longest correct prefix. Under
the target's sampling settings (temp 0.6, top-p 0.95, top-k 20) llama.cpp uses
rejection sampling that preserves the target distribution, so the output
remains a valid sample from the target model — **mathematically lossless**.

This is the ideal regime for it: a single-user daemon runs at batch 1, which
is memory-bandwidth bound on the target's weights — exactly where a cheap
draft buys the most.

### Hardware budget

2× RTX 4060 Ti = 32 GB total VRAM. Target + KV currently uses ~23.7 GB,
leaving **~7.8 GB free**. A Qwen3-0.6B Q8 draft (~0.6 GB) or Qwen3-1.7B Q8
(~1.8 GB) plus its small KV fits comfortably.

## 3. Pre-flight gates (hard stops — no fallback)

Per the project's no-fallback rule, if either gate fails we stop and report;
we do not silently degrade.

### Gate A — vocab compatibility
Speculative decoding requires the draft and target to share a vocabulary.
The target is a Qwen3-family finetune (`Qwen3.6-35B-A3B`, HauhauCS), expected
to carry the standard Qwen3 tokenizer (151,936 vocab). Read the target GGUF's
tokenizer metadata and confirm a stock Qwen3-0.6B / 1.7B GGUF matches exactly
(vocab size + `tokenizer.ggml.model` + added-token check). Mismatch →
speculative decoding is not viable on this pair; report and stop.

### Gate B — CUDA restart works without a reboot
`nvidia-smi` currently fails with `Driver/library version mismatch` (driver
updated, no reboot). The running server (loaded before the update) works, but
enabling speculative decoding requires restarting llama-server with
`--model-draft`. The user has asked **not to reboot**.

Probe safely: **without killing the running server**, start a separate small
process that loads only the draft model on a different port and confirm it
initialises a fresh CUDA context. If it inits → safe to restart the real
server. If it fails → speculative decoding needs a reboot; stop and report so
the user decides.

## 4. Benchmark methodology

Once both gates pass:

- **Draft candidates**: Qwen3-0.6B and Qwen3-1.7B (Q8 drafts).
- **Sweep**: `--draft-max` ∈ {4, 8, 16}, with `--draft-min` and
  `--draft-p-min` tuned per candidate.
- **Prompts** (representative of real turns):
  1. CJK think-heavy — a prompt that elicits the full `seen/intent/review/mood`
     think block (Chinese reasoning).
  2. EN Say — a Yes Man-style English spoken reply.
- **Metrics per config**: decode tps, draft **acceptance rate**, TTFT, peak
  VRAM.
- **Quality identity check**: same seed + same prompt, speculative on vs off;
  confirm outputs match (or are distribution-equivalent) — verifies the
  lossless claim empirically, not just on paper.

Acceptance is data-dependent (a 0.6B dense draft predicting a 35B-A3B
finetune's CJK reasoning tokens may land ~50–65%), so the winner is chosen
from measured tps, not assumed.

## 5. Deliverables

1. Benchmark data table appended to this spec.
2. Blessed launch configuration captured as **`scripts/run_llama_server.sh`**,
   replacing the copy-paste blob currently in `CLAUDE.md`.
3. `CLAUDE.md` self-host section updated to point at the script and note the
   draft model.
4. Memory updated ([[project_latency_compression]]) with the result.

## 6. Risks & non-goals

- **Risk**: low acceptance → marginal speedup. Mitigation: the benchmark
  measures it directly; if even the better draft yields <1.3×, report the
  result and stop rather than ship a config that adds VRAM cost for little
  gain.
- **Risk**: draft GGUF download requires network / `-hf` access.
- **Non-goals**: think restructuring, target re-quantisation, cascade changes,
  ASR/TTS work, any DollOS Python change.
