# Cascade governance exploration (2026-05-08 to 05-09)

Research notes from exploring how to bound DollOS's `_respond` cascade
loop. Multi-message conversation history shipped; everything else
explored and reverted.

## Baseline before exploration

Step 12 deterministic smoke (temp=0, top_k=1) scored **3/8** on T1-T8.
Failure modes:
- T4/T5 deterministic InvokeSkill hallucination (model guessed
  non-existent skill body filenames).
- T7 / T8 cascade-Say weakening — model didn't forward tool result back
  to user.
- T2 misinterpreted as repeat of T1.

## What shipped (commits 9a95376 + 7794bbe)

### `cascade governance + Hermes skills audit` (9a95376)
- `MAX_CASCADE_DEPTH = 5`.
- Same-tool consecutive **failure** counter (≥3 → break + ErrorMsg).
- `InvokeSkill.run` ENOENT short-circuit — returns corrective str
  listing existing skills.
- `scaffolding.jinja` `# Skills` section made conditional on actual
  skill availability (per Hermes #1: cross-referencing absent tools
  causes hallucination). When zero skills installed, the section
  (and `InvokeSkill` concept) is fully absent.
- `dispatcher.py` reads `data/memory/skills/*.md` per-iter and passes
  `available_skills=` to renderer.

### `multi-message conversation history within turn` (7794bbe)
- Replace single-shot perception re-render with multi-message ChatML
  history per cascade iteration. Original user perception persists in
  `messages[0]`; each iter appends raw model emit as assistant message
  + per-tool-result `<tool_response>` user message.
- `Qwen3ThinkingTemplate.render_messages(...)` new method;
  `LLMAdapter.stream_messages(...)` new method; legacy
  `stream_completion` retained for InnerVoice / Instinct.
- Removes `_format_results_perception` entirely.

Smoke: 3 sampling runs, fresh data each, average ~6/8. T4/T5 went
1/3 → 3/3, T8 went 0/3 → 2/3. Self-First emerged on T2 in some runs
(Doll mentioning her own preference and asking user). Cross-turn
recall via `[Memory context]` worked on T7 in some runs.

## What was tried and reverted

### Budget pressure (Hermes-style 70%/90% pressure injection)
- `MAX_CASCADE_DEPTH 5 → 20`, plus same-tool any-outcome counter
  (count successes too, threshold 4), plus pressure note injection at
  iter 8 (soft) and iter 14 (hard).
- 3 sampling runs scored ~7/8. New "successful loop" pattern caught
  cleanly (Recall returns `[no relevant memory]` 4× in a row → abort).
- **Reverted**: user wanted small-model self-regulation instead of
  hard thresholds.

### Naive YES/NO Instinct judge
- Replace fixed thresholds with per-iter small-model judge: streams a
  GBNF-constrained `root ::= "YES" | "NO"` query asking "should
  cascade continue?". NO → break + ErrorMsg.
- 3 sampling runs scored ~6.5/8. **Worse** than budget pressure.
- Failure: judge sometimes correctly said "stop" but dispatcher's
  break+ErrorMsg gave user nothing — Doll hadn't called Say yet.
- **Reverted**: 2-state output is too coarse; judge=NO means "wrap up
  and Say", not "abort everything".

### Wrap-up iter (judge=NO injects hint, runs one more iter)
- When judge returns NO, inject `[Cascade note: Inner voice says you
  have what you need. Now Say...]` into messages[-1] and run one more
  iter to give big model chance to Say. Break unconditionally after.
- 3 sampling runs scored ~7/8.
- **But**: judge=NO never actually fired in the 3 runs (small model
  always said YES). The 7/8 score is essentially multi-message-only
  performance with extra small-model latency overhead.
- **Reverted**: code path provides no measured benefit, only cost.

### Sanity guard (5-flag enum + correction injection + 3-consecutive abort)
- Replace judge with sanity guard that runs after **every** big-model
  emit (including Say-only iter 1). Output:
  `Literal["ok","loop","drift","stuck","malformed"] + correction str`.
  Non-OK → inject correction into last user msg if results exist.
  Same flag 3 consecutive iters → break + ErrorMsg.
- 3 sampling runs scored ~5.7/8. **Significant regression**.
- Failure: small model (0.6-1.7B Qwen3) miscalibrated for the task.
  T5 reliably triggered 3× malformed abort across all 3 runs even
  on legit Shell+Say emissions. Run 1 had 4 aborts total.
- **Reverted**: small model's pattern-matching on Hermes tool format
  vs natural-language emit isn't reliable enough to drive cascade
  control. Would need finetune or extensive few-shot to calibrate.

## Conclusions

- **Multi-message conversation history** is the single highest-leverage
  cascade fix — addresses the root cause (model losing original user
  perception + own prior tool_calls between cascade iterations) and
  cleanly aligns with Qwen3 / Hermes 4 training distribution. Ships.
- **Skills audit** (Hermes #1: don't reference absent tools) cleanly
  kills the InvokeSkill hallucination at the source. Ships.
- **Hard cascade depth caps** (5, 20, 30) and **budget pressure** are
  serviceable bounded-resource safety nets but don't fundamentally
  improve behavior beyond multi-message. Skipped — not needed at
  current cascade lengths.
- **Small-model cascade judging** (yes/no, sanity-guard typed enum)
  is appealing in principle but doesn't pay off at the 0.6-1.7B model
  scale. False positives cost more than false negatives gain. Either
  scale up the judge model (defeats the point) or finetune for the
  task (out of scope) — neither shipped.
- **Future direction**: if cascade governance is needed beyond what
  multi-message gives us, evaluate Hermes budget pressure (cheap, no
  small-model dependency, predictable behavior) before re-attempting
  small-model self-regulation.

## Final shipped state

Two commits on `cascade-governance` branch, post-revert:

1. **9a95376** `feat(cascade): governance + Hermes skills audit`
   - `MAX_CASCADE_DEPTH = 5`
   - Same-tool consecutive **failure** counter (≥3 → break + ErrorMsg)
   - `InvokeSkill.run` ENOENT → corrective str (lists existing skills)
   - `scaffolding.jinja` `# Skills` section conditional on actual skills
   - `dispatcher.py` per-iter glob of `data/memory/skills/*.md`

2. **7794bbe** `feat(cascade): multi-message conversation history`
   - Replace single-shot perception re-render with multi-message ChatML
   - Original user perception persists in `messages[0]`
   - Per-iter assistant message + `<tool_response>` user messages
   - Recall + scaffolding rendered ONCE per turn, not per iter
   - Removes `_format_results_perception`

The naive-judge / wrap-up / sanity-guard experiments were reverted; this
file is the only artifact left from that exploration.
